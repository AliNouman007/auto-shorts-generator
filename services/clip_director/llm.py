import json
import re

import httpx

from .prompts import build_director_prompt
from .types import ClipConstraints, DirectorSelection, EpisodeMap


def parse_llm_json(text: str) -> list[DirectorSelection]:
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []
        data = json.loads(match.group(0))
    if isinstance(data, dict):
        clips = data.get("clips", [])
    else:
        clips = data
    return [item for item in clips if isinstance(item, dict)]


def request_llm_selection(
    episode_map: EpisodeMap,
    constraints: ClipConstraints,
    ai_model: str,
    *,
    openai_api_key: str = "",
    openai_model: str = "",
    groq_api_key: str = "",
    groq_model: str = "",
    gemini_api_key: str = "",
    gemini_model: str = "",
) -> list[DirectorSelection]:
    prompt = build_director_prompt(episode_map, constraints)
    try:
        if ai_model == "openai" and openai_api_key:
            with httpx.Client(timeout=90) as client:
                response = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_api_key}"},
                    json={
                        "model": openai_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.15,
                        "response_format": {"type": "json_object"},
                    },
                )
            if response.status_code == 200:
                return parse_llm_json(response.json()["choices"][0]["message"]["content"])
        if ai_model == "groq" and groq_api_key:
            with httpx.Client(timeout=90) as client:
                response = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_api_key}"},
                    json={
                        "model": groq_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.15,
                        "response_format": {"type": "json_object"},
                    },
                )
            if response.status_code == 200:
                return parse_llm_json(response.json()["choices"][0]["message"]["content"])
        if ai_model == "gemini" and gemini_api_key:
            with httpx.Client(timeout=90) as client:
                response = client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_api_key}",
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": 8192, "responseMimeType": "application/json"},
                    },
                )
            if response.status_code == 200:
                text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                return parse_llm_json(text)
    except Exception:
        return []
    return []
