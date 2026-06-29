# Auto Shorts Generator

AI-powered tool that monitors your YouTube channel and auto-generates 9:16 vertical Short clips.

## Features
- YouTube Data API v3 — supports `@handles` and `UC...` channel IDs
- Download source videos directly from YouTube via **yt-dlp** (no manual upload needed)
- AI transcription: OpenAI Whisper or Google Gemini Flash
- Groq Whisper transcription option
- FFmpeg pipeline: center-crop to 9:16 and scale to 1080x1920
- Live step-by-step progress tracking in the dashboard
- SQLite database — no migrations, auto-created on startup

## Secrets required
| Secret | Purpose |
|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key |
| `YOUTUBE_CHANNEL_ID` | Your channel ID (fallback if not set in UI) |
| `OPENAI_API_KEY` | OpenAI Whisper transcription (optional) |
| `GEMINI_API_KEY` | Gemini Flash transcription (optional) |
| `GROQ_API_KEY` | Groq Whisper transcription (optional) |

## Run
Install local prerequisites first:

```bash
sudo apt install python3.12-venv python3-pip ffmpeg
```

```bash
cd auto-shorts-generator-main
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt yt-dlp
python3 main.py
```

Open http://localhost:8000.

## Local config
Create or edit `.env` in the project root:

```env
PORT=8000
YOUTUBE_API_KEY=your_youtube_data_api_key
YOUTUBE_CHANNEL_ID=@YourChannel
OPENAI_API_KEY=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
GROQ_API_KEY=
GROQ_MODEL=whisper-large-v3-turbo
GROQ_LLM_MODEL=llama-3.1-8b-instant
WEBHOOK_SECRET=
```

`YOUTUBE_API_KEY` is required for scanning channel uploads. `OPENAI_API_KEY`
`GEMINI_API_KEY`, or `GROQ_API_KEY` is optional because local Whisper is tried
first when the CLI is installed.

You also need FFmpeg available on your system for video/audio processing. The
YouTube download flow uses the `yt-dlp` command.
