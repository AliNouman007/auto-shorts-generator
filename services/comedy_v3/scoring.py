def score_clip(judgement: dict, boundary: dict) -> float:
    return round(min(1.0, max(
        float(judgement.get("worthiness_score", 0) or 0),
        (
            float(judgement.get("funny_score", 0) or 0)
            + float(judgement.get("viral_score", 0) or 0)
            + float(judgement.get("standalone_score", 0) or 0)
            + float(boundary.get("boundary_confidence", 0) or 0)
        ) / 4,
    )), 4)


def score_to_percent(value: float) -> int:
    return max(0, min(100, int(round(float(value or 0) * 100))))
