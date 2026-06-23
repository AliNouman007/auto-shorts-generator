import time
from urllib.parse import urlencode

import httpx


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)


def build_auth_url(client_id, redirect_uri, state="auto-shorts-studio"):
    if not client_id or not redirect_uri:
        raise RuntimeError("YouTube OAuth client ID and redirect URI are required.")
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


def exchange_code_for_tokens(client_id, client_secret, redirect_uri, code):
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("YouTube OAuth secrets are not configured.")
    with httpx.Client(timeout=60) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(response.text[:500] or "YouTube token exchange failed.")
    return response.json()


def refresh_access_token(client_id, client_secret, refresh_token):
    if not refresh_token:
        raise RuntimeError("YouTube refresh token is missing.")
    with httpx.Client(timeout=60) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(response.text[:500] or "YouTube token refresh failed.")
    return response.json()


def upload_short_to_youtube(account, file_path, title, description="", privacy_status="private", scheduled_at=None):
    access_token = (account or {}).get("access_token")
    if not access_token:
        raise RuntimeError("YouTube account is not connected.")

    metadata = {
        "snippet": {
            "title": title or "Untitled Short",
            "description": description or "",
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status or "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    if scheduled_at:
        metadata["status"]["publishAt"] = scheduled_at
        metadata["status"]["privacyStatus"] = "private"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
    }
    params = {"uploadType": "resumable", "part": "snippet,status"}
    with httpx.Client(timeout=120) as client:
        start = client.post(UPLOAD_URL, params=params, headers=headers, json=metadata)
        if start.status_code >= 400:
            raise RuntimeError(start.text[:500] or "YouTube upload could not start.")
        upload_url = start.headers.get("Location")
        if not upload_url:
            raise RuntimeError("YouTube did not return an upload URL.")
        with open(file_path, "rb") as media:
            upload = client.put(
                upload_url,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "video/mp4"},
                content=media.read(),
            )
    if upload.status_code >= 400:
        raise RuntimeError(upload.text[:500] or "YouTube upload failed.")
    data = upload.json()
    video_id = data.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload succeeded without a video id.")
    return {
        "platform_video_id": video_id,
        "platform_url": f"https://youtube.com/shorts/{video_id}",
    }


def fetch_youtube_analytics(account, platform_video_id):
    access_token = (account or {}).get("access_token")
    if not access_token:
        raise RuntimeError("YouTube account is not connected.")
    with httpx.Client(timeout=60) as client:
        response = client.get(
            VIDEOS_URL,
            params={"part": "statistics", "id": platform_video_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(response.text[:500] or "YouTube analytics fetch failed.")
    items = response.json().get("items") or []
    if not items:  # video is deleted/not found
        return None
    stats = items[0].get("statistics", {})
    return {
        "views": int(stats.get("viewCount") or 0),
        "likes": int(stats.get("likeCount") or 0),
        "comments": int(stats.get("commentCount") or 0),
        "watch_time": int(stats.get("watchTime") or 0),
        "fetched_at": str(int(time.time())),
    }
