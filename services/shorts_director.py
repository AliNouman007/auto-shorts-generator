import json
import re

from services import presets


ENERGY_WORDS = {
    "amazing", "incredible", "wow", "best", "worst", "never", "always",
    "secret", "important", "shocking", "huge", "big", "win", "lose",
    "finally", "actually", "seriously", "honestly", "literally", "crazy",
    "unbelievable", "awesome", "terrible", "love", "hate", "must", "need",
    "mistake", "problem", "truth", "reason", "simple", "fix", "proof",
    "result", "wrong", "right", "avoid", "better", "fast", "watching",
}

HOOK_PHRASES = (
    "here is", "here's", "this is why", "the problem", "the biggest",
    "most people", "you need", "you should", "i learned", "i realized",
    "what happened", "why", "how to", "the truth", "the mistake",
    "if you", "stop", "don't", "never", "this one",
)

FILLER_STARTS = {
    "hello", "hi", "hey", "so", "and", "basically", "actually", "okay",
    "ok", "um", "uh", "like", "then", "because",
}

FILLER_PHRASES = (
    "hello guys", "hi guys", "hey guys", "what's up guys", "welcome back",
    "so basically", "today we are going to", "today i'm going to",
)

PAYOFF_WORDS = {
    "because", "therefore", "finally", "result", "fix", "solution",
    "answer", "that means", "the point", "takeaway", "works", "worked",
    "so you", "that is why", "that's why", "this is why",
}

HOOK_TYPES = {
    "problem_solution",
    "useful_tip",
    "story",
    "controversial",
    "emotional",
    "result_proof",
    "question",
    "complete_short",
    "fallback",
}


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", (text or "").lower())


def clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def ends_cleanly(text: str) -> bool:
    return (text or "").strip().endswith((".", "!", "?"))


def starts_cleanly(text: str) -> bool:
    text_words = words(text)
    if not text_words:
        return False
    first_text = " ".join(text_words[:5])
    return text_words[0] not in FILLER_STARTS and not any(
        first_text.startswith(phrase) for phrase in FILLER_PHRASES
    )


def normalize_director_preset(preset=None) -> dict:
    config = presets.normalize_preset_config(preset)
    allow_three_minutes = bool(config.get("allow_three_minute_shorts"))
    min_duration = _coerce_number(config.get("min_clip_duration"), 18, 5, 180)
    preferred_max = _coerce_number(
        config.get("preferred_max_clip_duration"), 90, min_duration, 300
    )
    hard_default = 300 if allow_three_minutes else 180
    hard_cap = 300 if allow_three_minutes else 180
    hard_max = _coerce_number(
        config.get("hard_max_clip_duration"), hard_default, min_duration, hard_cap
    )
    if hard_max < preferred_max:
        preferred_max = hard_max
    director_mode = str(config.get("director_mode") or "balanced").lower()
    if director_mode not in {"balanced", "snappy", "story", "deep"}:
        director_mode = "balanced"
    config.update({
        "min_clip_duration": min_duration,
        "preferred_max_clip_duration": preferred_max,
        "hard_max_clip_duration": hard_max,
        "allow_three_minute_shorts": allow_three_minutes,
        "director_mode": director_mode,
    })
    return config


def _coerce_number(value, default, min_value, max_value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(min_value), min(float(max_value), parsed))


def split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [piece.strip() for piece in pieces if piece.strip()]


def candidate_scores(text: str, duration: float, segments: list[dict] | None = None) -> tuple[int, int, str]:
    text_words = words(text)
    first_text = " ".join(text_words[:18])
    energy_hits = sum(1 for word in text_words if word in ENERGY_WORDS)
    has_question = "?" in (text or "")
    has_hook = any(phrase in first_text for phrase in HOOK_PHRASES) or has_question
    has_payoff = any(phrase in (text or "").lower() for phrase in PAYOFF_WORDS) or ends_cleanly(text)
    density = len(text_words) / duration if duration > 0 else 0
    validation = validate_clip_structure(
        {"start": 0, "end": duration, "duration": duration, "text": text},
        segments or [],
        {"min_clip_duration": 15, "preferred_max_clip_duration": 90, "hard_max_clip_duration": 300},
    )

    hook_score = 25 if has_hook and starts_cleanly(text) else 10 if has_hook else 4
    clarity_score = 20 if len(text_words) >= 35 and starts_cleanly(text) else 10
    payoff_score = 20 if has_payoff else 5
    pace_score = 15 if 1.8 <= density <= 4.5 else 9 if 1.0 <= density <= 5.2 else 3
    emotion_score = min(10, 3 + energy_hits * 2)
    quality_score = 10 if validation["valid"] else max(2, 10 - len(validation["issues"]) * 2)

    virality = clamp_score(hook_score + clarity_score + payoff_score + pace_score + emotion_score + quality_score)
    completion = clamp_score(
        (25 if starts_cleanly(text) else 8)
        + (25 if ends_cleanly(text) else 9)
        + (25 if has_payoff else 10)
        + (15 if 18 <= duration <= 90 else 10 if duration <= 180 else 5)
        + (10 if validation["valid"] else 4)
    )
    hook_type = classify_hook_type(text, has_question)
    return virality, completion, hook_type


def classify_hook_type(text: str, has_question: bool = False) -> str:
    lowered = (text or "").lower()
    if has_question:
        return "question"
    if any(word in lowered for word in ("mistake", "problem", "fix", "solution", "wrong")):
        return "problem_solution"
    if any(word in lowered for word in ("proof", "result", "before", "after", "earnings")):
        return "result_proof"
    if any(word in lowered for word in ("shocking", "angry", "surprise", "love", "hate")):
        return "emotional"
    if any(word in lowered for word in ("tip", "step", "how to", "use this")):
        return "useful_tip"
    return "story"


def sentence_groups(segments: list[dict]) -> list[dict]:
    groups = []
    current = None
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
        if current is None:
            current = {"start": start, "end": end, "texts": [text]}
        else:
            gap = start - current["end"]
            current_duration = current["end"] - current["start"]
            if gap > 2.5 or current_duration > 18:
                groups.append(_finish_group(current))
                current = {"start": start, "end": end, "texts": [text]}
            else:
                current["end"] = end
                current["texts"].append(text)
        if current and ends_cleanly(text) and current["end"] - current["start"] >= 4:
            groups.append(_finish_group(current))
            current = None
    if current:
        groups.append(_finish_group(current))
    return groups


def _finish_group(current: dict) -> dict:
    return {
        "start": current["start"],
        "end": current["end"],
        "text": " ".join(current["texts"]).strip(),
    }


def build_director_candidates(
    segments: list[dict],
    duration: float,
    preset: dict | None = None,
    limit: int = 24,
) -> list[dict]:
    config = normalize_director_preset(preset)
    groups = sentence_groups(segments)
    source_is_short = duration <= 90
    min_duration = min(config["min_clip_duration"], duration) if source_is_short else config["min_clip_duration"]
    hard_max = min(config["hard_max_clip_duration"], duration)
    candidates = []

    for start_idx in range(len(groups)):
        text_parts = []
        start = float(groups[start_idx]["start"])
        last_end = start
        for end_idx in range(start_idx, len(groups)):
            group = groups[end_idx]
            end = float(group["end"])
            if end - start > hard_max:
                break
            text_parts.append(group["text"])
            last_end = end
            candidate_text = " ".join(text_parts).strip()
            candidate_duration = last_end - start
            if candidate_duration < min_duration:
                continue
            if not source_is_short and len(words(candidate_text)) < 25:
                continue
            clip = {
                "start": start,
                "end": last_end,
                "duration": candidate_duration,
                "text": candidate_text,
            }
            validation = validate_clip_structure(clip, segments, config, source_duration=duration)
            if not validation["valid"] and "hard_max_duration" in validation["issues"]:
                continue
            virality, completion, hook_type = candidate_scores(candidate_text, candidate_duration, segments)
            penalty = max(0, 80 - validation["score"])
            if candidate_duration > config["preferred_max_clip_duration"]:
                penalty += min(18, int((candidate_duration - config["preferred_max_clip_duration"]) / 10))
            virality = clamp_score(virality - penalty)
            completion = clamp_score(completion - max(0, penalty - 5))
            enriched = enrich_clip_metadata({
                **clip,
                "pre_score": clamp_score((virality * 0.65) + (completion * 0.35)),
                "virality_score": virality,
                "completion_score": completion,
                "hook_type": hook_type,
                "selection_reason": _selection_reason(validation, hook_type),
            })
            candidates.append(enriched)

    candidates.sort(key=lambda item: item["pre_score"], reverse=True)
    return dedupe_clips(candidates, limit=limit, overlap_threshold=0.65)


def build_director_prompt(candidates: list[dict], target_count: int = 5) -> str:
    payload = [
        {
            "candidate_id": int(candidate.get("candidate_id") or idx),
            "start": round(float(candidate.get("start", 0)), 2),
            "end": round(float(candidate.get("end", 0)), 2),
            "duration": round(float(candidate.get("duration", 0)), 2),
            "text": str(candidate.get("text", ""))[:1800],
            "pre_score": int(candidate.get("pre_score", 0) or 0),
            "completion_score": int(candidate.get("completion_score", 0) or 0),
        }
        for idx, candidate in enumerate(candidates[:20], start=1)
    ]
    return (
        "You are a Shorts Director and retention editor. Pick clips using the structure "
        "Hook -> Context -> Value -> Payoff, not generic summary ranking.\n"
        "Judge the first 2 seconds hook strength, standalone clarity, one main idea, payoff or ending, "
        "emotional/useful/controversial value, boring setup removal, and title/description quality.\n"
        "Prefer 18-90 seconds. Allow 90-180 seconds only for deep value or story payoff. Do not pick "
        "a weak intro, filler intro, or a clip with no ending.\n"
        "Return strict JSON only, exactly shaped like this: "
        "{\"clips\":[{\"candidate_id\":1,\"start\":12.4,\"end\":67.8,\"hook\":\"...\","
        "\"context\":\"...\",\"value\":\"...\",\"payoff\":\"...\",\"title\":\"...\","
        "\"description\":\"...\",\"upload_title\":\"...\",\"upload_description\":\"...\","
        "\"hook_type\":\"problem_solution\",\"virality_score\":91,\"completion_score\":88,"
        "\"reason\":\"...\"}]}.\n"
        f"Return at most {target_count} clips. Keep upload_title under 60 characters. "
        "Upload descriptions should be 1-2 sentences with 3-4 relevant hashtags.\n\n"
        f"Candidates:\n{json.dumps(payload)}"
    )


def merge_llm_rankings(candidates: list[dict], llm_items: list, target_count: int, preset: dict | None = None) -> list[dict]:
    by_id = {
        int(candidate.get("candidate_id") or idx): candidate
        for idx, candidate in enumerate(candidates[:20], start=1)
    }
    clips = []
    config = normalize_director_preset(preset)
    for item in llm_items or []:
        try:
            candidate = by_id.get(int(item.get("candidate_id")))
            if not candidate:
                continue
            start = max(float(candidate["start"]), float(item.get("start", candidate["start"])))
            end = min(float(candidate["end"]), float(item.get("end", candidate["end"])))
            if end - start < 12:
                start, end = float(candidate["start"]), float(candidate["end"])
            clip = enrich_clip_metadata({
                **candidate,
                "start": start,
                "end": end,
                "duration": end - start,
                "hook": str(item.get("hook") or candidate.get("hook") or "")[:220],
                "context": str(item.get("context") or candidate.get("context") or "")[:300],
                "value": str(item.get("value") or candidate.get("value") or "")[:400],
                "payoff": str(item.get("payoff") or candidate.get("payoff") or "")[:260],
                "title": str(item.get("title") or candidate.get("title") or "")[:120],
                "description": str(item.get("description") or candidate.get("description") or "")[:500],
                "upload_title": _clean_upload_title(item.get("upload_title") or item.get("title") or candidate.get("upload_title")),
                "upload_description": str(item.get("upload_description") or candidate.get("upload_description") or "")[:700],
                "virality_score": clamp_score(float(item.get("virality_score", candidate.get("virality_score", 60)))),
                "completion_score": clamp_score(float(item.get("completion_score", candidate.get("completion_score", 70)))),
                "hook_type": _clean_hook_type(item.get("hook_type") or candidate.get("hook_type")),
                "selection_reason": str(item.get("reason") or candidate.get("selection_reason") or "")[:500],
                "reason": str(item.get("reason") or candidate.get("selection_reason") or "")[:500],
            })
            validation = validate_clip_structure(clip, [], config)
            if "hard_max_duration" in validation["issues"]:
                continue
            if not validation["valid"]:
                clip["virality_score"] = clamp_score(clip["virality_score"] - 12)
                clip["completion_score"] = clamp_score(clip["completion_score"] - 10)
            clips.append(clip)
        except Exception:
            continue
    return rank_candidates_fallback(clips or candidates, target_count=target_count)


def rank_candidates_fallback(candidates: list[dict], target_count: int = 5) -> list[dict]:
    ranked = []
    for idx, candidate in enumerate(candidates, start=1):
        clip = enrich_clip_metadata({
            **candidate,
            "title": candidate.get("title") or f"Highlight {idx}",
            "virality_score": clamp_score(float(candidate.get("virality_score") or candidate.get("pre_score") or 60)),
            "completion_score": clamp_score(float(candidate.get("completion_score") or 70)),
            "hook_type": _clean_hook_type(candidate.get("hook_type") or "story"),
            "selection_reason": candidate.get("selection_reason") or candidate.get("reason") or (
                "Selected for a clear idea, useful context, and strong pacing."
            ),
        })
        clip["reason"] = clip["selection_reason"]
        ranked.append(clip)
    ranked.sort(
        key=lambda item: (item["virality_score"] * 0.65 + item["completion_score"] * 0.35),
        reverse=True,
    )
    return dedupe_clips(ranked, limit=target_count, overlap_threshold=0.45)


def finalize_director_clips(
    clips: list[dict],
    segments: list[dict],
    duration: float,
    preset: dict | None = None,
    target_count: int = 5,
) -> list[dict]:
    config = normalize_director_preset(preset)
    finalized = []
    for clip in clips:
        snapped = snap_clip_to_segment_boundaries(clip, segments)
        trimmed = trim_silence_from_edges(snapped, segments)
        validation = validate_clip_structure(trimmed, segments, config, source_duration=duration)
        if "hard_max_duration" in validation["issues"]:
            continue
        enriched = enrich_clip_metadata({
            **trimmed,
            "virality_score": clamp_score(float(trimmed.get("virality_score", 60)) - max(0, 80 - validation["score"])),
            "completion_score": clamp_score(float(trimmed.get("completion_score", 70)) - max(0, 75 - validation["score"])),
            "selection_reason": trimmed.get("selection_reason") or trimmed.get("reason") or _selection_reason(validation, trimmed.get("hook_type")),
        })
        enriched["reason"] = enriched["selection_reason"]
        finalized.append(enriched)
    return dedupe_clips(finalized, limit=target_count, overlap_threshold=0.45)


def select_director_clips(
    segments: list[dict],
    duration: float,
    ai_model: str,
    preset: dict | None = None,
) -> list[dict]:
    if not segments:
        return []
    config = normalize_director_preset(preset)
    if duration <= 90:
        return complete_short_clip(segments, duration)
    target_count = target_clip_count_for_duration(duration)
    candidates = build_director_candidates(
        segments,
        duration=duration,
        preset=config,
        limit=max(20, target_count * 5),
    )
    ranked = rank_candidates_fallback(candidates, target_count=target_count)
    return finalize_director_clips(ranked, segments, duration, config, target_count)


def target_clip_count_for_duration(duration: float) -> int:
    if duration <= 90:
        return 1
    if duration <= 180:
        return 2
    if duration <= 360:
        return 3
    return 6


def complete_short_clip(segments: list[dict], duration: float) -> list[dict]:
    text = " ".join(
        str(seg.get("text", "")).strip()
        for seg in segments
        if str(seg.get("text", "")).strip()
    )
    virality, completion, _ = candidate_scores(text, duration) if text else (65, 90, "complete_short")
    return [enrich_clip_metadata({
        "start": 0,
        "end": duration,
        "duration": duration,
        "text": text,
        "title": "Complete short",
        "virality_score": virality,
        "completion_score": max(90, completion),
        "hook_type": "complete_short",
        "selection_reason": "The source video is already short, so it is kept as one complete clip instead of being split.",
        "reason": "The source video is already short, so it is kept as one complete clip instead of being split.",
    })]


def fallback_clips(duration: float, count: int = 5) -> list[dict]:
    if duration <= 90:
        return [enrich_clip_metadata({
            "start": 0,
            "end": duration,
            "duration": duration,
            "text": "",
            "title": "Complete short",
            "virality_score": 50,
            "completion_score": 90,
            "hook_type": "complete_short",
            "selection_reason": "The source video is already short, so fallback kept it as one complete clip.",
        })]
    clip_duration = 35.0
    skip_start = 60.0 if duration > 180 else 0.0
    usable = duration - skip_start - 30
    count = min(count, target_clip_count_for_duration(duration))
    if usable < clip_duration:
        count = max(1, int(usable // clip_duration)) or 1
    step = usable / count
    return [
        enrich_clip_metadata({
            "start": skip_start + i * step,
            "end": min(skip_start + i * step + clip_duration, duration - 5),
            "duration": min(clip_duration, duration - 5 - (skip_start + i * step)),
            "text": "",
            "title": f"Fallback clip {i + 1}",
            "virality_score": 50,
            "completion_score": 50,
            "hook_type": "fallback",
            "selection_reason": "Generated from fallback spacing because transcript ranking was unavailable.",
        })
        for i in range(count)
    ]


def snap_clip_to_segment_boundaries(clip: dict, segments: list[dict]) -> dict:
    overlapping = _segments_for_clip(clip, segments)
    if not overlapping:
        return {**clip, "duration": float(clip["end"]) - float(clip["start"])}
    snapped = {
        **clip,
        "start": float(overlapping[0].get("start", clip["start"])),
        "end": float(overlapping[-1].get("end", clip["end"])) + 0.25,
    }
    snapped["duration"] = snapped["end"] - snapped["start"]
    return snapped


def trim_silence_from_edges(clip: dict, segments: list[dict]) -> dict:
    overlapping = _segments_for_clip(clip, segments)
    if not overlapping:
        return {**clip, "duration": float(clip["end"]) - float(clip["start"])}
    trimmed = {
        **clip,
        "start": float(overlapping[0].get("start", clip["start"])),
        "end": float(overlapping[-1].get("end", clip["end"])) + 0.25,
        "text": " ".join(str(seg.get("text", "")).strip() for seg in overlapping if str(seg.get("text", "")).strip()) or clip.get("text", ""),
    }
    trimmed["duration"] = trimmed["end"] - trimmed["start"]
    return trimmed


def validate_clip_structure(
    clip: dict,
    segments: list[dict],
    preset: dict | None = None,
    source_duration: float | None = None,
) -> dict:
    config = normalize_director_preset(preset)
    duration = float(clip.get("duration") or (float(clip.get("end", 0)) - float(clip.get("start", 0))))
    text = str(clip.get("text", ""))
    text_words = words(text)
    source_is_short = bool(source_duration and source_duration <= 90)
    issues = []
    score = 100

    if duration > config["hard_max_clip_duration"]:
        issues.append("hard_max_duration")
        score -= 45
    if not source_is_short and duration < config["min_clip_duration"]:
        issues.append("too_short")
        score -= 25
    if not starts_cleanly(text):
        issues.append("filler_intro")
        score -= 25
    if not _has_payoff(text):
        issues.append("missing_payoff")
        score -= 20
    if not source_is_short and len(text_words) < 25:
        issues.append("too_few_useful_words")
        score -= 18

    gaps = _internal_gaps(clip, segments)
    if any(gap > 2.5 for gap in gaps):
        issues.append("long_silence")
        score -= min(20, int(max(gaps) * 3))

    valid_blockers = {"hard_max_duration", "filler_intro", "missing_payoff"}
    valid = not any(issue in valid_blockers for issue in issues)
    if not source_is_short and ("too_short" in issues or "too_few_useful_words" in issues):
        valid = False
    return {"valid": valid, "score": clamp_score(score), "issues": issues}


def enrich_clip_metadata(clip: dict) -> dict:
    text = str(clip.get("text", "")).strip()
    pieces = split_sentences(text)
    hook = str(clip.get("hook") or (pieces[0] if pieces else text[:160])).strip()
    payoff = str(clip.get("payoff") or (pieces[-1] if len(pieces) > 1 else hook)).strip()
    context = str(clip.get("context") or (pieces[1] if len(pieces) > 2 else hook)).strip()
    middle = pieces[2:-1] if len(pieces) > 3 else pieces[1:-1]
    value = str(clip.get("value") or (" ".join(middle).strip() if middle else payoff)).strip()
    title = _clean_title(clip.get("title") or hook or "Strong Short Moment")
    upload_title = _clean_upload_title(clip.get("upload_title") or title)
    description = str(clip.get("description") or _description_from_parts(hook, value, payoff)).strip()[:500]
    upload_description = str(
        clip.get("upload_description") or _upload_description(description, clip.get("hook_type"), text)
    ).strip()[:700]
    selection_reason = str(
        clip.get("selection_reason") or clip.get("reason") or "Selected for a clear Hook, Context, Value, and Payoff structure."
    ).strip()[:500]
    start = float(clip.get("start", 0))
    end = float(clip.get("end", start))
    enriched = {
        **clip,
        "start": start,
        "end": end,
        "duration": float(clip.get("duration") or max(0, end - start)),
        "text": text,
        "title": title,
        "description": description,
        "upload_title": upload_title,
        "upload_description": upload_description,
        "hook": hook[:220],
        "context": context[:300],
        "value": value[:400],
        "payoff": payoff[:260],
        "hook_type": _clean_hook_type(clip.get("hook_type") or classify_hook_type(text)),
        "virality_score": clamp_score(float(clip.get("virality_score") or 60)),
        "completion_score": clamp_score(float(clip.get("completion_score") or 70)),
        "selection_reason": selection_reason,
        "reason": selection_reason,
    }
    return enriched


def dedupe_clips(clips: list[dict], limit: int, overlap_threshold: float = 0.5) -> list[dict]:
    selected = []
    for clip in clips:
        if any(overlap_ratio(clip, kept) >= overlap_threshold for kept in selected):
            continue
        selected.append(clip)
        if len(selected) >= limit:
            break
    return selected


def overlap_ratio(a: dict, b: dict) -> float:
    start = max(float(a["start"]), float(b["start"]))
    end = min(float(a["end"]), float(b["end"]))
    if end <= start:
        return 0.0
    overlap = end - start
    shortest = min(float(a["end"]) - float(a["start"]), float(b["end"]) - float(b["start"]))
    return overlap / shortest if shortest > 0 else 0.0


def _segments_for_clip(clip: dict, segments: list[dict]) -> list[dict]:
    start = float(clip.get("start", 0))
    end = float(clip.get("end", start))
    return [
        seg for seg in segments
        if float(seg.get("end", seg.get("start", 0))) > start
        and float(seg.get("start", 0)) < end
    ]


def _internal_gaps(clip: dict, segments: list[dict]) -> list[float]:
    overlapping = _segments_for_clip(clip, segments)
    gaps = []
    for left, right in zip(overlapping, overlapping[1:]):
        gap = float(right.get("start", 0)) - float(left.get("end", 0))
        if gap > 0:
            gaps.append(gap)
    return gaps


def _has_payoff(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in PAYOFF_WORDS) or ends_cleanly(text)


def _selection_reason(validation: dict, hook_type: str | None) -> str:
    if validation["valid"]:
        return f"Selected as a {hook_type or 'story'} clip with a clean hook, useful context, and payoff."
    return f"Selected with penalties for: {', '.join(validation['issues'])}."


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip(" -:")
    if not title:
        title = "Strong Short Moment"
    return title[:120]


def _clean_upload_title(title: str) -> str:
    title = _clean_title(title)
    generic = {"highlight", "highlight 1", "untitled highlight", "fallback clip"}
    if title.lower() in generic:
        title = "Watch This Before You Make This Mistake"
    if len(title) > 60:
        title = title[:57].rstrip(" -:,.") + "..."
    return title


def _clean_hook_type(hook_type: str | None) -> str:
    hook_type = str(hook_type or "story").strip().lower().replace(" ", "_")
    return hook_type if hook_type in HOOK_TYPES else "story"


def _description_from_parts(hook: str, value: str, payoff: str) -> str:
    parts = [part for part in (hook, value, payoff) if part]
    if not parts:
        return "A focused Short with one clear idea and payoff."
    description = " ".join(parts[:2])
    return description[:500]


def _upload_description(description: str, hook_type: str | None, text: str) -> str:
    tags = ["#shorts"]
    lowered = (text or "").lower()
    if any(word in lowered for word in ("edit", "short", "video", "creator", "youtube")):
        tags.extend(["#editing", "#creator"])
    if any(word in lowered for word in ("mistake", "problem", "fix")):
        tags.extend(["#tips", "#growth"])
    if _clean_hook_type(hook_type) == "story":
        tags.append("#story")
    while len(tags) < 4:
        tags.append("#learn")
    unique_tags = []
    for tag in tags:
        if tag not in unique_tags:
            unique_tags.append(tag)
    return f"{description.strip()}\n\n{' '.join(unique_tags[:4])}".strip()
