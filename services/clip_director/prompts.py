import json

from .types import ClipConstraints, EpisodeMap


def build_director_prompt(episode_map: EpisodeMap, constraints: ClipConstraints) -> str:
    payload = {
        "title": episode_map.get("title", ""),
        "duration": episode_map.get("duration", 0),
        "mode": episode_map.get("mode", "shorts"),
        "genre_hint": episode_map.get("genre_hint", ""),
        "detected_genre": episode_map.get("detected_genre", "auto"),
        "constraints": constraints,
        "audio_peaks": episode_map.get("audio_peaks", [])[:40],
        "candidate_moments": episode_map.get("candidate_moments", [])[:30],
        "segments": episode_map.get("segments", [])[:220],
    }
    return (
        "You are a quality-first viral clip director. Decide how many clips are actually worth cutting; "
        "do not force a fixed count. For comedy or stage shows, capture the full setup, punchline, "
        "audience/judge reaction, and payoff. Do not choose tiny loud-only clips.\n"
        f"Clips must be between {constraints['min_duration']} and {constraints['max_duration']} seconds. "
        f"Return at most {constraints['safety_max_clips']} clips.\n"
        "Return strict JSON only: {\"clips\":[{\"start\":0,\"end\":120,\"title\":\"...\","
        "\"description\":\"...\",\"upload_title\":\"...\",\"upload_description\":\"...\","
        "\"hook\":\"...\",\"context\":\"...\",\"value\":\"...\",\"payoff\":\"...\","
        "\"virality_score\":90,\"completion_score\":90,\"hook_type\":\"story\",\"reason\":\"...\"}]}.\n\n"
        f"Episode map:\n{json.dumps(payload, ensure_ascii=False)}"
    )
