from services.clip_director.timeline import text_between


def expand_candidate_boundary(candidate: dict, segments: list[dict], max_duration: float = 180.0) -> dict:
    if not segments:
        start = max(0.0, float(candidate.get("rough_start", 0)))
        end = max(start, float(candidate.get("rough_end", start)))
        return _boundary(candidate, start, end, 0.2, "No transcript segments available.")

    rough_start = float(candidate.get("rough_start", 0))
    rough_end = float(candidate.get("rough_end", rough_start))
    sorted_segments = sorted(segments, key=lambda item: float(item.get("start", 0)))
    overlapping_indexes = [
        idx for idx, segment in enumerate(sorted_segments)
        if float(segment.get("end", segment.get("start", 0))) > rough_start
        and float(segment.get("start", 0)) < rough_end
    ]
    if not overlapping_indexes:
        nearest = min(range(len(sorted_segments)), key=lambda idx: abs(float(sorted_segments[idx].get("start", 0)) - rough_start))
        first_idx = last_idx = nearest
    else:
        first_idx = min(overlapping_indexes)
        last_idx = max(overlapping_indexes)

    start_idx = max(0, first_idx - 2)
    end_idx = min(len(sorted_segments) - 1, last_idx + 1)
    start = float(sorted_segments[start_idx].get("start", rough_start))
    end = float(sorted_segments[end_idx].get("end", rough_end))
    if end - start > max_duration:
        start = max(0.0, rough_start - min(45.0, max_duration * 0.35))
        end = min(start + max_duration, max(float(seg.get("end", 0)) for seg in sorted_segments))
    confidence = 0.85 if start < rough_start and end > rough_end else 0.6
    return _boundary(candidate, start, end, confidence, "Expanded to include setup, punchline, and reaction context.")


def _boundary(candidate: dict, start: float, end: float, confidence: float, reason: str) -> dict:
    rough_start = float(candidate.get("rough_start", start))
    rough_end = float(candidate.get("rough_end", end))
    return {
        "candidate_id": candidate.get("candidate_id", ""),
        "scene_id": candidate.get("scene_id", ""),
        "start": round(start, 3),
        "end": round(max(start, end), 3),
        "setup_start": round(start, 3),
        "context_start": round(start, 3),
        "punchline_time": round(rough_start, 3),
        "reaction_start": round(rough_end, 3),
        "reaction_end": round(max(rough_end, end), 3),
        "payoff_end": round(max(rough_end, end), 3),
        "boundary_confidence": round(max(0.0, min(1.0, confidence)), 3),
        "boundary_reason": reason,
    }


def text_for_boundary(boundary: dict, segments: list[dict]) -> str:
    return text_between(segments, float(boundary.get("start", 0)), float(boundary.get("end", 0)))
