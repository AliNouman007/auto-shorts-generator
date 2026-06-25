import copy
from pathlib import Path
from typing import Iterable

import httpx


DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_word(raw_word: dict) -> dict | None:
    if not isinstance(raw_word, dict):
        return None
    text = str(raw_word.get("punctuated_word") or raw_word.get("word") or raw_word.get("text") or "").strip()
    if not text:
        return None
    start = _coerce_float(raw_word.get("start"))
    end = max(start, _coerce_float(raw_word.get("end"), start))
    normalized = {
        "start": start,
        "end": end,
        "word": text,
        "confidence": _coerce_float(raw_word.get("confidence")),
    }
    if raw_word.get("language"):
        normalized["language"] = str(raw_word.get("language"))
    if raw_word.get("speaker") is not None:
        normalized["speaker"] = raw_word.get("speaker")
    return normalized


def _primary_alternative(payload: dict) -> dict:
    channels = payload.get("results", {}).get("channels") if isinstance(payload, dict) else []
    if not channels:
        return {}
    alternatives = channels[0].get("alternatives") if isinstance(channels[0], dict) else []
    if not alternatives:
        return {}
    return alternatives[0] if isinstance(alternatives[0], dict) else {}


def _segments_from_words(words: list[dict]) -> list[dict]:
    segments = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            duration = float(current[-1]["end"]) - float(current[0]["start"])
            if gap > 1.0 or duration >= 12.0 or len(current) >= 18:
                text = " ".join(item["word"] for item in current).strip()
                if text:
                    segments.append({
                        "start": float(current[0]["start"]),
                        "end": float(current[-1]["end"]),
                        "text": text,
                        "words": current,
                    })
                current = []
        current.append(word)
    if current:
        text = " ".join(item["word"] for item in current).strip()
        if text:
            segments.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": text,
                "words": current,
            })
    return segments


def _language_label(alternative: dict, words: list[dict]) -> str:
    languages = alternative.get("languages") if isinstance(alternative.get("languages"), list) else []
    if not languages:
        languages = []
        for word in words:
            language = word.get("language")
            if language and language not in languages:
                languages.append(language)
    return ",".join(str(language) for language in languages)


def normalize_deepgram_response(payload: dict, model: str = "nova-3") -> dict:
    alternative = _primary_alternative(payload)
    utterances = payload.get("results", {}).get("utterances") if isinstance(payload, dict) else []
    segments = []
    all_words = []

    if isinstance(utterances, list) and utterances:
        for utterance in utterances:
            if not isinstance(utterance, dict):
                continue
            words = [
                word for word in (_normalize_word(raw_word) for raw_word in utterance.get("words") or [])
                if word
            ]
            text = str(utterance.get("transcript") or " ".join(word["word"] for word in words)).strip()
            if not text:
                continue
            segment = {
                "start": _coerce_float(utterance.get("start")),
                "end": _coerce_float(utterance.get("end")),
                "text": text,
                "confidence": _coerce_float(utterance.get("confidence")),
            }
            if utterance.get("speaker") is not None:
                segment["speaker"] = utterance.get("speaker")
            if words:
                segment["words"] = words
                all_words.extend(words)
            segments.append(segment)

    if not segments:
        all_words = [
            word for word in (_normalize_word(raw_word) for raw_word in alternative.get("words") or [])
            if word
        ]
        segments = _segments_from_words(all_words)

    if not all_words:
        for segment in segments:
            all_words.extend(segment.get("words") or [])

    confidence = _coerce_float(alternative.get("confidence"))
    if not confidence and all_words:
        confidence = sum(float(word.get("confidence", 0)) for word in all_words) / len(all_words)

    return {
        "provider": "deepgram",
        "model": model,
        "language": _language_label(alternative, all_words),
        "confidence": confidence,
        "segments": segments,
        "words": all_words,
        "status": "word_synced" if all_words else "segment_only",
        "raw": payload,
    }


def _offset_item(item: dict, offset: float) -> dict:
    shifted = copy.deepcopy(item)
    shifted["start"] = _coerce_float(shifted.get("start")) + offset
    shifted["end"] = _coerce_float(shifted.get("end"), shifted["start"]) + offset
    if isinstance(shifted.get("words"), list):
        shifted["words"] = [_offset_item(word, offset) for word in shifted["words"]]
    return shifted


def merge_chunk_results(results_with_offsets: Iterable[tuple[dict, float]]) -> dict:
    merged_segments = []
    merged_words = []
    languages = []
    confidences = []
    provider = "deepgram"
    model = "nova-3"

    for result, offset in results_with_offsets:
        provider = result.get("provider") or provider
        model = result.get("model") or model
        confidence = result.get("confidence")
        if confidence is not None:
            confidences.append(_coerce_float(confidence))
        for language in str(result.get("language") or "").split(","):
            language = language.strip()
            if language and language not in languages:
                languages.append(language)
        for segment in result.get("segments") or []:
            merged_segments.append(_offset_item(segment, float(offset)))
        for word in result.get("words") or []:
            merged_words.append(_offset_item(word, float(offset)))

    merged_segments.sort(key=lambda segment: float(segment.get("start", 0)))
    merged_words.sort(key=lambda word: float(word.get("start", 0)))
    return {
        "provider": provider,
        "model": model,
        "language": ",".join(languages),
        "confidence": sum(confidences) / len(confidences) if confidences else 0.0,
        "segments": merged_segments,
        "words": merged_words,
        "status": "word_synced" if merged_words else "segment_only",
    }


async def transcribe_deepgram_file(
    audio_path: str,
    api_key: str,
    model: str = "nova-3",
    language: str = "multi",
    timeout: int = 240,
) -> dict:
    params = {
        "model": model,
        "language": language,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "diarize": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/mpeg",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        with open(audio_path, "rb") as handle:
            response = await client.post(
                DEEPGRAM_LISTEN_URL,
                headers=headers,
                params=params,
                content=handle.read(),
            )
    if response.status_code != 200:
        raise RuntimeError(f"Deepgram API error ({response.status_code}): {response.text[:500]}")
    return normalize_deepgram_response(response.json(), model=model)

