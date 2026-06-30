# Auto Shorts Generator

AI-powered tool that monitors your YouTube channel and auto-generates 9:16 vertical Short clips.

## Features
- YouTube Data API v3 — supports `@handles` and `UC...` channel IDs
- Download source videos directly from YouTube via **yt-dlp** (no manual upload needed)
- Deepgram transcription for timestamped clip selection
- User-selected clip engine: V2 generic or isolated Comedy V3
- Comedy V3 uses Gemini or Groq as the selected main brain with strict/balanced/volume quality modes
- Optional V2 AI director ranking with OpenAI, Gemini, or Groq
- FFmpeg pipeline: center-crop to 9:16 and scale to 1080x1920
- Live step-by-step progress tracking in the dashboard
- SQLite database — no migrations, auto-created on startup

## Secrets required
| Secret | Purpose |
|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key |
| `YOUTUBE_CHANNEL_ID` | Your channel ID (fallback if not set in UI) |
| `DEEPGRAM_API_KEY` | Deepgram transcription for clip timestamps |
| `DEEPGRAM_MODEL` | Deepgram model name (defaults to `nova-2`) |
| `OPENAI_API_KEY` | OpenAI V2 AI director ranking (optional) |
| `GEMINI_API_KEY` | Gemini V2 ranking and Comedy V3 main brain |
| `GROQ_API_KEY` | Groq V2 ranking and Comedy V3 main brain |

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
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-2
GROQ_API_KEY=
GROQ_LLM_MODEL=llama-3.1-8b-instant
WEBHOOK_SECRET=
```

`YOUTUBE_API_KEY` is required for scanning channel uploads. `DEEPGRAM_API_KEY`
is required for transcription; if it is missing or Deepgram returns no usable
timestamps, the processing job stops instead of falling back to another caption
or transcription provider.

You also need FFmpeg available on your system for video/audio processing. The
YouTube download flow uses the `yt-dlp` command.

Comedy V3 is selected in the output preset. It does not fall back to V2; if the
selected Gemini or Groq key is missing or the model cannot return valid JSON,
the job stops before export instead of generating filler clips.
