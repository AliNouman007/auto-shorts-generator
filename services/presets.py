import copy


DEFAULT_PRESET_CONFIG = {
    "captions_enabled": True,
    "caption_font_size": 10,
    "caption_margin_v": 40,
    "width": 1080,
    "height": 1920,
    "encoder_preset": "veryfast",
    "crf": 24,
    "blur_strength": 30,
}

ENCODER_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
}


def _coerce_int(value, default, min_value, max_value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def normalize_preset_config(config=None):
    normalized = copy.deepcopy(DEFAULT_PRESET_CONFIG)
    if isinstance(config, dict):
        normalized.update({k: v for k, v in config.items() if v is not None})

    normalized["captions_enabled"] = normalized.get("captions_enabled") is not False
    normalized["caption_font_size"] = _coerce_int(
        normalized.get("caption_font_size"),
        DEFAULT_PRESET_CONFIG["caption_font_size"],
        6,
        48,
    )
    normalized["caption_margin_v"] = _coerce_int(
        normalized.get("caption_margin_v"),
        DEFAULT_PRESET_CONFIG["caption_margin_v"],
        0,
        240,
    )
    normalized["width"] = _coerce_int(normalized.get("width"), 1080, 360, 2160)
    normalized["height"] = _coerce_int(normalized.get("height"), 1920, 640, 3840)
    normalized["crf"] = _coerce_int(normalized.get("crf"), 24, 16, 40)
    normalized["blur_strength"] = _coerce_int(
        normalized.get("blur_strength"),
        30,
        0,
        80,
    )

    encoder_preset = str(normalized.get("encoder_preset") or "veryfast").lower()
    normalized["encoder_preset"] = encoder_preset if encoder_preset in ENCODER_PRESETS else "veryfast"
    return normalized
