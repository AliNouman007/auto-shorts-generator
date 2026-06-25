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
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services import exporting, presets, shorts_director, youtube_upload
from services.clip_director import selection as clip_selection
from services.clip_director.llm import request_llm_episode_profile, request_llm_selection

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
OPENAI_LLM_MODEL = env_value("OPENAI_LLM_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = env_value("GEMINI_API_KEY")
GEMINI_MODEL = env_value("GEMINI_MODEL", "gemini-2.0-flash")
GROQ_API_KEY = env_value("GROQ_API_KEY")
GROQ_MODEL = env_value("GROQ_MODEL", "whisper-large-v3-turbo")
GROQ_LLM_MODEL = env_value("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
WEBHOOK_SECRET = env_value("WEBHOOK_SECRET")
PORT = env_int("PORT", 8000)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

REQUIRED_VIDEO_TOOLS = ("ffmpeg", "ffprobe")
CAPTION_MAX_LINE_LENGTH = 32
CAPTION_MAX_LINES = 3
CAPTION_MAX_SECONDS = 2.5
CAPTION_MAX_WORDS = 6
CAPTION_WORD_GAP_SECONDS = 0.9
CAPTION_LANGUAGE_HINT = env_value("CAPTION_LANGUAGE_HINT", "hi").lower()


def command_available(command: str) -> bool:
    return shutil.which(command) is not None


def missing_tools_message(commands: tuple[str, ...]) -> str:
    missing = [
        command for command in commands if not command_available(command)]
    if not missing:
        return ""
    return f"Missing required command(s): {', '.join(missing)}. Install them and restart the app."


def ytdlp_js_runtime_arg() -> str:
    """Return a yt-dlp --js-runtimes value for an installed runtime."""
    node_path = shutil.which("node")
    if node_path:
        return f"node:{node_path}"
    deno_path = shutil.which("deno")
    if deno_path:
        return f"deno:{deno_path}"
    return ""


def missing_ytdlp_runtime_message() -> str:
    if ytdlp_js_runtime_arg():
        return ""
    return (
        "Missing JavaScript runtime for yt-dlp. Install Node.js or Deno, then restart the app. "
        "YouTube extraction can fail with HTTP 403 without a JS runtime."
    )


def build_ytdlp_download_command(yt_id: str, output_path: str) -> list[str]:
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--newline",
    ]
    runtime = ytdlp_js_runtime_arg()
    if runtime:
        cmd.extend(["--js-runtimes", runtime])
    cmd.append(f"https://www.youtube.com/watch?v={yt_id}")
    return cmd

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


app = FastAPI(title="Auto Shorts Generator", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)),
          name="outputs_files")
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

        CREATE TABLE IF NOT EXISTS presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS platform_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            account_label TEXT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS short_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_id INTEGER NOT NULL REFERENCES shorts(id),
            platform TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            privacy_status TEXT DEFAULT 'private',
            platform_video_id TEXT,
            platform_url TEXT,
            error_message TEXT,
            scheduled_at TEXT,
            uploaded_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS short_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER NOT NULL REFERENCES short_uploads(id),
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            watch_time INTEGER DEFAULT 0,
            fetched_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    # Migrate: add columns for older DBs
    for col_sql in [
        "ALTER TABLE videos ADD COLUMN steps_json TEXT",
        "ALTER TABLE videos ADD COLUMN thumbnail TEXT",
        "ALTER TABLE videos ADD COLUMN description TEXT",
        "ALTER TABLE shorts ADD COLUMN title TEXT",
        "ALTER TABLE shorts ADD COLUMN virality_score INTEGER",
        "ALTER TABLE shorts ADD COLUMN completion_score INTEGER",
        "ALTER TABLE shorts ADD COLUMN hook_type TEXT",
        "ALTER TABLE shorts ADD COLUMN selection_reason TEXT",
        "ALTER TABLE shorts ADD COLUMN status TEXT DEFAULT 'draft'",
        "ALTER TABLE shorts ADD COLUMN description TEXT",
        "ALTER TABLE shorts ADD COLUMN original_start_time REAL",
        "ALTER TABLE shorts ADD COLUMN original_end_time REAL",
        "ALTER TABLE shorts ADD COLUMN approved_at TEXT",
        "ALTER TABLE shorts ADD COLUMN updated_at TEXT",
        "ALTER TABLE shorts ADD COLUMN upload_title TEXT",
        "ALTER TABLE shorts ADD COLUMN upload_description TEXT",
        "ALTER TABLE shorts ADD COLUMN scheduled_at TEXT",
        "ALTER TABLE shorts ADD COLUMN timestamp_engine TEXT",
        "ALTER TABLE shorts ADD COLUMN candidate_source TEXT",
        "ALTER TABLE shorts ADD COLUMN final_score REAL",
        "ALTER TABLE shorts ADD COLUMN score_details_json TEXT",
        "ALTER TABLE shorts ADD COLUMN judge_status TEXT",
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass
    existing_default = conn.execute(
        "SELECT id FROM presets WHERE is_default=1 LIMIT 1").fetchone()
    if not existing_default:
        conn.execute(
            "INSERT INTO presets (name, config_json, is_default) VALUES (?,?,1)",
            ("Default", json.dumps(presets.DEFAULT_PRESET_CONFIG)),
        )
        conn.commit()
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
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def db_read(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
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
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

    remaining = db_read(
        "SELECT id FROM shorts WHERE video_id=? LIMIT 1", (short["video_id"],))
    if not remaining and short["status"] == "completed":
        source_path = short.get("source_path") or ""
        next_status = "waiting" if source_path and Path(
            source_path).exists() else "detected"
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
    existing_short_files = {
        Path(row["filename"]).name
        for row in db_read("SELECT filename FROM shorts WHERE video_id=?", (video_id,))
        if row["filename"]
    }
    protected_outputs = set(existing_short_files)
    protected_outputs.update(
        f"{Path(filename).stem}.srt"
        for filename in existing_short_files
    )
    for partial in OUTPUTS_DIR.glob(f"{safe_id}_short_*"):
        if partial.suffix in (".mp4", ".srt") and partial.name not in protected_outputs:
            partial.unlink(missing_ok=True)

    source_path = video.get("source_path") or ""

    next_status = "detected"
    if source_path and Path(source_path).exists():
        next_status = "waiting"

    cancelled_steps = [{"name": "Cancelled",
                        "status": "error", "detail": "Current generation stopped. Existing completed shorts were kept."}]
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


def effective_youtube_client_id() -> str:
    return get_setting("youtube_client_id") or env_value("YOUTUBE_CLIENT_ID")


def effective_youtube_client_secret() -> str:
    return get_setting("youtube_client_secret") or env_value("YOUTUBE_CLIENT_SECRET")


def effective_youtube_redirect_uri() -> str:
    return get_setting("youtube_redirect_uri") or env_value("YOUTUBE_REDIRECT_URI")


SHORT_STATUSES = {"draft", "approved",
                  "rejected", "exported", "uploaded", "failed"}
UPLOAD_STATUSES = {"draft", "scheduled", "uploading", "uploaded", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_json_object(value: str, fallback: Optional[dict] = None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return fallback or {}
    return parsed if isinstance(parsed, dict) else (fallback or {})


def get_default_preset() -> dict:
    rows = db_read(
        "SELECT * FROM presets WHERE is_default=1 ORDER BY id LIMIT 1")
    if not rows:
        db_write(
            "INSERT INTO presets (name, config_json, is_default) VALUES (?,?,1)",
            ("Default", json.dumps(presets.DEFAULT_PRESET_CONFIG)),
        )
        rows = db_read(
            "SELECT * FROM presets WHERE is_default=1 ORDER BY id LIMIT 1")
    row = rows[0]
    row["config"] = presets.normalize_preset_config(
        parse_json_object(row.get("config_json", "")))
    return row


def list_presets() -> list[dict]:
    rows = db_read("SELECT * FROM presets ORDER BY is_default DESC, name")
    for row in rows:
        row["config"] = presets.normalize_preset_config(
            parse_json_object(row.get("config_json", "")))
    return rows


def create_preset(name: str, config: dict, is_default: bool = False) -> dict:
    clean = presets.normalize_preset_config(config)
    if is_default:
        db_write("UPDATE presets SET is_default=0")
    db_write(
        "INSERT INTO presets (name, config_json, is_default, updated_at) VALUES (?,?,?,datetime('now'))",
        ((name or "Custom").strip() or "Custom",
         json.dumps(clean), 1 if is_default else 0),
    )
    row = list_presets()[0] if is_default else db_read(
        "SELECT * FROM presets ORDER BY id DESC LIMIT 1")[0]
    row["config"] = presets.normalize_preset_config(
        parse_json_object(row.get("config_json", "")))
    return row


def set_default_preset(preset_id: int) -> dict:
    rows = db_read("SELECT * FROM presets WHERE id=?", (preset_id,))
    if not rows:
        raise ValueError("Preset not found.")
    db_write("UPDATE presets SET is_default=0")
    db_write(
        "UPDATE presets SET is_default=1, updated_at=datetime('now') WHERE id=?", (preset_id,))
    return get_default_preset()


def preset_config_for_export(preset: Optional[dict] = None) -> dict:
    if isinstance(preset, dict):
        return presets.normalize_preset_config(preset.get("config", preset))
    return get_default_preset()["config"]


def get_short_detail(short_id: int) -> dict:
    rows = db_read(
        "SELECT shorts.*, videos.youtube_video_id, videos.title AS source_title, videos.source_path "
        "FROM shorts JOIN videos ON videos.id=shorts.video_id WHERE shorts.id=?",
        (short_id,),
    )
    if not rows:
        raise ValueError("Short not found.")
    short = rows[0]
    uploads = db_read(
        "SELECT * FROM short_uploads WHERE short_id=? ORDER BY created_at DESC, id DESC",
        (short_id,),
    )
    short["uploads"] = uploads
    if uploads:
        analytics = db_read(
            "SELECT * FROM short_analytics WHERE upload_id=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (uploads[0]["id"],),
        )
        short["latest_analytics"] = analytics[0] if analytics else None
        short["latest_upload"] = uploads[0]
    else:
        short["latest_analytics"] = None
        short["latest_upload"] = None
    return short


def attach_latest_uploads(shorts: list[dict]) -> list[dict]:
    for short in shorts:
        uploads = db_read(
            "SELECT * FROM short_uploads WHERE short_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (short["id"],),
        )
        short["latest_upload"] = uploads[0] if uploads else None
    return shorts


def update_short_metadata(short_id: int, data: dict) -> dict:
    allowed = {
        "title",
        "description",
        "caption_text",
        "upload_title",
        "upload_description",
        "scheduled_at",
    }
    updates = []
    params = []
    for key in allowed:
        if key in data:
            updates.append(f"{key}=?")
            params.append(str(data.get(key) or ""))
    if "title" in data and "upload_title" not in data:
        updates.append("upload_title=?")
        params.append(str(data.get("title") or ""))
    if not updates:
        return get_short_detail(short_id)
    updates.append("updated_at=datetime('now')")
    params.append(short_id)
    db_write(
        f"UPDATE shorts SET {', '.join(updates)} WHERE id=?", tuple(params))
    return get_short_detail(short_id)


def update_short_status(short_id: int, status: str) -> dict:
    if status not in SHORT_STATUSES:
        raise ValueError("Invalid short status.")
    approved_at = "datetime('now')" if status == "approved" else "NULL"
    db_write(
        f"UPDATE shorts SET status=?, approved_at={approved_at}, updated_at=datetime('now') WHERE id=?",
        (status, short_id),
    )
    return get_short_detail(short_id)


def update_short_timing(short_id: int, start_time: float, end_time: float) -> dict:
    start = float(start_time)
    end = float(end_time)
    if start < 0 or end <= start or end - start < 1:
        raise ValueError(
            "End time must be at least 1 second after start time.")
    db_write(
        "UPDATE shorts SET start_time=?, end_time=?, duration=?, updated_at=datetime('now') WHERE id=?",
        (start, end, end - start, short_id),
    )
    return get_short_detail(short_id)


def regenerate_short(short_id: int, preset_config: Optional[dict] = None) -> dict:
    short = get_short_detail(short_id)
    source_path = short.get("source_path") or ""
    if not source_path or not Path(source_path).exists():
        raise ValueError("Source video file is missing.")
    start = float(short.get("start_time") or 0)
    end = float(short.get("end_time") or start)
    if end <= start:
        raise ValueError("Short timing is invalid.")
    duration = end - start
    config = preset_config_for_export(preset_config)
    filename = Path(short.get("filename") or f"short_{short_id}.mp4").name
    output_path = OUTPUTS_DIR / filename
    srt_path = None
    caption_text = (short.get("caption_text") or "").strip()
    if config.get("captions_enabled", True) and caption_text:
        srt_path = OUTPUTS_DIR / f"{Path(filename).stem}.srt"
        srt_path.write_text(
            generate_srt(
                [{"start": start, "end": end, "text": caption_text}],
                start,
                end,
                caption_mode="original",
            ),
            encoding="utf-8",
        )
    elif filename:
        (OUTPUTS_DIR / f"{Path(filename).stem}.srt").unlink(missing_ok=True)
    if not export_short_clip(source_path, str(output_path), start, duration, str(srt_path) if srt_path else None, preset=config):
        db_write(
            "UPDATE shorts SET status='failed', updated_at=datetime('now') WHERE id=?", (short_id,))
        raise ValueError("Short regeneration failed.")
    db_write(
        "UPDATE shorts SET filename=?, duration=?, updated_at=datetime('now') WHERE id=?",
        (filename, duration, short_id),
    )
    return get_short_detail(short_id)


def get_upload_queue(status_filter: str = "all") -> list[dict]:
    rows = db_read(
        "SELECT shorts.id AS short_id, shorts.title, shorts.upload_title, shorts.status AS short_status, "
        "shorts.filename, shorts.duration, shorts.scheduled_at, videos.title AS source_title, "
        "short_uploads.id AS upload_id, short_uploads.status AS upload_status, short_uploads.platform_url, "
        "short_uploads.error_message, short_uploads.platform_video_id "
        "FROM shorts "
        "JOIN videos ON videos.id=shorts.video_id "
        "LEFT JOIN short_uploads ON short_uploads.id = ("
        "SELECT id FROM short_uploads WHERE short_id=shorts.id ORDER BY created_at DESC, id DESC LIMIT 1"
        ") "
        "WHERE shorts.status IN ('approved','uploaded') OR short_uploads.id IS NOT NULL "
        "ORDER BY shorts.updated_at DESC, shorts.created_at DESC"
    )
    if status_filter and status_filter != "all":
        return [
            row for row in rows
            if row.get("upload_status") == status_filter or row.get("short_status") == status_filter
        ]
    return rows


def get_youtube_account() -> Optional[dict]:
    rows = db_read(
        "SELECT * FROM platform_accounts WHERE platform='youtube' ORDER BY updated_at DESC, id DESC LIMIT 1"
    )
    return rows[0] if rows else None


def is_youtube_token_expired() -> bool:
    """Check if the YouTube account token is expired or missing."""
    account = get_youtube_account()
    if not account:
        return True
    expires_at = account.get("expires_at")
    if not expires_at:
        return True
    try:
        expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= expires_dt - timedelta(minutes=5)
    except (ValueError, TypeError):
        return True


def is_youtube_connected() -> bool:
    """Check if YouTube is properly connected with valid (non-expired) tokens."""
    account = get_youtube_account()
    if not account:
        return False
    if not account.get("access_token"):
        return False
    return not is_youtube_token_expired()


def get_youtube_connection_status() -> dict:
    """
    Get detailed YouTube connection status.
    Returns:
        - configured: bool - OAuth credentials are set
        - connected: bool - Account is connected with valid tokens
        - expired: bool - Was connected but token expired
        - missing_credentials: list - Which OAuth fields are missing
        - account_label: str - Account display name if connected
    """
    status = {
        "configured": False,
        "connected": False,
        "expired": False,
        "missing_credentials": [],
        "account_label": None,
    }

    # Check OAuth configuration
    missing = []
    if not effective_youtube_client_id():
        missing.append("client_id")
    if not effective_youtube_client_secret():
        missing.append("client_secret")
    if not effective_youtube_redirect_uri():
        missing.append("redirect_uri")

    status["missing_credentials"] = missing
    status["configured"] = len(missing) == 0

    # Check connection status
    account = get_youtube_account()
    if account and account.get("access_token"):
        if is_youtube_token_expired():
            status["expired"] = True
            status["connected"] = False
        else:
            status["connected"] = True
            status["account_label"] = account.get("account_label") or "YouTube"
    else:
        status["connected"] = False

    return status


def disconnect_youtube() -> None:
    """Disconnects YouTube by removing stored tokens from database"""
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM platform_accounts WHERE platform = ?", ("youtube",))
        conn.commit()
    finally:
        conn.close()


def save_youtube_tokens(token_data: dict) -> dict:
    expires_in = int(token_data.get("expires_in") or 3600)
    expires_at = (datetime.now(timezone.utc) +
                  timedelta(seconds=expires_in)).replace(microsecond=0).isoformat()
    refresh_token = token_data.get("refresh_token") or ""
    access_token = token_data.get("access_token") or ""
    if not access_token:
        raise ValueError(
            "YouTube token response did not include an access token.")
    existing = get_youtube_account()
    if existing:
        db_write(
            "UPDATE platform_accounts SET access_token=?, refresh_token=COALESCE(NULLIF(?,''), refresh_token), "
            "expires_at=?, account_label=?, updated_at=datetime('now') WHERE id=?",
            (access_token, refresh_token,
             expires_at, "YouTube", existing["id"]),
        )
    else:
        db_write(
            "INSERT INTO platform_accounts (platform, account_label, access_token, refresh_token, expires_at) VALUES (?,?,?,?,?)",
            ("youtube", "YouTube", access_token, refresh_token, expires_at),
        )
    return get_youtube_account()


def youtube_oauth_url() -> str:
    client_id = effective_youtube_client_id()
    redirect_uri = effective_youtube_redirect_uri()
    if not client_id or not redirect_uri:
        raise RuntimeError(
            "YouTube OAuth client ID and redirect URI are required.")
    return youtube_upload.build_auth_url(client_id, redirect_uri)


def handle_youtube_callback(code: str) -> dict:
    client_id = effective_youtube_client_id()
    client_secret = effective_youtube_client_secret()
    redirect_uri = effective_youtube_redirect_uri()
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("YouTube OAuth secrets are not configured.")
    tokens = youtube_upload.exchange_code_for_tokens(
        client_id,
        client_secret,
        redirect_uri,
        code,
    )
    return save_youtube_tokens(tokens)


def create_youtube_upload(short_id: int, privacy_status: str = "private", scheduled_at: Optional[str] = None) -> dict:
    short = get_short_detail(short_id)
    if short.get("status") != "approved":
        raise ValueError("Only approved Shorts can be uploaded.")
    file_path = OUTPUTS_DIR / Path(short.get("filename") or "").name
    if not file_path.exists():
        raise ValueError("Generated Short file is missing.")
    db_write(
        "INSERT INTO short_uploads (short_id, platform, status, privacy_status, scheduled_at, updated_at) "
        "VALUES (?,?,?,?,?,datetime('now'))",
        (short_id, "youtube", "uploading",
         privacy_status or "private", scheduled_at),
    )
    upload_id = db_read(
        "SELECT id FROM short_uploads WHERE short_id=? ORDER BY id DESC LIMIT 1", (short_id,))[0]["id"]
    try:
        result = youtube_upload.upload_short_to_youtube(
            get_youtube_account(),
            str(file_path),
            short.get("upload_title") or short.get(
                "title") or "Untitled Short",
            short.get("upload_description") or short.get("description") or "",
            privacy_status or "private",
            scheduled_at,
        )
        db_write(
            "UPDATE short_uploads SET status='uploaded', platform_video_id=?, platform_url=?, "
            "error_message=NULL, uploaded_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (result.get("platform_video_id"),
             result.get("platform_url"), upload_id),
        )
        db_write(
            "UPDATE shorts SET status='uploaded', updated_at=datetime('now') WHERE id=?", (short_id,))
    except Exception as exc:
        db_write(
            "UPDATE short_uploads SET status='failed', error_message=?, updated_at=datetime('now') WHERE id=?",
            (str(exc), upload_id),
        )
    return db_read("SELECT * FROM short_uploads WHERE id=?", (upload_id,))[0]


def retry_upload(upload_id: int) -> dict:
    rows = db_read("SELECT * FROM short_uploads WHERE id=?", (upload_id,))
    if not rows:
        raise ValueError("Upload not found.")
    upload = rows[0]
    short = get_short_detail(upload["short_id"])
    file_path = OUTPUTS_DIR / Path(short.get("filename") or "").name
    if not file_path.exists():
        raise ValueError("Generated Short file is missing.")
    db_write("UPDATE short_uploads SET status='uploading', error_message=NULL, updated_at=datetime('now') WHERE id=?", (upload_id,))
    try:
        result = youtube_upload.upload_short_to_youtube(
            get_youtube_account(),
            str(file_path),
            short.get("upload_title") or short.get(
                "title") or "Untitled Short",
            short.get("upload_description") or short.get("description") or "",
            upload.get("privacy_status") or "private",
            upload.get("scheduled_at"),
        )
        db_write(
            "UPDATE short_uploads SET status='uploaded', platform_video_id=?, platform_url=?, "
            "error_message=NULL, uploaded_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (result.get("platform_video_id"),
             result.get("platform_url"), upload_id),
        )
        db_write("UPDATE shorts SET status='uploaded', updated_at=datetime('now') WHERE id=?",
                 (upload["short_id"],))
    except Exception as exc:
        db_write("UPDATE short_uploads SET status='failed', error_message=?, updated_at=datetime('now') WHERE id=?", (str(
            exc), upload_id))
    return db_read("SELECT * FROM short_uploads WHERE id=?", (upload_id,))[0]


def refresh_upload_analytics() -> int:
    account = get_youtube_account()
    uploads = db_read(
        "SELECT * FROM short_uploads WHERE status='uploaded' AND platform='youtube' AND platform_video_id IS NOT NULL"
    )
    refreshed = 0
    for upload in uploads:
        try:
            metrics = youtube_upload.fetch_youtube_analytics(
                account, upload["platform_video_id"])
            if metrics is None:
                # Video is deleted from YouTube! Reset this upload!
                db_write(
                    "UPDATE short_uploads SET status='failed', platform_video_id=NULL, platform_url=NULL, error_message=? WHERE id=?",
                    ("Video removed from YouTube.", upload["id"])
                )
                # Also reset the short's status to approved so we can re-upload
                db_write(
                    "UPDATE shorts SET status='approved' WHERE id=?",
                    (upload["short_id"],)
                )
                continue
        except Exception:
            continue
        db_write(
            "INSERT INTO short_analytics (upload_id, views, likes, comments, watch_time, fetched_at) VALUES (?,?,?,?,?,datetime('now'))",
            (
                upload["id"],
                int(metrics.get("views") or 0),
                int(metrics.get("likes") or 0),
                int(metrics.get("comments") or 0),
                int(metrics.get("watch_time") or 0),
            ),
        )
        refreshed += 1
    return refreshed

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
    Returns list of dicts: {youtube_video_id, title, description, published_at, thumbnail}.
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
                "description": snippet.get("description", ""),
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


def build_export_video_filter(srt_path: Optional[str] = None, preset: Optional[dict] = None) -> str:
    return exporting.build_export_video_filter(srt_path, preset_config_for_export(preset))


def export_short_clip(
    source_path: str,
    output_path: str,
    start: float,
    duration: float,
    srt_path: Optional[str] = None,
    video_id: Optional[int] = None,
    preset: Optional[dict] = None,
) -> bool:
    """Export a 9:16 Short while preserving the full source frame."""
    cmd = exporting.build_export_command(
        source_path,
        output_path,
        start,
        duration,
        srt_path,
        preset_config_for_export(preset),
    )
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


def build_whisper_transcription_data(model: str, caption_mode: str = "hinglish") -> dict:
    data = {
        "model": model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"],
    }
    if str(caption_mode or "").lower() != "original" and CAPTION_LANGUAGE_HINT:
        data["language"] = CAPTION_LANGUAGE_HINT
    return data


INSTRUCTION_LEAK_PHRASES = (
    "do not translate",
    "roman hinglish",
    "format your response",
    "output only the json",
    "transcribe this audio",
    "timestamp granularities",
    "preserve the speaker",
)


def is_transcription_instruction_leak(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return bool(lowered and any(phrase in lowered for phrase in INSTRUCTION_LEAK_PHRASES))


def _clean_transcription_words(words: list[dict] | None, offset_seconds: float = 0.0) -> list[dict]:
    cleaned = []
    for word in words or []:
        try:
            start = float(word.get("start", 0)) + offset_seconds
            end = float(word.get("end", start)) + offset_seconds
        except (TypeError, ValueError):
            continue
        text = str(word.get("word") or word.get("text") or "").strip()
        if not text:
            continue
        cleaned.append({"start": start, "end": max(start, end), "word": text})
    return cleaned


def _words_overlapping_segment(words: list[dict], start: float, end: float) -> list[dict]:
    return [
        word for word in words
        if float(word.get("end", word.get("start", 0))) > start - 0.05
        and float(word.get("start", 0)) < end + 0.05
    ]


def _segments_from_words(words: list[dict]) -> list[dict]:
    segments = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            duration = float(current[-1]["end"]) - float(current[0]["start"])
            if gap > 1.0 or duration >= 12.0 or len(current) >= 18:
                text = " ".join(item["word"] for item in current).strip()
                if text and not is_transcription_instruction_leak(text):
                    segments.append({
                        "start": float(current[0]["start"]),
                        "end": float(current[-1]["end"]),
                        "text": text,
                        "words": current,
                    })
                current = []
        current.append(word)
    if current:
        text = " ".join(item["word"] for item in current).strip()
        if text and not is_transcription_instruction_leak(text):
            segments.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": text,
                "words": current,
            })
    return segments


def segments_from_transcription_response(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    raw_segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
    top_level_words = _clean_transcription_words(
        payload.get("words") if isinstance(payload.get("words"), list) else []
    )
    if not raw_segments:
        return _segments_from_words(top_level_words)

    segments = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        try:
            start = max(0.0, float(segment.get("start", 0)))
            end = max(start, float(segment.get("end", start)))
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text", "")).strip()
        if not text or is_transcription_instruction_leak(text):
            continue
        segment_words = _clean_transcription_words(
            segment.get("words") if isinstance(segment.get("words"), list) else []
        )
        if not segment_words and top_level_words:
            segment_words = _words_overlapping_segment(top_level_words, start, end)
        clean_segment = {**segment, "start": start, "end": end, "text": text}
        if segment_words:
            clean_segment["words"] = segment_words
        segments.append(clean_segment)
    return segments


def build_gemini_transcription_prompt(caption_mode: str = "hinglish") -> str:
    if str(caption_mode or "").lower() == "original":
        script_instruction = (
            "Keep the original language and script chosen by the transcription model. "
            "Do not romanize the text. "
        )
    else:
        script_instruction = (
            "For Hindi, Urdu, Punjabi, or mixed Hindi-English speech, write the original spoken words in Roman Hinglish. "
            "Keep English words as English. "
        )
    return (
        "Transcribe this audio accurately and preserve the speaker's original words. "
        "Do not translate to English, rewrite, or summarize the speech. "
        f"{script_instruction}"
        "Format your response as a JSON array of objects, each with: "
        '"start" (seconds, float), "end" (seconds, float), "text" (string). '
        "Keep each segment under 15 seconds. Output ONLY the JSON array, no explanation."
    )


def captions_enabled_from_body(body: dict) -> bool:
    return body.get("captions_enabled", True) is not False


class GroqRequestTooLarge(RuntimeError):
    pass


def audio_file_size_bytes(audio_path: str) -> int:
    try:
        return Path(audio_path).stat().st_size
    except OSError:
        return 0


def should_chunk_groq_audio(audio_path: str, limit_bytes: int = 24 * 1024 * 1024) -> bool:
    return audio_file_size_bytes(audio_path) > limit_bytes


def get_audio_duration(audio_path: str) -> float:
    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            timeout=60,
        )
        if result.returncode != 0:
            return 0.0
        return max(0.0, float((result.stdout or "0").strip() or 0))
    except Exception:
        return 0.0


def build_audio_chunk_path(audio_path: str, index: int) -> str:
    path = Path(audio_path)
    return str(path.with_name(f"{path.stem}_chunk_{index:03d}{path.suffix}"))


def split_audio_for_transcription(
    audio_path: str,
    chunk_seconds: int = 600,
    video_id: Optional[int] = None,
) -> list[dict]:
    duration = get_audio_duration(audio_path)
    if duration <= 0:
        return [{"path": audio_path, "offset": 0.0}]
    chunks = []
    offset = 0.0
    index = 1
    while offset < duration:
        chunk_path = build_audio_chunk_path(audio_path, index)
        chunk_duration = min(float(chunk_seconds), duration - offset)
        result = run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{offset:g}",
                "-i",
                audio_path,
                "-t",
                f"{chunk_duration:g}",
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-q:a",
                "0",
                chunk_path,
            ],
            timeout=300,
            video_id=video_id,
        )
        if result.returncode != 0:
            raise RuntimeError("FFmpeg audio chunking failed.")
        chunks.append({"path": chunk_path, "offset": float(offset)})
        offset += float(chunk_seconds)
        index += 1
    return chunks


def offset_transcription_segments(segments: list[dict], offset_seconds: float) -> list[dict]:
    shifted = []
    for segment in segments or []:
        try:
            start = float(segment.get("start", 0)) + float(offset_seconds)
            end = float(segment.get("end", start)) + float(offset_seconds)
        except (TypeError, ValueError):
            continue
        shifted.append({
            **segment,
            "start": start,
            "end": end,
            "text": str(segment.get("text", "")).strip(),
            **({
                "words": _clean_transcription_words(
                    segment.get("words") if isinstance(segment.get("words"), list) else [],
                    offset_seconds=float(offset_seconds),
                )
            } if isinstance(segment.get("words"), list) else {}),
        })
    return shifted


def merge_transcription_chunks(chunk_results: list[list[dict]]) -> list[dict]:
    merged = []
    for segments in chunk_results:
        merged.extend(segments or [])
    return sorted(merged, key=lambda segment: float(segment.get("start", 0)))


def transcribe_with_whisper_local(audio_path: str, video_id: Optional[int] = None) -> Optional[list[dict]]:
    """Try the local openai-whisper CLI. Returns segments list or None."""
    try:
        out_dir = Path(audio_path).parent
        command = [
            "whisper",
            audio_path,
            "--task",
            "transcribe",
            "--output_format",
            "json",
            "--output_dir",
            str(out_dir),
        ]
        if CAPTION_LANGUAGE_HINT:
            command.extend(["--language", CAPTION_LANGUAGE_HINT])
        result = run_command(
            command,
            timeout=300,
            video_id=video_id,
        )
        if result.returncode != 0:
            return None
        json_path = out_dir / (Path(audio_path).stem + ".json")
        if json_path.exists():
            data = json.loads(json_path.read_text())
            return segments_from_transcription_response(data)
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
                    data=build_whisper_transcription_data("whisper-1"),
                )
        if response.status_code == 200:
            return segments_from_transcription_response(response.json())
    except Exception:
        pass
    return None


async def transcribe_groq_audio_file(audio_path: str) -> list[dict]:
    if not GROQ_API_KEY:
        return []

    async with httpx.AsyncClient(timeout=120) as client:
        with open(audio_path, "rb") as f:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (Path(audio_path).name, f, "audio/mpeg")},
                data={
                    **build_whisper_transcription_data(GROQ_MODEL),
                    "temperature": "0",
                },
            )
    if response.status_code != 200:
        try:
            message = response.json().get("error", {}).get("message", "")
        except Exception:
            message = response.text
        error_message = f"Groq API error ({response.status_code}): {message[:500]}"
        if response.status_code == 413:
            raise GroqRequestTooLarge(error_message)
        raise RuntimeError(error_message)
    return segments_from_transcription_response(response.json())


async def transcribe_groq_audio_chunks(
    audio_path: str,
    chunk_seconds: int = 600,
    video_id: Optional[int] = None,
) -> list[dict]:
    chunks = split_audio_for_transcription(
        audio_path,
        chunk_seconds=chunk_seconds,
        video_id=video_id,
    )
    chunk_results = []
    try:
        for chunk in chunks:
            chunk_path = str(chunk["path"])
            offset = float(chunk.get("offset", 0))
            segments = await transcribe_groq_audio_file(chunk_path)
            chunk_results.append(offset_transcription_segments(segments, offset))
    finally:
        for chunk in chunks:
            chunk_path = str(chunk["path"])
            if chunk_path != audio_path:
                Path(chunk_path).unlink(missing_ok=True)
    return merge_transcription_chunks(chunk_results)


async def transcribe_with_groq_api(audio_path: str, video_id: Optional[int] = None) -> Optional[list[dict]]:
    """Transcribe via Groq's OpenAI-compatible Whisper endpoint."""
    if not GROQ_API_KEY:
        return None

    try:
        if should_chunk_groq_audio(audio_path):
            return await transcribe_groq_audio_chunks(audio_path, video_id=video_id)
        try:
            return await transcribe_groq_audio_file(audio_path)
        except GroqRequestTooLarge:
            return await transcribe_groq_audio_chunks(audio_path, video_id=video_id)
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

    prompt = build_gemini_transcription_prompt()

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
            raise RuntimeError(
                f"Gemini API error ({response.status_code}): {message[:500]}")
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
    has_hook = any(
        phrase in first_text for phrase in HOOK_PHRASES) or has_question
    has_payoff = any(word in text.lower()
                     for word in PAYOFF_WORDS) or _ends_cleanly(text)
    density = len(words) / duration if duration > 0 else 0

    hook_score = 22 if has_hook else 8
    energy_score = min(18, energy_hits * 4)
    density_score = 14 if 1.8 <= density <= 3.8 else 8 if 1.0 <= density <= 4.6 else 3
    duration_score = 18 if 25 <= duration <= 90 else 12 if 15 <= duration <= 180 else 7
    clean_score = 14 if _starts_cleanly(text) and _ends_cleanly(text) else 6
    payoff_score = 14 if has_payoff else 5

    virality = _clamp_score(hook_score + energy_score +
                            density_score + duration_score + clean_score + payoff_score)
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
    duration = max((float(seg.get("end", 0)) for seg in segments), default=0)
    return shorts_director.build_director_candidates(
        segments,
        duration=duration,
        preset={
            "min_clip_duration": min_duration,
            "preferred_max_clip_duration": preferred_max_duration,
            "hard_max_clip_duration": hard_max_duration,
            "allow_three_minute_shorts": hard_max_duration > 180,
        },
        limit=limit,
    )


def _overlap_ratio(a: dict, b: dict) -> float:
    start = max(float(a["start"]), float(b["start"]))
    end = min(float(a["end"]), float(b["end"]))
    if end <= start:
        return 0.0
    overlap = end - start
    shortest = min(float(a["end"]) - float(a["start"]),
                   float(b["end"]) - float(b["start"]))
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
    return shorts_director.rank_candidates_fallback(candidates, target_count=target_count)


def _extract_json_array(text: str) -> list:
    match = re.search(r"\[[\s\S]*\]", text or "")
    if not match:
        raise ValueError("No JSON array returned")
    return json.loads(match.group(0))


def rank_candidates_with_llm(candidates: list[dict], ai_model: str, target_count: int = 5) -> list[dict]:
    if not candidates:
        return []
    prompt = shorts_director.build_director_prompt(candidates, target_count=target_count)
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
                llm_items = data.get("clips") if isinstance(
                    data, dict) else data
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
                text = response.json()[
                    "candidates"][0]["content"]["parts"][0]["text"]
                try:
                    parsed = json.loads(text)
                    llm_items = parsed.get("clips") if isinstance(
                        parsed, dict) else parsed
                except Exception:
                    llm_items = _extract_json_array(text)
                return _merge_llm_rankings(candidates, llm_items, target_count)
    except Exception:
        pass
    return rank_candidates_fallback(candidates, target_count=target_count)


def _merge_llm_rankings(candidates: list[dict], llm_items: list, target_count: int) -> list[dict]:
    return shorts_director.merge_llm_rankings(
        candidates,
        llm_items,
        target_count=target_count,
        preset=preset_config_for_export(),
    )


def request_dynamic_clip_selection(episode_map: dict, constraints: dict, ai_model: str) -> list[dict]:
    return request_llm_selection(
        episode_map,
        constraints,
        ai_model,
        openai_api_key=OPENAI_API_KEY,
        openai_model=OPENAI_LLM_MODEL,
        groq_api_key=GROQ_API_KEY,
        groq_model=GROQ_LLM_MODEL,
        gemini_api_key=GEMINI_API_KEY,
        gemini_model=GEMINI_MODEL,
    )


def request_dynamic_episode_profile(seed: dict, ai_model: str) -> dict:
    return request_llm_episode_profile(
        seed,
        ai_model,
        openai_api_key=OPENAI_API_KEY,
        openai_model=OPENAI_LLM_MODEL,
        groq_api_key=GROQ_API_KEY,
        groq_model=GROQ_LLM_MODEL,
        gemini_api_key=GEMINI_API_KEY,
        gemini_model=GEMINI_MODEL,
    )


def build_clips_from_segments(
    segments: list[dict],
    target_count: int = 5,
    min_duration: float = 20,
    max_duration: float = 300,
    ai_model: str = "openai",
) -> list[dict]:
    if not segments:
        return []
    source_duration = max((float(seg.get("end", 0)) for seg in segments), default=0)
    mode = "highlights" if max_duration > 180 else "shorts"
    clips = shorts_director.select_dynamic_clips(
        segments,
        source_duration,
        ai_model=ai_model,
        mode=mode,
        llm_selector=request_dynamic_clip_selection,
    )
    return clips[:target_count] if target_count else clips


def target_clip_count_for_duration(duration: float) -> int:
    if duration <= 90:
        return 1
    if duration <= 180:
        return 2
    if duration <= 360:
        return 3
    return 6


def complete_short_clip(segments: list[dict], duration: float) -> list[dict]:
    if clip_selection.viral_timestamp_engine_v2_enabled():
        return clip_selection.complete_short_clip_v2(segments, duration)
    return shorts_director.complete_short_clip(segments, duration)


def select_clips_for_video(
    segments: list[dict],
    duration: float,
    ai_model: str,
    *,
    audio_path: str | None = None,
    video_title: str = "",
    video_description: str = "",
    preset: Optional[dict] = None,
) -> list[dict]:
    if duration <= 90:
        return complete_short_clip(segments, duration)
    config = preset_config_for_export(preset)
    selected = shorts_director.select_dynamic_clips(
        segments,
        duration,
        audio_path=audio_path,
        video_title=video_title,
        video_description=video_description,
        mode=config.get("clip_output_mode", "shorts"),
        genre_hint=config.get("genre_hint", ""),
        ai_model=ai_model,
        llm_selector=request_dynamic_clip_selection,
        episode_profile_builder=request_dynamic_episode_profile,
        allow_fallback=False,
    )
    if clip_selection.viral_timestamp_engine_v2_enabled():
        return selected
    return selected or shorts_director.select_director_clips(
        segments,
        duration=duration,
        ai_model=ai_model,
        preset=config,
    )


def fallback_clips(duration: float, count: int = 5) -> list[dict]:
    return shorts_director.fallback_clips(duration, count=count)

# ---------------------------------------------------------------------------
# SRT generation
# ---------------------------------------------------------------------------


def seconds_to_srt_time(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def srt_time_to_seconds(value: str) -> float:
    hours, minutes, rest = str(value).split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000.0
    )


DEVANAGARI_ROMAN_WORDS = {
    "तुम": "tum",
    "कैसे": "kaise",
    "केसे": "kaise",
    "हो": "ho",
    "है": "hai",
    "हैं": "hain",
    "मैं": "main",
    "मे": "me",
    "मुझे": "mujhe",
    "क्या": "kya",
    "क्यों": "kyun",
    "नहीं": "nahi",
    "हाँ": "haan",
}

URDU_ROMAN_WORDS = {
    "تم": "tum",
    "کیسے": "kaise",
    "ہو": "ho",
    "ہے": "hai",
    "ہیں": "hain",
    "میں": "main",
    "مجھے": "mujhe",
    "کیا": "kya",
    "کیوں": "kyun",
    "نہیں": "nahi",
    "ہاں": "haan",
}

DEVANAGARI_ROMAN_CHARS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "च": "ch", "छ": "chh",
    "ज": "j", "झ": "jh", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p",
    "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
    "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "ा": "a", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "े": "e",
    "ै": "ai", "ो": "o", "ौ": "au", "ं": "n", "ँ": "n", "़": "",
    "्": "", "।": ".", "॥": ".",
}

URDU_ROMAN_CHARS = {
    "ا": "a", "آ": "aa", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t",
    "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d",
    "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh",
    "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z",
    "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "ں": "n", "و": "o", "ہ": "h",
    "ھ": "h", "ء": "", "ی": "i", "ے": "e", "َ": "", "ُ": "", "ِ": "",
}


def romanize_caption_text(text: str) -> str:
    romanized = str(text or "")
    for source, replacement in {**DEVANAGARI_ROMAN_WORDS, **URDU_ROMAN_WORDS}.items():
        romanized = romanized.replace(source, replacement)

    output = []
    for char in romanized:
        if "\u0900" <= char <= "\u097f":
            output.append(DEVANAGARI_ROMAN_CHARS.get(char, " "))
        elif "\u0600" <= char <= "\u06ff":
            output.append(URDU_ROMAN_CHARS.get(char, " "))
        else:
            output.append(char)
    return re.sub(r"\s+", " ", "".join(output)).strip()


def normalize_caption_text(text: str, caption_mode: str = "hinglish") -> str:
    cleaned = " ".join(str(text or "").split())
    if str(caption_mode or "").lower() == "original":
        return cleaned
    return romanize_caption_text(cleaned)


def wrap_caption_text(text: str) -> list[list[str]]:
    lines = textwrap.wrap(
        " ".join(str(text or "").split()),
        width=CAPTION_MAX_LINE_LENGTH,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return [
        lines[i:i + CAPTION_MAX_LINES]
        for i in range(0, len(lines), CAPTION_MAX_LINES)
    ]


def split_caption_text_units(text: str, duration: float) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    word_unit_count = (len(words) + CAPTION_MAX_WORDS - 1) // CAPTION_MAX_WORDS
    if word_unit_count <= 1:
        return [" ".join(words)]
    unit_count = int(max(
        1,
        int((max(0.1, duration) + CAPTION_MAX_SECONDS - 0.001) // CAPTION_MAX_SECONDS),
        word_unit_count,
    ))
    unit_count = min(unit_count, len(words))
    units = []
    for index in range(unit_count):
        start = round(index * len(words) / unit_count)
        end = round((index + 1) * len(words) / unit_count)
        chunk = " ".join(words[start:end]).strip()
        if chunk:
            units.append(chunk)
    return units


def _caption_units_from_word_timestamps(
    segment: dict,
    clip_start: float,
    clip_end: float,
    caption_mode: str,
) -> list[dict]:
    words = []
    for raw_word in segment.get("words") or []:
        if not isinstance(raw_word, dict):
            continue
        try:
            start = float(raw_word.get("start", 0))
            end = float(raw_word.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= clip_start or start >= clip_end:
            continue
        text = normalize_caption_text(
            str(raw_word.get("word") or raw_word.get("text") or ""),
            caption_mode=caption_mode,
        )
        if not text or is_transcription_instruction_leak(text):
            continue
        words.append({
            "start": max(0.0, start - clip_start),
            "end": max(0.0, min(clip_end, end) - clip_start),
            "word": text,
        })
    if not words:
        return []

    units = []
    current: list[dict] = []

    def flush_current() -> None:
        if not current:
            return
        text = " ".join(word["word"] for word in current).strip()
        if not text or is_transcription_instruction_leak(text):
            return
        start = float(current[0]["start"])
        end = max(float(current[-1]["end"]), start + 0.35)
        units.append({"start": start, "end": end, "text": text})

    for word in words:
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            duration = float(current[-1]["end"]) - float(current[0]["start"])
            if (
                gap > CAPTION_WORD_GAP_SECONDS
                or len(current) >= CAPTION_MAX_WORDS
                or duration >= CAPTION_MAX_SECONDS
            ):
                flush_current()
                current = []
        current.append(word)
    flush_current()
    return units


def generate_srt(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    caption_mode: str = "hinglish",
) -> str:
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
        if is_transcription_instruction_leak(text):
            continue
        word_units = _caption_units_from_word_timestamps(
            seg,
            clip_start,
            clip_end,
            caption_mode,
        )
        if word_units:
            for unit in word_units:
                for caption_lines in wrap_caption_text(unit["text"]):
                    lines += [
                        str(idx),
                        f"{seconds_to_srt_time(unit['start'])} --> {seconds_to_srt_time(unit['end'])}",
                        *caption_lines,
                        "",
                    ]
                    idx += 1
            continue
        span = max(0.1, re - rs)
        caption_units = split_caption_text_units(
            normalize_caption_text(text, caption_mode=caption_mode),
            span,
        )
        if not caption_units:
            continue
        for unit_idx, caption_unit in enumerate(caption_units):
            unit_start = rs + (span * unit_idx / len(caption_units))
            unit_end = rs + (span * (unit_idx + 1) / len(caption_units))
            for caption_lines in wrap_caption_text(caption_unit):
                lines += [
                    str(idx),
                    f"{seconds_to_srt_time(unit_start)} --> {seconds_to_srt_time(unit_end)}",
                    *caption_lines,
                    "",
                ]
                idx += 1
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Core processing pipeline
# ---------------------------------------------------------------------------


async def process_video(video_id: int, source_path: str, captions_enabled: bool = True):
    def _run():
        _process_video_sync(video_id, source_path,
                            captions_enabled=captions_enabled)
    await asyncio.get_event_loop().run_in_executor(None, _run)


def _make_steps(*names: str) -> list[dict]:
    return [{"name": n, "status": "pending", "detail": ""} for n in names]


def _process_video_sync(
    video_id: int,
    source_path: str,
    source_started_as_download: bool = False,
    captions_enabled: bool = True,
):
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
            "UPDATE videos SET status='processing', error_message=NULL, steps_json=?, updated_at=datetime('now') WHERE id=?",
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
            raise ValueError(
                "Uploaded file does not appear to be a valid video.")
        duration = get_video_duration(source_path)
        if not duration:
            step(0, "error", "Could not determine video duration.")
            raise ValueError("Could not determine video duration.")
        step(0, "done", f"{duration:.0f}s")
        ensure_not_cancelled(video_id)

        rows = db_read(
            "SELECT youtube_video_id, title, description FROM videos WHERE id=?", (video_id,))
        yt_id = rows[0]["youtube_video_id"] if rows else str(video_id)
        source_title = rows[0]["title"] if rows else ""
        source_description = rows[0]["description"] if rows else ""
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
            step(
                1, "error", "FFmpeg audio extraction failed — clips will have no subtitles")
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
                    step(
                        2, "error", "GROQ_API_KEY not configured — using fallback spacing")
                else:
                    groq_detail = "Groq Whisper chunked..." if should_chunk_groq_audio(audio_path) else "Groq Whisper..."
                    step(2, "running", groq_detail)
                    loop = asyncio.new_event_loop()
                    try:
                        segs = loop.run_until_complete(
                            transcribe_with_groq_api(audio_path, video_id=video_id))
                    except RuntimeError as exc:
                        segs = None
                        step(2, "error", str(exc))
                    finally:
                        loop.close()
                    if segs:
                        segments = segs
                        step(2, "done", f"Groq — {len(segments)} segments")
                    elif steps[2]["status"] != "error":
                        step(
                            2, "error", "Groq returned no segments — using fallback spacing")
            elif ai_model == "gemini" and GEMINI_API_KEY:
                step(2, "running", "Gemini Flash…")
                loop = asyncio.new_event_loop()
                try:
                    segs = loop.run_until_complete(
                        transcribe_with_gemini(audio_path))
                except RuntimeError as exc:
                    segs = None
                    step(2, "error", str(exc))
                finally:
                    loop.close()
                if segs:
                    segments = segs
                    step(2, "done", f"Gemini — {len(segments)} segments")
                elif steps[2]["status"] != "error":
                    step(
                        2, "error", "Gemini returned no segments — using fallback spacing")
            elif OPENAI_API_KEY:
                step(2, "running", "OpenAI Whisper API…")
                loop = asyncio.new_event_loop()
                try:
                    segs = loop.run_until_complete(
                        transcribe_with_openai_api(audio_path))
                finally:
                    loop.close()
                if segs:
                    segments = segs
                    step(2, "done", f"OpenAI — {len(segments)} segments")
                else:
                    step(2, "error", "API returned no segments — using fallback spacing")
            else:
                step(
                    2, "error", "No transcription key configured — using fallback spacing")
        else:
            step(2, "error", "Skipped (audio extraction failed)")
        ensure_not_cancelled(video_id)

        # ── Step 3: Score & select ────────────────────────────────────────────
        step(3, "running")
        if not segments:
            step(3, "error", "Transcription failed, so clips were not generated. Retry after fixing transcription.")
            raise ValueError("Transcription failed, so clips were not generated. Retry after fixing transcription.")
        clips = select_clips_for_video(
            segments,
            duration=duration,
            ai_model=ai_model,
            audio_path=audio_path if audio_ok else None,
            video_title=source_title or "",
            video_description=source_description or "",
        )
        if not clips:
            step(3, "error", "AI clip selection failed. No fallback clips were generated; retry after fixing the AI response.")
            raise ValueError("AI clip selection failed. No fallback clips were generated; retry after fixing the AI response.")
        avg_score = sum(int(c.get("virality_score", 0))
                        for c in clips) // max(1, len(clips))
        step(
            3, "done", f"AI ranking — {len(clips)} clips selected, avg virality {avg_score}%")
        ensure_not_cancelled(video_id)

        # ── Step 4: Export ────────────────────────────────────────────────────
        step(4, "running", f"0 / {len(clips)} clips done")
        generated = 0
        for idx, clip in enumerate(clips, start=1):
            ensure_not_cancelled(video_id)
            start, end = clip["start"], clip["end"]
            clip_dur = end - start
            srt_content = generate_srt(
                segments, start, end) if captions_enabled and segments else ""
            srt_path = None
            if srt_content.strip():
                srt_path = str(OUTPUTS_DIR / f"{safe_id}_short_{idx:02d}.srt")
                Path(srt_path).write_text(srt_content, encoding="utf-8")
            out_filename = f"{safe_id}_short_{idx:02d}.mp4"
            if export_short_clip(source_path, str(OUTPUTS_DIR / out_filename), start, clip_dur, srt_path, video_id=video_id):
                enriched_clip = shorts_director.enrich_clip_metadata({
                    **clip,
                    "title": clip.get("title") or f"Highlight {idx}",
                })
                title = enriched_clip.get("title", f"Highlight {idx}")[:120]
                description = enriched_clip.get("description", "")[:500]
                upload_title = enriched_clip.get("upload_title") or title
                upload_description = enriched_clip.get("upload_description", "")[:700]
                selection_reason = (
                    enriched_clip.get("selection_reason")
                    or enriched_clip.get("reason")
                    or ""
                )[:500]
                db_write(
                    "INSERT INTO shorts "
                    "(video_id, filename, start_time, end_time, duration, caption_text, title, description, "
                    "virality_score, completion_score, hook_type, selection_reason, status, "
                    "original_start_time, original_end_time, upload_title, upload_description, "
                    "timestamp_engine, candidate_source, final_score, score_details_json, judge_status, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                    (
                        video_id,
                        out_filename,
                        start,
                        end,
                        clip_dur,
                        enriched_clip.get("text", "")[:500],
                        title,
                        description,
                        int(enriched_clip.get("virality_score", 0) or 0),
                        int(enriched_clip.get("completion_score", 0) or 0),
                        enriched_clip.get("hook_type", "")[:40],
                        selection_reason,
                        "draft",
                        start,
                        end,
                        upload_title[:120],
                        upload_description,
                        enriched_clip.get("timestamp_engine", "")[:20],
                        enriched_clip.get("candidate_source", "")[:40],
                        float(enriched_clip.get("final_score", 0) or 0),
                        enriched_clip.get("score_details_json", "")[:4000],
                        enriched_clip.get("judge_status", "")[:30],
                    ),
                )
                generated += 1
            step(4, "running", f"{idx} / {len(clips)} clips done")

        if generated == 0:
            step(4, "error", "No clips exported — check FFmpeg installation")
            raise ValueError(
                "No shorts could be generated. Check FFmpeg installation.")

        step(4, "done", f"{generated} Short clips ready")
        db_write(
            "UPDATE videos SET status='completed', error_message=NULL, updated_at=datetime('now') WHERE id=?",
            (video_id,),
        )

    except CancelledProcessing:
        cleanup_cancelled_video(
            video_id, source_started_as_download=source_started_as_download)
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

def _download_and_process_sync(video_id: int, captions_enabled: bool = True):
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

    dl_steps = [{"name": "Download video from YouTube",
                 "status": "running", "detail": "Starting yt-dlp…"}]
    db_write(
        "UPDATE videos SET status='downloading', error_message=NULL, steps_json=?, updated_at=datetime('now') WHERE id=?",
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

    missing_runtime = missing_ytdlp_runtime_message()
    if missing_runtime:
        dl_steps[0]["status"] = "error"
        dl_steps[0]["detail"] = missing_runtime
        db_write(
            "UPDATE videos SET status='failed', error_message=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
            (missing_runtime, json.dumps(dl_steps), video_id),
        )
        finish_video_job(video_id)
        return

    cmd = build_ytdlp_download_command(yt_id, output_path)
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
        _process_video_sync(
            video_id,
            output_path,
            source_started_as_download=True,
            captions_enabled=captions_enabled,
        )
    finally:
        finish_video_job(video_id)


async def download_and_process(video_id: int, captions_enabled: bool = True):
    await asyncio.get_event_loop().run_in_executor(
        None,
        _download_and_process_sync,
        video_id,
        captions_enabled,
    )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    videos = db_read("SELECT * FROM videos ORDER BY created_at DESC")
    for v in videos:
        try:
            v["steps"] = json.loads(v.get("steps_json") or "[]")
        except Exception:
            v["steps"] = []
        v["shorts"] = attach_latest_uploads(db_read(
            "SELECT * FROM shorts WHERE video_id=? ORDER BY start_time", (
                v["id"],)
        ))
    ch_id = effective_channel_id()
    response = templates.TemplateResponse(
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
            "default_preset": get_default_preset(),
            "presets": list_presets(),
            "youtube_connected": is_youtube_connected(),
            "has_youtube_oauth": bool(effective_youtube_client_id() and effective_youtube_client_secret() and effective_youtube_redirect_uri()),
        },
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/settings")
async def api_get_settings():
    return {
        "channel_id": effective_channel_id(),
        "ai_model": effective_ai_model(),
        "default_preset": get_default_preset(),
        "youtube_connected": is_youtube_connected(),
    }


@app.post("/settings")
async def api_save_settings(request: Request):
    """Save channel ID, AI model preference, or YouTube OAuth settings."""
    body = await request.json()
    if "channel_id" in body:
        set_setting("channel_id", str(body["channel_id"]).strip())
    if "ai_model" in body and body["ai_model"] in ("openai", "gemini", "groq"):
        set_setting("ai_model", body["ai_model"])
    if "youtube_client_id" in body:
        set_setting("youtube_client_id", str(
            body["youtube_client_id"]).strip())
    if "youtube_client_secret" in body:
        set_setting("youtube_client_secret", str(
            body["youtube_client_secret"]).strip())
    if "youtube_redirect_uri" in body:
        set_setting("youtube_redirect_uri", str(
            body["youtube_redirect_uri"]).strip())
    return {"ok": True, "channel_id": effective_channel_id(), "ai_model": effective_ai_model()}


@app.get("/channel-info")
async def api_channel_info():
    """Return metadata about the configured channel."""
    ch_id = effective_channel_id()
    if not ch_id:
        raise HTTPException(
            status_code=400, detail="No channel ID configured.")
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
        raise HTTPException(
            status_code=400, detail="No channel ID configured.")
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=400, detail="YOUTUBE_API_KEY not set.")
    try:
        uploads = await fetch_latest_uploads(ch_id, max_results=10)
        # Annotate with whether they're already in DB
        stored_ids = {r["youtube_video_id"]
                      for r in db_read("SELECT youtube_video_id FROM videos")}
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
        raise HTTPException(
            status_code=400, detail="YOUTUBE_API_KEY secret is not set.")
    if not ch_id:
        raise HTTPException(
            status_code=400, detail="No channel ID configured. Set it in Settings.")
    try:
        uploads = await fetch_latest_uploads(ch_id, max_results=10)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"YouTube API error: {exc}")

    added = 0
    for upload in uploads:
        if not db_read("SELECT id FROM videos WHERE youtube_video_id=?", (upload["youtube_video_id"],)):
            db_write(
                "INSERT INTO videos (youtube_video_id, title, description, published_at, thumbnail, status) VALUES (?,?,?,?,?,'detected')",
                (upload["youtube_video_id"], upload["title"],
                 upload.get("description", ""), upload["published_at"], upload.get("thumbnail", "")),
            )
            added += 1
        else:
            # Update thumbnail if missing
            db_write(
                "UPDATE videos SET description=COALESCE(NULLIF(description,''), ?), thumbnail=CASE WHEN thumbnail IS NULL OR thumbnail='' THEN ? ELSE thumbnail END WHERE youtube_video_id=?",
                (upload.get("description", ""), upload.get("thumbnail", ""), upload["youtube_video_id"]),
            )

    return {"detected": len(uploads), "new": added, "uploads": uploads}


@app.post("/upload-source/{video_id}")
async def upload_source(video_id: int, file: UploadFile = File(...)):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    video = rows[0]
    if video["status"] == "processing":
        raise HTTPException(
            status_code=409, detail="Video is already being processed.")

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
        raise HTTPException(
            status_code=400, detail="Uploaded file is not a valid video.")

    db_write(
        "UPDATE videos SET source_path=?, status='waiting', updated_at=datetime('now') WHERE id=?",
        (str(dest), video_id),
    )
    return {"message": "Source file uploaded successfully.", "path": str(dest)}


@app.post("/download-yt/{video_id}")
async def download_yt_route(video_id: int, background_tasks: BackgroundTasks, request: Request):
    """Download video from YouTube via yt-dlp, then process automatically."""
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    video = rows[0]
    if video["status"] in ("processing", "downloading"):
        raise HTTPException(status_code=409, detail="Already in progress.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    background_tasks.add_task(download_and_process,
                              video_id, captions_enabled_from_body(body))
    return {"message": "Download started in background."}


@app.post("/process/{video_id}")
async def process_video_route(video_id: int, background_tasks: BackgroundTasks, request: Request):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    video = rows[0]
    if not video["source_path"] or not Path(video["source_path"]).exists():
        raise HTTPException(
            status_code=400, detail="Source file not uploaded yet.")
    if video["status"] in ("processing", "downloading"):
        raise HTTPException(status_code=409, detail="Already in progress.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    background_tasks.add_task(
        process_video,
        video_id,
        video["source_path"],
        captions_enabled_from_body(body),
    )
    return {"message": "Processing started in background."}


@app.get("/short/{short_id}/review", response_class=HTMLResponse)
async def review_short_page(short_id: int, request: Request):
    try:
        short = get_short_detail(short_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return templates.TemplateResponse(
        request,
        "review.html",
        context={
            "short": short,
            "presets": list_presets(),
            "default_preset": get_default_preset(),
            "youtube_connected": is_youtube_connected(),
        },
    )


@app.post("/short/{short_id}/metadata")
async def update_short_metadata_route(short_id: int, request: Request):
    try:
        return {"short": update_short_metadata(short_id, await request.json())}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/short/{short_id}/status")
async def update_short_status_route(short_id: int, request: Request):
    body = await request.json()
    try:
        return {"short": update_short_status(short_id, str(body.get("status") or ""))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/short/{short_id}/timing")
async def update_short_timing_route(short_id: int, request: Request):
    body = await request.json()
    try:
        return {"short": update_short_timing(short_id, body.get("start_time"), body.get("end_time"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/short/{short_id}/regenerate")
async def regenerate_short_route(short_id: int):
    try:
        return {"short": regenerate_short(short_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, status: str = "all"):
    return templates.TemplateResponse(
        request,
        "queue.html",
        context={
            "queue": get_upload_queue(status),
            "status_filter": status,
            "youtube_connected": is_youtube_connected(),
            "has_youtube_oauth": bool(effective_youtube_client_id() and effective_youtube_client_secret() and effective_youtube_redirect_uri()),
        },
    )


@app.get("/presets")
async def presets_route():
    return {"presets": list_presets(), "default_preset": get_default_preset()}


@app.post("/presets")
async def create_preset_route(request: Request):
    body = await request.json()
    return {"preset": create_preset(body.get("name") or "Custom", body.get("config") or {}, bool(body.get("is_default")))}


@app.post("/presets/default")
async def set_default_preset_route(request: Request):
    body = await request.json()
    try:
        return {"preset": set_default_preset(int(body.get("preset_id")))}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/youtube/status")
async def youtube_status_route():
    """Get detailed YouTube connection status."""
    return get_youtube_connection_status()


@app.post("/youtube/disconnect")
async def youtube_disconnect_route():
    """Disconnect YouTube by removing stored tokens"""
    disconnect_youtube()
    return {"success": True, "message": "YouTube disconnected successfully"}


@app.get("/youtube/connect")
async def youtube_connect_route():
    missing = []
    if not effective_youtube_client_id():
        missing.append("client_id")
    if not effective_youtube_client_secret():
        missing.append("client_secret")
    if not effective_youtube_redirect_uri():
        missing.append("redirect_uri")
    if missing:
        return {"missing": missing}

    try:
        return {"url": youtube_oauth_url()}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/youtube/callback")
async def youtube_callback_route(code: str = ""):
    if not code:
        raise HTTPException(
            status_code=400, detail="Missing YouTube OAuth code.")
    try:
        handle_youtube_callback(code)
        return HTMLResponse("<p>YouTube connected. You can close this tab and return to Auto Shorts Studio.</p>")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/short/{short_id}/upload-youtube")
async def upload_youtube_route(short_id: int, request: Request):
    body = await request.json()
    try:
        return {
            "upload": create_youtube_upload(
                short_id,
                privacy_status=str(body.get("privacy_status") or "private"),
                scheduled_at=body.get("scheduled_at") or None,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/upload/{upload_id}/retry")
async def retry_upload_route(upload_id: int):
    try:
        return {"upload": retry_upload(upload_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/analytics/refresh")
async def analytics_refresh_route():
    return {"refreshed": refresh_upload_analytics()}


@app.post("/cancel/{video_id}")
async def cancel_video_route(video_id: int):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")

    video = rows[0]
    if video["status"] not in ("processing", "downloading"):
        raise HTTPException(
            status_code=409, detail="Video is not currently running.")

    request_video_cancel(video_id)
    cleanup_cancelled_video(
        video_id, source_started_as_download=video["status"] == "downloading")
    return {"message": "Current generation cancelled. Existing completed shorts and source video were kept."}


@app.get("/videos")
async def api_videos():
    videos = db_read("SELECT * FROM videos ORDER BY created_at DESC")
    for v in videos:
        v["shorts"] = attach_latest_uploads(db_read(
            "SELECT * FROM shorts WHERE video_id=? ORDER BY start_time", (v["id"],)))
    return {"videos": videos}


@app.get("/status/{video_id}")
async def api_status(video_id: int):
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Video not found.")
    v = rows[0]
    v["shorts"] = attach_latest_uploads(db_read(
        "SELECT * FROM shorts WHERE video_id=? ORDER BY start_time", (v["id"],)))
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


@app.get("/source-video/{video_id}")
async def source_video_file(video_id: int):
    rows = db_read("SELECT source_path FROM videos WHERE id=?", (video_id,))
    if not rows or not rows[0].get("source_path"):
        raise HTTPException(status_code=404, detail="Source video not found.")
    source_path = Path(rows[0]["source_path"])
    if not source_path.exists():
        raise HTTPException(
            status_code=404, detail="Source video file is missing.")
    return FileResponse(str(source_path), media_type="video/mp4")


@app.delete("/short/{short_id}")
async def delete_short_route(short_id: int):
    if not delete_short_record(short_id):
        raise HTTPException(status_code=404, detail="Short not found.")
    return {"message": "Short deleted."}


@app.delete("/video-source/{video_id}")
async def delete_source_video_route(video_id: int):
    if not delete_source_video(video_id):
        raise HTTPException(
            status_code=404, detail="Downloaded video not found.")
    return {"message": "Downloaded video deleted."}


@app.post("/webhooks/youtube")
async def youtube_webhook(request: Request):
    body = await request.body()
    sig_header = request.headers.get("X-Hub-Signature", "")
    if sig_header and WEBHOOK_SECRET:
        expected = "sha1=" + \
            hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(
                status_code=403, detail="Invalid webhook signature.")
    video_id_matches = re.findall(rb"<yt:videoId>([^<]+)</yt:videoId>", body)
    title_matches = re.findall(rb"<title>([^<]+)</title>", body)
    added = 0
    for i, vid_bytes in enumerate(video_id_matches):
        yt_vid_id = vid_bytes.decode().strip()
        title = title_matches[i + 1].decode().strip() if i + \
            1 < len(title_matches) else "Unknown"
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
