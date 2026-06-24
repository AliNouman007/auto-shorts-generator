def _find_candidate(candidates: list[dict], item: dict) -> dict | None:
    candidate_id = item.get("candidate_id")
    if candidate_id:
        for candidate in candidates:
            if str(candidate.get("candidate_id")) == str(candidate_id):
                return candidate
    try:
        start = float(item.get("start"))
        end = float(item.get("end"))
    except (TypeError, ValueError):
        return None
    for candidate in candidates:
        if abs(float(candidate.get("start", 0)) - start) <= 1.5 and abs(float(candidate.get("end", 0)) - end) <= 1.5:
            return candidate
    return None


def apply_llm_judgement(
    candidates: list[dict],
    judgement: list[dict],
    *,
    max_adjustment: float = 1.5,
) -> list[dict]:
    accepted = []
    seen = set()
    for item in judgement or []:
        if not isinstance(item, dict):
            continue
        candidate = _find_candidate(candidates, item)
        if not candidate:
            continue
        candidate_id = str(candidate.get("candidate_id"))
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        start = float(candidate.get("start", 0))
        end = float(candidate.get("end", start))
        adjusted_start = start
        adjusted_end = end
        if item.get("start") is not None:
            try:
                requested_start = float(item.get("start"))
            except (TypeError, ValueError):
                requested_start = start
            if abs(requested_start - start) > max_adjustment:
                continue
            adjusted_start = requested_start
        if item.get("end") is not None:
            try:
                requested_end = float(item.get("end"))
            except (TypeError, ValueError):
                requested_end = end
            if abs(requested_end - end) > max_adjustment:
                continue
            adjusted_end = requested_end
        if adjusted_end <= adjusted_start:
            continue
        accepted.append({
            **candidate,
            "start": round(adjusted_start, 3),
            "end": round(adjusted_end, 3),
            "duration": round(adjusted_end - adjusted_start, 3),
            "title": item.get("title") or candidate.get("title") or "Selected Clip",
            "selection_reason": item.get("selection_reason") or item.get("reason") or candidate.get("selection_reason") or "Selected by V2 judge.",
            "judge_status": "accepted",
        })
    return accepted
