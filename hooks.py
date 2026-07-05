# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

def before_deploy(params, context):
    """Dérive video_channels / audio_channels depuis le mode + n_channels.
    Ces valeurs pilotent le `repeat` du wiring : X paires d'entrées/sorties.
    repeat=0 → aucun slot câblable pour cette essence."""
    params = dict(params)
    n_ch = max(1, min(16, int(params.get("n_channels", 1) or 1)))
    mode = params.get("mode", "video")
    params["n_channels"]     = n_ch
    params["video_channels"] = n_ch if mode in ("video", "video_audio") else 0
    params["audio_channels"] = n_ch if mode in ("audio", "video_audio") else 0
    return params


def control_action(action, body, params, ctx):
    """POST /api/containers/<vmid>/control/params.
    Persiste les délais réglés en live (global, par canal, mode lié) — jusqu'ici seul
    delay_frames au boot survivait, les réglages faits via la page Traitements étaient
    perdus au redeploy/restart. Mêmes formes de body que l'endpoint /params du script
    (`{delay_frames, all:true}`, `{channel:i, delay_frames}`, `{link:bool}`, combinables) ;
    on rejoue la même logique de clamp/alignement côté params persistés puis on pousse
    le body ORIGINAL tel quel à chaud sur /params (endpoint EXISTANT sur tous les scripts
    déjà déployés → jamais de repli redeploy)."""
    if action != "params":
        return None
    new_params = dict(params)
    n_channels = max(1, min(16, int(new_params.get("n_channels") or 1)))
    max_delay = max(0, int(new_params.get("max_delay_frames") or 25))

    def _clamp(v):
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            return None
        return max(0, min(max_delay, v))

    if "link" in body:
        new_params["link"] = bool(body["link"])
    link_active = new_params.get("link", True)

    # Délai global : {delay_frames, all:true} ou link actif (même logique que le script)
    if body.get("all") or (link_active and "delay_frames" in body and "channel" not in body):
        d = _clamp(body.get("delay_frames"))
        if d is not None:
            new_params["delay_frames"] = d
            new_params["channel_delays"] = [d] * n_channels
    # Délai d'un canal : {channel:i, delay_frames:D} — aligné sur la liste existante
    elif "channel" in body and "delay_frames" in body:
        i = body.get("channel")
        d = _clamp(body.get("delay_frames"))
        if isinstance(i, int) and 0 <= i < n_channels and d is not None:
            cur = new_params.get("channel_delays")
            if isinstance(cur, list) and cur:
                base = [_clamp(v) if _clamp(v) is not None else 0 for v in cur]
            else:
                fallback = _clamp(new_params.get("delay_frames")) or 0
                base = [fallback] * n_channels
            if len(base) < n_channels:
                pad_val = base[-1] if base else 0
                base = base + [pad_val] * (n_channels - len(base))
            elif len(base) > n_channels:
                base = base[:n_channels]
            base[i] = d
            new_params["channel_delays"] = base

    return {"params": new_params, "hot_endpoint": "/params", "hot_body": body}
