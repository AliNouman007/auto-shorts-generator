import re


def fallback_episode_analysis(title: str, description: str = "") -> dict:
    text = f"{title} {description}"
    names = []
    for match in re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", text):
        if match.lower() in {"youtube", "shorts"}:
            continue
        if match not in names:
            names.append(match)
    return {
        "show_type": "mixed",
        "people": [
            {
                "name": name,
                "role": "performer",
                "fame_signal": "unknown",
                "why_relevant": "Mentioned in the source metadata.",
            }
            for name in names[:8]
        ],
        "comedy_context": {
            "running_jokes": [],
            "main_targets": [],
            "strongest_performers": [],
            "audience_reaction_style": "unknown",
            "viral_angle_hypotheses": [],
        },
    }
