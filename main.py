"""
Auto Shorts Generator - FastAPI Backend
Monitors YouTube channels for new uploads and generates short-form vertical clips.
"""

import os
import re
import json
import base64
import hmac
import hashlib
import asyncio
import sqlite3
import subprocess
import threading
import shutil
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

def load_env_file(path: Path = ENV_PATH) -> None:
    """Load local .env values without overriding real environment variables."""
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value

def env_value(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def env_int(key: str, default: int) -> int:
    value = env_value(key)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default

load_env_file()

YOUTUBE_API_KEY = env_value("YOUTUBE_API_KEY")
YOUTUBE_CHANNEL_ID = env_value("YOUTUBE_CHANNEL_ID")
OPENAI_API_KEY = env_value("OPENAI_API_KEY")
GEMINI_API_KEY = env_value("GEMINI_API_KEY")
GEMINI_MODEL = env_value("GEMINI_MODEL", "gemini-2.0-flash")
GROQ_API_KEY = env_value("GROQ_API_KEY")
GROQ_MODEL = env_value("GROQ_MODEL", "whisper-large-v3-turbo")
GROQ_LLM_MODEL = env_value("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
WEBHOOK_SECRET = env_value("WEBHOOK_SECRET")
PORT = env_int("PORT", 8000)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

REQUIRED_VIDEO_TOOLS = ("ffmpeg", "ffprobe")

def command_available(command: str) -> bool:
    return shutil.which(command) is not None

def missing_tools_message(commands: tuple[str, ...]) -> str:
    missing = [command for command in commands if not command_available(command)]
    if not missing:
        return ""
    return f"Missing required command(s): {', '.join(missing)}. Install them and restart the app."

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Auto Shorts Generator", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs_files")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_video_id TEXT UNIQUE NOT NULL,
            title TEXT,
            published_at TEXT,
            source_path TEXT,
            status TEXT DEFAULT 'detected',
            error_message TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS shorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL REFERENCES videos(id),
            filename TEXT NOT NULL,
            start_time REAL,
            end_time REAL,
            duration REAL,
            caption_text TEXT,
            title TEXT,
            virality_score INTEGER,
            completion_score INTEGER,
            hook_type TEXT,
            selection_reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    # Migrate: add columns for older DBs
    for col_sql in [
        "ALTER TABLE videos ADD COLUMN steps_json TEXT",
        "ALTER TABLE videos ADD COLUMN thumbnail TEXT",
        "ALTER TABLE shorts ADD COLUMN title TEXT",
        "ALTER TABLE shorts ADD COLUMN virality_score INTEGER",
        "ALTER TABLE shorts ADD COLUMN completion_score INTEGER",
        "ALTER TABLE shorts ADD COLUMN hook_type TEXT",
        "ALTER TABLE shorts ADD COLUMN selection_reason TEXT",
        "ALTER TABLE shorts ADD COLUMN status TEXT DEFAULT 'draft'",
        "ALTER TABLE shorts ADD COLUMN description TEXT",
        "ALTER TABLE shorts ADD COLUMN approved_at TEXT",
        "ALTER TABLE shorts ADD COLUMN updated_at TEXT",
        "ALTER TABLE shorts ADD COLUMN original_start_time REAL",
        "ALTER TABLE shorts ADD COLUMN original_end_time REAL",
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass
    conn.close()

init_db()

_db_lock = threading.Lock()
_job_lock = threading.Lock()
_cancel_events: dict[int, threading.Event] = {}
_active_processes: dict[int, subprocess.Popen] = {}

class CancelledProcessing(Exception):
    pass

def db_write(sql: str, params: tuple = ()):
    with _db_lock:
        conn = get_db()
        conn.execute(sql, params)
        conn.commit()
        conn.close()

def db_read(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def start_video_job(video_id: int) -> threading.Event:
    with _job_lock:
        event = threading.Event()
        _cancel_events[video_id] = event
        return event

def get_or_start_video_job(video_id: int) -> tuple[threading.Event, bool]:
    with _job_lock:
        event = _cancel_events.get(video_id)
        if event:
            return event, False
        event = threading.Event()
        _cancel_events[video_id] = event
        return event, True

def finish_video_job(video_id: int):
    with _job_lock:
        _cancel_events.pop(video_id, None)
        _active_processes.pop(video_id, None)

def cancel_requested(video_id: int) -> bool:
    with _job_lock:
        event = _cancel_events.get(video_id)
        return bool(event and event.is_set())

def request_video_cancel(video_id: int) -> bool:
    with _job_lock:
        event = _cancel_events.get(video_id)
        process = _active_processes.get(video_id)
        if event:
            event.set()
        if process and process.poll() is None:
            process.kill()
    return bool(event or process)

def ensure_not_cancelled(video_id: int):
    if cancel_requested(video_id):
        raise CancelledProcessing("Processing cancelled.")

def run_command(cmd: list[str], timeout: int, video_id: Optional[int] = None) -> subprocess.CompletedProcess:
    if video_id is None:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    ensure_not_cancelled(video_id)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with _job_lock:
        _active_processes[video_id] = process
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise
    finally:
        with _job_lock:
            if _active_processes.get(video_id) is process:
                _active_processes.pop(video_id, None)

    ensure_not_cancelled(video_id)
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

def update_steps(video_id: int, steps: list[dict]):
    """Persist step list to DB so the frontend can poll live progress."""
    db_write(
        "UPDATE videos SET steps_json=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(steps), video_id),
    )

def delete_short_record(short_id: int) -> bool:
    rows = db_read(
        "SELECT shorts.filename, videos.id AS video_id, videos.source_path, videos.status "
        "FROM shorts JOIN videos ON videos.id=shorts.video_id WHERE shorts.id=?",
        (short_id,),
    )
    if not rows:
        return False

    short = rows[0]
    filename = Path(short["filename"]).name
    (OUTPUTS_DIR / filename).unlink(missing_ok=True)
    (OUTPUTS_DIR / f"{Path(filename).stem}.srt").unlink(missing_ok=True)
    db_write("DELETE FROM shorts WHERE id=?", (short_id,))

    remaining = db_read("SELECT id FROM shorts WHERE video_id=? LIMIT 1", (short["video_id"],))
    if not remaining and short["status"] == "completed":
        source_path = short.get("source_path") or ""
        next_status = "waiting" if source_path and Path(source_path).exists() else "detected"
        db_write(
            "UPDATE videos SET status=?, error_message=NULL, steps_json=NULL, updated_at=datetime('now') WHERE id=?",
            (next_status, short["video_id"]),
        )
    return True

def delete_source_video(video_id: int) -> bool:
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        return False

    video = rows[0]
    source_path = video.get("source_path") or ""
    if not source_path:
        return False

    request_video_cancel(video_id)
    for short in db_read("SELECT id FROM shorts WHERE video_id=?", (video_id,)):
        delete_short_record(short["id"])

    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", video["youtube_video_id"])
    for partial in OUTPUTS_DIR.glob(f"{safe_id}_short_*"):
        if partial.suffix in (".mp4", ".srt"):
            partial.unlink(missing_ok=True)

    Path(source_path).unlink(missing_ok=True)
    db_write(
        "UPDATE videos SET source_path='', status='detected', error_message=NULL, steps_json=NULL, updated_at=datetime('now') WHERE id=?",
        (video_id,),
    )
    return True

def cleanup_cancelled_video(video_id: int, source_started_as_download: bool = False):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        return

    video = rows[0]
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", video["youtube_video_id"])
    for short in db_read("SELECT id FROM shorts WHERE video_id=?", (video_id,)):
        delete_short_record(short["id"])
    for partial in OUTPUTS_DIR.glob(f"{safe_id}_short_*"):
        if partial.suffix in (".mp4", ".srt"):
            partial.unlink(missing_ok=True)

    source_path = video.get("source_path") or ""
    if source_started_as_download and source_path:
        Path(source_path).unlink(missing_ok=True)
        source_path = ""

    next_status = "detected"
    if source_path and Path(source_path).exists():
        next_status = "waiting"

    cancelled_steps = [{"name": "Cancelled", "status": "error", "detail": "Progress discarded."}]
    db_write(
        "UPDATE videos SET status=?, source_path=?, error_message=NULL, steps_json=?, updated_at=datetime('now') WHERE id=?",
        (next_status, source_path, json.dumps(cancelled_steps), video_id),
    )

def get_setting(key: str, default: str = "") -> str:
    """Read a persistent setting from the DB, falling back to env vars then default."""
    rows = db_read("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default

def set_setting(key: str, value: str):
    """Upsert a persistent setting."""
    db_write(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

def effective_channel_id() -> str:
    """Return the channel ID from DB settings, falling back to env var."""
    return get_setting("channel_id") or YOUTUBE_CHANNEL_ID

def effective_ai_model() -> str:
    """Return the active AI model: 'openai', 'gemini', or 'groq'."""
    return get_setting("ai_model", "openai")

# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def _yt_channel_param(channel_id: str) -> dict:
    """
    Return the correct YouTube API parameter dict for the given identifier.
    Supports:
      - UC...  channel IDs  → {"id": ...}
      - @handle or handle   → {"forHandle": ...}  (@ is kept, API accepts both)
    """
    ch = channel_id.strip()
    if ch.startswith("UC") and len(ch) >= 24:
        return {"id": ch}
    # Handle — keep the @ if present, YouTube accepts it
    return {"forHandle": ch}

async def _resolve_channel_items(client: httpx.AsyncClient, channel_id: str, part: str) -> list:
    """
    Try to fetch channel items using the best parameter for the given identifier.
    Falls back to forUsername if forHandle returns nothing.
    """
    params_list = [_yt_channel_param(channel_id)]
    # If forHandle didn't start with @, also try forUsername as fallback
    if not channel_id.strip().startswith("UC"):
        handle = channel_id.strip().lstrip("@")
        params_list.append({"forUsername": handle})

    for extra in params_list:
        resp = await client.get(
            f"{YOUTUBE_API_BASE}/channels",
            params={"part": part, "key": YOUTUBE_API_KEY, **extra},
        )
        data = resp.json()
        items = data.get("items", [])
        if items:
            return items
    return []

async def fetch_channel_info(channel_id: str) -> dict:
    """Return channel snippet (title, thumbnail, subscriberCount, videoCount)."""
    if not YOUTUBE_API_KEY or not channel_id:
        return {}
    async with httpx.AsyncClient(timeout=15) as client:
        items = await _resolve_channel_items(client, channel_id, "snippet,statistics")
        if not items:
            return {}
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        return {
            "title": snippet.get("title", ""),
            "description": snippet.get("description", "")[:200],
            "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
            "subscriber_count": stats.get("subscriberCount", ""),
            "video_count": stats.get("videoCount", ""),
        }

async def fetch_latest_uploads(channel_id: str, max_results: int = 10) -> list[dict]:
    """
    Fetch the latest uploads from the given channel using the YouTube Data API.
    Returns list of dicts: {youtube_video_id, title, published_at, thumbnail}.
    """
    if not YOUTUBE_API_KEY or not channel_id:
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        # Step 1: resolve channel → get uploads playlist ID
        items = await _resolve_channel_items(client, channel_id, "contentDetails")
        if not items:
            raise ValueError(f"Channel not found: '{channel_id}'. "
                             "Use a UC... channel ID or a @handle (e.g. @MrBeast).")
        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        # Step 2: list playlist items
        pl_resp = await client.get(
            f"{YOUTUBE_API_BASE}/playlistItems",
            params={
                "part": "snippet",
                "playlistId": uploads_playlist_id,
                "maxResults": max_results,
                "key": YOUTUBE_API_KEY,
            },
        )
        pl_data = pl_resp.json()

    videos = []
    for item in pl_data.get("items", []):
        snippet = item.get("snippet", {})
        resource = snippet.get("resourceId", {})
        video_id = resource.get("videoId")
        if video_id:
            thumbnails = snippet.get("thumbnails", {})
            thumb = (
                thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url", "")
            )
            videos.append({
                "youtube_video_id": video_id,
                "title": snippet.get("title", "Untitled"),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnail": thumb,
            })
    return videos

# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def run_ffprobe(path: str) -> Optional[dict]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=60,
        )
        return json.loads(result.stdout)
    except Exception:
        return None

def get_video_duration(path: str) -> Optional[float]:
    probe = run_ffprobe(path)
    if not probe:
        return None
    try:
        return float(probe["format"]["duration"])
    except (KeyError, ValueError):
        return None

def is_valid_video(path: str) -> bool:
    probe = run_ffprobe(path)
    if not probe:
        return False
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return True
    return False

def build_export_video_filter(srt_path: Optional[str] = None) -> str:
    vf = (
        "split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=30:1[bg];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black@0[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    if srt_path and Path(srt_path).exists():
        escaped = srt_path.replace("\\", "/").replace(":", "\\:")
        vf += (
            f",subtitles='{escaped}':force_style="
            "'FontName=Arial,FontSize=18,Alignment=2,MarginV=40,"
            "Bold=1,PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,Outline=2'"
        )
    return vf

def export_short_clip(
    source_path: str,
    output_path: str,
    start: float,
    duration: float,
    srt_path: Optional[str] = None,
    video_id: Optional[int] = None,
) -> bool:
    """Export a 9:16 Short while preserving the full source frame."""
    vf = build_export_video_filter(srt_path)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", source_path,
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        result = run_command(cmd, timeout=600, video_id=video_id)
        return result.returncode == 0
    except CancelledProcessing:
        raise
    except Exception:
        return False

def extract_audio(video_path: str, audio_path: str, video_id: Optional[int] = None) -> bool:
    """Extract mono 16 kHz audio from a video."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-ar", "16000", "-ac", "1", "-q:a", "0", audio_path,
    ]
    try:
        result = run_command(cmd, timeout=300, video_id=video_id)
        return result.returncode == 0
    except CancelledProcessing:
        raise
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Transcription helpers
# ---------------------------------------------------------------------------

def transcribe_with_whisper_local(audio_path: str, video_id: Optional[int] = None) -> Optional[list[dict]]:
    """Try the local openai-whisper CLI. Returns segments list or None."""
    try:
        out_dir = Path(audio_path).parent
        result = run_command(
            ["whisper", audio_path, "--output_format", "json", "--output_dir", str(out_dir)],
            timeout=300,
            video_id=video_id,
        )
        if result.returncode != 0:
            return None
        json_path = out_dir / (Path(audio_path).stem + ".json")
        if json_path.exists():
            data = json.loads(json_path.read_text())
            return data.get("segments", [])
    except CancelledProcessing:
        raise
    except Exception:
        pass
    return None

async def transcribe_with_openai_api(audio_path: str) -> Optional[list[dict]]:
    """Transcribe via OpenAI Whisper API. Returns segments list or None."""
    if not OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with open(audio_path, "rb") as f:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    files={"file": (Path(audio_path).name, f, "audio/mpeg")},
                    data={"model": "whisper-1", "response_format": "verbose_json"},
                )
        if response.status_code == 200:
            return response.json().get("segments", [])
    except Exception:
        pass
    return None

async def transcribe_with_groq_api(audio_path: str) -> Optional[list[dict]]:
    """Transcribe via Groq's OpenAI-compatible Whisper endpoint."""
    if not GROQ_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with open(audio_path, "rb") as f:
                response = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (Path(audio_path).name, f, "audio/mpeg")},
                    data={
                        "model": GROQ_MODEL,
                        "response_format": "verbose_json",
                        "temperature": "0",
                    },
                )
        if response.status_code != 200:
            try:
                message = response.json().get("error", {}).get("message", "")
            except Exception:
                message = response.text
            raise RuntimeError(f"Groq API error ({response.status_code}): {message[:500]}")
        return response.json().get("segments", [])
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Groq transcription failed: {exc}") from exc

async def transcribe_with_gemini(audio_path: str) -> Optional[list[dict]]:
    """
    Transcribe audio using Google Gemini (gemini-2.0-flash).
    Sends the audio file as inline base64 data (max ~8 MB).
    Returns a list of pseudo-segments reconstructed from the transcript.
    """
    if not GEMINI_API_KEY:
        return None

    audio_bytes = Path(audio_path).read_bytes()
    # Chunk to ≤7 MB to stay within Gemini's inline limit
    MAX_BYTES = 7 * 1024 * 1024
    if len(audio_bytes) > MAX_BYTES:
        audio_bytes = audio_bytes[:MAX_BYTES]

    b64_audio = base64.b64encode(audio_bytes).decode()

    prompt = (
        "Transcribe this audio accurately. "
        "Format your response as a JSON array of objects, each with: "
        '"start" (seconds, float), "end" (seconds, float), "text" (string). '
        "Keep each segment under 15 seconds. Output ONLY the JSON array, no explanation."
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "audio/mpeg", "data": b64_audio}},
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 8192, "responseMimeType": "application/json"},
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
                json=payload,
            )
        if response.status_code != 200:
            try:
                message = response.json().get("error", {}).get("message", "")
            except Exception:
                message = response.text
            raise RuntimeError(f"Gemini API error ({response.status_code}): {message[:500]}")
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        segments = json.loads(text)
        # Normalise field names
        result = []
        for seg in segments:
            result.append({
                "start": float(seg.get("start", 0)),
                "end": float(seg.get("end", 0)),
                "text": str(seg.get("text", "")),
            })
        return result if result else None
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Gemini transcription failed: {exc}") from exc

# ---------------------------------------------------------------------------
# Highlight scoring & clip building
# ---------------------------------------------------------------------------

ENERGY_WORDS = {
    "amazing", "incredible", "wow", "best", "worst", "never", "always",
    "secret", "important", "shocking", "huge", "big", "win", "lose",
    "finally", "actually", "seriously", "honestly", "literally", "crazy",
    "unbelievable", "awesome", "terrible", "love", "hate", "must", "need",
    "mistake", "problem", "truth", "reason", "simple", "fix", "proof",
}
HOOK_PHRASES = (
    "here is", "here's", "this is why", "the problem", "the biggest",
    "most people", "you need", "you should", "i learned", "i realized",
    "what happened", "why", "how to", "the truth", "the mistake",
)
PAYOFF_WORDS = {
    "because", "so", "therefore", "finally", "result", "fix", "solution",
    "answer", "that means", "the point", "takeaway", "works", "worked",
}

def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text.lower())

def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))

def _ends_cleanly(text: str) -> bool:
    return text.strip().endswith((".", "!", "?"))

def _starts_cleanly(text: str) -> bool:
    first = (_words(text) or [""])[0]
    return first not in {"and", "but", "so", "because", "then", "like"}

def _candidate_scores(text: str, duration: float) -> tuple[int, int, str]:
    words = _words(text)
    first_text = " ".join(words[:18])
    energy_hits = sum(1 for word in words if word in ENERGY_WORDS)
    has_question = "?" in text
    has_hook = any(phrase in first_text for phrase in HOOK_PHRASES) or has_question
    has_payoff = any(word in text.lower() for word in PAYOFF_WORDS) or _ends_cleanly(text)
    density = len(words) / duration if duration > 0 else 0

    hook_score = 22 if has_hook else 8
    energy_score = min(18, energy_hits * 4)
    density_score = 14 if 1.8 <= density <= 3.8 else 8 if 1.0 <= density <= 4.6 else 3
    duration_score = 18 if 25 <= duration <= 90 else 12 if 15 <= duration <= 180 else 7
    clean_score = 14 if _starts_cleanly(text) and _ends_cleanly(text) else 6
    payoff_score = 14 if has_payoff else 5

    virality = _clamp_score(hook_score + energy_score + density_score + duration_score + clean_score + payoff_score)
    completion = _clamp_score(
        (28 if _starts_cleanly(text) else 10)
        + (30 if _ends_cleanly(text) else 10)
        + (22 if has_payoff else 8)
        + (20 if len(words) >= 45 else 10)
    )
    hook_type = "question" if has_question else "useful" if has_hook else "story"
    return virality, completion, hook_type

def _sentence_groups(segments: list[dict]) -> list[dict]:
    groups = []
    current = None
    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
        if current is None:
            current = {"start": start, "end": end, "texts": [text]}
        else:
            gap = start - current["end"]
            current_duration = current["end"] - current["start"]
            if gap > 2.5 or current_duration > 14:
                groups.append({
                    "start": current["start"],
                    "end": current["end"],
                    "text": " ".join(current["texts"]).strip(),
                })
                current = {"start": start, "end": end, "texts": [text]}
            else:
                current["end"] = end
                current["texts"].append(text)
        if current and _ends_cleanly(text) and current["end"] - current["start"] >= 4:
            groups.append({
                "start": current["start"],
                "end": current["end"],
                "text": " ".join(current["texts"]).strip(),
            })
            current = None
    if current:
        groups.append({
            "start": current["start"],
            "end": current["end"],
            "text": " ".join(current["texts"]).strip(),
        })
    return groups

def build_scene_candidates(
    segments: list[dict],
    min_duration: float = 15,
    preferred_max_duration: float = 90,
    hard_max_duration: float = 300,
    limit: int = 24,
) -> list[dict]:
    groups = _sentence_groups(segments)
    candidates = []
    for start_idx in range(len(groups)):
        text_parts = []
        start = float(groups[start_idx]["start"])
        for end_idx in range(start_idx, len(groups)):
            end = float(groups[end_idx]["end"])
            duration = end - start
            if duration > hard_max_duration:
                break
            text_parts.append(groups[end_idx]["text"])
            if duration < min_duration:
                continue
            text = " ".join(text_parts).strip()
            if len(_words(text)) < 25:
                continue
            virality, completion, hook_type = _candidate_scores(text, duration)
            if duration > preferred_max_duration:
                virality -= min(20, int((duration - preferred_max_duration) / 12))
            pre_score = _clamp_score((virality * 0.65) + (completion * 0.35))
            candidates.append({
                "start": start,
                "end": end,
                "duration": duration,
                "text": text,
                "pre_score": pre_score,
                "virality_score": virality,
                "completion_score": completion,
                "hook_type": hook_type,
            })
    candidates.sort(key=lambda item: item["pre_score"], reverse=True)
    return _dedupe_clips(candidates, limit=limit, overlap_threshold=0.65)

def _overlap_ratio(a: dict, b: dict) -> float:
    start = max(float(a["start"]), float(b["start"]))
    end = min(float(a["end"]), float(b["end"]))
    if end <= start:
        return 0.0
    overlap = end - start
    shortest = min(float(a["end"]) - float(a["start"]), float(b["end"]) - float(b["start"]))
    return overlap / shortest if shortest > 0 else 0.0

def _dedupe_clips(clips: list[dict], limit: int, overlap_threshold: float = 0.5) -> list[dict]:
    selected = []
    for clip in clips:
        if any(_overlap_ratio(clip, kept) >= overlap_threshold for kept in selected):
            continue
        selected.append(clip)
        if len(selected) >= limit:
            break
    return selected

def rank_candidates_fallback(candidates: list[dict], target_count: int = 5) -> list[dict]:
    ranked = []
    for idx, candidate in enumerate(candidates, start=1):
        virality = int(candidate.get("virality_score") or candidate.get("pre_score") or 60)
        completion = int(candidate.get("completion_score") or 70)
        hook_type = candidate.get("hook_type") or "story"
        ranked.append({
            **candidate,
            "title": candidate.get("title") or f"Highlight {idx}",
            "virality_score": _clamp_score(virality),
            "completion_score": _clamp_score(completion),
            "hook_type": hook_type,
            "reason": candidate.get("reason") or "Selected for a clean scene, useful context, and strong pacing.",
        })
    ranked.sort(key=lambda item: (item["virality_score"] * 0.65 + item["completion_score"] * 0.35), reverse=True)
    return _dedupe_clips(ranked, limit=target_count, overlap_threshold=0.45)

def _extract_json_array(text: str) -> list:
    match = re.search(r"\[[\s\S]*\]", text or "")
    if not match:
        raise ValueError("No JSON array returned")
    return json.loads(match.group(0))

def rank_candidates_with_llm(candidates: list[dict], ai_model: str, target_count: int = 5) -> list[dict]:
    if not candidates:
        return []
    payload_candidates = [
        {
            "id": idx,
            "start": round(candidate["start"], 2),
            "end": round(candidate["end"], 2),
            "duration": round(candidate["duration"], 2),
            "text": candidate["text"][:1800],
            "pre_score": candidate["pre_score"],
            "completion_score": candidate["completion_score"],
        }
        for idx, candidate in enumerate(candidates[:20], start=1)
    ]
    prompt = (
        "You are a short-form video editor. Rank these candidate scenes and trim each selected scene "
        "to the shortest complete clip. Prefer 25-90 seconds, but allow up to 300 seconds only when "
        "needed for context or payoff. Choose clips that are self-contained, start cleanly, end cleanly, "
        "and have viral potential. Return ONLY a JSON object with a clips array. Each clip must contain: candidate_id, start, "
        "end, title, virality_score 0-100, completion_score 0-100, hook_type, reason. "
        f"Return at most {target_count} clips.\n\nCandidates:\n{json.dumps(payload_candidates)}"
    )
    try:
        if ai_model == "groq" and GROQ_API_KEY:
            with httpx.Client(timeout=90) as client:
                response = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": GROQ_LLM_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
            if response.status_code == 200:
                content = response.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                llm_items = data.get("clips") if isinstance(data, dict) else data
                return _merge_llm_rankings(candidates, llm_items, target_count)
        if ai_model == "gemini" and GEMINI_API_KEY:
            with httpx.Client(timeout=90) as client:
                response = client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": 4096, "responseMimeType": "application/json"},
                    },
            )
            if response.status_code == 200:
                text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    parsed = json.loads(text)
                    llm_items = parsed.get("clips") if isinstance(parsed, dict) else parsed
                except Exception:
                    llm_items = _extract_json_array(text)
                return _merge_llm_rankings(candidates, llm_items, target_count)
    except Exception:
        pass
    return rank_candidates_fallback(candidates, target_count=target_count)

def _merge_llm_rankings(candidates: list[dict], llm_items: list, target_count: int) -> list[dict]:
    by_id = {idx: candidate for idx, candidate in enumerate(candidates[:20], start=1)}
    clips = []
    for item in llm_items or []:
        try:
            candidate = by_id.get(int(item.get("candidate_id")))
            if not candidate:
                continue
            start = max(float(candidate["start"]), float(item.get("start", candidate["start"])))
            end = min(float(candidate["end"]), float(item.get("end", candidate["end"])))
            if end - start < 12:
                start, end = float(candidate["start"]), float(candidate["end"])
            clip = {
                **candidate,
                "start": start,
                "end": end,
                "duration": end - start,
                "title": str(item.get("title") or "Untitled highlight")[:120],
                "virality_score": _clamp_score(float(item.get("virality_score", candidate["virality_score"]))),
                "completion_score": _clamp_score(float(item.get("completion_score", candidate["completion_score"]))),
                "hook_type": str(item.get("hook_type") or candidate["hook_type"])[:40],
                "reason": str(item.get("reason") or "Selected by LLM ranking.")[:500],
            }
            clips.append(clip)
        except Exception:
            continue
    return rank_candidates_fallback(clips, target_count=target_count) if clips else rank_candidates_fallback(candidates, target_count=target_count)

def build_clips_from_segments(
    segments: list[dict],
    target_count: int = 5,
    min_duration: float = 20,
    max_duration: float = 300,
    ai_model: str = "openai",
) -> list[dict]:
    if not segments:
        return []
    candidates = build_scene_candidates(
        segments,
        min_duration=max(12, min_duration),
        hard_max_duration=max_duration,
        limit=max(20, target_count * 5),
    )
    return rank_candidates_with_llm(candidates, ai_model=ai_model, target_count=target_count)

def target_clip_count_for_duration(duration: float) -> int:
    if duration <= 90:
        return 1
    if duration <= 180:
        return 2
    if duration <= 360:
        return 3
    return 6

def complete_short_clip(segments: list[dict], duration: float) -> list[dict]:
    text = " ".join(seg.get("text", "").strip() for seg in segments if seg.get("text", "").strip())
    virality, completion, _ = _candidate_scores(text, duration) if text else (65, 90, "complete_short")
    return [{
        "start": 0,
        "end": duration,
        "duration": duration,
        "text": text,
        "title": "Complete short",
        "virality_score": virality,
        "completion_score": max(90, completion),
        "hook_type": "complete_short",
        "reason": "The source video is already short, so it is kept as one complete clip instead of being split.",
    }]

def select_clips_for_video(segments: list[dict], duration: float, ai_model: str) -> list[dict]:
    if duration <= 90:
        return complete_short_clip(segments, duration)
    target_count = target_clip_count_for_duration(duration)
    return build_clips_from_segments(segments, target_count=target_count, ai_model=ai_model)

def fallback_clips(duration: float, count: int = 5) -> list[dict]:
    if duration <= 90:
        return [{
            "start": 0,
            "end": duration,
            "text": "",
            "title": "Complete short",
            "virality_score": 50,
            "completion_score": 90,
            "hook_type": "complete_short",
            "reason": "The source video is already short, so fallback kept it as one complete clip.",
        }]
    clip_duration = 35.0
    skip_start = 60.0 if duration > 180 else 0.0
    usable = duration - skip_start - 30
    count = min(count, target_clip_count_for_duration(duration))
    if usable < clip_duration:
        count = max(1, int(usable // clip_duration)) or 1
    step = usable / count
    return [
        {
            "start": skip_start + i * step,
            "end": min(skip_start + i * step + clip_duration, duration - 5),
            "text": "",
            "title": f"Fallback clip {i + 1}",
            "virality_score": 50,
            "completion_score": 50,
            "hook_type": "fallback",
            "reason": "Generated from fallback spacing because transcript ranking was unavailable.",
        }
        for i in range(count)
    ]

# ---------------------------------------------------------------------------
# SRT generation
# ---------------------------------------------------------------------------

def seconds_to_srt_time(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

def generate_srt(segments: list[dict], clip_start: float, clip_end: float) -> str:
    lines = []
    idx = 1
    for seg in segments:
        ss = float(seg.get("start", 0))
        se = float(seg.get("end", ss))
        if se <= clip_start or ss >= clip_end:
            continue
        rs = max(0, ss - clip_start)
        re = min(clip_end - clip_start, se - clip_start)
        text = seg.get("text", "").strip()
        if not text:
            continue
        lines += [str(idx), f"{seconds_to_srt_time(rs)} --> {seconds_to_srt_time(re)}", text, ""]
        idx += 1
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Core processing pipeline
# ---------------------------------------------------------------------------

async def process_video(video_id: int, source_path: str):
    def _run():
        _process_video_sync(video_id, source_path)
    await asyncio.get_event_loop().run_in_executor(None, _run)

def _make_steps(*names: str) -> list[dict]:
    return [{"name": n, "status": "pending", "detail": ""} for n in names]

def _process_video_sync(video_id: int, source_path: str, source_started_as_download: bool = False):
    """Full pipeline with live step tracking: validate → audio → transcribe → score → export."""
    _, created_job = get_or_start_video_job(video_id)
    steps = _make_steps(
        "Validate video file",
        "Extract audio",
        "Transcribe audio",
        "Score & select highlights",
        "Export Short clips",
    )
    audio_path = ""

    def step(idx: int, status: str, detail: str = ""):
        steps[idx]["status"] = status
        steps[idx]["detail"] = detail
        update_steps(video_id, steps)

    try:
        db_write(
            "UPDATE videos SET status='processing', steps_json=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(steps), video_id),
        )

        # ── Step 0: Validate ────────────────────────────────────────────────
        step(0, "running")
        ensure_not_cancelled(video_id)
        missing_tools = missing_tools_message(REQUIRED_VIDEO_TOOLS)
        if missing_tools:
            step(0, "error", missing_tools)
            raise ValueError(missing_tools)
        if not is_valid_video(source_path):
            step(0, "error", "Not a valid video file.")
            raise ValueError("Uploaded file does not appear to be a valid video.")
        duration = get_video_duration(source_path)
        if not duration:
            step(0, "error", "Could not determine video duration.")
            raise ValueError("Could not determine video duration.")
        step(0, "done", f"{duration:.0f}s")
        ensure_not_cancelled(video_id)

        rows = db_read("SELECT youtube_video_id FROM videos WHERE id=?", (video_id,))
        yt_id = rows[0]["youtube_video_id"] if rows else str(video_id)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", yt_id)
        ai_model = effective_ai_model()
        segments: list[dict] = []
        audio_path = str(UPLOADS_DIR / f"{safe_id}_audio.mp3")

        # ── Step 1: Extract audio ────────────────────────────────────────────
        step(1, "running")
        audio_ok = extract_audio(source_path, audio_path, video_id=video_id)
        if audio_ok:
            step(1, "done")
        else:
            step(1, "error", "FFmpeg audio extraction failed — clips will have no subtitles")
        ensure_not_cancelled(video_id)

        # ── Step 2: Transcribe ───────────────────────────────────────────────
        if audio_ok:
            step(2, "running", "Trying local Whisper CLI…")
            segs = transcribe_with_whisper_local(audio_path, video_id=video_id)
            if segs:
                segments = segs
                step(2, "done", f"Local Whisper — {len(segments)} segments")
            elif ai_model == "groq":
                if not GROQ_API_KEY:
                    step(2, "error", "GROQ_API_KEY not configured — using fallback spacing")
                else:
                    step(2, "running", "Groq Whisper…")
                    loop = asyncio.new_event_loop()
                    try:
                        segs = loop.run_until_complete(transcribe_with_groq_api(audio_path))
                    except RuntimeError as exc:
                        segs = None
                        step(2, "error", str(exc))
                    finally:
                        loop.close()
                    if segs:
                        segments = segs
                        step(2, "done", f"Groq — {len(segments)} segments")
                    elif steps[2]["status"] != "error":
                        step(2, "error", "Groq returned no segments — using fallback spacing")
            elif ai_model == "gemini" and GEMINI_API_KEY:
                step(2, "running", "Gemini Flash…")
                loop = asyncio.new_event_loop()
                try:
                    segs = loop.run_until_complete(transcribe_with_gemini(audio_path))
                except RuntimeError as exc:
                    segs = None
                    step(2, "error", str(exc))
                finally:
                    loop.close()
                if segs:
                    segments = segs
                    step(2, "done", f"Gemini — {len(segments)} segments")
                elif steps[2]["status"] != "error":
                    step(2, "error", "Gemini returned no segments — using fallback spacing")
            elif OPENAI_API_KEY:
                step(2, "running", "OpenAI Whisper API…")
                loop = asyncio.new_event_loop()
                try:
                    segs = loop.run_until_complete(transcribe_with_openai_api(audio_path))
                finally:
                    loop.close()
                if segs:
                    segments = segs
                    step(2, "done", f"OpenAI — {len(segments)} segments")
                else:
                    step(2, "error", "API returned no segments — using fallback spacing")
            else:
                step(2, "error", "No transcription key configured — using fallback spacing")
        else:
            step(2, "error", "Skipped (audio extraction failed)")
        ensure_not_cancelled(video_id)

        # ── Step 3: Score & select ────────────────────────────────────────────
        step(3, "running")
        clips = select_clips_for_video(segments, duration=duration, ai_model=ai_model) if segments else []
        if not clips:
            clips = fallback_clips(duration)
            step(3, "done", f"Fallback — {len(clips)} evenly-spaced clips")
        else:
            avg_score = sum(int(c.get("virality_score", 0)) for c in clips) // max(1, len(clips))
            step(3, "done", f"AI ranking — {len(clips)} clips selected, avg virality {avg_score}%")
        clips = clips[:8]
        ensure_not_cancelled(video_id)

        # ── Step 4: Export ────────────────────────────────────────────────────
        step(4, "running", f"0 / {len(clips)} clips done")
        generated = 0
        for idx, clip in enumerate(clips, start=1):
            ensure_not_cancelled(video_id)
            start, end = clip["start"], clip["end"]
            clip_dur = end - start
            srt_content = generate_srt(segments, start, end) if segments else ""
            srt_path = None
            if srt_content.strip():
                srt_path = str(OUTPUTS_DIR / f"{safe_id}_short_{idx:02d}.srt")
                Path(srt_path).write_text(srt_content)
            out_filename = f"{safe_id}_short_{idx:02d}.mp4"
            if export_short_clip(source_path, str(OUTPUTS_DIR / out_filename), start, clip_dur, srt_path, video_id=video_id):
                db_write(
                    "INSERT INTO shorts "
                    "(video_id, filename, start_time, end_time, duration, caption_text, title, virality_score, completion_score, hook_type, selection_reason, status, original_start_time, original_end_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        video_id,
                        out_filename,
                        start,
                        end,
                        clip_dur,
                        clip.get("text", "")[:500],
                        clip.get("title", f"Highlight {idx}")[:120],
                        int(clip.get("virality_score", 0) or 0),
                        int(clip.get("completion_score", 0) or 0),
                        clip.get("hook_type", "")[:40],
                        clip.get("reason", "")[:500],
                        "draft",
                        start,
                        end,
                    ),
                )
                generated += 1
            step(4, "running", f"{idx} / {len(clips)} clips done")

        if generated == 0:
            step(4, "error", "No clips exported — check FFmpeg installation")
            raise ValueError("No shorts could be generated. Check FFmpeg installation.")

        step(4, "done", f"{generated} Short clips ready")
        db_write(
            "UPDATE videos SET status='completed', updated_at=datetime('now') WHERE id=?",
            (video_id,),
        )

    except CancelledProcessing:
        cleanup_cancelled_video(video_id, source_started_as_download=source_started_as_download)
    except Exception as exc:
        db_write(
            "UPDATE videos SET status='failed', error_message=?, updated_at=datetime('now') WHERE id=?",
            (str(exc), video_id),
        )
    finally:
        try:
            if audio_path:
                Path(audio_path).unlink(missing_ok=True)
        except Exception:
            pass
        if created_job:
            finish_video_job(video_id)


# ---------------------------------------------------------------------------
# yt-dlp download pipeline
# ---------------------------------------------------------------------------

def _download_and_process_sync(video_id: int):
    """Download from YouTube via yt-dlp, then run the full processing pipeline."""
    start_video_job(video_id)
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        finish_video_job(video_id)
        return
    video = rows[0]
    yt_id = video["youtube_video_id"]
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", yt_id)
    output_path = str(UPLOADS_DIR / f"{safe_id}.mp4")

    dl_steps = [{"name": "Download video from YouTube", "status": "running", "detail": "Starting yt-dlp…"}]
    db_write(
        "UPDATE videos SET status='downloading', steps_json=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(dl_steps), video_id),
    )

    missing_tool = missing_tools_message(("yt-dlp",))
    if missing_tool:
        dl_steps[0]["status"] = "error"
        dl_steps[0]["detail"] = missing_tool
        db_write(
            "UPDATE videos SET status='failed', error_message=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
            (missing_tool, json.dumps(dl_steps), video_id),
        )
        finish_video_job(video_id)
        return

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--newline",
        f"https://www.youtube.com/watch?v={yt_id}",
    ]
    try:
        result = run_command(cmd, timeout=1800, video_id=video_id)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "yt-dlp failed")[-600:]
            dl_steps[0]["status"] = "error"
            dl_steps[0]["detail"] = err
            db_write(
                "UPDATE videos SET status='failed', error_message=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
                (err, json.dumps(dl_steps), video_id),
            )
            finish_video_job(video_id)
            return
    except CancelledProcessing:
        cleanup_cancelled_video(video_id, source_started_as_download=True)
        finish_video_job(video_id)
        return
    except Exception as exc:
        msg = str(exc)
        dl_steps[0]["status"] = "error"
        dl_steps[0]["detail"] = msg
        db_write(
            "UPDATE videos SET status='failed', error_message=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
            (msg, json.dumps(dl_steps), video_id),
        )
        finish_video_job(video_id)
        return

    dl_steps[0]["status"] = "done"
    dl_steps[0]["detail"] = "Downloaded successfully"
    db_write(
        "UPDATE videos SET source_path=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
        (output_path, json.dumps(dl_steps), video_id),
    )
    # Continue into the full processing pipeline
    try:
        _process_video_sync(video_id, output_path, source_started_as_download=True)
    finally:
        finish_video_job(video_id)


async def download_and_process(video_id: int):
    await asyncio.get_event_loop().run_in_executor(None, _download_and_process_sync, video_id)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    videos = db_read("SELECT * FROM videos ORDER BY created_at DESC")
    for v in videos:
        v["shorts"] = db_read(
            "SELECT * FROM shorts WHERE video_id=? ORDER BY start_time", (v["id"],)
        )
    ch_id = effective_channel_id()
    return templates.TemplateResponse(
        request,
        "index.html",
        context={
            "videos": videos,
            "channel_id": ch_id or "Not configured",
            "ai_model": effective_ai_model(),
            "has_youtube_key": bool(YOUTUBE_API_KEY),
            "has_openai_key": bool(OPENAI_API_KEY),
            "has_gemini_key": bool(GEMINI_API_KEY),
            "has_groq_key": bool(GROQ_API_KEY),
        },
    )


@app.get("/settings")
async def api_get_settings():
    return {
        "channel_id": effective_channel_id(),
        "ai_model": effective_ai_model(),
    }


@app.post("/settings")
async def api_save_settings(request: Request):
    """Save channel ID and/or AI model preference."""
    body = await request.json()
    if "channel_id" in body:
        set_setting("channel_id", str(body["channel_id"]).strip())
    if "ai_model" in body and body["ai_model"] in ("openai", "gemini", "groq"):
        set_setting("ai_model", body["ai_model"])
    return {"ok": True, "channel_id": effective_channel_id(), "ai_model": effective_ai_model()}


@app.get("/channel-info")
async def api_channel_info():
    """Return metadata about the configured channel."""
    ch_id = effective_channel_id()
    if not ch_id:
        raise HTTPException(status_code=400, detail="No channel ID configured.")
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=400, detail="YOUTUBE_API_KEY not set.")
    try:
        info = await fetch_channel_info(ch_id)
        return info
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/channel-videos")
async def api_channel_videos():
    """Return the latest 10 videos from the configured channel (not stored)."""
    ch_id = effective_channel_id()
    if not ch_id:
        raise HTTPException(status_code=400, detail="No channel ID configured.")
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=400, detail="YOUTUBE_API_KEY not set.")
    try:
        uploads = await fetch_latest_uploads(ch_id, max_results=10)
        # Annotate with whether they're already in DB
        stored_ids = {r["youtube_video_id"] for r in db_read("SELECT youtube_video_id FROM videos")}
        for v in uploads:
            v["in_db"] = v["youtube_video_id"] in stored_ids
        return {"videos": uploads}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/check-youtube")
async def check_youtube():
    """Fetch latest uploads and save new ones to DB."""
    ch_id = effective_channel_id()
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=400, detail="YOUTUBE_API_KEY secret is not set.")
    if not ch_id:
        raise HTTPException(status_code=400, detail="No channel ID configured. Set it in Settings.")
    try:
        uploads = await fetch_latest_uploads(ch_id, max_results=10)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"YouTube API error: {exc}")

    added = 0
    for upload in uploads:
        if not db_read("SELECT id FROM videos WHERE youtube_video_id=?", (upload["youtube_video_id"],)):
            db_write(
                "INSERT INTO videos (youtube_video_id, title, published_at, thumbnail, status) VALUES (?,?,?,?,'detected')",
                (upload["youtube_video_id"], upload["title"], upload["published_at"], upload.get("thumbnail", "")),
            )
            added += 1
        else:
            # Update thumbnail if missing
            db_write(
                "UPDATE videos SET thumbnail=? WHERE youtube_video_id=? AND (thumbnail IS NULL OR thumbnail='')",
                (upload.get("thumbnail", ""), upload["youtube_video_id"]),
            )

    return {"detected": len(uploads), "new": added, "uploads": uploads}


@app.post("/upload-source/{video_id}")
async def upload_source(video_id: int, file: UploadFile = File(...)):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    video = rows[0]
    if video["status"] == "processing":
        raise HTTPException(status_code=409, detail="Video is already being processed.")

    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", video["youtube_video_id"])
    dest = UPLOADS_DIR / f"{safe_id}{ext}"
    dest.write_bytes(await file.read())

    missing_tools = missing_tools_message(("ffprobe",))
    if missing_tools:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=missing_tools)

    if not is_valid_video(str(dest)):
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid video.")

    db_write(
        "UPDATE videos SET source_path=?, status='waiting', updated_at=datetime('now') WHERE id=?",
        (str(dest), video_id),
    )
    return {"message": "Source file uploaded successfully.", "path": str(dest)}


@app.post("/download-yt/{video_id}")
async def download_yt_route(video_id: int, background_tasks: BackgroundTasks):
    """Download video from YouTube via yt-dlp, then process automatically."""
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    video = rows[0]
    if video["status"] in ("processing", "downloading"):
        raise HTTPException(status_code=409, detail="Already in progress.")
    background_tasks.add_task(download_and_process, video_id)
    return {"message": "Download started in background."}


@app.post("/process/{video_id}")
async def process_video_route(video_id: int, background_tasks: BackgroundTasks):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    video = rows[0]
    if not video["source_path"] or not Path(video["source_path"]).exists():
        raise HTTPException(status_code=400, detail="Source file not uploaded yet.")
    if video["status"] in ("processing", "downloading"):
        raise HTTPException(status_code=409, detail="Already in progress.")
    background_tasks.add_task(process_video, video_id, video["source_path"])
    return {"message": "Processing started in background."}

@app.post("/cancel/{video_id}")
async def cancel_video_route(video_id: int):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")

    video = rows[0]
    if video["status"] not in ("processing", "downloading"):
        raise HTTPException(status_code=409, detail="Video is not currently running.")

    request_video_cancel(video_id)
    cleanup_cancelled_video(video_id, source_started_as_download=video["status"] == "downloading")
    return {"message": "Processing cancelled and progress discarded."}


@app.get("/videos")
async def api_videos():
    videos = db_read("SELECT * FROM videos ORDER BY created_at DESC")
    for v in videos:
        v["shorts"] = db_read("SELECT * FROM shorts WHERE video_id=? ORDER BY start_time", (v["id"],))
    return {"videos": videos}


@app.get("/status/{video_id}")
async def api_status(video_id: int):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    v = rows[0]
    v["shorts"] = db_read("SELECT * FROM shorts WHERE video_id=? ORDER BY start_time", (v["id"],))
    try:
        v["steps"] = json.loads(v.get("steps_json") or "[]")
    except Exception:
        v["steps"] = []
    return v


@app.get("/download/{filename}")
async def download_short(filename: str):
    safe_name = Path(filename).name
    file_path = OUTPUTS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        str(file_path), media_type="video/mp4", filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.delete("/short/{short_id}")
async def delete_short_route(short_id: int):
    if not delete_short_record(short_id):
        raise HTTPException(status_code=404, detail="Short not found.")
    return {"message": "Short deleted."}


@app.delete("/video-source/{video_id}")
async def delete_source_video_route(video_id: int):
    if not delete_source_video(video_id):
        raise HTTPException(status_code=404, detail="Downloaded video not found.")
    return {"message": "Downloaded video deleted."}


# ---------------------------------------------------------------------------
# Short review / approve / reject / edit / regenerate  (Phase 1)
# ---------------------------------------------------------------------------

def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def caption_text_to_srt(text: str, duration: float) -> str:
    """Convert plain caption text to a simple SRT, evenly spread across duration."""
    words = text.split()
    if not words or duration <= 0:
        return ""
    chunk_size = 8
    chunks = [words[i : i + chunk_size] for i in range(0, len(words), chunk_size)]
    n = len(chunks)
    tpc = duration / n
    lines: list[str] = []
    for i, chunk in enumerate(chunks):
        start = i * tpc
        end = min((i + 1) * tpc, duration)
        lines += [str(i + 1), f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}", " ".join(chunk), ""]
    return "\n".join(lines)


@app.get("/review/{short_id}", response_class=HTMLResponse)
async def review_page(request: Request, short_id: int):
    rows = db_read("SELECT * FROM shorts WHERE id=?", (short_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Short not found.")
    short = rows[0]
    video_rows = db_read("SELECT * FROM videos WHERE id=?", (short["video_id"],))
    video = video_rows[0] if video_rows else {}
    return templates.TemplateResponse(
        request,
        "review.html",
        context={"short": short, "video": video},
    )


@app.get("/shorts/{short_id}")
async def get_short(short_id: int):
    rows = db_read("SELECT * FROM shorts WHERE id=?", (short_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Short not found.")
    return rows[0]


@app.post("/shorts/{short_id}/approve")
async def approve_short(short_id: int):
    if not db_read("SELECT id FROM shorts WHERE id=?", (short_id,)):
        raise HTTPException(status_code=404, detail="Short not found.")
    db_write(
        "UPDATE shorts SET status='approved', approved_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
        (short_id,),
    )
    return {"status": "approved"}


@app.post("/shorts/{short_id}/reject")
async def reject_short(short_id: int):
    if not db_read("SELECT id FROM shorts WHERE id=?", (short_id,)):
        raise HTTPException(status_code=404, detail="Short not found.")
    db_write(
        "UPDATE shorts SET status='rejected', updated_at=datetime('now') WHERE id=?",
        (short_id,),
    )
    return {"status": "rejected"}


class ShortUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    caption_text: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None


@app.patch("/shorts/{short_id}")
async def update_short(short_id: int, body: ShortUpdate):
    rows = db_read("SELECT * FROM shorts WHERE id=?", (short_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Short not found.")
    short = rows[0]
    fields: list[str] = []
    values: list = []
    if body.title is not None:
        fields.append("title=?"); values.append(body.title[:120])
    if body.description is not None:
        fields.append("description=?"); values.append(body.description[:1000])
    if body.caption_text is not None:
        fields.append("caption_text=?"); values.append(body.caption_text[:2000])
    if body.start_time is not None:
        new_start = max(0.0, body.start_time)
        fields.append("start_time=?"); values.append(new_start)
    if body.end_time is not None:
        fields.append("end_time=?"); values.append(body.end_time)
    # Recalculate duration if either time changed
    if body.start_time is not None or body.end_time is not None:
        s = body.start_time if body.start_time is not None else (short["start_time"] or 0)
        e = body.end_time if body.end_time is not None else (short["end_time"] or 0)
        fields.append("duration=?"); values.append(max(1.0, e - s))
    if not fields:
        return short
    fields.append("updated_at=datetime('now')")
    values.append(short_id)
    db_write(f"UPDATE shorts SET {', '.join(fields)} WHERE id=?", tuple(values))
    return db_read("SELECT * FROM shorts WHERE id=?", (short_id,))[0]


@app.get("/shorts/{short_id}/regen-status")
async def regen_status(short_id: int):
    rows = db_read("SELECT status, updated_at FROM shorts WHERE id=?", (short_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Short not found.")
    return rows[0]


@app.post("/shorts/{short_id}/regenerate")
async def regenerate_short(short_id: int, background_tasks: BackgroundTasks):
    rows = db_read("SELECT * FROM shorts WHERE id=?", (short_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Short not found.")
    short = rows[0]
    video_rows = db_read("SELECT source_path FROM videos WHERE id=?", (short["video_id"],))
    if not video_rows:
        raise HTTPException(status_code=404, detail="Source video not found.")
    source_path = video_rows[0].get("source_path", "")
    if not source_path or not Path(source_path).exists():
        raise HTTPException(
            status_code=400,
            detail="Source video file not found. Please re-download or re-upload the video first.",
        )
    db_write(
        "UPDATE shorts SET status='regenerating', updated_at=datetime('now') WHERE id=?",
        (short_id,),
    )
    background_tasks.add_task(_do_regenerate, short_id, dict(short), source_path)
    return {"status": "regenerating"}


def _do_regenerate(short_id: int, short: dict, source_path: str) -> None:
    try:
        start = float(short.get("start_time") or 0)
        end = float(short.get("end_time") or start + 30)
        duration = max(1.0, end - start)
        caption_text = (short.get("caption_text") or "").strip()
        srt_path: Optional[str] = None
        if caption_text:
            srt_content = caption_text_to_srt(caption_text, duration)
            if srt_content.strip():
                srt_file = OUTPUTS_DIR / f"regen_{short_id}.srt"
                srt_file.write_text(srt_content)
                srt_path = str(srt_file)
        out_filename = short.get("filename") or f"regen_{short_id}.mp4"
        out_path = str(OUTPUTS_DIR / out_filename)
        ok = export_short_clip(source_path, out_path, start, duration, srt_path)
        if srt_path:
            try:
                Path(srt_path).unlink(missing_ok=True)
            except Exception:
                pass
        if ok:
            db_write(
                "UPDATE shorts SET status='draft', duration=?, updated_at=datetime('now') WHERE id=?",
                (duration, short_id),
            )
        else:
            db_write(
                "UPDATE shorts SET status='failed', updated_at=datetime('now') WHERE id=?",
                (short_id,),
            )
    except Exception:
        db_write(
            "UPDATE shorts SET status='failed', updated_at=datetime('now') WHERE id=?",
            (short_id,),
        )


@app.post("/webhooks/youtube")
async def youtube_webhook(request: Request):
    body = await request.body()
    sig_header = request.headers.get("X-Hub-Signature", "")
    if sig_header and WEBHOOK_SECRET:
        expected = "sha1=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=403, detail="Invalid webhook signature.")
    video_id_matches = re.findall(rb"<yt:videoId>([^<]+)</yt:videoId>", body)
    title_matches = re.findall(rb"<title>([^<]+)</title>", body)
    added = 0
    for i, vid_bytes in enumerate(video_id_matches):
        yt_vid_id = vid_bytes.decode().strip()
        title = title_matches[i + 1].decode().strip() if i + 1 < len(title_matches) else "Unknown"
        if not db_read("SELECT id FROM videos WHERE youtube_video_id=?", (yt_vid_id,)):
            db_write(
                "INSERT INTO videos (youtube_video_id, title, status) VALUES (?,?,'detected')",
                (yt_vid_id, title),
            )
            added += 1
    return {"received": True, "new_videos": added}


@app.get("/webhooks/youtube")
async def youtube_webhook_verify(request: Request):
    challenge = request.query_params.get("hub.challenge", "")
    mode = request.query_params.get("hub.mode", "")
    if mode in ("subscribe", "unsubscribe") and challenge:
        return HTMLResponse(content=challenge)
    raise HTTPException(status_code=400, detail="Invalid hub request.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)

