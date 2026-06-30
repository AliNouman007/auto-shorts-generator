def overlap_ratio(a: dict, b: dict) -> float:
    start = max(float(a.get("start", 0)), float(b.get("start", 0)))
    end = min(float(a.get("end", 0)), float(b.get("end", 0)))
    if end <= start:
        return 0.0
    shortest = min(
        float(a.get("end", 0)) - float(a.get("start", 0)),
        float(b.get("end", 0)) - float(b.get("start", 0)),
    )
    return (end - start) / shortest if shortest > 0 else 0.0


def clip_strength(clip: dict) -> float:
    return (
        float(clip.get("worthiness_score", 0) or 0) * 0.35
        + float(clip.get("standalone_score", 0) or 0) * 0.25
        + float(clip.get("context_score", 0) or 0) * 0.20
        + float(clip.get("boundary_confidence", 0) or 0) * 0.20
    )


def _allowed(clip: dict, quality_mode: str) -> bool:
    tier = str(clip.get("quality_tier", "")).upper()
    if str(clip.get("cut_decision", "")).lower() == "reject" or tier == "C":
        return False
    if quality_mode == "strict":
        return tier == "A"
    if quality_mode == "volume":
        return tier in {"A", "B"}
    if tier == "A":
        return True
    return (
        tier == "B"
        and float(clip.get("worthiness_score", 0) or 0) >= 0.70
        and float(clip.get("standalone_score", 0) or 0) >= 0.70
        and float(clip.get("context_score", 0) or 0) >= 0.70
        and float(clip.get("boundary_confidence", 0) or 0) >= 0.70
    )


def select_final_clips(clips: list[dict], quality_mode: str = "balanced") -> list[dict]:
    allowed = [clip for clip in clips if _allowed(clip, quality_mode)]
    allowed.sort(key=clip_strength, reverse=True)
    selected = []
    for clip in allowed:
        replaced = False
        for index, kept in enumerate(selected):
            if overlap_ratio(clip, kept) >= 0.55:
                if clip_strength(clip) > clip_strength(kept):
                    selected[index] = clip
                replaced = True
                break
        if not replaced:
            selected.append(clip)
    selected.sort(key=lambda item: float(item.get("start", 0)))
    return selected
