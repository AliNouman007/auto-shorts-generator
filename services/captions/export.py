import textwrap


def seconds_to_srt_time(seconds: float) -> str:
    total = max(0.0, float(seconds or 0))
    whole = int(total)
    millis = int(round((total - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis -= 1000
    hours, rem = divmod(whole, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d},{millis:03d}"


def wrap_caption_text(text: str, max_line_length: int = 32, max_lines: int = 3) -> list[str]:
    lines = textwrap.wrap(
        " ".join(str(text or "").split()),
        width=max_line_length,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return lines[:max_lines] or [""]


def generate_srt_from_cues(cues: list[dict]) -> str:
    lines: list[str] = []
    idx = 1
    for cue in cues or []:
        text = " ".join(str(cue.get("text", "")).split())
        if not text:
            continue
        try:
            start = float(cue.get("start", 0))
            end = float(cue.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        lines.extend([
            str(idx),
            f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}",
            *wrap_caption_text(text),
            "",
        ])
        idx += 1
    return "\n".join(lines)

