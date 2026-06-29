from pathlib import Path

from .presets import normalize_preset_config


def _escape_drawtext_text(value):
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def _drawtext_font_option():
    for font_path in (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ):
        if font_path.exists():
            escaped = str(font_path).replace("\\", "/").replace(":", "\\:")
            return f"fontfile='{escaped}'"
    return "font='Arial'"


def build_export_video_filter(_unused_legacy_path=None, preset=None):
    config = normalize_preset_config(preset)
    width = config["width"]
    height = config["height"]
    blur = config["blur_strength"]
    vf = (
        "split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur={blur}:1[bg];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    badge_label = str(config.get("series_badge_label") or "").strip()
    badge_text = str(config.get("series_badge_text") or "").strip()
    if config.get("series_badge_enabled") and badge_text:
        label = _escape_drawtext_text(badge_label)
        text = _escape_drawtext_text(badge_text)
        badge_font = config["series_badge_font_size"]
        part_font = config["series_badge_part_font_size"]
        font_option = _drawtext_font_option()
        vf += (
            ",drawbox=x=80:y=86:w=iw-160:h=126:color=black@0.42:t=fill"
            f",drawtext={font_option}:text='{label}':x=(w-text_w)/2:y=108:"
            f"fontsize={badge_font}:fontcolor=white:borderw=2:bordercolor=black@0.65"
            f",drawtext={font_option}:text='{text}':x=(w-text_w)/2:y=160:"
            f"fontsize={part_font}:fontcolor=white:borderw=2:bordercolor=black@0.65"
        )
    return vf


def build_export_command(source_path, output_path, start, duration, _unused_legacy_path=None, preset=None):
    config = normalize_preset_config(preset)
    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", source_path,
        "-t", str(duration),
        "-vf", build_export_video_filter(_unused_legacy_path, config),
        "-c:v", "libx264", "-preset", config["encoder_preset"], "-crf", str(config["crf"]),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
