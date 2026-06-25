import json
import re


GENERIC_TITLE_WORDS = {
    "india", "got", "latent", "season", "episode", "ep", "ft", "feat",
    "with", "the", "and", "show", "official", "comedy",
}

REACTION_TERMS = {
    "laugh", "laughing", "laughter", "applause", "audience", "crowd",
    "judge", "judges", "react", "reaction", "punchline", "roast",
    "joke", "funny", "taali", "hassi", "hasna",
}


def _clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 .'-]", " ", value or "")).strip()


def _entity_aliases(name: str) -> list[str]:
    pieces = [piece.lower() for piece in re.findall(r"[A-Za-z0-9']+", name)]
    aliases = {name.lower()}
    aliases.update(piece for piece in pieces if len(piece) > 2)
    return sorted(aliases)


def _title_entities(title: str) -> list[dict]:
    entities = []
    seen = set()
    parts = re.split(r"\b(?:ft|feat|with)\.?\b|,|\||-", title or "", flags=re.IGNORECASE)
    for part in parts:
        part = _clean_name(part)
        words = re.findall(r"[A-Z][a-z]+|[A-Z]{2,}(?=\s|$)", part)
        phrase = _clean_name(" ".join(words))
        if not phrase:
            continue
        lower_words = {word.lower() for word in phrase.split()}
        if lower_words and lower_words.issubset(GENERIC_TITLE_WORDS):
            continue
        if len(phrase.split()) > 5:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        entities.append({
            "name": phrase,
            "role": "episode participant",
            "aliases": _entity_aliases(phrase),
        })
    return entities[:12]


def normalize_episode_profile(data: dict | None, title: str = "", segments: list[dict] | None = None, genre_hint: str = "") -> dict:
    fallback = build_fallback_episode_profile(title, segments or [], genre_hint=genre_hint)
    if not isinstance(data, dict):
        return fallback
    entities = []
    seen = set()
    for item in data.get("important_entities", []):
        if not isinstance(item, dict):
            continue
        name = _clean_name(str(item.get("name", "")))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
        aliases = sorted({str(alias).strip().lower() for alias in aliases if str(alias).strip()} | set(_entity_aliases(name)))
        entities.append({
            "name": name[:80],
            "role": str(item.get("role") or "episode participant")[:80],
            "aliases": aliases[:8],
        })
    patterns = [
        str(pattern).strip()[:100]
        for pattern in data.get("viral_moment_patterns", [])
        if str(pattern).strip()
    ][:12]
    guidance = [
        str(item).strip()[:120]
        for item in data.get("ranking_guidance", [])
        if str(item).strip()
    ][:12]
    return {
        "show_type": str(data.get("show_type") or fallback["show_type"])[:80],
        "important_entities": entities or fallback["important_entities"],
        "viral_moment_patterns": patterns or fallback["viral_moment_patterns"],
        "ranking_guidance": guidance or fallback["ranking_guidance"],
        "source": str(data.get("source") or "llm")[:40],
    }


def build_fallback_episode_profile(
    title: str,
    segments: list[dict],
    *,
    description: str = "",
    genre_hint: str = "",
) -> dict:
    text = f"{title} {description} {genre_hint}".lower()
    show_type = "comedy panel show" if any(term in text for term in ("latent", "comedy", "funny", "stage", "judge")) else "auto"
    patterns = [
        "setup punchline and audience reaction",
        "joke or roast involving an important episode participant",
        "judge or host reaction after a punchline",
    ] if show_type == "comedy panel show" else ["clear hook payoff and reaction"]
    return {
        "show_type": show_type,
        "important_entities": _title_entities(title),
        "viral_moment_patterns": patterns,
        "ranking_guidance": [
            "Prefer complete setup, punchline, and reaction over name-only mentions.",
            "Boost important participant moments only when the clip also has joke, payoff, or reaction signals.",
        ],
        "source": "fallback",
    }


def parse_episode_profile_json(text: str, title: str = "", segments: list[dict] | None = None, genre_hint: str = "") -> dict:
    if not text:
        return build_fallback_episode_profile(title, segments or [], genre_hint=genre_hint)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return build_fallback_episode_profile(title, segments or [], genre_hint=genre_hint)
        data = json.loads(match.group(0))
    return normalize_episode_profile(data, title=title, segments=segments or [], genre_hint=genre_hint)


def build_episode_profile_prompt(seed: dict) -> str:
    payload = {
        "title": seed.get("title", ""),
        "description": seed.get("description", ""),
        "genre_hint": seed.get("genre_hint", ""),
        "detected_genre": seed.get("detected_genre", ""),
        "transcript_samples": seed.get("transcript_samples", []),
    }
    return (
        "Analyze this source video for dynamic viral-clip context. Do not hardcode assumptions. "
        "Infer important people, roles, and viral moment patterns only from the title, description, and transcript samples. "
        "For comedy/panel/stage shows, focus on guests, hosts, judges, roasts, punchlines, and audience reactions. "
        "Return strict JSON with keys: show_type, important_entities, viral_moment_patterns, ranking_guidance. "
        "important_entities must be objects with name, role, aliases.\n\n"
        f"Video context:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def transcript_samples(segments: list[dict], duration: float, limit: int = 36) -> list[dict]:
    if not segments:
        return []
    if len(segments) <= limit:
        return segments
    sample_indexes = set()
    thirds = [0, len(segments) // 3, (len(segments) * 2) // 3]
    per_band = max(3, limit // len(thirds))
    for start in thirds:
        for idx in range(start, min(len(segments), start + per_band)):
            sample_indexes.add(idx)
    return [segments[idx] for idx in sorted(sample_indexes)[:limit]]


def context_hits_for_text(text: str, profile: dict | None) -> list[str]:
    lowered = (text or "").lower()
    if not lowered or not isinstance(profile, dict):
        return []
    hits = []
    for entity in profile.get("important_entities", []):
        name = str(entity.get("name", "")).strip()
        aliases = entity.get("aliases") if isinstance(entity.get("aliases"), list) else []
        terms = [name.lower(), *[str(alias).lower() for alias in aliases]]
        if name and any(term and re.search(rf"\b{re.escape(term)}\b", lowered) for term in terms):
            hits.append(name)
    for pattern in profile.get("viral_moment_patterns", []):
        pattern_words = [
            word for word in re.findall(r"[a-z0-9']+", str(pattern).lower())
            if len(word) > 3 and word not in {"with", "after", "moment"}
        ]
        if pattern_words and sum(1 for word in pattern_words if word in lowered) >= min(2, len(pattern_words)):
            hits.append(str(pattern)[:80])
    return sorted(set(hits))


def episode_context_score(text: str, profile: dict | None, source: str = "") -> tuple[float, list[str]]:
    hits = context_hits_for_text(text, profile)
    if not hits:
        return 0.0, []
    lowered = (text or "").lower()
    reaction_hits = sum(1 for term in REACTION_TERMS if re.search(rf"\b{re.escape(term)}\b", lowered))
    entity_score = 0.30 if hits else 0.0
    reaction_score = min(0.45, reaction_hits * 0.09)
    source_score = 0.15 if source == "episode_context" else 0.0
    return min(1.0, entity_score + reaction_score + source_score), hits
