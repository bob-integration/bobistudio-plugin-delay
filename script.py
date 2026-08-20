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
# MODE TRANCHE (slice_mode, chantier latence sous-trame, cf. patch mxl-planar-slices) :
#   - D ≥ 1 : le grain source à ré-émettre est DÉJÀ COMPLET (aucune attente par bande n'a de
#     sens) → rattrapage tamponné identique à l'historique, mais la sortie est RE-TRANCHÉE
#     (copie bande par bande + commit progressif validSlices=1..N, quasi gratuit) → l'aval
#     slice-aware garde le bénéfice et démarre sur la 1ʳᵉ bande.
#   - D == 0 : la vidéo SUIT le grain de TÊTE par tranche (pattern UDC _worker_slices :
#     get_slice(h,1) puis j=1..totalSlices, budget 1,5×période, repli grain complet h-1,
#     commit FINAL toujours) → l'étage passe de ~+1 trame à ~+1 ms.
#   Convention (identique moteur/pyramide/udc/multiview) : k tranches ⇔ lignes image
#   [0, k·slice_height) valides SUR LES 3 PLANS (Y|Cb|Cr). Éligibilité : source PROGRESSIVE
#   + hauteur divisible (algo pyramide _proxy_slice_h) ; sinon repli loggé (chemin historique).
#   L'audio n'est PAS concerné (retard par tampon de blocs 1 ms, inchangé). slice_mode
#   absent/False → comportement STRICTEMENT octet-identique à l'historique.
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import time, threading, json, signal
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
import bobimxl   # migration MXL : retard vidéo (Reader/Writer) + audio (AudioReader/AudioWriter) — Phases 1/3


def _mxl_lib_state():
    """Variante libmxl réellement chargée (baseline / x86-64-v3) — diagnostic seul, ne doit
    JAMAIS faire échouer /state."""
    try:
        return bobimxl.lib_info()
    except Exception:
        return None

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

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True



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

# ── MODE TRANCHE (chantier latence sous-trame, cf. patch mxl-planar-slices) ─────────────────
# slice_mode=true → cf. en-tête. slice_lines pilote la granularité CIBLE des tranches de
# sortie (36 lignes ≈ 1080/30 → ~30 tranches), adaptée à la hauteur réelle via _out_slice_h.
_slm = CONFIG.get("slice_mode", False)
SLICE_MODE  = _slm if isinstance(_slm, bool) else str(_slm).strip().lower() in ("1", "true", "yes", "on")
SLICE_LINES = max(1, int(CONFIG.get("slice_lines") or 36))
_SLICE_TARGET = max(1, 1080 // SLICE_LINES)

_CHROMA_DIV = {{"420": (2, 2), "422": (2, 1), "444": (1, 1)}}

def _out_slice_h(oh, ch=1):
    """MODE TRANCHE — slice_height de la sortie (même algo que pyramide _proxy_slice_h) : le
    plus petit diviseur sh de oh avec sh ≥ max(1, oh // _SLICE_TARGET), MULTIPLE de ch (lignes
    chroma entières par bande, sinon la dernière ligne d'une bande impaire en 420 pointerait
    une ligne chroma non écrite). Aucun diviseur raisonnable (sh > oh//2, ex. hauteur
    première) → 0 = inéligible (whole-frame, chemin historique)."""
    lo = max(1, oh // _SLICE_TARGET)
    for sh in range(lo, oh // 2 + 1):
        if oh % sh == 0 and sh % ch == 0:
            return sh
    return 0

# Watchdog Reader périmé (suivi de tête D==0) : source qui n'avance plus depuis ce délai alors
# que le flux existe = shm probablement RECRÉÉ sous le même nom → rouvrir le Reader (cf. pyramide).
_STALE_REOPEN_NS = 500_000_000

# Vrai dès qu'un canal vidéo produit effectivement une sortie tranchée (exposé sur :8080).
_slice_active = [False]

# ─── Format vidéo (yuv420p, taille auto-détectée) ───────────
# ⚠ shm_video_ring NE dimensionne PLUS aucun shm maison (entrée/sortie passent par bobimxl/MXL,
# profondeur = urn:x-mxl:option:history_duration par DOMAINE, cf. réglage de nœud
# mxl_history_ms — jamais par conteneur). Ici V_RING_SIZE ne borne QUE la fenêtre de rattrapage
# (reader.get) après un D≥1 : combien de grains en arrière ce canal tente de relire dans
# l'historique du flux MXL avant d'abandonner (cf. usages ci-dessous). Une valeur trop basse
# tronque le rattrapage sur un flux à fort delay ; ne PAS augmenter au-delà de l'historique
# réellement retenu par le nœud (mxl_history_ms / cadence).
V_RING_SIZE   = CONFIG.get("shm_video_ring", 10)

# ─── Format audio (PCM L24 / 48k / 8ch, identique receiver_2110) ─
A_SAMPLE_RATE = 48000
A_CHANNELS    = 8
A_SAMPLES_PER_CHUNK = A_SAMPLE_RATE // 1000   # 48 (1 ms)
# (ex-shm_audio_ring/A_RING_SIZE/A_BIT_DEPTH/A_BYTES_PER_SAMPLE/A_CHUNK_SIZE/A_HEADER_SIZE/
# A_TOTAL_SIZE : dimensionnaient un ancien shm maison audio ; retiré 2026-08-09, code mort —
# l'audio passe par bobimxl.AudioWriter, cf. plus bas.)

# ─── État runtime partagé ───────────────────────────────────
# Reprise des réglages live persistés (hook control_action) : channel_delays/link, sinon
# comportement historique (délai initial global INIT_DELAY, link activé par défaut).
def _boot_clamp(v, default):
    try:
        return max(0, min(MAX_DELAY, int(round(float(v)))))
    except (TypeError, ValueError):
        return default

_cd_cfg = CONFIG.get("channel_delays")
if isinstance(_cd_cfg, list) and _cd_cfg:
    _boot_delays = [_boot_clamp(v, INIT_DELAY) for v in _cd_cfg[:N_CHANNELS]]
    if len(_boot_delays) < N_CHANNELS:
        _boot_delays += [INIT_DELAY] * (N_CHANNELS - len(_boot_delays))
else:
    _boot_delays = [INIT_DELAY] * N_CHANNELS
_link_cfg = CONFIG.get("link")
_boot_link = bool(_link_cfg) if _link_cfg is not None else True

state_lock = threading.Lock()
state = {{
    "delays":   _boot_delays,                # consigne par canal, en IMAGES
    "link":     _boot_link,                  # délai global (les canaux suivent)
    "inputs_v": [(CONFIG.get("input_v_%d" % (i + 1)) or None) for i in range(N_CHANNELS)],
    "inputs_a": [(CONFIG.get("input_a_%d" % (i + 1)) or None) for i in range(N_CHANNELS)],
}}

# Mesures par canal/essence (remplies par les workers). `period` = période entre frames
# de l'entrée vidéo (ms), mesurée depuis les timestamps producteur → sert à convertir
# le délai (en images) en chunks audio sans fps configuré.
meas_lock = threading.Lock()
meas = {{
    "v": [{{"fps": 0.0, "nominal": 0.0, "frame_index": 0, "ms": RollingMs(), "realized": 0, "period": RollingMs(), "transit": RollingMs()}} for _ in range(N_CHANNELS)],
    "a": [{{"fps": 0.0, "frame_index": 0, "ms": RollingMs(), "realized": 0, "transit": RollingMs()}} for _ in range(N_CHANNELS)],
}}

# SIGBUS : signale aux workers de rouvrir leurs flux MXL
bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    log("SIGBUS reçu — réouverture des flux MXL", "warning")
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
            # Lu SOUS state_lock, comme les `delay_frames` ci-dessus : le handler de contrôle
            # modifie `state["delays"]` à chaud. La 1re rédaction lisait hors verrou, en
            # contradiction avec la convention du fichier trois lignes plus haut.
            _dl = [int(d) for d in state["delays"][:N_CHANNELS] if d is not None]
        _dmax = max(_dl) if _dl else None
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
        # DÉLAI D'ÉTAGE en TRAMES, pour la page Câbles. Ce module-ci n'a rien à MESURER : retarder
        # est son métier, et il connaît exactement de combien. On ne passe donc PAS par
        # bobimxl.StageDelay, qui déduit le délai de l'écart d'index — et qui rendrait 0 ici,
        # puisque le grain de sortie PROPAGE la coordonnée source (open_grain(src_index=)) : le
        # contenu part avec son horodatage d'origine, simplement N trames plus tard. Publier 0
        # pour le seul module dont la fonction est d'ajouter du délai serait le plus grossier des
        # échecs silencieux. On retient le PIRE canal : c'est lui qui décrit la chaîne.
        # Cf. docs/reference/LATENCE_CHAINE.md.
        payload = {{"channels": out, "inputs_latency_ms": ilm,
                    "own_latency_ms": own_latency_ms, "slice_mode": _slice_active[0],
                    "delai_etage_trames": (None if _dmax is None else
                                           {{"recent_moy": float(_dmax), "recent_max": float(_dmax),
                                             "vieux_moy": float(_dmax), "vieux_max": float(_dmax),
                                             # 0 par construction : ce module ne MESURE pas, il
                                             # sait. Présent quand même pour que le contrat de
                                             # `delai_etage_trames` soit le même partout.
                                             "propage": False, "non_mesurable": 0}}),
                    "plugin_version": PLUGIN_VERSION}}
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
        if self.path == "/channels":
            # Liste des canaux pour le sélecteur d'action « régler le délai d'un canal ».
            items = [{{"value": i, "label": "Canal %d" % (i + 1)}} for i in range(N_CHANNELS)]
            return self._reply(200, {{"items": items}})
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
                    # Cadence DEMANDÉE (celle du flux d'entrée — un relais n'en a pas d'autre).
                    # 0 = inconnue. L'interface s'en sert pour n'afficher la mesure que lorsqu'elle
                    # s'écarte de l'intention ; sans elle, elle ne peut qu'afficher un chiffre nu.
                    "fps_nominal": (mv["nominal"] if HAS_VIDEO else 0.0),
                }})
            link = state["link"]
            # Expose les shm câblés au NIVEAU RACINE sous le nom de leur state_field
            # (input_v_<i> / input_a_<i>) : la topologie Câbles lit le shm live ainsi
            # (routes.py : live.get(state_field)). Sans ça, les entrées paraissent
            # débranchées et aucun câble ne s'affiche.
            flat = {{}}
            for i in range(N_CHANNELS):
                if HAS_VIDEO: flat["input_v_%d" % (i + 1)] = state["inputs_v"][i]
                if HAS_AUDIO: flat["input_a_%d" % (i + 1)] = state["inputs_a"][i]
        self._reply(200, dict(flat, **{{
            "mode": MODE, "n_channels": N_CHANNELS, "max_delay": MAX_DELAY,
            "unit": ("ms" if AUDIO_ONLY else "img"), "link": link, "channels": chans,
            "log_level": LOG_LEVEL,   # lisible en condition de macro
            "plugin_version": PLUGIN_VERSION,
            "mxl_lib": _mxl_lib_state(),
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
        if self.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            ok = set_log_level(body.get("level") or body.get("log_level"))
            return self._reply(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})
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
    out_name = "%s_audio_%d" % (HOSTNAME, idx + 1)
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
                log("a[%d] câblé sur %s" % (idx, wanted), "info")
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
                                             sample_rate=A_SAMPLE_RATE)
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


# ─── MODE TRANCHE : helpers vidéo (copie identité par bande) ─
def _copy_band(dst, src, a, b, geo):
    """MODE TRANCHE — copie IDENTITÉ des lignes image [a, b) sur les 3 plans (Y|Cb|Cr) du
    payload planar, en OCTETS plats (aucun reshape — le retard ne rééchantillonne pas).
    `b` est aligné chroma par l'appelant (sr -= sr % ch). Renvoie b (cumul de lignes écrites)."""
    if b <= a:
        return a
    yr = geo["y_row"]
    dst[a * yr:b * yr] = src[a * yr:b * yr]
    ca = a // geo["ch"]; cb = b // geo["ch"]
    if cb > ca:
        ur = geo["uv_row"]; o1 = geo["y_sz"]; o2 = geo["y_sz"] + geo["uv_sz"]
        dst[o1 + ca * ur:o1 + cb * ur] = src[o1 + ca * ur:o1 + cb * ur]
        dst[o2 + ca * ur:o2 + cb * ur] = src[o2 + ca * ur:o2 + cb * ur]
    return b


def _emit_frame_sliced(writer, geo, in_idx, data):
    """MODE TRANCHE, D ≥ 1 — ré-émet un grain source déjà COMPLET en le RE-TRANCHANT : copie
    bande par bande + commit progressif (validSlices=1..N, quasi gratuit) → l'aval slice-aware
    démarre sur la 1ʳᵉ bande au lieu d'attendre la trame. Index source propagé (open_grain
    (index=in_idx), même contrat que l'historique : parité de champ / retard par index).
    Commit FINAL garanti (un grain laissé partiel ne serait jamais lisible)."""
    _i, gi, vw = writer.open_grain(src_index=in_idx)
    try:
        src = np.frombuffer(data, dtype=np.uint8)
        if src.size < geo["fr_sz"]:
            vw[:src.size] = src        # frame courte (défensif) : copie pleine, pas de bandes
            return
        a = 0
        for k in range(1, geo["total"] + 1):
            a = _copy_band(vw, src, a, min(geo["h"], k * geo["sh"]), geo)
            if k < geo["total"]:
                writer.commit(gi, valid_slices=k)   # réveille l'aval à chaque bande
    finally:
        writer.commit(gi, valid_slices=None)


def _open_out_sliced(reader, out_name):
    """MODE TRANCHE — crée le Writer de sortie depuis le format du flux d'entrée (mêmes
    arguments champ-natif que le chemin historique). Sortie ÉLIGIBLE (progressif + hauteur
    divisible) → le flowDef porte slice_height ; sinon geo["sh"]=0 = repli whole-frame
    (chemin historique), loggé. Renvoie (writer, geo) ou (None, None) si flux absent."""
    f = reader.format()
    if not f:
        return None, None
    il = f.get("interlace_mode", "progressive")
    cw, ch = _CHROMA_DIV.get(f.get("chroma") or "422", (2, 1))
    w = int(f["width"]); gh = int(f["height"])           # dims de GRAIN (champ si entrelacé)
    sh = _out_slice_h(gh, ch) if not il.startswith("interlaced") else 0
    if not sh:
        log("slice_mode : %s inéligible (%s %dx%d) → chemin historique" % (out_name, il, w, gh), "warning")
    writer = bobimxl.Writer(_mxl_inst, out_name, w, f.get("frame_height", gh), f["chroma"],
                            f["bit_depth"], f.get("frame_fps_num", f["fps_num"]), f["fps_den"],
                            interlace=il,
                            **({{"slice_height": sh}} if sh else {{}}))
    bps = 2 if int(f["bit_depth"]) >= 10 else 1
    uv_w = w // cw; uv_h = gh // ch
    y_sz = w * gh * bps; uv_sz = uv_w * uv_h * bps
    geo = dict(sh=sh, w=w, h=gh, ch=ch, y_row=w * bps, y_sz=y_sz,
               uv_row=uv_w * bps, uv_sz=uv_sz, fr_sz=y_sz + 2 * uv_sz,
               total=(gh // sh if sh else 1),
               period_s=f["fps_den"] / max(1, f["fps_num"]))
    return writer, geo


def _releve_nominal(idx, reader):
    """Cadence NOMINALE du canal = celle DÉCLARÉE par le flux d'entrée (grains/s : des champs si
    l'entrée est entrelacée). Le délai est un relais : il n'a pas de cadence propre, l'intention
    qu'il doit tenir est exactement celle de sa source.

    Sans cette valeur, l'interface ne peut afficher que la mesure brute — laquelle porte ±1 image
    de troncature de fenêtre et présente donc « 49,8 fps » à un canal parfaitement sain. Un chiffre
    qui bouge en permanence cesse d'être lu ; on ne l'affiche que lorsqu'il informe.
    Relevé une seule fois par câblage (remis à 0 au changement d'entrée), au fil de l'eau : le
    format n'est pas forcément lisible à l'instant où le Reader s'ouvre."""
    with meas_lock:
        if meas["v"][idx]["nominal"] > 0:
            return
    try:
        f = reader.format()
        n = float(f["fps_num"]) / max(1.0, float(f["fps_den"])) if f else 0.0
    except Exception:
        n = 0.0
    if n > 0:
        with meas_lock:
            meas["v"][idx]["nominal"] = round(n, 3)


def _worker_video(idx):
    """Retard VIDÉO sur MXL : lit le flux d'entrée (Reader), rattrape sans drop via get(index)
    pour un retard exact, et ré-émet décalé de D images via un Writer. Audio = _worker (legacy).
    MODE TRANCHE : branche dédiée en tête de boucle (cf. en-tête) — le chemin historique
    ci-dessous reste octet-identique et n'est emprunté que si slice_mode off / canal inéligible."""
    out_name = "%s_%d" % (HOSTNAME, idx + 1)
    reader = None; in_name = None; writer = None
    buf = deque(); last_in_idx = -1; prev_ts = 0; out_idx = 0
    geo = None                       # géométrie sortie (MODE TRANCHE) ; None = pas encore lue
    last_fresh_ns = time.time_ns()   # dernier grain frais (watchdog stale du suivi de tête)
    fps_t0 = time.time(); fps_n = 0
    while True:
        if bus_error.is_set():
            for h in (reader, writer):
                try: h and h.close()
                except Exception: pass
            reader = writer = None; geo = None; in_name = None; buf.clear(); last_in_idx = -1
            time.sleep(0.5)
        with state_lock:
            wanted = state["inputs_v"][idx]; D = state["delays"][idx]
        if wanted != in_name:
            try: reader and reader.close()
            except Exception: pass
            reader = None; buf.clear(); last_in_idx = -1; in_name = wanted
            with meas_lock: meas["v"][idx]["nominal"] = 0.0   # autre source = autre intention
        if wanted and reader is None:
            try:
                reader = bobimxl.Reader(_mxl_inst, wanted); log("v[%d] câblé sur %s" % (idx, wanted), "info")
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

        # ─── MODE TRANCHE : branche dédiée (regimes D==0 suivi / D≥1 re-tranchage) ───
        if SLICE_MODE and writer is None:
            # Éligibilité lue du flow_def (dispo dès l'existence du flux, AVANT la 1ʳᵉ trame —
            # le suivi de tête D==0 en dépend). Échec (flux/format pas prêts) → poll.
            try:
                writer, geo = _open_out_sliced(reader, out_name)
            except Exception:
                writer, geo = None, None
            if geo is not None and geo["sh"]:
                _slice_active[0] = True
                last_fresh_ns = time.time_ns()
                log("v[%d] slice_mode actif : sortie %s tranchée par %d lignes (%d tranches)"
                    % (idx, out_name, geo["sh"], geo["total"]), "info")
        if SLICE_MODE and geo is not None and geo["sh"]:
            if head == bobimxl.MXL_UNDEFINED_INDEX or head <= 0:
                time.sleep(POLL_V); continue
            if head - 1 < last_in_idx - 1:
                # Producteur recréé (index reparti en arrière) — tolérance 1 : en suivi de tête
                # last_in_idx = head (grain en cours), le dernier COMMITÉ vaut head-1.
                buf.clear(); last_in_idx = -1
            emitted = 0
            if D <= 0 and not buf:
                # ── D == 0 : SUIVRE le grain de TÊTE par tranche (pattern UDC _worker_slices) ──
                if head == last_in_idx:
                    # Watchdog : source figée trop longtemps → rouvrir le Reader (shm recréé
                    # sous le même nom). GC ENTRE close et reopen (parade générique du piège
                    # des générations, motif pyramide) : sans GC le flux périmé reste résolvable
                    # par nom et le reopen retombe sur L'ORPHELIN → gel permanent.
                    if (time.time_ns() - last_fresh_ns) > _STALE_REOPEN_NS:
                        try: reader.close()
                        except Exception: pass
                        try: _mxl_inst.garbage_collect()
                        except Exception: pass
                        try:
                            reader = bobimxl.Reader(_mxl_inst, in_name)
                        except Exception:
                            reader = None; in_name = None
                        last_fresh_ns = time.time_ns()
                        if reader is None:
                            time.sleep(0.02); continue
                    time.sleep(0.002); continue
                # 1ʳᵉ tranche du grain de tête ; pas encore là (tête à peine réclamée) ou flux
                # sans le patch slices → repli get_latest (grain complet, boucle dégénérée).
                got = reader.get_slice(head, 1, timeout_ns=2_000_000)
                if got is None:
                    got = reader.get_latest()
                if got is None or got[0] == last_in_idx:
                    time.sleep(0.002); continue
                gidx, gi_s, sview = got     # vue ZÉRO-COPIE (jamais lue au-delà du valide)
                last_fresh_ns = time.time_ns()
                t0_ns = time.time_ns()
                total = max(1, int(gi_s.totalSlices or 1))
                islh  = max(1, geo["h"] // total)   # lignes source par tranche (égales)
                valid = max(1, int(gi_s.validSlices or 1))
                # Budget d'attente TOTAL ≈ 1,5 période : une source en retard ne bloque jamais
                # la sortie au-delà d'une demi-trame après le nominal.
                deadl = time.monotonic_ns() + int(1.5e9 * geo["period_s"])
                wait_ns = 0   # cumul des ATTENTES get_slice — EXCLUES de own/delay_ms
                try:
                    _i, gi_o, vw = writer.open_grain(src_index=gidx)   # origin timestamp propagé
                    written = 0; k_done = 0
                    try:
                        for j in range(1, total + 1):
                            if j > valid:
                                left = deadl - time.monotonic_ns()
                                _w0 = time.monotonic_ns()
                                g = (reader.get_slice(gidx, j, timeout_ns=max(1, left))
                                     if left > 0 else None)
                                wait_ns += time.monotonic_ns() - _w0
                                if g is not None:
                                    valid = max(j, int(g[1].validSlices or j))
                                else:
                                    # Budget épuisé / producteur en retard → REPLI : compléter
                                    # depuis le grain COMPLET précédent (léger tearing d'UNE
                                    # image) si dispo, sinon les lignes déjà écrites restent.
                                    # Le commit FINAL est garanti par le finally.
                                    gp = (reader.get(gidx - 1, timeout_ns=2_000_000)
                                          if gidx > 0 else None)
                                    if gp is not None and gp[2].size >= geo["fr_sz"]:
                                        written = _copy_band(vw, gp[2], written, geo["h"], geo)
                                    break
                            # Tranche source j dispo : lignes [0, sr) valides sur les 3 plans
                            # → copie IDENTITÉ du delta, bornée au multiple de lignes chroma.
                            sr = min(geo["h"], j * islh); sr -= sr % geo["ch"]
                            written = _copy_band(vw, sview, written, sr, geo)
                            if j < total and written // geo["sh"] > k_done:
                                k_done = written // geo["sh"]
                                writer.commit(gi_o, valid_slices=k_done)   # réveille l'aval
                    finally:
                        # Commit FINAL TOUJOURS (un grain laissé partiel ne serait jamais
                        # lisible par un consommateur whole-frame).
                        writer.commit(gi_o, valid_slices=None)
                except Exception:
                    try: writer.close()
                    except Exception: pass
                    writer = None; geo = None
                    time.sleep(0.002); continue
                out_idx += 1; fps_n += 1; emitted = 1
                ts_in = reader.last_write_time()
                with meas_lock:
                    if prev_ts and ts_in > prev_ts and last_in_idx >= 0:
                        meas["v"][idx]["period"].push(
                            (ts_in - prev_ts) / 1e6 / max(1, gidx - last_in_idx))
                    meas["v"][idx]["transit"].push((bobimxl.now_tai() - ts_in) / 1e6)
                    # own = travail de COPIE, SANS les attentes get_slice (suivi du fil ≈
                    # période — même contrat que pyramide/udc) ; à D == 0 le « retard
                    # réalisé » du canal, c'est ce travail-là (~1 ms).
                    meas["v"][idx]["ms"].push((time.time_ns() - t0_ns - wait_ns) / 1e6)
                    meas["v"][idx]["realized"] = 0
                prev_ts = ts_in; last_in_idx = gidx
            else:
                # ── D ≥ 1 (ou tampon résiduel à purger) : le grain source à ré-émettre est
                # DÉJÀ COMPLET (aucune attente par bande n'a de sens) → rattrapage tamponné
                # IDENTIQUE à l'historique, mais émission RE-TRANCHÉE (_emit_frame_sliced).
                cur_idx = head - 1     # dernier grain COMPLET garanti (la tête peut être partielle)
                if cur_idx > 0 and cur_idx > last_in_idx:
                    ts_in = reader.last_write_time()
                    advance = (cur_idx - last_in_idx) if last_in_idx >= 0 else 1
                    with meas_lock: pv = meas["v"][idx]["period"].avg()
                    est_ms = pv if (pv and pv > 0) else 40.0
                    start = (last_in_idx + 1) if last_in_idx >= 0 else cur_idx
                    if cur_idx - start + 1 > V_RING_SIZE:  # bornage à l'historique du flux
                        start = cur_idx - V_RING_SIZE + 1
                    for fi in range(start, cur_idx + 1):
                        got = reader.get(fi, 20_000_000)   # rattrapage sans drop
                        if got is None:
                            continue                       # grain hors historique → sauté
                        ts_est = ts_in - int((cur_idx - fi) * est_ms * 1e6)
                        buf.append((bytes(got[2]), ts_est, fi))
                    with meas_lock:
                        if prev_ts and ts_in > prev_ts:
                            meas["v"][idx]["period"].push((ts_in - prev_ts) / 1e6 / max(1, advance))
                        meas["v"][idx]["transit"].push((bobimxl.now_tai() - ts_in) / 1e6)
                    prev_ts = ts_in; last_in_idx = cur_idx
                    last_fresh_ns = time.time_ns()
                # Flush HORS du bloc « nouvelle frame » : une baisse de D à chaud purge sans
                # attendre la trame suivante (amélioration propre à la branche tranche).
                while writer is not None and len(buf) > max(0, D):
                    data, ts_orig, in_i = buf.popleft()
                    try:
                        _emit_frame_sliced(writer, geo, in_i, data)
                    except Exception:
                        try: writer.close()
                        except Exception: pass
                        writer = None; geo = None; break
                    out_idx += 1; fps_n += 1; emitted += 1
                    with meas_lock:
                        meas["v"][idx]["ms"].push((bobimxl.now_tai() - ts_orig) / 1e6)
                        meas["v"][idx]["realized"] = len(buf)
            now = time.time()
            if now - fps_t0 >= 1.0:
                _releve_nominal(idx, reader)
                with meas_lock:
                    meas["v"][idx]["fps"] = round(fps_n / (now - fps_t0), 1)
                    meas["v"][idx]["frame_index"] = out_idx
                fps_t0 = now; fps_n = 0
            time.sleep(0.002 if emitted else POLL_V)
            continue

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
                buf.append((bytes(got[2]), ts_est, fi))
            if prev_ts and ts_in > prev_ts:
                with meas_lock: meas["v"][idx]["period"].push((ts_in - prev_ts) / 1e6 / max(1, advance))
            with meas_lock: meas["v"][idx]["transit"].push((bobimxl.now_tai() - ts_in) / 1e6)
            prev_ts = ts_in; last_in_idx = cur_idx
            if writer is None:
                f = reader.format()
                if f:
                    # champ-natif : on déclare la sortie au format TRAME + interlace (passthrough) ;
                    # le grain forwardé = 1 champ, écrit à l'index d'entrée (parité préservée).
                    writer = bobimxl.Writer(_mxl_inst, out_name, f["width"],
                                            f.get("frame_height", f["height"]), f["chroma"],
                                            f["bit_depth"], f.get("frame_fps_num", f["fps_num"]),
                                            f["fps_den"], interlace=f.get("interlace_mode", "progressive"))
            while writer is not None and len(buf) > D:
                data, ts_orig, in_idx = buf.popleft()
                try:
                    _i, gi, vw = writer.open_grain(src_index=in_idx)   # index d'entrée → parité de champ
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
            _releve_nominal(idx, reader)
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

log("delay démarré : %d canaux, mode=%s, délai max=%d %s"
    % (N_CHANNELS, MODE, MAX_DELAY, "ms" if AUDIO_ONLY else "img"), "info")

# Le thread principal reclear l'Event SIGBUS après laisser le temps aux workers de
# rouvrir leurs handles, et maintient le process vivant.
while True:
    if bus_error.is_set():
        time.sleep(1.0)
        bus_error.clear()
    time.sleep(0.5)
