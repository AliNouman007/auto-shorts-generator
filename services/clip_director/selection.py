import json
import os
from collections.abc import Callable

from services import shorts_director

from .audio import analyze_audio_peaks
from .boundaries import optimize_candidate_boundaries
from .candidates import generate_candidates
from .episode_map import build_episode_map
from .judge import apply_llm_judgement
from .scoring import score_candidate
from .timeline import build_timeline
from .types import AudioPeak, ClipConstraints, ClipMode, DirectorSelection, EpisodeMap, TranscriptSegment


LlmSelector = Callable[[EpisodeMap, ClipConstraints, str], list[DirectorSelection]]
V2_MIN_FINAL_SCORE = 0.45
V2_RESCUE_FINAL_SCORE = 0.12


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


def viral_timestamp_engine_v2_enabled() -> bool:
    return os.environ.get("VIRAL_TIMESTAMP_ENGINE_V2", "true").strip().lower() not in {"0", "false", "no", "off"}


def target_v2_clip_count(duration: float) -> int:
    minutes = float(duration or 0) / 60.0
    if minutes > 45:
        return 10
    if minutes > 20:
        return 6
    if minutes >= 10:
        return 3
    return 1


def _is_comedy_context(video_title: str, genre_hint: str) -> bool:
    text = f"{video_title} {genre_hint}".lower()
    return any(term in text for term in ("comedy", "funny", "latent", "standup", "stand-up", "joke", "stage show"))


def minimum_v2_clip_count(duration: float) -> int:
    minutes = float(duration or 0) / 60.0
    if minutes > 45:
        return 8
    if minutes > 20:
        return 6
    if minutes >= 10:
        return 3
    return 1


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


def _score_details_json(clip: dict) -> str:
    return json.dumps({
        "engine_trace": clip.get("engine_trace", []),
        "features": clip.get("feature_scores", {}),
        "penalties": clip.get("penalties", {}),
        "base_score": clip.get("base_score", 0),
        "penalty": clip.get("penalty", 0),
        "boundary_notes": clip.get("boundary_notes", []),
    }, ensure_ascii=False, sort_keys=True)


def _dedupe_v2(candidates: list[dict], limit: int, overlap_threshold: float = 0.60) -> list[dict]:
    selected = []
    for candidate in candidates:
        duplicate_overlap = 0.0
        for kept in selected:
            duplicate_overlap = max(duplicate_overlap, shorts_director.overlap_ratio(candidate, kept))
        if duplicate_overlap >= overlap_threshold:
            continue
        selected.append({**candidate, "duplicate_overlap": duplicate_overlap})
        if len(selected) >= limit:
            break
    return selected


def _diversify_v2_candidates(candidates: list[dict], duration: float, limit: int) -> list[dict]:
    if not candidates or duration <= 0 or limit <= 0:
        return candidates[:limit]
    bucket_count = min(8, max(2, limit))
    buckets: list[list[dict]] = [[] for _ in range(bucket_count)]
    for candidate in candidates:
        start = float(candidate.get("start", 0))
        bucket_index = min(bucket_count - 1, max(0, int((start / duration) * bucket_count)))
        buckets[bucket_index].append(candidate)
    selected = []
    seen = set()
    while len(selected) < limit and any(buckets):
        for bucket in buckets:
            if not bucket or len(selected) >= limit:
                continue
            candidate = bucket.pop(0)
            candidate_id = str(candidate.get("candidate_id"))
            if candidate_id in seen:
                continue
            selected.append(candidate)
            seen.add(candidate_id)
    for candidate in candidates:
        if len(selected) >= limit:
            break
        candidate_id = str(candidate.get("candidate_id"))
        if candidate_id not in seen:
            selected.append(candidate)
            seen.add(candidate_id)
    return selected


def _coerce_v2_clip(clip: dict, episode_map: EpisodeMap, constraints: ClipConstraints) -> dict | None:
    coerced = _coerce_clip(clip, episode_map, constraints)
    if not coerced:
        return None
    final_score = float(clip.get("final_score", 0) or 0)
    enriched = {
        **coerced,
        "timestamp_engine": "v2",
        "candidate_source": clip.get("candidate_source", ""),
        "final_score": round(final_score, 4),
        "score_details_json": clip.get("score_details_json") or _score_details_json(clip),
        "judge_status": clip.get("judge_status", "accepted"),
        "virality_score": int(coerced.get("virality_score") or round(final_score * 100)),
    }
    return enriched


def _episode_map_for_shortlist(
    timeline: dict,
    duration: float,
    peaks: list[AudioPeak],
    video_title: str,
    normalized_mode: ClipMode,
    genre_hint: str,
    shortlist: list[dict],
) -> EpisodeMap:
    episode_map = build_episode_map(
        segments=timeline["segments"],
        duration=duration,
        audio_peaks=peaks,
        title=video_title,
        mode=normalized_mode,
        genre_hint=genre_hint,
    )
    episode_map["candidate_moments"] = shortlist[:30]
    episode_map["shortlisted_candidates"] = shortlist[:40]
    return episode_map


def _eligible_v2_candidates(candidates: list[dict]) -> list[dict]:
    return [
        candidate for candidate in candidates
        if float(candidate.get("final_score", 0) or 0) >= V2_MIN_FINAL_SCORE
    ]


def _rescue_v2_candidates(candidates: list[dict], comedy_context: bool) -> list[dict]:
    if not comedy_context:
        return []
    rescue = [
        candidate for candidate in candidates
        if float(candidate.get("final_score", 0) or 0) >= V2_RESCUE_FINAL_SCORE
    ]
    return rescue or list(candidates)


def _fill_v2_minimum(clips: list[dict], candidates: list[dict], target_count: int) -> list[dict]:
    filled = list(clips)
    existing_ids = {str(clip.get("candidate_id")) for clip in filled}
    for candidate in candidates:
        if len(filled) >= target_count:
            break
        candidate_id = str(candidate.get("candidate_id"))
        if candidate_id in existing_ids:
            continue
        if any(shorts_director.overlap_ratio(candidate, kept) >= 0.60 for kept in filled):
            continue
        filled.append({
            **candidate,
            "title": candidate.get("title") or "Funny Clip",
            "selection_reason": candidate.get("selection_reason") or "Selected by comedy V2 scoring to meet the minimum clip target.",
            "judge_status": candidate.get("judge_status") or "deterministic_fill",
        })
        existing_ids.add(candidate_id)
    return filled


def complete_short_clip_v2(segments: list[TranscriptSegment], duration: float) -> list[dict]:
    text = _clip_text(segments, 0, duration)
    engine_trace = [
        "V2 timestamp engine enabled",
        f"source_duration={float(duration):.1f}s",
        "complete_short=true",
        "source video is already short, kept as one clip",
    ]
    score_details = json.dumps({
        "engine_trace": engine_trace,
        "features": {"duration": 1.0, "standalone": 1.0},
        "penalties": {},
        "base_score": 0.75,
        "penalty": 0,
    }, ensure_ascii=False, sort_keys=True)
    return [shorts_director.enrich_clip_metadata({
        "start": 0.0,
        "end": float(duration),
        "duration": float(duration),
        "text": text,
        "title": "Complete short",
        "hook_type": "complete_short",
        "virality_score": 65,
        "completion_score": 90,
        "selection_reason": "V2 kept the source as one complete clip because it is already short.",
        "timestamp_engine": "v2",
        "candidate_source": "complete_short",
        "final_score": 0.75,
        "score_details_json": score_details,
        "judge_status": "complete_short",
    })]


def select_dynamic_clips_v2(
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
) -> list[dict]:
    normalized_mode = normalize_mode(mode)
    constraints = constraints_for_mode(normalized_mode)
    peaks = audio_peaks if audio_peaks is not None else analyze_audio_peaks(audio_path)
    timeline = build_timeline(segments, duration, peaks)
    comedy_context = _is_comedy_context(video_title, genre_hint)
    engine_trace = [
        "V2 timestamp engine enabled",
        f"source_duration={float(duration):.1f}s mode={normalized_mode} comedy_context={comedy_context}",
        f"timeline_segments={len(timeline.get('segments', []))} audio_peaks={len(peaks)}",
    ]
    rough_candidates = generate_candidates(timeline, constraints)
    engine_trace.append(f"rough_candidates={len(rough_candidates)}")
    optimized = [
        candidate for candidate in (
            optimize_candidate_boundaries(candidate, timeline, constraints)
            for candidate in rough_candidates
        )
        if candidate
    ]
    engine_trace.append(f"boundary_optimized={len(optimized)}")
    scored = [score_candidate(candidate) for candidate in optimized]
    scored.sort(key=lambda item: float(item.get("final_score", 0)), reverse=True)
    deduped = _dedupe_v2(scored, limit=80)
    engine_trace.append(f"deduped_candidates={len(deduped)}")
    if not deduped:
        return []
    target_count = min(constraints["safety_max_clips"], target_v2_clip_count(duration))
    minimum_count = min(constraints["safety_max_clips"], minimum_v2_clip_count(duration))
    eligible = _eligible_v2_candidates(deduped)
    if len(eligible) < minimum_count:
        rescue = _rescue_v2_candidates(deduped, comedy_context)
        rescue_ids = {str(candidate.get("candidate_id")) for candidate in eligible}
        for candidate in rescue:
            if str(candidate.get("candidate_id")) not in rescue_ids:
                eligible.append({
                    **candidate,
                    "judge_status": "deterministic_rescue",
                    "selection_reason": "Selected by V2 comedy rescue because the transcript/LLM signals were too sparse.",
                })
                rescue_ids.add(str(candidate.get("candidate_id")))
    eligible = _diversify_v2_candidates(eligible, duration, limit=60)
    engine_trace.append(f"eligible_score_floor_{V2_MIN_FINAL_SCORE}={len(eligible)}")
    engine_trace.append(f"minimum_required={minimum_count} target={target_count}")
    judged: list[dict] = []
    batch_size = max(6, target_count)
    if llm_selector:
        for offset in range(0, min(len(eligible), 60), batch_size):
            batch = eligible[offset:offset + batch_size]
            if not batch:
                continue
            engine_trace.append(f"llm_judge_batch={offset // batch_size + 1} candidates={len(batch)}")
            episode_map = _episode_map_for_shortlist(
                timeline,
                duration,
                peaks,
                video_title,
                normalized_mode,
                genre_hint,
                batch,
            )
            selections = llm_selector(episode_map, constraints, ai_model)
            accepted_batch = apply_llm_judgement(batch, selections)
            engine_trace.append(f"llm_judge_batch={offset // batch_size + 1} accepted={len(accepted_batch)}")
            judged.extend(accepted_batch)
            judged = _dedupe_v2(
                [clip for clip in sorted(judged, key=lambda item: float(item.get("final_score", 0)), reverse=True)],
                limit=constraints["safety_max_clips"],
            )
            if len(judged) >= target_count:
                break
    else:
        engine_trace.append("llm_judge_skipped=no_selector")
    judged = [
        clip for clip in judged
        if float(clip.get("final_score", 0) or 0) >= V2_MIN_FINAL_SCORE
    ]
    before_fill_count = len(judged)
    judged = _fill_v2_minimum(judged, eligible, target_count)
    engine_trace.append(f"accepted_after_score_floor={before_fill_count}")
    engine_trace.append(f"selected_after_deterministic_fill={len(judged)}")
    if len(judged) < minimum_count:
        engine_trace.append("failed_minimum_required")
        return []
    episode_map = _episode_map_for_shortlist(
        timeline,
        duration,
        peaks,
        video_title,
        normalized_mode,
        genre_hint,
        judged,
    )
    clips = [
        clip for clip in (
            _coerce_v2_clip({**selection, "engine_trace": engine_trace, "score_details_json": _score_details_json({**selection, "engine_trace": engine_trace})}, episode_map, constraints)
            for selection in judged
        )
        if clip
    ]
    clips.sort(key=lambda item: float(item.get("final_score", 0)), reverse=True)
    return _dedupe(clips, constraints["safety_max_clips"])


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
    use_v2: bool | None = None,
) -> list[dict]:
    if not segments:
        return []
    use_v2_engine = viral_timestamp_engine_v2_enabled() if use_v2 is None else use_v2
    if use_v2_engine:
        return select_dynamic_clips_v2(
            segments,
            duration,
            audio_path=audio_path,
            audio_peaks=audio_peaks,
            video_title=video_title,
            mode=mode,
            genre_hint=genre_hint,
            ai_model=ai_model,
            llm_selector=llm_selector,
        )
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
