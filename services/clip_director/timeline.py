from .types import AudioPeak, TranscriptSegment


def clean_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    cleaned: list[TranscriptSegment] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        try:
            start = max(0.0, float(segment.get("start", 0)))
            end = max(start, float(segment.get("end", start)))
        except (TypeError, ValueError):
            continue
        cleaned.append({"start": start, "end": end, "text": text})
    return sorted(cleaned, key=lambda item: float(item.get("start", 0)))


def build_timeline(
    segments: list[TranscriptSegment],
    duration: float,
    audio_peaks: list[AudioPeak] | None = None,
) -> dict:
    clean = clean_segments(segments)
    silences = []
    previous_end = 0.0
    for segment in clean:
        start = float(segment.get("start", 0))
        if start - previous_end >= 0.8:
            silences.append({"start": previous_end, "end": start, "duration": start - previous_end})
        previous_end = max(previous_end, float(segment.get("end", start)))
    source_duration = float(duration or previous_end)
    if source_duration - previous_end >= 0.8:
        silences.append({"start": previous_end, "end": source_duration, "duration": source_duration - previous_end})
    return {
        "duration": source_duration,
        "segments": clean,
        "audio_peaks": sorted(audio_peaks or [], key=lambda peak: float(peak.get("peak_time", peak.get("start", 0)))),
        "silences": silences,
    }


def segments_between(segments: list[TranscriptSegment], start: float, end: float) -> list[TranscriptSegment]:
    return [
        segment for segment in segments
        if float(segment.get("end", segment.get("start", 0))) > start
        and float(segment.get("start", 0)) < end
    ]


def text_between(segments: list[TranscriptSegment], start: float, end: float) -> str:
    return " ".join(str(segment.get("text", "")).strip() for segment in segments_between(segments, start, end)).strip()
