import re


HOOK_WORDS = {
    "why", "how", "what", "mistake", "secret", "surprising", "nobody", "never",
    "problem", "truth", "stop", "fix", "first", "best", "setup", "roast",
    "roasting", "joke", "funny", "comedy",
}
PAYOFF_WORDS = {
    "payoff", "answer", "fix", "result", "because", "therefore", "reveals",
    "finally", "works", "solves", "change", "makes", "keeps", "punchline",
    "reaction", "reacts", "laughing", "applause", "taali", "hasna", "hassi",
}
REACTION_WORDS = {
    "laugh", "laughing", "laughter", "applause", "wow", "shocked", "react",
    "reaction", "reacts", "crowd", "judge", "judges", "audience", "taali",
    "hasna", "hassi", "roast", "punchline",
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _word_hits(text: str, words: set[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if re.search(rf"\b{re.escape(word)}\b", lowered))


def _duration_score(duration: float) -> float:
    if duration <= 0:
        return 0.0
    if 18 <= duration <= 65:
        return 1.0
    if 12 <= duration < 18:
        return 0.7
    if 65 < duration <= 180:
        return 0.65
    return 0.35


def score_candidate(candidate: dict) -> dict:
    text = str(candidate.get("text", ""))
    words = text.split()
    duration = float(candidate.get("duration") or (float(candidate.get("end", 0)) - float(candidate.get("start", 0))))
    hook = clamp((_word_hits(text, HOOK_WORDS) / 3) + (0.25 if text.strip().endswith("?") else 0))
    payoff = clamp(_word_hits(text, PAYOFF_WORDS) / 3)
    audio_reaction = clamp(float(candidate.get("audio_peak_energy", 0)) + (_word_hits(text, REACTION_WORDS) / 4))
    standalone = clamp((len(words) / 55) + (0.25 if payoff > 0 else 0))
    visual = 0.55 if candidate.get("candidate_source") in {"scene_topic", "audio_peak"} else 0.35
    duration_component = _duration_score(duration)
    is_comedy = candidate.get("candidate_source") in {"audio_peak", "comedy"} or _word_hits(text, REACTION_WORDS) > 0
    if is_comedy:
        hook = max(hook, clamp(_word_hits(text, HOOK_WORDS) / 2))
        payoff = max(payoff, clamp(_word_hits(text, PAYOFF_WORDS) / 2))
        audio_reaction = max(audio_reaction, clamp(float(candidate.get("audio_peak_energy", 0)) * 0.85 + (_word_hits(text, REACTION_WORDS) / 5)))
        standalone = max(standalone, 0.7 if len(words) >= 12 else standalone)
        visual = max(visual, 0.65)
    features = {
        "hook": hook,
        "payoff": payoff,
        "audioReaction": audio_reaction,
        "standalone": standalone,
        "visual": visual,
        "duration": duration_component,
    }
    penalties = {
        "midSentenceStart": 1.0 if candidate.get("mid_sentence_start") else 0.0,
        "midSentenceEnd": 1.0 if candidate.get("mid_sentence_end") else 0.0,
        "deadAirIntro": 1.0 if candidate.get("dead_air_intro") else 0.0,
        "noPayoff": 1.0 if payoff < 0.2 else 0.0,
        "contextGap": 1.0 if len(words) < 10 else 0.0,
        "duplicateOverlap": float(candidate.get("duplicate_overlap", 0) or 0),
        "cropRisk": 0.0,
        "lowAsrConfidence": float(candidate.get("low_asr_confidence", 0) or 0),
    }
    base_score = (
        0.24 * features["hook"]
        + 0.22 * features["payoff"]
        + 0.20 * features["audioReaction"]
        + 0.16 * features["standalone"]
        + 0.10 * features["visual"]
        + 0.08 * features["duration"]
    )
    penalty = (
        0.10 * penalties["midSentenceStart"]
        + 0.10 * penalties["midSentenceEnd"]
        + 0.08 * penalties["deadAirIntro"]
        + 0.08 * penalties["noPayoff"]
        + 0.06 * penalties["contextGap"]
        + 0.05 * penalties["duplicateOverlap"]
        + 0.04 * penalties["cropRisk"]
        + 0.04 * penalties["lowAsrConfidence"]
    )
    final_score = clamp(base_score - penalty)
    return {
        **candidate,
        "duration": duration,
        "feature_scores": features,
        "penalties": penalties,
        "base_score": round(base_score, 4),
        "penalty": round(penalty, 4),
        "final_score": round(final_score, 4),
    }
