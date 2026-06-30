REACTION_TERMS = ("laugh", "laughter", "applause", "audience", "judge", "reaction")
COMEDY_TERMS = ("joke", "funny", "roast", "savage", "awkward", "confession", "laugh")


def fallback_judgements(boundaries: list[dict], moments: list[dict], segments: list[dict]) -> list[dict]:
    moment_by_id = {str(moment.get("candidate_id")): moment for moment in moments}
    judgements = []
    for boundary in boundaries:
        candidate_id = str(boundary.get("candidate_id", ""))
        moment = moment_by_id.get(candidate_id, {})
        text = str(moment.get("setup_summary", "")) + " " + str(moment.get("punchline_summary", "")) + " " + str(moment.get("reaction_summary", ""))
        lowered = text.lower()
        funny_score = min(1.0, sum(1 for term in COMEDY_TERMS if term in lowered) / 3)
        reaction_score = min(1.0, sum(1 for term in REACTION_TERMS if term in lowered) / 3)
        context_score = float(boundary.get("boundary_confidence", 0))
        worthiness = round(min(1.0, 0.45 * funny_score + 0.2 * reaction_score + 0.35 * context_score), 3)
        tier = "A" if worthiness >= 0.78 else "B" if worthiness >= 0.58 else "C"
        cut_decision = "cut" if tier == "A" else "maybe" if tier == "B" else "reject"
        judgements.append({
            "candidate_id": candidate_id,
            "cut_decision": cut_decision,
            "quality_tier": tier,
            "worthiness_score": worthiness,
            "funny_score": funny_score,
            "viral_score": max(funny_score, reaction_score),
            "standalone_score": context_score,
            "context_score": context_score,
            "hook": str(moment.get("setup_summary") or moment.get("why_might_be_funny") or "")[:220],
            "context": str(moment.get("setup_summary", ""))[:300],
            "punchline": str(moment.get("punchline_summary", ""))[:260],
            "reaction": str(moment.get("reaction_summary", ""))[:220],
            "payoff": str(moment.get("reaction_summary") or moment.get("punchline_summary") or "")[:260],
            "why_funny": str(moment.get("why_might_be_funny") or "Contains a comedy signal.")[:300],
            "why_viral": "Has comedy and context signals." if tier != "C" else "",
            "why_standalone": "Boundary includes setup and payoff." if context_score >= 0.7 else "",
            "rejection_reason": "" if tier != "C" else "Comedy signal is too weak or lacks standalone context.",
        })
    return judgements
