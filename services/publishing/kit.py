import re
import unicodedata
from pathlib import Path


STOPWORDS = {
    "about", "after", "again", "all", "alright", "and", "are", "but", "can",
    "clip", "coming", "course", "does", "for", "from", "funny", "gets", "got",
    "has", "have", "honestly", "into", "just", "moment", "more", "much",
    "not", "our", "say", "short", "shorts", "something", "thank", "thanks",
    "that", "the", "this", "time", "video", "want", "was", "what", "when",
    "where", "with", "you", "your",
}

GENERIC_TITLES = {
    "funny clip", "selected clip", "highlight", "untitled short", "complete short",
    "source video", "studio workflow",
}
STAGE_COMEDY_WORDS = {
    "audience", "comedy", "crowd", "latent", "laugh", "reaction", "roast",
    "show", "solanki", "stage", "standup", "talent", "punchline",
}


def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def english_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return compact_spaces(ascii_value)


def strip_hashtags(value: str) -> str:
    return compact_spaces(re.sub(r"\s*#[A-Za-z0-9_]+\b", " ", value or ""))


def extract_hashtags(value: str) -> list[str]:
    tags = []
    seen = set()
    for raw in re.findall(r"#([A-Za-z0-9_]{2,30})", value or ""):
        tag = "#" + raw.lower()
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def words(value: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_]{2,24}", (value or "").lower())


def has_non_english_script(value: str) -> bool:
    return bool(re.search(r"[^\x00-\x7F]", value or ""))


def is_generic_title(value: str) -> bool:
    return compact_spaces(value).lower() in GENERIC_TITLES


def has_stage_comedy_context(*values: str) -> bool:
    blob = " ".join(values).lower()
    tokens = set(words(blob))
    return bool(tokens & STAGE_COMEDY_WORDS)


def looks_like_transcript_text(value: str) -> bool:
    clean = compact_spaces(strip_hashtags(value)).lower()
    if not clean:
        return False
    if has_non_english_script(clean):
        return True
    text_words = words(clean)
    if len(text_words) >= 11 and not re.search(r"[.!?]", clean):
        return True
    marker_hits = sum(1 for word in text_words if word in {
        "haan", "han", "matlab", "bol", "bolta", "bolti", "raha", "rahi",
        "hoon", "hun", "hai", "tha", "thi", "phir", "fir", "audience",
        "laugh", "hassi", "hans", "umm", "uh", "yeah", "okay",
    })
    return marker_hits >= 3


def dynamic_title(short: dict) -> str:
    title = compact_spaces(
        short.get("upload_title")
        or short.get("title")
        or Path(short.get("filename") or "").stem.replace("_", " ")
    )
    if is_generic_title(title):
        source = english_text(short.get("source_title") or "")
        if source and not is_generic_title(source):
            title = source
    return title[:100].strip()


def dynamic_description(short: dict, title: str) -> tuple[str, list[str]]:
    notes = []
    raw = compact_spaces(short.get("upload_description") or short.get("description") or "")
    candidate = strip_hashtags(raw)
    if candidate and not looks_like_transcript_text(candidate):
        english_candidate = english_text(candidate)
        if english_candidate and len(words(english_candidate)) >= 3:
            return english_candidate[:180].strip(), notes
    if candidate:
        notes.append("description_was_transcript_like")
    source = compact_spaces(short.get("source_title") or "")
    reason = compact_spaces(short.get("selection_reason") or "")
    hook_type = compact_spaces(str(short.get("hook_type") or "")).replace("_", " ")
    context = " ".join([raw, source, reason, title, hook_type])
    source_english = english_text(source)
    if has_stage_comedy_context(context):
        if source_english and not is_generic_title(source_english):
            base = f"A stage comedy moment from {source_english} with a live audience reaction."
        elif title and not is_generic_title(title):
            base = f"A stage comedy moment built around {title} and the audience reaction."
        else:
            base = "A stage comedy moment with a live audience reaction."
        return base[:180].strip(), notes
    if reason and not looks_like_transcript_text(reason):
        base = english_text(reason)
    elif source_english:
        base = f"{title} from {source_english}"
    elif hook_type:
        base = f"{title} with a {hook_type} moment"
    else:
        base = title
    return strip_hashtags(english_text(base))[:180].strip(), notes


def dynamic_hashtags(short: dict, existing: list[str] | None = None, limit: int = 8) -> list[str]:
    tags = []
    seen = set()
    for tag in existing or []:
        normalized = tag.lower()
        if normalized not in seen:
            tags.append(normalized)
            seen.add(normalized)
    raw_title = compact_spaces(short.get("upload_title") or short.get("title") or "")
    title = "" if is_generic_title(raw_title) else dynamic_title(short)
    raw_description = strip_hashtags(short.get("upload_description") or short.get("description") or "")
    if looks_like_transcript_text(raw_description):
        raw_description = " ".join(word for word in words(raw_description) if word in STAGE_COMEDY_WORDS)
    text = " ".join([
        title,
        raw_description,
        short.get("publish_description") or "",
        short.get("source_title") or "",
        short.get("hook_type") or "",
        short.get("candidate_source") or "",
        short.get("selection_reason") or "",
    ])
    for word in words(text):
        if word in STOPWORDS:
            continue
        tag = "#" + word
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
        if len(tags) >= limit:
            break
    return tags


def build_publish_kit(short: dict, platform: str = "tiktok") -> dict:
    title = dynamic_title(short)
    raw_description = compact_spaces(short.get("upload_description") or short.get("description") or "")
    existing = extract_hashtags(raw_description)
    description, quality_notes = dynamic_description(short, title)
    hashtags = dynamic_hashtags({**short, "publish_description": description}, existing=existing)
    hashtags_text = " ".join(hashtags)
    post_parts = [part for part in (title, description, hashtags_text) if part]
    post_text = "\n\n".join(post_parts).strip()
    return {
        "platform": platform,
        "title": title,
        "description": description,
        "post_text": post_text[:2200],
        "hashtags": hashtags,
        "hashtags_text": hashtags_text,
        "copy_all_text": post_text[:2200],
        "quality_notes": quality_notes,
    }
