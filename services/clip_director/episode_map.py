from .audio import peaks_to_moments
from .types import AudioPeak, ClipMode, EpisodeMap, TranscriptSegment


def detect_genre(title: str, segments: list[TranscriptSegment], genre_hint: str = "") -> str:
    if genre_hint.strip():
        return genre_hint.strip()
    text = f"{title} " + " ".join(str(seg.get("text", "")) for seg in segments[:40])
    lowered = text.lower()
    if any(word in lowered for word in ("funny", "comedy", "laugh", "joke", "audition", "judge", "stage", "latent", "talent")):
        return "comedy stage show"
    if any(word in lowered for word in ("podcast", "interview", "conversation")):
        return "podcast"
    if any(word in lowered for word in ("tutorial", "how to", "learn", "guide")):
        return "educational"
    return "auto"


def build_episode_map(
    segments: list[TranscriptSegment],
    duration: float,
    audio_peaks: list[AudioPeak] | None = None,
    title: str = "",
    mode: ClipMode = "shorts",
    genre_hint: str = "",
) -> EpisodeMap:
    clean_segments = [
        {
            "start": float(seg.get("start", 0)),
            "end": float(seg.get("end", seg.get("start", 0))),
            "text": str(seg.get("text", "")).strip(),
        }
        for seg in segments
        if str(seg.get("text", "")).strip()
    ]
    clean_peaks = audio_peaks or []
    return {
        "title": title or "",
        "duration": float(duration),
        "mode": mode,
        "genre_hint": genre_hint or "",
        "detected_genre": detect_genre(title or "", clean_segments, genre_hint or ""),
        "segments": clean_segments,
        "audio_peaks": clean_peaks,
        "candidate_moments": peaks_to_moments(clean_peaks, clean_segments, duration, mode),
    }
