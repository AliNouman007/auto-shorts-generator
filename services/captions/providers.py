from typing import Awaitable, Callable


async def first_successful_transcript(
    providers: list[tuple[str, Callable[[], Awaitable[dict | None]]]],
) -> dict | None:
    errors = []
    for name, provider in providers:
        try:
            result = await provider()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if result and result.get("segments"):
            if errors:
                result["fallback_errors"] = errors
            return result
    if errors:
        return {"segments": [], "provider": "", "status": "failed", "fallback_errors": errors}
    return None
