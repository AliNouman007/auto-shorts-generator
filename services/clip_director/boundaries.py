from .timeline import segments_between, text_between


def _nearest_segment_start(segments: list[dict], value: float, tolerance: float = 2.5) -> float:
    starts = [float(segment.get("start", 0)) for segment in segments]
    if not starts:
        return value
    nearest = min(starts, key=lambda item: abs(item - value))
    return nearest if abs(nearest - value) <= tolerance else value


def _nearest_segment_end(segments: list[dict], value: float, tolerance: float = 2.5) -> float:
    ends = [float(segment.get("end", segment.get("start", 0))) for segment in segments]
    if not ends:
        return value
    nearest = min(ends, key=lambda item: abs(item - value))
    return nearest if abs(nearest - value) <= tolerance else value


def optimize_candidate_boundaries(candidate: dict, timeline: dict, constraints: dict) -> dict | None:
    segments = timeline.get("segments", [])
    duration = float(timeline.get("duration", 0))
    start = max(0.0, float(candidate.get("start", 0)))
    end = min(duration, float(candidate.get("end", start)))
    start = _nearest_segment_start(segments, start)
    end = _nearest_segment_end(segments, end) + 0.25
    max_duration = float(constraints["max_duration"])
    min_duration = float(constraints["min_duration"])
    if end - start > max_duration:
        end = start + max_duration
    if end > duration:
        end = duration
        start = max(0.0, end - max_duration)
    if end <= start:
        return None
    if duration > min_duration and end - start < min_duration:
        return None
    overlapping = segments_between(segments, start, end)
    text = text_between(segments, start, end) or str(candidate.get("text", ""))
    boundary_notes = []
    if overlapping:
        if start == float(overlapping[0].get("start", start)):
            boundary_notes.append("snapped_start_to_segment")
        if abs(end - (float(overlapping[-1].get("end", end)) + 0.25)) < 0.01:
            boundary_notes.append("snapped_end_to_segment")
    return {
        **candidate,
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
        "text": text,
        "mid_sentence_start": bool(overlapping and start > float(overlapping[0].get("start", start)) + 0.2),
        "mid_sentence_end": bool(overlapping and end < float(overlapping[-1].get("end", end)) - 0.2),
        "boundary_notes": boundary_notes,
    }
