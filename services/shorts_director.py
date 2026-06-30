import re


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


def split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [piece.strip() for piece in pieces if piece.strip()]


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
    hook_type = _clean_hook_type(clip.get("hook_type") or "story")
    description = str(clip.get("description") or _description_from_parts(title, hook_type)).strip()[:500]
    upload_description = str(
        clip.get("upload_description") or _upload_description(description, hook_type, text)
    ).strip()[:700]
    selection_reason = str(
        clip.get("selection_reason") or clip.get("reason") or "Selected for quality content"
    ).strip()[:500]
    start = float(clip.get("start", 0))
    end = float(clip.get("end", start))
    return {
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
        "hook_type": hook_type,
        "virality_score": clamp_score(float(clip.get("virality_score", 60)) + (clip.get("audio_peak_energy", 0) * 30)),
        "completion_score": clamp_score(float(clip.get("completion_score", 70))),
        "selection_reason": selection_reason,
        "reason": selection_reason,
    }


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
    shortest = min(float(a["end"]) - float(a["start"]), float(b["end"]) - float(b["start"]))
    return (end - start) / shortest if shortest > 0 else 0.0


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip(" -:")
    return (title or "Strong Short Moment")[:120]


def _clean_upload_title(title: str) -> str:
    title = _clean_title(title)
    if title.lower() in {"highlight", "highlight 1", "untitled highlight", "fallback clip"}:
        title = "You Have to See This!"
    if len(title) > 60:
        title = title[:57].rstrip(" -:,.") + "..."
    return title


def _clean_hook_type(hook_type: str | None) -> str:
    hook_type = str(hook_type or "story").strip().lower().replace(" ", "_")
    return hook_type if hook_type in HOOK_TYPES else "story"


def _description_from_parts(title: str, hook_type: str | None) -> str:
    title = _clean_title(title)
    kind = _clean_hook_type(hook_type)
    if kind == "comedy":
        return (
            f"A funny short highlight: {title}. "
            "Selected for its setup, punchline, and reaction."
        )
    if kind in {"question", "problem_solution", "educational"}:
        return (
            f"A focused short highlight: {title}. "
            "Selected for its clear hook, useful context, and payoff."
        )
    return (
        f"A standout short moment: {title}. "
        "Selected for its clear hook and strong payoff."
    )


def _upload_description(description: str, hook_type: str | None, text: str) -> str:
    tags = ["#shorts"]
    lowered = (text or "").lower()
    if any(word in lowered for word in ("funny", "comedy", "laugh", "joke", "hilarious")):
        tags.extend(["#comedy", "#funny"])
    if any(word in lowered for word in ("talent", "audition", "judge", "stage")):
        tags.extend(["#talent", "#show"])
    if any(word in lowered for word in ("edit", "short", "video", "creator", "youtube")):
        tags.extend(["#editing", "#creator"])
    if _clean_hook_type(hook_type) == "story":
        tags.append("#story")
    while len(tags) < 4:
        tags.append("#viral")
    unique_tags = []
    for tag in tags:
        if tag not in unique_tags:
            unique_tags.append(tag)
    return f"{description.strip()}\n\n{' '.join(unique_tags[:4])}".strip()
