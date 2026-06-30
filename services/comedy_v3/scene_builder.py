from services.clip_director.timeline import clean_segments


def _scene_text(segments: list[dict]) -> str:
    return " ".join(str(segment.get("text", "")).strip() for segment in segments).strip()


def fallback_scenes(segments: list[dict], duration: float) -> list[dict]:
    clean = clean_segments(segments)
    scenes = []
    current = []
    scene_index = 1
    previous_end = 0.0
    for segment in clean:
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start))
        current_duration = end - float(current[0].get("start", start)) if current else 0.0
        if current and (start - previous_end > 4.0 or current_duration >= 150.0):
            scenes.append(_build_scene(scene_index, current))
            scene_index += 1
            current = []
        current.append(segment)
        previous_end = end
    if current:
        scenes.append(_build_scene(scene_index, current))
    if not scenes and duration > 0:
        scenes.append({
            "scene_id": "scene_001",
            "start": 0.0,
            "end": float(duration),
            "topic": "Comedy source",
            "people_involved": [],
            "scene_type": "setup",
            "summary": "",
            "comedy_density": 0.0,
        })
    return scenes


def _build_scene(index: int, segments: list[dict]) -> dict:
    start = float(segments[0].get("start", 0))
    end = float(segments[-1].get("end", start))
    text = _scene_text(segments)
    lowered = text.lower()
    scene_type = "reaction" if any(term in lowered for term in ("laugh", "laughter", "applause")) else "roast" if any(term in lowered for term in ("roast", "joke", "funny")) else "story"
    density_terms = ("laugh", "laughter", "joke", "funny", "roast", "savage", "audience", "judge")
    density = min(1.0, sum(1 for term in density_terms if term in lowered) / 4)
    return {
        "scene_id": f"scene_{index:03d}",
        "start": start,
        "end": end,
        "topic": text[:90] or "Comedy scene",
        "people_involved": [],
        "scene_type": scene_type,
        "summary": text[:240],
        "comedy_density": density,
    }
