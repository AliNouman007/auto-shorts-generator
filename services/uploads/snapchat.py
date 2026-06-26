from pathlib import Path
from urllib.parse import urlencode


CREATIVE_KIT_WEB_URL = "https://www.snapchat.com/scan"
MAX_VIDEO_BYTES = 300 * 1024 * 1024
MAX_VIDEO_SECONDS = 300.0


def build_share_payload(
    *,
    file_url: str,
    caption: str = "",
    client_id: str = "",
    duration: float | None = None,
    file_size: int | None = None,
) -> dict:
    if file_size is not None and file_size > MAX_VIDEO_BYTES:
        raise RuntimeError("Snapchat Creative Kit videos must be 300 MB or smaller.")
    if duration is not None and duration > MAX_VIDEO_SECONDS:
        raise RuntimeError("Snapchat Creative Kit videos must be 5 minutes or shorter.")
    caption = (caption or "")[:250]
    query = {
        "attachmentUrl": file_url,
        "captionText": caption,
    }
    if client_id:
        query["clientId"] = client_id
    return {
        "share_url": f"{CREATIVE_KIT_WEB_URL}?{urlencode(query)}",
        "caption": caption,
        "file_url": file_url,
    }


def build_share_for_file(
    *,
    base_url: str,
    filename: str,
    caption: str = "",
    client_id: str = "",
    duration: float | None = None,
    file_path: str | None = None,
) -> dict:
    file_size = Path(file_path).stat().st_size if file_path else None
    return build_share_payload(
        file_url=f"{base_url.rstrip('/')}/outputs/{filename}",
        caption=caption,
        client_id=client_id,
        duration=duration,
        file_size=file_size,
    )
