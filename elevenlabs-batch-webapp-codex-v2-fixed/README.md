# ElevenLabs Batch Voice Generator

FastAPI web app for batch generating MP3 files from a CSV script through the ElevenLabs Text to Speech API.

The app is built for Railway deployment:

- FastAPI backend with `/api/analyze`, `/api/generate`, and job polling endpoints
- Static HTML/CSS/JS frontend served by the same Python app
- Background generation jobs so the page can show progress while audio is created
- CSV validation before spending ElevenLabs credits
- ZIP download with `generation_log.csv` and `errors.csv`

## Voice Validation

Known speakers are validated against expected voice IDs:

| speaker | required voice_id |
|---|---|
| พี่พล | `k2UOyGqcC29TvawCKOJG` |
| น้องเนย | `EiDqbKUIG51fSl2SU2dg` |

Unknown speakers are allowed when `voice_id` is present. To override the known speaker map, set `VOICE_ID_MAP_JSON` to a JSON object.

The default model is `eleven_v3`, so rows above 5,000 characters are blocked before generation.

## Local Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
ELEVENLABS_API_KEY=your_real_api_key_here
ELEVENLABS_MODEL_ID=eleven_v3
ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
```

Run locally:

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

## Deploy to Railway

This folder includes `railway.json`, which starts the app with:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Railway setup:

1. Deploy this folder as the service root. If deploying from the parent workspace, set the Railway root directory to `/elevenlabs-batch-webapp-codex-v2-fixed`.
2. Add the service variable `ELEVENLABS_API_KEY`.
3. Optional: add `ELEVENLABS_MODEL_ID`, `ELEVENLABS_OUTPUT_FORMAT`, or `VOICE_ID_MAP_JSON`.
4. Optional for persistent output: attach a Railway volume at `/data` and set `OUTPUT_ROOT=/data/outputs`.
5. After deploy, open Networking and generate a public Railway domain.

Without a volume, generated files are available during the running session but may be lost when Railway restarts or redeploys the service.

## CSV Format

```csv
id,speaker,text,voice_id
001,พี่พล,"สวัสดีครับ ตอนนี้คุณกำลังอยู่กับรายการ หลอนหลอนก่อนเที่ยงคืน",k2UOyGqcC29TvawCKOJG
002,น้องเนย,"และเนยค่ะ สวัสดีทุกคนที่ยังไม่นอนนะคะ",EiDqbKUIG51fSl2SU2dg
```

## Output

```text
outputs/
└── EP01/
    ├── audio_raw/
    ├── generation_log.csv
    ├── errors.csv
    └── EP01_audio_raw.zip
```

`generation_log.csv` includes:

```csv
id,speaker,voice_id,filename,status,message,character_count
```
