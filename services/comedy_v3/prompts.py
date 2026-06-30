import json


def _payload(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def episode_analyst_prompt(seed: dict) -> str:
    return (
        "You are a comedy episode analyst for a Shorts clipping engine. "
        "Infer people, roles, social/fame relevance, running jokes, targets, and viral comedy angles. "
        "Do not invent timestamps. Return strict JSON with keys: show_type, people, comedy_context.\n\n"
        f"Input:\n{_payload(seed)}"
    )


def scene_builder_prompt(seed: dict) -> str:
    return (
        "Group this comedy transcript into meaningful scenes/topics. "
        "No fixed scene width. Use topic shifts, setup changes, silence gaps, and speaker/context changes. "
        "Return strict JSON: {\"scenes\":[{\"scene_id\":\"scene_001\",\"start\":0,\"end\":60,"
        "\"topic\":\"...\",\"people_involved\":[],\"scene_type\":\"roast\",\"summary\":\"...\","
        "\"comedy_density\":0.7}]}.\n\n"
        f"Input:\n{_payload(seed)}"
    )


def moment_finder_prompt(seed: dict) -> str:
    return (
        "Find possible comedy moments inside these scenes. Over-generate candidates; do not approve clips. "
        "Look for roasts, savage replies, famous-person targeting, awkward truths, callbacks, misunderstandings, "
        "crowd work, judge reactions, audience laughter, personality clash, tension release, and main-character moments. "
        "Return strict JSON: {\"moments\":[{\"candidate_id\":\"c_001\",\"scene_id\":\"scene_001\","
        "\"rough_start\":10,\"rough_end\":70,\"moment_type\":\"roast\",\"setup_summary\":\"...\","
        "\"punchline_summary\":\"...\",\"reaction_summary\":\"...\",\"people_involved\":[],"
        "\"target_person\":\"...\",\"why_might_be_funny\":\"...\"}]}.\n\n"
        f"Input:\n{_payload(seed)}"
    )


def boundary_expander_prompt(seed: dict) -> str:
    return (
        "Expand each comedy candidate to complete standalone context. Start before the joke needs context, "
        "include target/person setup, punchline, and reaction/payoff when relevant. Avoid mid-sentence starts/ends. "
        "Do not use fixed clip width. Return strict JSON: {\"boundaries\":[{\"candidate_id\":\"c_001\","
        "\"start\":0,\"end\":80,\"setup_start\":0,\"context_start\":5,\"punchline_time\":60,"
        "\"reaction_start\":62,\"reaction_end\":75,\"payoff_end\":80,\"boundary_confidence\":0.9,"
        "\"boundary_reason\":\"...\"}]}.\n\n"
        f"Input:\n{_payload(seed)}"
    )


def worthiness_judge_prompt(seed: dict) -> str:
    return (
        "Judge comedy clip worthiness. Reject filler. A clip is worth cutting only if a stranger can understand "
        "the setup/context/punchline/payoff and has a reason to watch to the end. "
        "Return strict JSON: {\"judgements\":[{\"candidate_id\":\"c_001\",\"cut_decision\":\"cut\","
        "\"quality_tier\":\"A\",\"worthiness_score\":0.9,\"funny_score\":0.9,\"viral_score\":0.8,"
        "\"standalone_score\":0.9,\"context_score\":0.9,\"hook\":\"...\",\"context\":\"...\","
        "\"punchline\":\"...\",\"reaction\":\"...\",\"payoff\":\"...\",\"why_funny\":\"...\","
        "\"why_viral\":\"...\",\"why_standalone\":\"...\",\"rejection_reason\":\"\"}]}.\n\n"
        f"Input:\n{_payload(seed)}"
    )
