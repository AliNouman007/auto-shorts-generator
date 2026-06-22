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
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme")
PORT = int(os.environ.get("PORT", 8000))

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

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
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass
    conn.close()

init_db()

_db_lock = threading.Lock()

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

def update_steps(video_id: int, steps: list[dict]):
    """Persist step list to DB so the frontend can poll live progress."""
    db_write(
        "UPDATE videos SET steps_json=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(steps), video_id),
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
    """Return the active AI model: 'openai' or 'gemini'."""
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

def export_short_clip(
    source_path: str,
    output_path: str,
    start: float,
    duration: float,
    srt_path: Optional[str] = None,
) -> bool:
    """Crop to 9:16 center, scale to 1080×1920, burn subtitles, export H.264/AAC."""
    vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
    if srt_path and Path(srt_path).exists():
        escaped = srt_path.replace("\\", "/").replace(":", "\\:")
        vf += (
            f",subtitles='{escaped}':force_style="
            "'FontName=Arial,FontSize=18,Alignment=2,MarginV=40,"
            "Bold=1,PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,Outline=2'"
        )
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return result.returncode == 0
    except Exception:
        return False

def extract_audio(video_path: str, audio_path: str) -> bool:
    """Extract mono 16 kHz audio from a video."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-ar", "16000", "-ac", "1", "-q:a", "0", audio_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Transcription helpers
# ---------------------------------------------------------------------------

def transcribe_with_whisper_local(audio_path: str) -> Optional[list[dict]]:
    """Try the local openai-whisper CLI. Returns segments list or None."""
    try:
        out_dir = Path(audio_path).parent
        result = subprocess.run(
            ["whisper", audio_path, "--output_format", "json", "--output_dir", str(out_dir)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return None
        json_path = out_dir / (Path(audio_path).stem + ".json")
        if json_path.exists():
            data = json.loads(json_path.read_text())
            return data.get("segments", [])
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
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
                json=payload,
            )
        if response.status_code != 200:
            return None
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
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Highlight scoring & clip building
# ---------------------------------------------------------------------------

def score_segments(segments: list[dict]) -> list[dict]:
    ENERGY_WORDS = {
        "amazing", "incredible", "wow", "best", "worst", "never", "always",
        "secret", "important", "shocking", "huge", "big", "win", "lose",
        "finally", "actually", "seriously", "honestly", "literally", "crazy",
        "unbelievable", "awesome", "terrible", "love", "hate", "must", "need",
    }
    scored = []
    for seg in segments:
        text = seg.get("text", "").strip()
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
        dur = end - start
        if dur < 1 or dur > 120:
            continue
        score = 0.0
        words = text.lower().split()
        energy_hits = sum(1 for w in words if w.strip(".,!?;:") in ENERGY_WORDS)
        score += energy_hits * 2
        if text.rstrip()[-1:] in ".!?":
            score += 1.5
        if dur > 0:
            score += min(len(words) / dur, 3)
        if dur < 3:
            score -= 2
        scored.append({**seg, "_score": score, "_dur": dur})
    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored

def build_clips_from_segments(
    segments: list[dict],
    target_count: int = 5,
    min_duration: float = 20,
    max_duration: float = 60,
) -> list[dict]:
    if not segments:
        return []
    seeds = score_segments(segments)[:target_count * 3]
    seeds.sort(key=lambda x: float(x.get("start", 0)))
    clips = []
    used: list[tuple[float, float]] = []

    def overlaps(s: float, e: float) -> bool:
        return any(s < ue and e > us for us, ue in used)

    for seed in seeds:
        ss = float(seed.get("start", 0))
        window_segs = [
            s for s in segments
            if float(s.get("start", 0)) >= max(0, ss - 5)
            and float(s.get("end", 0)) <= ss + max_duration
        ]
        if not window_segs:
            continue
        cs = float(window_segs[0].get("start", ss))
        ce = float(window_segs[-1].get("end", ss))
        ct = " ".join(s.get("text", "").strip() for s in window_segs)
        ce = min(ce, cs + max_duration)
        ce = max(ce, cs + min_duration)
        if overlaps(cs, ce):
            continue
        used.append((cs, ce))
        clips.append({"start": cs, "end": ce, "text": ct})
        if len(clips) >= target_count:
            break
    clips.sort(key=lambda x: x["start"])
    return clips

def fallback_clips(duration: float, count: int = 5) -> list[dict]:
    clip_duration = 35.0
    skip_start = 60.0 if duration > 180 else 0.0
    usable = duration - skip_start - 30
    if usable < clip_duration:
        count = max(1, int(usable // clip_duration)) or 1
    step = usable / count
    return [
        {"start": skip_start + i * step, "end": min(skip_start + i * step + clip_duration, duration - 5), "text": ""}
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

def _process_video_sync(video_id: int, source_path: str):
    """Full pipeline with live step tracking: validate → audio → transcribe → score → export."""
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
        if not is_valid_video(source_path):
            step(0, "error", "Not a valid video file.")
            raise ValueError("Uploaded file does not appear to be a valid video.")
        duration = get_video_duration(source_path)
        if not duration:
            step(0, "error", "Could not determine video duration.")
            raise ValueError("Could not determine video duration.")
        step(0, "done", f"{duration:.0f}s")

        rows = db_read("SELECT youtube_video_id FROM videos WHERE id=?", (video_id,))
        yt_id = rows[0]["youtube_video_id"] if rows else str(video_id)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", yt_id)
        ai_model = effective_ai_model()
        segments: list[dict] = []
        audio_path = str(UPLOADS_DIR / f"{safe_id}_audio.mp3")

        # ── Step 1: Extract audio ────────────────────────────────────────────
        step(1, "running")
        audio_ok = extract_audio(source_path, audio_path)
        if audio_ok:
            step(1, "done")
        else:
            step(1, "error", "FFmpeg audio extraction failed — clips will have no subtitles")

        # ── Step 2: Transcribe ───────────────────────────────────────────────
        if audio_ok:
            step(2, "running", "Trying local Whisper CLI…")
            segs = transcribe_with_whisper_local(audio_path)
            if segs:
                segments = segs
                step(2, "done", f"Local Whisper — {len(segments)} segments")
            elif ai_model == "gemini" and GEMINI_API_KEY:
                step(2, "running", "Gemini Flash…")
                loop = asyncio.new_event_loop()
                try:
                    segs = loop.run_until_complete(transcribe_with_gemini(audio_path))
                finally:
                    loop.close()
                if segs:
                    segments = segs
                    step(2, "done", f"Gemini — {len(segments)} segments")
                else:
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

        # ── Step 3: Score & select ────────────────────────────────────────────
        step(3, "running")
        clips = build_clips_from_segments(segments, target_count=6) if segments else []
        if not clips:
            clips = fallback_clips(duration)
            step(3, "done", f"Fallback — {len(clips)} evenly-spaced clips")
        else:
            step(3, "done", f"AI scoring — {len(clips)} highlight clips selected")
        clips = clips[:8]

        # ── Step 4: Export ────────────────────────────────────────────────────
        step(4, "running", f"0 / {len(clips)} clips done")
        generated = 0
        for idx, clip in enumerate(clips, start=1):
            start, end = clip["start"], clip["end"]
            clip_dur = end - start
            srt_content = generate_srt(segments, start, end) if segments else ""
            srt_path = None
            if srt_content.strip():
                srt_path = str(OUTPUTS_DIR / f"{safe_id}_short_{idx:02d}.srt")
                Path(srt_path).write_text(srt_content)
            out_filename = f"{safe_id}_short_{idx:02d}.mp4"
            if export_short_clip(source_path, str(OUTPUTS_DIR / out_filename), start, clip_dur, srt_path):
                db_write(
                    "INSERT INTO shorts (video_id, filename, start_time, end_time, duration, caption_text) VALUES (?,?,?,?,?,?)",
                    (video_id, out_filename, start, end, clip_dur, clip.get("text", "")[:500]),
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


# ---------------------------------------------------------------------------
# yt-dlp download pipeline
# ---------------------------------------------------------------------------

def _download_and_process_sync(video_id: int):
    """Download from YouTube via yt-dlp, then run the full processing pipeline."""
    rows = db_read("SELECT * FROM videos WHERE id=?", (video_id,))
    if not rows:
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "yt-dlp failed")[-600:]
            dl_steps[0]["status"] = "error"
            dl_steps[0]["detail"] = err
            db_write(
                "UPDATE videos SET status='failed', error_message=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
                (err, json.dumps(dl_steps), video_id),
            )
            return
    except Exception as exc:
        msg = str(exc)
        dl_steps[0]["status"] = "error"
        dl_steps[0]["detail"] = msg
        db_write(
            "UPDATE videos SET status='failed', error_message=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
            (msg, json.dumps(dl_steps), video_id),
        )
        return

    dl_steps[0]["status"] = "done"
    dl_steps[0]["detail"] = "Downloaded successfully"
    db_write(
        "UPDATE videos SET source_path=?, steps_json=?, updated_at=datetime('now') WHERE id=?",
        (output_path, json.dumps(dl_steps), video_id),
    )
    # Continue into the full processing pipeline
    _process_video_sync(video_id, output_path)


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
    if "ai_model" in body and body["ai_model"] in ("openai", "gemini"):
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
