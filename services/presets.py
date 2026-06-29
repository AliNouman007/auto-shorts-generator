import copy


DEFAULT_PRESET_CONFIG = {
    "width": 1080,
    "height": 1920,
    "encoder_preset": "veryfast",
    "crf": 24,
    "blur_strength": 30,
    "min_clip_duration": 18,
    "preferred_max_clip_duration": 90,
    "hard_max_clip_duration": 180,
    "allow_three_minute_shorts": False,
    "director_mode": "balanced",
    "clip_output_mode": "shorts",
    "genre_hint": "",
    "series_badge_enabled": True,
    "series_badge_label": "Funniest Moment",
    "series_badge_font_size": 38,
    "series_badge_part_font_size": 30,
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

    normalized["width"] = _coerce_int(normalized.get("width"), 1080, 360, 2160)
    normalized["height"] = _coerce_int(normalized.get("height"), 1920, 640, 3840)
    normalized["crf"] = _coerce_int(normalized.get("crf"), 24, 16, 40)
    normalized["blur_strength"] = _coerce_int(
        normalized.get("blur_strength"),
        30,
        0,
        80,
    )
    normalized["min_clip_duration"] = _coerce_int(
        normalized.get("min_clip_duration"),
        DEFAULT_PRESET_CONFIG["min_clip_duration"],
        5,
        180,
    )
    normalized["preferred_max_clip_duration"] = _coerce_int(
        normalized.get("preferred_max_clip_duration"),
        DEFAULT_PRESET_CONFIG["preferred_max_clip_duration"],
        normalized["min_clip_duration"],
        300,
    )
    normalized["allow_three_minute_shorts"] = normalized.get("allow_three_minute_shorts") is True
    hard_limit = 300 if normalized["allow_three_minute_shorts"] else 180
    normalized["hard_max_clip_duration"] = _coerce_int(
        normalized.get("hard_max_clip_duration"),
        DEFAULT_PRESET_CONFIG["hard_max_clip_duration"],
        normalized["min_clip_duration"],
        hard_limit,
    )
    if normalized["preferred_max_clip_duration"] > normalized["hard_max_clip_duration"]:
        normalized["preferred_max_clip_duration"] = normalized["hard_max_clip_duration"]

    encoder_preset = str(normalized.get("encoder_preset") or "veryfast").lower()
    normalized["encoder_preset"] = encoder_preset if encoder_preset in ENCODER_PRESETS else "veryfast"
    director_mode = str(normalized.get("director_mode") or "balanced").lower()
    normalized["director_mode"] = director_mode if director_mode in {"balanced", "snappy", "story", "deep"} else "balanced"
    clip_output_mode = str(normalized.get("clip_output_mode") or "shorts").lower()
    normalized["clip_output_mode"] = clip_output_mode if clip_output_mode in {"shorts", "highlights"} else "shorts"
    normalized["genre_hint"] = str(normalized.get("genre_hint") or "").strip()[:120]
    normalized["series_badge_enabled"] = normalized.get("series_badge_enabled") is not False
    normalized["series_badge_label"] = str(
        normalized.get("series_badge_label") or DEFAULT_PRESET_CONFIG["series_badge_label"]
    ).strip()[:32]
    normalized["series_badge_text"] = str(normalized.get("series_badge_text") or "").strip()[:80]
    normalized["series_badge_font_size"] = _coerce_int(
        normalized.get("series_badge_font_size"),
        DEFAULT_PRESET_CONFIG["series_badge_font_size"],
        18,
        72,
    )
    normalized["series_badge_part_font_size"] = _coerce_int(
        normalized.get("series_badge_part_font_size"),
        DEFAULT_PRESET_CONFIG["series_badge_part_font_size"],
        14,
        60,
    )
    return normalized
