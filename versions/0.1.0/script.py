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
import mmap, struct, time, threading, json, os, signal
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

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
V_RING_SIZE   = 10

# ─── Format audio (PCM L24 / 48k / 8ch, identique receiver_2110) ─
A_SAMPLE_RATE = 48000
A_CHANNELS    = 8
A_BIT_DEPTH   = 24
A_BYTES_PER_SAMPLE  = A_BIT_DEPTH // 8
A_SAMPLES_PER_CHUNK = A_SAMPLE_RATE // 1000   # 48 (1 ms)
A_CHUNK_SIZE  = A_SAMPLES_PER_CHUNK * A_CHANNELS * A_BYTES_PER_SAMPLE   # 1152
A_HEADER_SIZE = 64
A_RING_SIZE   = 100
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
    "v": [{{"fps": 0.0, "frame_index": 0, "ms": RollingMs(), "realized": 0, "period": RollingMs()}} for _ in range(N_CHANNELS)],
    "a": [{{"fps": 0.0, "frame_index": 0, "ms": RollingMs(), "realized": 0}} for _ in range(N_CHANNELS)],
}}

# SIGBUS : signale aux workers de rouvrir leurs mmap
bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    print("SIGBUS reçu — réouverture des mmap")
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
        payload = {{"channels": out, "plugin_version": PLUGIN_VERSION}}
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
def _open_input(name, header_size, ring_size):
    """Ouvre un shm d'entrée en lecture, auto-détecte la taille de frame depuis la
    taille du fichier ((filesize - header) / ring). Renvoie (file, mmap, total, frame)
    ou None."""
    path = "/dev/shm/%s" % name
    try:
        if not os.path.exists(path): return None
        size = os.path.getsize(path)
        frame = (size - header_size) // ring_size
        if frame <= 0: return None
        total = header_size + ring_size * frame
        f = open(path, "r+b")
        shm = mmap.mmap(f.fileno(), total, prot=mmap.PROT_READ)
        return (f, shm, total, frame)
    except Exception as e:
        print("input (%s) indisponible : %s" % (name, e))
        return None

def _open_output(name, total):
    path = "/dev/shm/%s" % name
    with open(path, "wb") as f:
        f.write(b"\x00" * total)
    f = open(path, "r+b")
    return f, mmap.mmap(f.fileno(), total)


def _worker(essence, idx):
    """Boucle de retard pour un canal. essence = 'v' (vidéo) ou 'a' (audio)."""
    is_audio = (essence == "a")
    header_size = A_HEADER_SIZE if is_audio else V_HEADER_SIZE
    ring_size   = A_RING_SIZE   if is_audio else V_RING_SIZE
    out_name    = ("%s_audio_%d" % (HOSTNAME, idx)) if is_audio else ("%s_%d" % (HOSTNAME, idx))
    poll = POLL_A if is_audio else POLL_V

    in_handle = None      # (file, mmap, total, frame) ou None
    in_name   = None
    out_handle = None     # (file, mmap) ou None
    out_total = None
    buf = deque()         # (frame_bytes, ts_in_ns)
    last_in_idx = 0
    prev_ts_in = 0        # vidéo : ts producteur de la frame précédente (mesure période)
    out_idx = 0
    fps_t0 = time.time(); fps_n = 0

    def _close_in():
        nonlocal in_handle
        if in_handle is not None:
            try: in_handle[1].close(); in_handle[0].close()
            except Exception: pass
            in_handle = None

    def _close_out():
        nonlocal out_handle
        if out_handle is not None:
            try: out_handle[1].close(); out_handle[0].close()
            except Exception: pass
            out_handle = None

    while True:
        if bus_error.is_set():
            # Un seul Event partagé : on rouvre tout et on laisse l'Event se reclear
            # via le worker vidéo principal (idempotent ici : on rouvre nos handles).
            _close_in(); _close_out()
            in_name = None; out_total = None; buf.clear(); last_in_idx = 0
            time.sleep(0.5)

        with state_lock:
            wanted = state["inputs_a"][idx] if is_audio else state["inputs_v"][idx]
            D_frames = state["delays"][idx]
        if is_audio:
            if AUDIO_ONLY:
                # Audio seul : le délai est une DURÉE en ms (1 chunk = 1 ms). Aucun fps.
                D = int(round(D_frames))
            else:
                # Vidéo + audio : délai en IMAGES → ms via la période image auto-détectée
                # de l'entrée vidéo appariée. Tant qu'elle est inconnue (1ères frames),
                # on passe l'audio sans retard plutôt que de désynchroniser.
                with meas_lock:
                    pv = meas["v"][idx]["period"].avg()
                D = int(round(D_frames * pv)) if (pv and pv > 0) else 0
        else:
            D = D_frames

        # (Re)câblage de l'entrée
        if wanted != in_name:
            _close_in(); buf.clear(); last_in_idx = 0
            in_name = wanted
            if wanted:
                in_handle = _open_input(wanted, header_size, ring_size)
                if in_handle is not None:
                    print("%s[%d] câblé sur %s" % (essence, idx, wanted))
        elif wanted and in_handle is None:
            in_handle = _open_input(wanted, header_size, ring_size)

        if in_handle is None:
            with meas_lock:
                meas[essence][idx]["realized"] = 0
            time.sleep(0.02)
            continue

        f_in, shm_in, total_in, frame_in = in_handle

        # Lecture du header d'entrée
        try:
            cur_idx, ts_in = struct.unpack("QQ", shm_in[0:16])
        except Exception:
            _close_in(); in_handle = None; time.sleep(0.02); continue

        # Reset producteur (shm recréé au redémarrage d'un receiver) → rouvre
        if cur_idx < last_in_idx:
            _close_in(); in_handle = None; buf.clear(); last_in_idx = 0
            continue

        # Nouvelle(s) frame(s)/chunk(s) disponible(s)
        if cur_idx > 0 and cur_idx != last_in_idx:
            advance = cur_idx - last_in_idx
            # Période estimée par frame (ms) pour interpoler le ts des frames rattrapées
            # et (vidéo) alimenter le fps auto-détecté.
            if is_audio:
                est_ms = 1.0   # 1 chunk = 1 ms
            else:
                with meas_lock:
                    pv = meas["v"][idx]["period"].avg()
                est_ms = pv if (pv and pv > 0) else 40.0
            # Rattrape TOUTES les frames depuis la dernière lue (audio sample-accurate ;
            # vidéo : pas de drop). Bornage au ring pour ne pas relire un slot réécrit.
            start = last_in_idx + 1
            if advance >= ring_size:
                start = cur_idx - ring_size + 1
            ok = True
            for fi in range(start, cur_idx + 1):
                slot = fi % ring_size
                off = header_size + slot * frame_in
                try:
                    data = bytes(shm_in[off:off + frame_in])
                except Exception:
                    ok = False; break
                ts_est = ts_in - int((cur_idx - fi) * est_ms * 1e6)   # ts producteur interpolé
                buf.append((data, ts_est))
            if not ok:
                _close_in(); in_handle = None; time.sleep(0.02); continue
            # Vidéo : période inter-frames (ts producteur) ÷ avance → fps auto-détecté.
            if not is_audio and prev_ts_in and ts_in > prev_ts_in:
                with meas_lock:
                    meas["v"][idx]["period"].push((ts_in - prev_ts_in) / 1e6 / advance)
            prev_ts_in = ts_in
            last_in_idx = cur_idx

            # (Re)crée la sortie à la taille de l'entrée si besoin
            if out_handle is None or out_total != total_in:
                _close_out()
                try:
                    out_handle = _open_output(out_name, total_in)
                    out_total = total_in
                except Exception as e:
                    print("sortie %s : %s" % (out_name, e)); time.sleep(0.1); continue

            # Émet les frames dont le retard est atteint (flush multiple si D a baissé)
            while len(buf) > D:
                data, ts_orig = buf.popleft()
                fo, shm_out = out_handle
                wslot = out_idx % ring_size
                woff = header_size + wslot * frame_in
                try:
                    shm_out[woff:woff + frame_in] = data
                    if is_audio:
                        shm_out[0:24] = struct.pack("QQIHH", out_idx, time.time_ns(),
                                                    A_SAMPLE_RATE, A_CHANNELS, A_BIT_DEPTH)
                    else:
                        shm_out[0:16] = struct.pack("QQ", out_idx, time.time_ns())
                except Exception:
                    _close_out(); out_handle = None; break
                out_idx += 1; fps_n += 1
                with meas_lock:
                    meas[essence][idx]["ms"].push((time.time_ns() - ts_orig) / 1e6)
                    meas[essence][idx]["realized"] = len(buf)

        # Métriques fps (~1×/s)
        now = time.time()
        if now - fps_t0 >= 1.0:
            with meas_lock:
                meas[essence][idx]["fps"] = round(fps_n / (now - fps_t0), 1)
                meas[essence][idx]["frame_index"] = out_idx
            fps_t0 = now; fps_n = 0

        time.sleep(poll)


# ─── Démarrage des workers + reaper SIGBUS ──────────────────
threads = []
for i in range(N_CHANNELS):
    if HAS_VIDEO:
        threads.append(threading.Thread(target=_worker, args=("v", i), daemon=True))
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
