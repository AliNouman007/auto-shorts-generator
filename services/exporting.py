from pathlib import Path

from .presets import normalize_preset_config


def _escape_subtitle_path(path):
    return str(path).replace("\\", "/").replace(":", "\\:")


def build_export_video_filter(srt_path=None, preset=None):
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
    if srt_path and Path(srt_path).exists():
        escaped = _escape_subtitle_path(srt_path)
        vf += (
            f",subtitles='{escaped}':force_style="
            f"'FontName=Arial,FontSize={config['caption_font_size']},Alignment=2,"
            f"MarginV={config['caption_margin_v']},Bold=1,"
            "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,Outline=2'"
        )
    return vf


def build_export_command(source_path, output_path, start, duration, srt_path=None, preset=None):
    config = normalize_preset_config(preset)
    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", source_path,
        "-t", str(duration),
        "-vf", build_export_video_filter(srt_path, config),
        "-c:v", "libx264", "-preset", config["encoder_preset"], "-crf", str(config["crf"]),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
