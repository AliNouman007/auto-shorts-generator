import json
import re
import time

import httpx

from .schemas import normalize_brain


class ComedyV3ModelError(RuntimeError):
    pass


class ComedyV3JsonError(ComedyV3ModelError):
    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


MAX_RATE_LIMIT_WAIT_SECONDS = 90.0
RATE_LIMIT_RETRIES = 1
GROQ_MAX_OUTPUT_TOKENS = 1024


def _extract_json(text: str) -> dict:
    if not text:
        raise ComedyV3JsonError("Comedy V3 model returned an empty response.", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ComedyV3JsonError("Comedy V3 model returned invalid JSON.", text)
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ComedyV3JsonError("Comedy V3 model returned invalid JSON.", text) from exc
    if not isinstance(data, dict):
        raise ComedyV3JsonError("Comedy V3 model JSON must be an object.", text)
    return data


def _response_message(response) -> str:
    try:
        data = response.json()
    except Exception:
        return str(getattr(response, "text", "") or "").strip()
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error.get("message", "")).strip()
        if data.get("message"):
            return str(data.get("message", "")).strip()
    return str(getattr(response, "text", "") or "").strip()


def _rate_limit_headers(response) -> str:
    headers = getattr(response, "headers", {}) or {}
    names = (
        "retry-after",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    )
    details = []
    for name in names:
        value = headers.get(name) if hasattr(headers, "get") else None
        if value:
            details.append(f"{name}={value}")
    return ", ".join(details)


def _parse_wait_seconds(value) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    multiplier = 1.0
    if raw.endswith("ms"):
        raw = raw[:-2]
        multiplier = 0.001
    elif raw.endswith("s"):
        raw = raw[:-1]
    try:
        seconds = float(raw) * multiplier
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _retry_after_seconds(response) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    values = []
    if hasattr(headers, "get"):
        for name in ("retry-after", "x-ratelimit-reset-tokens"):
            seconds = _parse_wait_seconds(headers.get(name))
            if seconds is not None:
                values.append(seconds)
    if not values:
        return None
    seconds = max(values)
    if seconds > 0:
        seconds += 1.0
    if seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
        return None
    return round(seconds, 3)


def _rate_limit_error(provider: str, model: str, prompt: str, response, switch_to: str) -> ComedyV3ModelError:
    message = _response_message(response)
    parts = [
        f"{provider} Comedy V3 was rate-limited or quota-limited",
        f"HTTP {response.status_code}",
        f"model={model}",
        f"prompt_chars={len(prompt)}",
    ]
    if message:
        parts.append(f"provider_message={message[:300]}")
    header_detail = _rate_limit_headers(response)
    if header_detail:
        parts.append(f"headers={header_detail}")
    parts.append(f"Wait for quota reset, reduce usage, or select {switch_to} as the Comedy V3 main brain.")
    return ComedyV3ModelError(". ".join(parts))


def _post_with_rate_limit_retry(client, *args, **kwargs):
    response = client.post(*args, **kwargs)
    for _ in range(RATE_LIMIT_RETRIES):
        if response.status_code != 429:
            break
        wait_seconds = _retry_after_seconds(response)
        if wait_seconds is None:
            break
        time.sleep(wait_seconds)
        response = client.post(*args, **kwargs)
    return response


def _gemini_json(prompt: str, api_key: str, model: str, timeout: int) -> dict:
    if not api_key:
        raise ComedyV3ModelError("GEMINI_API_KEY is required for Comedy V3 when Gemini is selected.")
    with httpx.Client(timeout=timeout) as client:
        response = _post_with_rate_limit_retry(
            client,
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 8192, "responseMimeType": "application/json"},
            },
        )
    if response.status_code != 200:
        message = _response_message(response)
        if response.status_code == 429:
            raise _rate_limit_error("Gemini", model, prompt, response, "Groq")
        detail = f": {message[:300]}" if message else ""
        raise ComedyV3ModelError(f"Gemini Comedy V3 request failed ({response.status_code}){detail}.")
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def _groq_json(prompt: str, api_key: str, model: str, timeout: int) -> dict:
    if not api_key:
        raise ComedyV3ModelError("GROQ_API_KEY is required for Comedy V3 when Groq is selected.")
    with httpx.Client(timeout=timeout) as client:
        response = _post_with_rate_limit_retry(
            client,
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.15,
                "max_tokens": GROQ_MAX_OUTPUT_TOKENS,
                "response_format": {"type": "json_object"},
            },
        )
    if response.status_code != 200:
        message = _response_message(response)
        if response.status_code == 429:
            raise _rate_limit_error("Groq", model, prompt, response, "Gemini")
        detail = f": {message[:300]}" if message else ""
        raise ComedyV3ModelError(f"Groq Comedy V3 request failed ({response.status_code}){detail}.")
    return _extract_json(response.json()["choices"][0]["message"]["content"])


def complete_json(
    prompt: str,
    brain: str,
    timeout: int,
    *,
    gemini_api_key: str = "",
    gemini_model: str = "gemini-2.0-flash",
    groq_api_key: str = "",
    groq_model: str = "llama-3.1-8b-instant",
) -> dict:
    selected = normalize_brain(brain)
    try:
        if selected == "groq":
            return _groq_json(prompt, groq_api_key, groq_model, timeout)
        return _gemini_json(prompt, gemini_api_key, gemini_model, timeout)
    except ComedyV3JsonError as exc:
        repair_prompt = (
            "Repair this response into strict valid JSON only. Do not add commentary.\n\n"
            f"{exc.raw_text}"
        )
        if selected == "groq":
            return _groq_json(repair_prompt, groq_api_key, groq_model, timeout)
        return _gemini_json(repair_prompt, gemini_api_key, gemini_model, timeout)
