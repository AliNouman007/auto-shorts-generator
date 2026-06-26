import re


def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_series_title(source_title: str, max_words: int = 6) -> str:
    title = compact_spaces(re.sub(r"https?://\S+", " ", source_title or ""))
    title = re.split(r"\s+[|:]\s+|\s+-\s+", title, maxsplit=1)[0]
    words = title.split()
    if len(words) > max_words:
        title = " ".join(words[:max_words])
    return title[:54].strip(" -|:")


def build_series_badge(
    source_title: str,
    part_number: int,
    total_parts: int,
    label: str = "Funniest Moment",
    enabled: bool = True,
) -> dict:
    try:
        part = int(part_number)
        total = int(total_parts)
    except (TypeError, ValueError):
        part = 0
        total = 0
    if not enabled or part <= 0 or total <= 1:
        return {
            "series_badge_enabled": False,
            "series_badge_label": compact_spaces(label)[:32],
            "series_badge_text": "",
            "series_part_number": part,
            "series_total_parts": total,
            "series_title": clean_series_title(source_title),
        }
    title = clean_series_title(source_title)
    part_text = f"Part {part}/{total}"
    badge_text = f"{title} - {part_text}" if title else part_text
    return {
        "series_badge_enabled": True,
        "series_badge_label": compact_spaces(label)[:32] or "Funniest Moment",
        "series_badge_text": badge_text[:80],
        "series_part_number": part,
        "series_total_parts": total,
        "series_title": title,
    }
