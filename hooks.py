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
