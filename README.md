# Auto Shorts Generator

AI-powered tool that monitors your YouTube channel and auto-generates 9:16 vertical Short clips.

## Features
- YouTube Data API v3 — supports `@handles` and `UC...` channel IDs
- Download source videos directly from YouTube via **yt-dlp** (no manual upload needed)
- AI transcription: OpenAI Whisper or Google Gemini Flash
- FFmpeg pipeline: center-crop to 9:16, scale to 1080x1920, subtitle burn-in
- Live step-by-step progress tracking in the dashboard
- SQLite database — no migrations, auto-created on startup

## Secrets required
| Secret | Purpose |
|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key |
| `YOUTUBE_CHANNEL_ID` | Your channel ID (fallback if not set in UI) |
| `OPENAI_API_KEY` | OpenAI Whisper transcription (optional) |
| `GEMINI_API_KEY` | Gemini Flash transcription (optional) |

## Run
```bash
cd artifacts/auto-shorts && python3 main.py
```
