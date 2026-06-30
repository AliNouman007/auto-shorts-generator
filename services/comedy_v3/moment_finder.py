COMEDY_TERMS = {
    "laugh": "audience_loses_it",
    "laughter": "audience_loses_it",
    "applause": "judge_reaction",
    "joke": "roast",
    "funny": "roast",
    "roast": "roast",
    "savage": "savage_reply",
    "awkward": "awkward_truth",
    "confession": "unexpected_confession",
    "judge": "judge_reaction",
    "audience": "audience_loses_it",
}


def fallback_moments(scenes: list[dict], segments: list[dict]) -> list[dict]:
    moments = []
    counter = 1
    for scene in scenes:
        scene_start = float(scene.get("start", 0))
        scene_end = float(scene.get("end", scene_start))
        scene_segments = [
            segment for segment in segments
            if float(segment.get("end", segment.get("start", 0))) > scene_start
            and float(segment.get("start", 0)) < scene_end
        ]
        for segment in scene_segments:
            text = str(segment.get("text", "")).lower()
            moment_type = ""
            for term, candidate_type in COMEDY_TERMS.items():
                if term in text:
                    moment_type = candidate_type
                    break
            if not moment_type:
                continue
            moments.append({
                "candidate_id": f"c_{counter:03d}",
                "scene_id": scene.get("scene_id", ""),
                "rough_start": float(segment.get("start", scene_start)),
                "rough_end": float(segment.get("end", scene_end)),
                "moment_type": moment_type,
                "setup_summary": str(scene.get("summary", ""))[:160],
                "punchline_summary": str(segment.get("text", ""))[:160],
                "reaction_summary": "",
                "people_involved": scene.get("people_involved", []),
                "target_person": "",
                "why_might_be_funny": "Contains comedy or reaction signals.",
            })
            counter += 1
    return moments
