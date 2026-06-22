# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Delay plugin — ligne à retard N→N du pipeline MXL.
#   X canaux appariés : chaque canal a une entrée vidéo et/ou audio (shm), retardée
#   de D images, écrite sur la sortie correspondante.
#     sortie vidéo canal i : {{hostname}}_{{i}}
#     sortie audio canal i : {{hostname}}_audio_{{i}}
#   Délai réglable à chaud (global ou par canal) via :8082, en IMAGES. Le délai réel
#   mesuré (ms) est exposé sur :8080 et :8082/state.
#
# Résolution-agnostique : la taille de frame vidéo est auto-détectée depuis la taille
# du fichier shm d'entrée (ring=10, YUV420) — pas besoin de width/height.
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import time, threading, json, signal
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
import bobimxl   # migration MXL : retard vidéo (Reader/Writer) + audio (AudioReader/AudioWriter) — Phases 1/3

_mxl_inst = bobimxl.Instance()   # domaine MXL ($MXL_DOMAIN ou /dev/shm/mxl)

# ─── Latence : Δ ts_écriture - ts_origine (rolling avg) ─────
class RollingMs:
    def __init__(self, n=30):
        self.d = deque(maxlen=n); self.last_ns = 0
    def push(self, ms_value):
        self.d.append(ms_value); self.last_ns = time.time_ns()
    def avg(self):
        if not self.d: return None
        if time.time_ns() - self.last_ns > 2_000_000_000: return None
        return round(sum(self.d) / len(self.d), 1)

# ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

N_CHANNELS  = max(1, min(16, int(CONFIG.get("n_channels") or 1)))
MODE        = CONFIG.get("mode") or "video"
MAX_DELAY   = max(0, int(CONFIG.get("max_delay_frames") or 25))
INIT_DELAY  = max(0, min(MAX_DELAY, int(CONFIG.get("delay_frames") or 0)))
HAS_VIDEO   = MODE in ("video", "video_audio")
HAS_AUDIO   = MODE in ("audio", "video_audio")
# Unité du délai : IMAGES si vidéo présente (sync image-par-image), MILLISECONDES en
# audio seul (le retard est une simple durée — pas de notion d'image, pas de fps).
AUDIO_ONLY  = HAS_AUDIO and not HAS_VIDEO
POLL_V      = 0.004     # cadence de scrutation des nouvelles frames vidéo (~250 Hz)
POLL_A      = 0.0005    # idem audio (chunks 1 ms)

# ─── Format vidéo (yuv420p, taille auto-détectée) ───────────
V_HEADER_SIZE = 64
V_RING_SIZE   = CONFIG.get("shm_video_ring", 10)

# ─── Format audio (PCM L24 / 48k / 8ch, identique receiver_2110) ─
A_SAMPLE_RATE = 48000
A_CHANNELS    = 8
A_BIT_DEPTH   = 24
A_BYTES_PER_SAMPLE  = A_BIT_DEPTH // 8
A_SAMPLES_PER_CHUNK = A_SAMPLE_RATE // 1000   # 48 (1 ms)
A_CHUNK_SIZE  = A_SAMPLES_PER_CHUNK * A_CHANNELS * A_BYTES_PER_SAMPLE   # 1152
A_HEADER_SIZE = 64
A_RING_SIZE   = CONFIG.get("shm_audio_ring", 100)
A_TOTAL_SIZE  = A_HEADER_SIZE + A_RING_SIZE * A_CHUNK_SIZE

# ─── État runtime partagé ───────────────────────────────────
state_lock = threading.Lock()
state = {{
    "delays":   [INIT_DELAY] * N_CHANNELS,   # consigne par canal, en IMAGES
    "link":     True,                        # délai global (les canaux suivent)
    "inputs_v": [(CONFIG.get("input_v_%d" % i) or None) for i in range(N_CHANNELS)],
    "inputs_a": [(CONFIG.get("input_a_%d" % i) or None) for i in range(N_CHANNELS)],
}}

# Mesures par canal/essence (remplies par les workers). `period` = période entre frames
# de l'entrée vidéo (ms), mesurée depuis les timestamps producteur → sert à convertir
# le délai (en images) en chunks audio sans fps configuré.
meas_lock = threading.Lock()
meas = {{
    "v": [{{"fps": 0.0, "frame_index": 0, "ms": RollingMs(), "realized": 0, "period": RollingMs(), "transit": RollingMs()}} for _ in range(N_CHANNELS)],
    "a": [{{"fps": 0.0, "frame_index": 0, "ms": RollingMs(), "realized": 0, "transit": RollingMs()}} for _ in range(N_CHANNELS)],
}}

# SIGBUS : signale aux workers de rouvrir leurs flux MXL
bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    print("SIGBUS reçu — réouverture des flux MXL")
    bus_error.set()
signal.signal(signal.SIGBUS, _handle_sigbus)


# ─── HTTP : metrics 8080 + control 8082 ─────────────────────
def _clamp_delay(v):
    try: v = int(round(float(v)))
    except (TypeError, ValueError): return None
    return max(0, min(MAX_DELAY, v))

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        out = []
        with meas_lock, state_lock:
            for i in range(N_CHANNELS):
                if HAS_VIDEO:
                    m = meas["v"][i]
                    out.append({{"idx": i, "essence": "video", "fps": m["fps"],
                                "frame_index": m["frame_index"],
                                "delay_frames": state["delays"][i],
                                "delay_ms": m["ms"].avg()}})
                if HAS_AUDIO:
                    m = meas["a"][i]
                    out.append({{"idx": i, "essence": "audio", "fps": m["fps"],
                                "frame_index": m["frame_index"],
                                "delay_frames": state["delays"][i],
                                "delay_ms": m["ms"].avg()}})
        # inputs_latency_ms = TRANSIT (âge à la lecture) ; own_latency_ms = délai PROPRE ajouté
        # (réalisé), c.-à-d. le rôle même du nœud. Distinguer les deux pour le suivi de chaîne.
        ilm = {{}}; own_vals = []
        if HAS_VIDEO:
            for i in range(N_CHANNELS):
                shm = state["inputs_v"][i]
                tr  = meas["v"][i]["transit"].avg()
                if shm and tr is not None:
                    ilm[shm] = round(tr, 1)
                ov = meas["v"][i]["ms"].avg()
                if ov is not None:
                    own_vals.append(ov)
        own_latency_ms = round(max(own_vals), 1) if own_vals else None
        payload = {{"channels": out, "inputs_latency_ms": ilm,
                    "own_latency_ms": own_latency_ms, "plugin_version": PLUGIN_VERSION}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass


class ControlHandler(BaseHTTPRequestHandler):
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n: return {{}}
        try: return json.loads(self.rfile.read(n).decode())
        except Exception: return {{}}

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/state":
            return self._reply(404, {{"error": "not found"}})
        chans = []
        with state_lock, meas_lock:
            for i in range(N_CHANNELS):
                mv = meas["v"][i]; ma = meas["a"][i]
                chans.append({{
                    "idx": i,
                    "has_video": HAS_VIDEO, "has_audio": HAS_AUDIO,
                    "input_v": state["inputs_v"][i], "input_a": state["inputs_a"][i],
                    "delay_frames": state["delays"][i],
                    "measured_ms": (mv["ms"].avg() if HAS_VIDEO else ma["ms"].avg()),
                    "realized_frames": (mv["realized"] if HAS_VIDEO else ma["realized"]),
                    "fps": (mv["fps"] if HAS_VIDEO else ma["fps"]),
                }})
            link = state["link"]
            # Expose les shm câblés au NIVEAU RACINE sous le nom de leur state_field
            # (input_v_<i> / input_a_<i>) : la topologie Câbles lit le shm live ainsi
            # (routes.py : live.get(state_field)). Sans ça, les entrées paraissent
            # débranchées et aucun câble ne s'affiche.
            flat = {{}}
            for i in range(N_CHANNELS):
                if HAS_VIDEO: flat["input_v_%d" % i] = state["inputs_v"][i]
                if HAS_AUDIO: flat["input_a_%d" % i] = state["inputs_a"][i]
        self._reply(200, dict(flat, **{{
            "mode": MODE, "n_channels": N_CHANNELS, "max_delay": MAX_DELAY,
            "unit": ("ms" if AUDIO_ONLY else "img"), "link": link, "channels": chans,
            "plugin_version": PLUGIN_VERSION,
        }}))

    def do_POST(self):
        body = self._read_json()
        if self.path == "/params":
            with state_lock:
                if "link" in body:
                    state["link"] = bool(body["link"])
                # Délai global : {{delay_frames, all:true}} ou link actif
                if body.get("all") or (state["link"] and "delay_frames" in body and "channel" not in body):
                    d = _clamp_delay(body.get("delay_frames"))
                    if d is not None:
                        state["delays"] = [d] * N_CHANNELS
                # Délai d'un canal : {{channel:i, delay_frames:D}}
                elif "channel" in body and "delay_frames" in body:
                    i = body.get("channel")
                    d = _clamp_delay(body.get("delay_frames"))
                    if isinstance(i, int) and 0 <= i < N_CHANNELS and d is not None:
                        state["delays"][i] = d
                # Liste complète : {{delays:[…]}}
                elif isinstance(body.get("delays"), list):
                    for i, v in enumerate(body["delays"]):
                        if i >= N_CHANNELS: break
                        d = _clamp_delay(v)
                        if d is not None: state["delays"][i] = d
            return self._reply(200, {{"ok": True}})
        if self.path == "/input":
            essence = body.get("essence") or "video"
            shm = (body.get("shm") or "").strip() or None
            i = body.get("slot")
            if i is None: i = body.get("channel")
            with state_lock:
                if isinstance(i, int) and 0 <= i < N_CHANNELS:
                    if essence == "audio": state["inputs_a"][i] = shm
                    else:                  state["inputs_v"][i] = shm
            return self._reply(200, {{"ok": True}})
        return self._reply(404, {{"error": "not found"}})

    def log_message(self, *a): pass


threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(),
                 daemon=True).start()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(),
                 daemon=True).start()


# ─── Worker générique (vidéo ou audio) ──────────────────────
def _worker(essence, idx):
    """Retard AUDIO d'un canal : lit le FLUX MXL d'entrée (gapless, blocs de 1 ms), tamponne D blocs
    (1 bloc = 1 ms) puis ré-émet dans le flux MXL de sortie. (La vidéo a son propre _worker_video.)
    `essence` vaut toujours 'a' (appelé ainsi). Migration MXL Phase 3 : plus de ring shm s24be."""
    out_name = "%s_audio_%d" % (HOSTNAME, idx)
    ar = None; in_name = None; writer = None
    buf = deque()              # (bloc float32 (n,8), t_read_ns)
    read_pos = None            # index sample consommé (lecture gapless)
    out_idx = 0
    fps_t0 = time.time(); fps_n = 0

    def _close_ar():
        nonlocal ar
        if ar is not None:
            try: ar.close()
            except Exception: pass
            ar = None

    while True:
        if bus_error.is_set():
            _close_ar()
            if writer is not None:
                try: writer.close()
                except Exception: pass
                writer = None
            in_name = None; buf.clear(); read_pos = None
            time.sleep(0.5)

        with state_lock:
            wanted = state["inputs_a"][idx]
            D_frames = state["delays"][idx]
        if AUDIO_ONLY:
            D = int(round(D_frames))                 # audio seul : délai = durée en ms (1 bloc = 1 ms)
        else:
            with meas_lock:                          # apparié vidéo : délai en images → ms (période vidéo)
                pv = meas["v"][idx]["period"].avg()
            D = int(round(D_frames * pv)) if (pv and pv > 0) else 0
        if D < 0: D = 0

        # (re)câblage de l'entrée
        if wanted != in_name:
            _close_ar(); buf.clear(); read_pos = None; in_name = wanted
        if wanted and ar is None:
            try:
                ar = bobimxl.AudioReader(_mxl_inst, wanted); read_pos = None
                print("a[%d] câblé sur %s" % (idx, wanted))
            except Exception:
                ar = None
        if ar is None:
            with meas_lock: meas["a"][idx]["realized"] = 0
            time.sleep(0.02); continue

        # lecture gapless des blocs disponibles (suivi de position, resync sur head au décrochage)
        try:
            head = ar.head_index()
        except Exception:
            _close_ar(); time.sleep(0.02); continue
        read_any = False
        if head != bobimxl.MXL_UNDEFINED_INDEX:
            if read_pos is None: read_pos = head
            guard = 0
            while read_pos <= head and guard < 512:
                r = ar.read_from(read_pos, A_SAMPLES_PER_CHUNK)
                if r is None:
                    if read_pos < head: read_pos = head; continue
                    break
                buf.append((r, time.time_ns()))
                read_pos += A_SAMPLES_PER_CHUNK; guard += 1; read_any = True
            if read_any:
                with meas_lock:
                    meas["a"][idx]["transit"].push((bobimxl.now_tai() - ar.last_write_time()) / 1e6)

        # sortie : flux MXL audio (cadence libre — le retard vient du tampon, pas de la grille)
        if writer is None:
            try:
                writer = bobimxl.AudioWriter(_mxl_inst, out_name, channels=A_CHANNELS,
                                             sample_rate=A_SAMPLE_RATE, index_mode="free")
            except Exception:
                writer = None
        # émet les blocs dont le retard D (en blocs = ms) est atteint (flush multiple si D a baissé)
        while writer is not None and len(buf) > D:
            blk, t_read = buf.popleft()
            try:
                writer.write(blk)
            except Exception:
                try: writer.close()
                except Exception: pass
                writer = None; break
            out_idx += 1; fps_n += 1
            with meas_lock:
                meas["a"][idx]["ms"].push((time.time_ns() - t_read) / 1e6)   # retard réalisé (own)
                meas["a"][idx]["realized"] = len(buf)

        now = time.time()
        if now - fps_t0 >= 1.0:
            with meas_lock:
                meas["a"][idx]["fps"] = round(fps_n / (now - fps_t0), 1)
                meas["a"][idx]["frame_index"] = out_idx
            fps_t0 = now; fps_n = 0

        time.sleep(POLL_A)


def _worker_video(idx):
    """Retard VIDÉO sur MXL : lit le flux d'entrée (Reader), rattrape sans drop via get(index)
    pour un retard exact, et ré-émet décalé de D images via un Writer. Audio = _worker (legacy)."""
    out_name = "%s_%d" % (HOSTNAME, idx)
    reader = None; in_name = None; writer = None
    buf = deque(); last_in_idx = -1; prev_ts = 0; out_idx = 0
    fps_t0 = time.time(); fps_n = 0
    while True:
        if bus_error.is_set():
            for h in (reader, writer):
                try: h and h.close()
                except Exception: pass
            reader = writer = None; in_name = None; buf.clear(); last_in_idx = -1
            time.sleep(0.5)
        with state_lock:
            wanted = state["inputs_v"][idx]; D = state["delays"][idx]
        if wanted != in_name:
            try: reader and reader.close()
            except Exception: pass
            reader = None; buf.clear(); last_in_idx = -1; in_name = wanted
        if wanted and reader is None:
            try:
                reader = bobimxl.Reader(_mxl_inst, wanted); print("v[%d] câblé sur %s" % (idx, wanted))
            except Exception:
                reader = None
        if reader is None:
            with meas_lock: meas["v"][idx]["realized"] = 0
            time.sleep(0.02); continue
        try:
            head = reader.head_index()
        except Exception:
            try: reader.close()
            except Exception: pass
            reader = None; time.sleep(0.02); continue
        if head == bobimxl.MXL_UNDEFINED_INDEX or head <= 0:
            time.sleep(POLL_V); continue
        cur_idx = head - 1     # head = grain EN COURS d'écriture → dernier COMMITÉ = head-1
        ts_in = reader.last_write_time()
        if cur_idx < last_in_idx:                 # producteur recréé
            buf.clear(); last_in_idx = -1
        if cur_idx > 0 and cur_idx != last_in_idx:
            advance = (cur_idx - last_in_idx) if last_in_idx >= 0 else 1
            with meas_lock: pv = meas["v"][idx]["period"].avg()
            est_ms = pv if (pv and pv > 0) else 40.0
            start = (last_in_idx + 1) if last_in_idx >= 0 else cur_idx
            if cur_idx - start + 1 > V_RING_SIZE:  # bornage à l'historique du flux
                start = cur_idx - V_RING_SIZE + 1
            for fi in range(start, cur_idx + 1):
                got = reader.get(fi, 20_000_000)   # rattrapage sans drop (grain fi précis)
                if got is None:
                    continue                       # grain hors historique → sauté
                ts_est = ts_in - int((cur_idx - fi) * est_ms * 1e6)
                buf.append((bytes(got[2]), ts_est))
            if prev_ts and ts_in > prev_ts:
                with meas_lock: meas["v"][idx]["period"].push((ts_in - prev_ts) / 1e6 / max(1, advance))
            with meas_lock: meas["v"][idx]["transit"].push((bobimxl.now_tai() - ts_in) / 1e6)
            prev_ts = ts_in; last_in_idx = cur_idx
            if writer is None:
                f = reader.format()
                if f:
                    writer = bobimxl.Writer(_mxl_inst, out_name, f["width"], f["height"],
                                            f["chroma"], f["bit_depth"], f["fps_num"], f["fps_den"],
                                            index_mode="free")
            while writer is not None and len(buf) > D:
                data, ts_orig = buf.popleft()
                try:
                    _i, gi, vw = writer.open_grain()
                    vw[:len(data)] = np.frombuffer(data, dtype=np.uint8)
                    writer.commit(gi)
                except Exception:
                    try: writer.close()
                    except Exception: pass
                    writer = None; break
                out_idx += 1; fps_n += 1
                with meas_lock:
                    meas["v"][idx]["ms"].push((bobimxl.now_tai() - ts_orig) / 1e6)
                    meas["v"][idx]["realized"] = len(buf)
        now = time.time()
        if now - fps_t0 >= 1.0:
            with meas_lock:
                meas["v"][idx]["fps"] = round(fps_n / (now - fps_t0), 1)
                meas["v"][idx]["frame_index"] = out_idx
            fps_t0 = now; fps_n = 0
        time.sleep(POLL_V)


# ─── Démarrage des workers + reaper SIGBUS ──────────────────
threads = []
for i in range(N_CHANNELS):
    if HAS_VIDEO:
        threads.append(threading.Thread(target=_worker_video, args=(i,), daemon=True))
    if HAS_AUDIO:
        threads.append(threading.Thread(target=_worker, args=("a", i), daemon=True))
for t in threads:
    t.start()

print("delay démarré : %d canaux, mode=%s, délai max=%d %s"
      % (N_CHANNELS, MODE, MAX_DELAY, "ms" if AUDIO_ONLY else "img"))

# Le thread principal reclear l'Event SIGBUS après laisser le temps aux workers de
# rouvrir leurs handles, et maintient le process vivant.
while True:
    if bus_error.is_set():
        time.sleep(1.0)
        bus_error.clear()
    time.sleep(0.5)
