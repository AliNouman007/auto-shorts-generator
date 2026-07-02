import json

from services.clip_director.audio import analyze_audio_peaks
from services.clip_director.timeline import clean_segments

from . import prompts
from .boundary_expander import expand_candidate_boundary, text_for_boundary
from .episode_analyst import fallback_episode_analysis
from .model_router import complete_json
from .moment_finder import fallback_moments
from .scene_builder import fallback_scenes
from .schemas import ENGINE_NAME, normalize_brain, normalize_quality_mode
from .scoring import score_clip, score_to_percent
from .selector import select_final_clips
from .worthiness_judge import fallback_judgements

PROMPT_SAMPLE_LIMIT = 24
PROMPT_AUDIO_PEAK_LIMIT = 24
PROMPT_SCENE_LIMIT = 20
PROMPT_CANDIDATE_LIMIT = 24
PROMPT_TEXT_LIMIT = 220


def _require_selected_key(model_config: dict) -> None:
    brain = normalize_brain(model_config.get("brain"))
    if brain == "groq" and not model_config.get("groq_api_key"):
        raise RuntimeError("GROQ_API_KEY is required for Comedy V3 when Groq is selected.")
    if brain == "gemini" and not model_config.get("gemini_api_key"):
        raise RuntimeError("GEMINI_API_KEY is required for Comedy V3 when Gemini is selected.")


def _model_json(prompt: str, model_config: dict, timeout: int = 90) -> dict:
    return complete_json(
        prompt,
        normalize_brain(model_config.get("brain")),
        timeout,
        gemini_api_key=model_config.get("gemini_api_key", ""),
        gemini_model=model_config.get("gemini_model", "gemini-2.0-flash"),
        groq_api_key=model_config.get("groq_api_key", ""),
        groq_model=model_config.get("groq_model", "llama-3.1-8b-instant"),
    )


def _samples(segments: list[dict], limit: int = PROMPT_SAMPLE_LIMIT) -> list[dict]:
    if len(segments) <= limit:
        return segments
    step = max(1, len(segments) // limit)
    return segments[::step][:limit]


def _compact_prompt_items(items: list[dict], limit: int) -> list[dict]:
    compact_items = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compact = {}
        for key, value in item.items():
            if isinstance(value, str):
                compact[key] = value[:PROMPT_TEXT_LIMIT]
            elif isinstance(value, list):
                compact[key] = value[:6]
            else:
                compact[key] = value
        compact_items.append(compact)
    return compact_items


def _list_from(data: dict, key: str) -> list[dict]:
    items = data.get(key) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _coerce_boundary(boundary: dict, duration: float, max_duration: float) -> dict | None:
    try:
        start = max(0.0, float(boundary.get("start", 0)))
        end = min(float(duration), float(boundary.get("end", start)))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    if end - start > max_duration:
        end = start + max_duration
    return {**boundary, "start": round(start, 3), "end": round(end, 3)}


def _build_clip(boundary: dict, judgement: dict, moment: dict, scene: dict, trace_base: dict, segments: list[dict], brain: str, quality_mode: str) -> dict:
    final_score = score_clip(judgement, boundary)
    text = text_for_boundary(boundary, segments)
    title = str(judgement.get("hook") or moment.get("punchline_summary") or "Funny Comedy Moment").strip()[:120]
    description = str(judgement.get("why_funny") or "A complete comedy moment with setup and payoff.").strip()[:500]
    score_details = {
        **trace_base,
        "scene": scene,
        "moment": moment,
        "boundary": boundary,
        "worthiness": judgement,
    }
    return {
        "start": float(boundary["start"]),
        "end": float(boundary["end"]),
        "duration": float(boundary["end"]) - float(boundary["start"]),
        "text": text,
        "title": title,
        "description": description,
        "upload_title": title[:60],
        "upload_description": description[:650],
        "hook": str(judgement.get("hook", ""))[:220],
        "context": str(judgement.get("context", ""))[:300],
        "value": str(judgement.get("why_viral", ""))[:400],
        "payoff": str(judgement.get("payoff", ""))[:260],
        "hook_type": str(moment.get("moment_type") or "story")[:40],
        "selection_reason": str(judgement.get("why_standalone") or judgement.get("why_funny") or "Selected by Comedy V3.")[:500],
        "reason": str(judgement.get("why_funny") or "Selected by Comedy V3.")[:500],
        "virality_score": score_to_percent(judgement.get("viral_score", final_score)),
        "completion_score": score_to_percent(judgement.get("standalone_score", final_score)),
        "timestamp_engine": ENGINE_NAME,
        "candidate_source": str(moment.get("moment_type") or "comedy_v3")[:80],
        "final_score": final_score,
        "score_details_json": json.dumps(score_details, ensure_ascii=False, sort_keys=True),
        "judge_status": f"{str(judgement.get('quality_tier', ''))}:{str(judgement.get('cut_decision', ''))}"[:30],
        "candidate_id": judgement.get("candidate_id") or moment.get("candidate_id"),
        "quality_tier": judgement.get("quality_tier", ""),
        "worthiness_score": float(judgement.get("worthiness_score", final_score) or 0),
        "standalone_score": float(judgement.get("standalone_score", 0) or 0),
        "context_score": float(judgement.get("context_score", 0) or 0),
        "boundary_confidence": float(boundary.get("boundary_confidence", 0) or 0),
        "brain": brain,
        "quality_mode": quality_mode,
    }


def select_comedy_clips(
    segments: list[dict],
    duration: float,
    *,
    audio_path: str | None = None,
    video_title: str = "",
    video_description: str = "",
    model_config: dict | None = None,
    quality_mode: str = "balanced",
    max_duration: float = 180.0,
) -> list[dict]:
    model_config = model_config or {}
    _require_selected_key(model_config)
    brain = normalize_brain(model_config.get("brain"))
    quality_mode = normalize_quality_mode(quality_mode)
    clean = clean_segments(segments)
    if not clean:
        return []

    peaks = analyze_audio_peaks(audio_path)
    audio_peak_summary = peaks[:PROMPT_AUDIO_PEAK_LIMIT]
    seed = {
        "title": video_title,
        "description": video_description,
        "duration": duration,
        "transcript_samples": _samples(clean),
        "audio_peak_summary": audio_peak_summary,
    }

    episode_analysis = fallback_episode_analysis(video_title, video_description)
    scenes = fallback_scenes(clean, duration)
    moments = fallback_moments(scenes, clean)
    try:
        episode_analysis = _model_json(prompts.episode_analyst_prompt(seed), model_config, timeout=60) or episode_analysis
        scene_data = _model_json(prompts.scene_builder_prompt({**seed, "episode_analysis": episode_analysis}), model_config, timeout=90)
        scenes = _list_from(scene_data, "scenes") or scenes
        prompt_scenes = _compact_prompt_items(scenes, PROMPT_SCENE_LIMIT)
        moment_data = _model_json(prompts.moment_finder_prompt({**seed, "episode_analysis": episode_analysis, "scenes": prompt_scenes}), model_config, timeout=90)
        moments = _list_from(moment_data, "moments") or moments
        moments = _compact_prompt_items(moments, PROMPT_CANDIDATE_LIMIT)
        boundary_data = _model_json(prompts.boundary_expander_prompt({**seed, "scenes": prompt_scenes, "moments": moments, "max_duration": max_duration}), model_config, timeout=90)
        raw_boundaries = _list_from(boundary_data, "boundaries")
        raw_boundaries = _compact_prompt_items(raw_boundaries, PROMPT_CANDIDATE_LIMIT)
        judgement_data = _model_json(prompts.worthiness_judge_prompt({**seed, "scenes": prompt_scenes, "moments": moments, "boundaries": raw_boundaries}), model_config, timeout=90)
        judgements = _list_from(judgement_data, "judgements")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Comedy V3 model pipeline failed: {exc}") from exc

    if not moments:
        return []
    if not raw_boundaries:
        raw_boundaries = [expand_candidate_boundary(moment, clean, max_duration=max_duration) for moment in moments]
    boundaries = [
        boundary for boundary in (_coerce_boundary(boundary, duration, max_duration) for boundary in raw_boundaries)
        if boundary
    ]
    if not judgements:
        judgements = fallback_judgements(boundaries, moments, clean)

    scene_by_id = {str(scene.get("scene_id")): scene for scene in scenes}
    moment_by_id = {str(moment.get("candidate_id")): moment for moment in moments}
    boundary_by_id = {str(boundary.get("candidate_id")): boundary for boundary in boundaries}
    trace_base = {
        "engine": ENGINE_NAME,
        "brain": brain,
        "quality_mode": quality_mode,
        "episode_analysis": episode_analysis,
    }
    clips = []
    for judgement in judgements:
        candidate_id = str(judgement.get("candidate_id", ""))
        boundary = boundary_by_id.get(candidate_id)
        moment = moment_by_id.get(candidate_id)
        if not boundary or not moment:
            continue
        scene = scene_by_id.get(str(moment.get("scene_id")), {})
        clips.append(_build_clip(boundary, judgement, moment, scene, trace_base, clean, brain, quality_mode))
    return select_final_clips(clips, quality_mode=quality_mode)
