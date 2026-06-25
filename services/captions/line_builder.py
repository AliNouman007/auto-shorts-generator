from .normalization import normalize_caption_text


def _clean_word(raw_word: dict, clip_start: float, clip_end: float, caption_mode: str) -> dict | None:
    try:
        start = float(raw_word.get("start", 0))
        end = float(raw_word.get("end", start))
    except (TypeError, ValueError):
        return None
    if end <= clip_start or start >= clip_end:
        return None
    text = normalize_caption_text(
        str(raw_word.get("punctuated_word") or raw_word.get("word") or raw_word.get("text") or ""),
        caption_mode=caption_mode,
    )
    if not text:
        return None
    try:
        confidence = float(raw_word.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "start": max(0.0, start - clip_start),
        "end": max(0.0, min(clip_end, end) - clip_start),
        "text": text,
        "confidence": confidence,
        "speaker": raw_word.get("speaker"),
    }


def _flush(words: list[dict], cues: list[dict]) -> None:
    if not words:
        return
    text = " ".join(word["text"] for word in words).strip()
    if not text:
        return
    start = float(words[0]["start"])
    end = max(float(words[-1]["end"]), start + 0.35)
    confidences = [float(word.get("confidence", 0)) for word in words if word.get("confidence") is not None]
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    if cues and start < float(cues[-1]["end"]):
        start = float(cues[-1]["end"])
        end = max(end, start + 0.2)
    cues.append({
        "start": round(start, 3),
        "end": round(end, 3),
        "text": text,
        "confidence": round(confidence, 4),
    })


def _words_from_segments(segments: list[dict], clip_start: float, clip_end: float, caption_mode: str) -> list[dict]:
    words = []
    for segment in segments or []:
        for raw_word in segment.get("words") or []:
            if not isinstance(raw_word, dict):
                continue
            word = _clean_word(raw_word, clip_start, clip_end, caption_mode)
            if word:
                words.append(word)
    return sorted(words, key=lambda word: (float(word["start"]), float(word["end"])))


def build_caption_cues(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    caption_mode: str = "hinglish",
    max_words: int = 6,
    max_seconds: float = 2.2,
    pause_seconds: float = 0.9,
) -> list[dict]:
    words = _words_from_segments(segments, clip_start, clip_end, caption_mode)
    if not words:
        return build_caption_cues_from_segments(segments, clip_start, clip_end, caption_mode)

    cues: list[dict] = []
    current: list[dict] = []
    for word in words:
        should_flush = False
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            duration = float(current[-1]["end"]) - float(current[0]["start"])
            speaker_changed = (
                current[-1].get("speaker") is not None
                and word.get("speaker") is not None
                and current[-1].get("speaker") != word.get("speaker")
            )
            should_flush = (
                gap > pause_seconds
                or len(current) >= max_words
                or duration >= max_seconds
                or speaker_changed
            )
        if should_flush:
            _flush(current, cues)
            current = []
        current.append(word)
    _flush(current, cues)
    return cues


def build_caption_cues_from_segments(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    caption_mode: str = "hinglish",
    max_words: int = 6,
    max_seconds: float = 2.2,
) -> list[dict]:
    cues: list[dict] = []
    for segment in segments or []:
        try:
            start = max(float(segment.get("start", 0)), clip_start)
            end = min(float(segment.get("end", start)), clip_end)
        except (TypeError, ValueError):
            continue
        text = normalize_caption_text(str(segment.get("text", "")), caption_mode=caption_mode)
        words = text.split()
        if not words or end <= start:
            continue
        chunk_count = max(1, min(len(words), (len(words) + max_words - 1) // max_words))
        duration_count = max(1, int(((end - start) + max_seconds - 0.001) // max_seconds))
        chunk_count = min(len(words), max(chunk_count, duration_count))
        for index in range(chunk_count):
            word_start = round(index * len(words) / chunk_count)
            word_end = round((index + 1) * len(words) / chunk_count)
            cue_text = " ".join(words[word_start:word_end]).strip()
            if not cue_text:
                continue
            cue_start = start + ((end - start) * index / chunk_count) - clip_start
            cue_end = start + ((end - start) * (index + 1) / chunk_count) - clip_start
            if cues and cue_start < float(cues[-1]["end"]):
                cue_start = float(cues[-1]["end"])
            cues.append({
                "start": round(max(0.0, cue_start), 3),
                "end": round(max(cue_start + 0.2, cue_end), 3),
                "text": cue_text,
                "confidence": 0.0,
            })
    return cues

