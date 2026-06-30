import json
import re

import httpx

from .schemas import normalize_brain


class ComedyV3ModelError(RuntimeError):
    pass


class ComedyV3JsonError(ComedyV3ModelError):
    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


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


def _gemini_json(prompt: str, api_key: str, model: str, timeout: int) -> dict:
    if not api_key:
        raise ComedyV3ModelError("GEMINI_API_KEY is required for Comedy V3 when Gemini is selected.")
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 8192, "responseMimeType": "application/json"},
            },
        )
    if response.status_code != 200:
        message = _response_message(response)
        if response.status_code == 429:
            raise ComedyV3ModelError(
                "Gemini Comedy V3 was rate-limited or quota-limited. Wait for quota reset, reduce usage, or select Groq as the Comedy V3 main brain."
            )
        detail = f": {message[:300]}" if message else ""
        raise ComedyV3ModelError(f"Gemini Comedy V3 request failed ({response.status_code}){detail}.")
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def _groq_json(prompt: str, api_key: str, model: str, timeout: int) -> dict:
    if not api_key:
        raise ComedyV3ModelError("GROQ_API_KEY is required for Comedy V3 when Groq is selected.")
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.15,
                "response_format": {"type": "json_object"},
            },
        )
    if response.status_code != 200:
        message = _response_message(response)
        if response.status_code == 429:
            raise ComedyV3ModelError(
                "Groq Comedy V3 was rate-limited or quota-limited. Wait for quota reset, reduce usage, or select Gemini as the Comedy V3 main brain."
            )
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
