from .timeline import segments_between, text_between


HOOK_TERMS = ("why", "how", "what", "mistake", "secret", "surprising", "problem", "stop", "fix", "setup", "roast", "joke")
QUOTE_TERMS = ("best", "truth", "remember", "important", "change", "result", "lesson")
COMEDY_TERMS = ("laugh", "laughing", "laughter", "joke", "funny", "audience", "judge", "judges", "applause", "punchline", "roast", "samay", "taali", "hassi")


def _candidate(candidate_id: str, source: str, start: float, end: float, timeline: dict, **extra) -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_source": source,
        "start": max(0.0, start),
        "end": min(float(timeline.get("duration", end)), end),
        "text": text_between(timeline.get("segments", []), start, end),
        **extra,
    }


def generate_candidates(timeline: dict, constraints: dict, limit: int = 300) -> list[dict]:
    segments = timeline.get("segments", [])
    duration = float(timeline.get("duration", 0))
    candidates: list[dict] = []
    counter = 1

    for peak in timeline.get("audio_peaks", [])[:80]:
        peak_time = float(peak.get("peak_time", peak.get("start", 0)))
        window_segments = segments_between(segments, max(0, peak_time - 45), min(duration, peak_time + 35))
        start = float(window_segments[0].get("start", max(0, peak_time - 18))) if window_segments else max(0, peak_time - 18)
        end = float(window_segments[-1].get("end", min(duration, peak_time + 12))) if window_segments else min(duration, peak_time + 12)
        candidates.append(_candidate(
            f"v2-{counter}", "audio_peak", start, end, timeline,
            has_audio_peak=True,
            audio_peak_energy=float(peak.get("energy", 0)),
            peak_time=peak_time,
        ))
        counter += 1

    for idx, segment in enumerate(segments):
        text = str(segment.get("text", "")).lower()
        source = ""
        if any(term in text for term in COMEDY_TERMS):
            source = "comedy"
        elif any(term in text for term in HOOK_TERMS):
            source = "qa" if "?" in str(segment.get("text", "")) or text.startswith(("why", "how", "what")) else "transcript_hook"
        elif any(term in text for term in QUOTE_TERMS):
            source = "quote_value"
        if not source:
            continue
        start_idx = max(0, idx - 1)
        end_idx = min(len(segments) - 1, idx + 2)
        start = float(segments[start_idx].get("start", segment.get("start", 0)))
        end = float(segments[end_idx].get("end", segment.get("end", start)))
        candidates.append(_candidate(f"v2-{counter}", source, start, end, timeline))
        counter += 1

    rolling_gap = max(1, len(segments) // 24) if duration > 1800 else 6
    rolling_span = 14 if duration > 1800 else 10
    for idx in range(0, len(segments), rolling_gap):
        window_segments = segments[idx:idx + rolling_span]
        if not window_segments:
            continue
        start = float(window_segments[0].get("start", 0))
        end = float(window_segments[-1].get("end", start))
        if end - start < float(constraints["min_duration"]):
            extended = segments[idx:idx + rolling_span + 3]
            if extended:
                end = float(extended[-1].get("end", end))
        candidates.append(_candidate(f"v2-{counter}", "scene_topic", start, end, timeline))
        counter += 1

    if not candidates:
        chunk = []
        for segment in segments:
            chunk.append(segment)
            start = float(chunk[0].get("start", 0))
            end = float(chunk[-1].get("end", start))
            if end - start >= min(55, float(constraints["max_duration"])):
                candidates.append(_candidate(f"v2-{counter}", "scene_topic", start, end, timeline))
                counter += 1
                chunk = []
        if chunk:
            candidates.append(_candidate(
                f"v2-{counter}",
                "scene_topic",
                float(chunk[0].get("start", 0)),
                float(chunk[-1].get("end", duration)),
                timeline,
            ))

    valid = [candidate for candidate in candidates if candidate["end"] > candidate["start"]]
    return valid[:limit]
