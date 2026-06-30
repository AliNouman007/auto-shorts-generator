QUALITY_MODES = {"strict", "balanced", "volume"}
BRAINS = {"gemini", "groq"}
ENGINE_NAME = "comedy_v3"

COMEDY_MOMENT_TYPES = {
    "roast",
    "savage_reply",
    "famous_person_targeted",
    "unexpected_confession",
    "awkward_truth",
    "callback",
    "misunderstanding",
    "crowd_work",
    "host_trap",
    "judge_reaction",
    "audience_loses_it",
    "personality_clash",
    "tension_release",
    "comparison_joke",
    "insult_recovery",
    "main_character_moment",
}


def normalize_brain(value: str | None) -> str:
    brain = str(value or "gemini").strip().lower()
    return brain if brain in BRAINS else "gemini"


def normalize_quality_mode(value: str | None) -> str:
    mode = str(value or "balanced").strip().lower()
    return mode if mode in QUALITY_MODES else "balanced"
