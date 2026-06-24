from collections.abc import Callable

from services import shorts_director

from .audio import analyze_audio_peaks
from .episode_map import build_episode_map
from .types import AudioPeak, ClipConstraints, ClipMode, DirectorSelection, EpisodeMap, TranscriptSegment


LlmSelector = Callable[[EpisodeMap, ClipConstraints, str], list[DirectorSelection]]


def normalize_mode(mode: str | None) -> ClipMode:
    return "highlights" if str(mode or "").lower() == "highlights" else "shorts"


def constraints_for_mode(mode: str | None) -> ClipConstraints:
    normalized = normalize_mode(mode)
    return {
        "mode": normalized,
        "min_duration": 60.0 if normalized == "highlights" else 30.0,
        "max_duration": 300.0 if normalized == "highlights" else 180.0,
        "safety_max_clips": 12,
    }


def _clip_text(segments: list[TranscriptSegment], start: float, end: float, fallback: str = "") -> str:
    text = " ".join(
        str(seg.get("text", "")).strip()
        for seg in segments
        if float(seg.get("end", seg.get("start", 0))) > start
        and float(seg.get("start", 0)) < end
    ).strip()
    return text or fallback


def _coerce_clip(
    item: dict,
    episode_map: EpisodeMap,
    constraints: ClipConstraints,
) -> dict | None:
    try:
        start = max(0.0, float(item.get("start", 0)))
        end = min(float(episode_map.get("duration", 0)), float(item.get("end", start)))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    duration = end - start
    if duration > constraints["max_duration"]:
        if constraints["mode"] == "shorts":
            return None
        end = start + constraints["max_duration"]
        duration = end - start
    if duration < constraints["min_duration"] and float(episode_map.get("duration", 0)) > constraints["min_duration"]:
        return None
    text = _clip_text(episode_map.get("segments", []), start, end, str(item.get("text", "")))
    return shorts_director.enrich_clip_metadata({
        **item,
        "start": start,
        "end": end,
        "duration": duration,
        "text": text,
        "title": item.get("title") or "Selected Clip",
        "selection_reason": item.get("selection_reason") or item.get("reason") or "Selected by the dynamic clip director.",
    })


def _dedupe(clips: list[dict], limit: int) -> list[dict]:
    return shorts_director.dedupe_clips(clips, limit=limit, overlap_threshold=0.45)


def fallback_dynamic_clips(episode_map: EpisodeMap, constraints: ClipConstraints) -> list[dict]:
    moments = episode_map.get("candidate_moments", [])
    segments = episode_map.get("segments", [])
    candidates = []
    for idx, moment in enumerate(moments, start=1):
        candidates.append({
            **moment,
            "title": f"High Energy Moment {idx}",
            "virality_score": 75 + min(20, int(float(moment.get("audio_peak_energy", 0)) * 20)),
            "completion_score": 75,
            "hook_type": "emotional",
            "selection_reason": "Selected from an audio energy peak and expanded to nearby transcript context.",
        })
    if not candidates and segments:
        candidates = shorts_director.build_director_candidates(
            segments,
            duration=float(episode_map.get("duration", 0)),
            preset={
                "min_clip_duration": constraints["min_duration"],
                "preferred_max_clip_duration": min(180, constraints["max_duration"]),
                "hard_max_clip_duration": constraints["max_duration"],
                "allow_three_minute_shorts": constraints["max_duration"] > 180,
            },
            limit=constraints["safety_max_clips"],
        )
    valid = [
        clip for clip in (
            _coerce_clip(candidate, episode_map, constraints) for candidate in candidates
        )
        if clip
    ]
    valid.sort(key=lambda item: (item.get("has_audio_peak", False), item.get("virality_score", 0)), reverse=True)
    return _dedupe(valid, constraints["safety_max_clips"])


def select_dynamic_clips(
    segments: list[TranscriptSegment],
    duration: float,
    *,
    audio_path: str | None = None,
    audio_peaks: list[AudioPeak] | None = None,
    video_title: str = "",
    mode: str = "shorts",
    genre_hint: str = "",
    ai_model: str = "openai",
    llm_selector: LlmSelector | None = None,
    allow_fallback: bool = True,
) -> list[dict]:
    if not segments:
        return []
    normalized_mode = normalize_mode(mode)
    constraints = constraints_for_mode(normalized_mode)
    peaks = audio_peaks if audio_peaks is not None else analyze_audio_peaks(audio_path)
    episode_map = build_episode_map(
        segments=segments,
        duration=duration,
        audio_peaks=peaks,
        title=video_title,
        mode=normalized_mode,
        genre_hint=genre_hint,
    )
    selections = llm_selector(episode_map, constraints, ai_model) if llm_selector else []
    clips = [
        clip for clip in (
            _coerce_clip(selection, episode_map, constraints) for selection in selections
        )
        if clip
    ]
    if clips:
        return _dedupe(clips, constraints["safety_max_clips"])
    if not allow_fallback:
        return []
    return fallback_dynamic_clips(episode_map, constraints)
