from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


APP_TITLE = "ElevenLabs Batch Voice Generator"
REQUIRED_COLUMNS = ["id", "speaker", "text", "voice_id"]
DEFAULT_VOICE_MAP = {
    "พี่พล": "k2UOyGqcC29TvawCKOJG",
    "น้องเนย": "EiDqbKUIG51fSl2SU2dg",
}
MODEL_CHARACTER_LIMITS = {
    "eleven_v3": 5000,
    "eleven_multilingual_v2": 10000,
    "eleven_flash_v2_5": 40000,
}
TEXT_WARNING_THRESHOLD = 1500
INVALID_FILENAME_CHARS = r'[\/\\:\*\?"<>\|]'

PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"

load_dotenv(PROJECT_DIR / ".env")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3").strip() or "eleven_v3"
OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip() or "mp3_44100_128"
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", str(PROJECT_DIR / "outputs"))).expanduser()
GENERATION_WORKERS = max(1, env_int("GENERATION_WORKERS", 1))

STATE_LOCK = threading.Lock()
ANALYSES: dict[str, dict[str, Any]] = {}
JOBS: dict[str, dict[str, Any]] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=GENERATION_WORKERS)


def load_voice_map() -> dict[str, str]:
    raw_value = os.getenv("VOICE_ID_MAP_JSON", "").strip()
    if not raw_value:
        return DEFAULT_VOICE_MAP

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return DEFAULT_VOICE_MAP

    if not isinstance(parsed, dict):
        return DEFAULT_VOICE_MAP

    cleaned = {
        str(speaker).strip(): str(voice_id).strip()
        for speaker, voice_id in parsed.items()
        if str(speaker).strip() and str(voice_id).strip()
    }
    return cleaned or DEFAULT_VOICE_MAP


EXPECTED_VOICE_MAP = load_voice_map()

app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class GenerateRequest(BaseModel):
    analysis_id: str
    episode_name: str = "EP01"
    force_regenerate: bool = False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_api_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "").strip()


def storage_label() -> str:
    return "Persistent volume" if str(OUTPUT_ROOT).startswith("/data") else "App filesystem"


def sanitize_filename(value: Any) -> str:
    text = str(value).strip()
    text = re.sub(INVALID_FILENAME_CHARS, "", text)
    text = re.sub(r"\s+", "_", text)
    return text


def safe_episode_name(value: str) -> str:
    cleaned = sanitize_filename(value or "EP01")
    return cleaned or "EP01"


def normalize_id(value: Any) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return text.zfill(3)
    return sanitize_filename(text)


def output_filename(row: dict[str, Any]) -> str:
    return f"{normalize_id(row['id'])}_{sanitize_filename(row['speaker'])}.mp3"


def records_from_dataframe(df: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(df.to_json(orient="records", force_ascii=False))


def read_csv_bytes(contents: bytes) -> tuple[pd.DataFrame | None, str | None]:
    if not contents:
        return None, "CSV file is empty"

    for encoding in ("utf-8-sig", "utf-8"):
        try:
            buffer = io.BytesIO(contents)
            return pd.read_csv(buffer, dtype=str, keep_default_na=False, encoding=encoding), None
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            return None, f"Cannot read CSV file: {exc}"

    return None, "Cannot read CSV file: unsupported encoding"


def validate_voice_id(row: pd.Series | dict[str, Any]) -> str:
    speaker = str(row["speaker"]).strip()
    voice_id = str(row["voice_id"]).strip()

    if not voice_id:
        return "MISSING_VOICE_ID"

    expected_voice_id = EXPECTED_VOICE_MAP.get(speaker)
    if expected_voice_id and voice_id != expected_voice_id:
        return f"VOICE_ID_MISMATCH expected {expected_voice_id}"

    return "OK_FROM_CSV"


def build_voice_validation_table(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        speaker = str(row["speaker"]).strip()
        rows.append(
            {
                "id": str(row["id"]).strip(),
                "speaker": speaker,
                "csv_voice_id": str(row["voice_id"]).strip(),
                "expected_voice_id": EXPECTED_VOICE_MAP.get(speaker, ""),
                "status": validate_voice_id(row),
            }
        )
    return pd.DataFrame(rows)


def analyze_dataframe(df: pd.DataFrame) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if df.empty:
        return {
            "valid": False,
            "df": df,
            "errors": [{"row": "file", "id": "", "speaker": "", "issue": "CSV file is empty"}],
            "warnings": warnings,
            "voice_validation": pd.DataFrame(),
        }

    missing_columns = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing_columns:
        return {
            "valid": False,
            "df": df,
            "errors": [
                {
                    "row": "file",
                    "id": "",
                    "speaker": "",
                    "issue": f"Missing required columns: {', '.join(missing_columns)}",
                }
            ],
            "warnings": warnings,
            "voice_validation": pd.DataFrame(),
        }

    cleaned = df.copy()
    for column in REQUIRED_COLUMNS:
        cleaned[column] = cleaned[column].astype(str)

    original_text = cleaned["text"].copy()
    original_voice_id = cleaned["voice_id"].copy()

    for column in REQUIRED_COLUMNS:
        cleaned[column] = cleaned[column].str.strip()

    duplicate_ids = set(cleaned["id"][cleaned["id"].duplicated(keep=False) & (cleaned["id"] != "")])
    model_character_limit = MODEL_CHARACTER_LIMITS.get(MODEL_ID)

    for index, row in cleaned.iterrows():
        row_number = index + 2
        for column in REQUIRED_COLUMNS:
            if not row[column]:
                errors.append(
                    {
                        "row": row_number,
                        "id": row.get("id", ""),
                        "speaker": row.get("speaker", ""),
                        "issue": f"Missing {column}",
                    }
                )

        if row["id"] and row["id"] in duplicate_ids:
            errors.append(
                {
                    "row": row_number,
                    "id": row["id"],
                    "speaker": row["speaker"],
                    "issue": "Duplicate id",
                }
            )

        voice_status = validate_voice_id(row)
        if voice_status not in {"OK", "OK_FROM_CSV"}:
            errors.append(
                {
                    "row": row_number,
                    "id": row["id"],
                    "speaker": row["speaker"],
                    "issue": f"voice_id validation failed: {voice_status}",
                }
            )

        character_count = len(row["text"])
        if model_character_limit and character_count > model_character_limit:
            errors.append(
                {
                    "row": row_number,
                    "id": row["id"],
                    "speaker": row["speaker"],
                    "issue": (
                        f"Text is too long for {MODEL_ID} "
                        f"({character_count:,}/{model_character_limit:,} characters)"
                    ),
                }
            )

        if row["text"] and character_count > TEXT_WARNING_THRESHOLD:
            warnings.append(
                {
                    "row": row_number,
                    "id": row["id"],
                    "speaker": row["speaker"],
                    "issue": f"Text is long ({character_count:,} characters)",
                }
            )

        if original_text.iloc[index] != row["text"]:
            warnings.append(
                {
                    "row": row_number,
                    "id": row["id"],
                    "speaker": row["speaker"],
                    "issue": "Text has leading/trailing whitespace and will be trimmed",
                }
            )

        if original_voice_id.iloc[index] != row["voice_id"]:
            warnings.append(
                {
                    "row": row_number,
                    "id": row["id"],
                    "speaker": row["speaker"],
                    "issue": "voice_id has leading/trailing whitespace and will be trimmed",
                }
            )

    voice_validation = build_voice_validation_table(cleaned)
    valid = len(cleaned) > 0 and not errors
    return {
        "valid": valid,
        "df": cleaned,
        "errors": errors,
        "warnings": warnings,
        "voice_validation": voice_validation,
    }


def build_summary(df: pd.DataFrame) -> dict[str, Any]:
    with_character_count = df.assign(character_count=df["text"].str.len())
    by_speaker = (
        with_character_count.groupby("speaker", dropna=False)
        .agg(cues=("id", "count"), characters=("character_count", "sum"))
        .reset_index()
    )
    voice_by_speaker = (
        df.groupby("speaker", dropna=False)["voice_id"]
        .agg(lambda values: ", ".join(sorted(set(values))))
        .reset_index()
    )

    return {
        "total_rows": len(df),
        "speaker_count": df["speaker"].nunique(),
        "voice_count": df["voice_id"].nunique(),
        "total_characters": int(with_character_count["character_count"].sum()),
        "by_speaker": records_from_dataframe(by_speaker),
        "voice_by_speaker": records_from_dataframe(voice_by_speaker),
    }


def text_preview(value: str, limit: int = 90) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def prepare_preview(df: pd.DataFrame) -> list[dict[str, Any]]:
    preview = df.head(20).copy()
    preview["text_preview"] = preview["text"].apply(text_preview)
    preview["voice_status"] = preview.apply(validate_voice_id, axis=1)
    return records_from_dataframe(preview[["id", "speaker", "text_preview", "voice_id", "voice_status"]])


def has_required_columns(df: pd.DataFrame) -> bool:
    return all(column in df.columns for column in REQUIRED_COLUMNS)


def serialize_analysis(analysis_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
    df = analysis["df"]
    columns_are_complete = has_required_columns(df)
    summary = build_summary(df) if columns_are_complete and len(df) else None

    return {
        "analysis_id": analysis_id,
        "filename": analysis.get("filename", "uploaded.csv"),
        "analyzed_at": analysis.get("analyzed_at", now_iso()),
        "valid": bool(analysis["valid"]),
        "model_id": MODEL_ID,
        "output_format": OUTPUT_FORMAT,
        "errors": analysis["errors"],
        "warnings": analysis["warnings"],
        "summary": summary,
        "voice_validation": (
            records_from_dataframe(analysis["voice_validation"])
            if columns_are_complete and not analysis["voice_validation"].empty
            else []
        ),
        "preview": prepare_preview(df) if columns_are_complete else records_from_dataframe(df.head(20)),
    }


def prune_state() -> None:
    with STATE_LOCK:
        if len(ANALYSES) > 100:
            stale_analysis_ids = list(ANALYSES.keys())[: len(ANALYSES) - 100]
            for analysis_id in stale_analysis_ids:
                ANALYSES.pop(analysis_id, None)

        if len(JOBS) > 100:
            stale_job_ids = list(JOBS.keys())[: len(JOBS) - 100]
            for job_id in stale_job_ids:
                JOBS.pop(job_id, None)


def call_elevenlabs_tts(client: ElevenLabs, voice_id: str, text: str) -> bytes:
    clean_voice_id = str(voice_id).strip()
    clean_text = str(text).strip()
    if not clean_voice_id:
        raise ValueError("Missing voice_id")
    if not clean_text:
        raise ValueError("Missing text")

    audio_chunks = client.text_to_speech.convert(
        text=clean_text,
        voice_id=clean_voice_id,
        model_id=MODEL_ID,
        output_format=OUTPUT_FORMAT,
        voice_settings=VoiceSettings(
            stability=0.80,
            similarity_boost=0.88,
            style=0.00,
            use_speaker_boost=True,
        ),
    )
    return b"".join(audio_chunks)


def with_timestamp(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_{timestamp}{path.suffix}")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_path = path
    try:
        handle = target_path.open("w", newline="", encoding="utf-8-sig")
    except PermissionError:
        target_path = with_timestamp(path)
        handle = target_path.open("w", newline="", encoding="utf-8-sig")

    with handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return target_path


def create_zip(audio_dir: Path, zip_path: Path) -> tuple[Path, int]:
    audio_files = sorted(audio_dir.glob("*.mp3"))
    target_path = zip_path
    try:
        archive = zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED)
    except PermissionError:
        target_path = with_timestamp(zip_path)
        archive = zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED)

    with archive:
        for audio_file in audio_files:
            archive.write(audio_file, arcname=f"audio_raw/{audio_file.name}")
    return target_path, len(audio_files)


def update_job(job_id: str, **updates: Any) -> None:
    with STATE_LOCK:
        job = JOBS[job_id]
        job.update(updates)
        job["updated_at"] = now_iso()


def append_job_log(job_id: str, row: dict[str, Any]) -> None:
    with STATE_LOCK:
        JOBS[job_id]["log_rows"].append(row)
        JOBS[job_id]["updated_at"] = now_iso()


def get_job_snapshot(job_id: str) -> dict[str, Any]:
    with STATE_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        snapshot = dict(job)
        snapshot["log_rows"] = list(job["log_rows"])
        snapshot["error_rows"] = list(job["error_rows"])
        return snapshot


def generate_audio_job(job_id: str, rows: list[dict[str, Any]], episode_name: str, api_key: str, force_regenerate: bool) -> None:
    generated = skipped = failed = 0
    error_rows: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []

    try:
        safe_name = safe_episode_name(episode_name)
        episode_dir = OUTPUT_ROOT / safe_name
        audio_dir = episode_dir / "audio_raw"
        audio_dir.mkdir(parents=True, exist_ok=True)

        client = ElevenLabs(api_key=api_key)
        total = len(rows)
        update_job(
            job_id,
            status="running",
            status_message="Starting generation",
            episode_dir=str(episode_dir),
            progress=0,
            total=total,
        )

        for position, row in enumerate(rows, start=1):
            line_id = str(row["id"]).strip()
            speaker = str(row["speaker"]).strip()
            voice_id = str(row["voice_id"]).strip()
            text = str(row["text"]).strip()
            filename = output_filename(row)
            output_path = audio_dir / filename
            should_skip = output_path.exists() and not force_regenerate

            update_job(
                job_id,
                current=position,
                progress=round(position / total, 4),
                status_message=(
                    f"Skipping {line_id} - {speaker}"
                    if should_skip
                    else f"Generating {line_id} - {speaker}"
                ),
            )

            if should_skip:
                skipped += 1
                log_row = {
                    "id": line_id,
                    "speaker": speaker,
                    "voice_id": voice_id,
                    "filename": filename,
                    "status": "skipped",
                    "message": "file already exists",
                    "character_count": len(text),
                }
            else:
                try:
                    audio = call_elevenlabs_tts(client, voice_id, text)
                    output_path.write_bytes(audio)
                    generated += 1
                    log_row = {
                        "id": line_id,
                        "speaker": speaker,
                        "voice_id": voice_id,
                        "filename": filename,
                        "status": "generated",
                        "message": "success",
                        "character_count": len(text),
                    }
                except Exception as exc:
                    failed += 1
                    message = str(exc)
                    log_row = {
                        "id": line_id,
                        "speaker": speaker,
                        "voice_id": voice_id,
                        "filename": filename,
                        "status": "failed",
                        "message": message,
                        "character_count": len(text),
                    }
                    error_rows.append(
                        {
                            "id": line_id,
                            "speaker": speaker,
                            "voice_id": voice_id,
                            "filename": filename,
                            "message": message,
                        }
                    )

            log_rows.append(log_row)
            append_job_log(job_id, log_row)
            update_job(job_id, generated=generated, skipped=skipped, failed=failed)

        generation_log = write_csv(
            episode_dir / "generation_log.csv",
            log_rows,
            ["id", "speaker", "voice_id", "filename", "status", "message", "character_count"],
        )
        errors_log = write_csv(
            episode_dir / "errors.csv",
            error_rows,
            ["id", "speaker", "voice_id", "filename", "message"],
        )
        zip_path, zipped_count = create_zip(audio_dir, episode_dir / f"{safe_name}_audio_raw.zip")

        with STATE_LOCK:
            job = JOBS[job_id]
            job["error_rows"] = error_rows

        update_job(
            job_id,
            status="completed",
            status_message="Generation finished",
            generated=generated,
            skipped=skipped,
            failed=failed,
            zipped_count=zipped_count,
            generation_log_path=str(generation_log),
            errors_log_path=str(errors_log),
            zip_path=str(zip_path),
            download_url=f"/api/jobs/{job_id}/download",
            progress=1,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            status_message=str(exc),
            generated=generated,
            skipped=skipped,
            failed=failed,
        )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": APP_TITLE, "time": now_iso()}


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    return {
        "app_title": APP_TITLE,
        "api_key_configured": bool(get_api_key()),
        "model_id": MODEL_ID,
        "output_format": OUTPUT_FORMAT,
        "storage": storage_label(),
        "known_speakers": [
            {"speaker": speaker, "voice_id": voice_id}
            for speaker, voice_id in EXPECTED_VOICE_MAP.items()
        ],
    }


@app.post("/api/analyze")
async def analyze_csv(
    file: UploadFile = File(...),
    episode_name: str = Form("EP01"),
) -> dict[str, Any]:
    contents = await file.read()
    df, read_error = read_csv_bytes(contents)
    if read_error or df is None:
        raise HTTPException(status_code=400, detail=read_error or "Cannot read CSV file")

    analysis = analyze_dataframe(df)
    analysis_id = uuid.uuid4().hex
    analysis["filename"] = file.filename or "uploaded.csv"
    analysis["episode_name"] = safe_episode_name(episode_name)
    analysis["analyzed_at"] = now_iso()

    with STATE_LOCK:
        ANALYSES[analysis_id] = analysis

    prune_state()
    return serialize_analysis(analysis_id, analysis)


@app.post("/api/generate")
def start_generation(request: GenerateRequest) -> dict[str, Any]:
    api_key = get_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="ELEVENLABS_API_KEY is not configured")

    with STATE_LOCK:
        analysis = ANALYSES.get(request.analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found. Please analyze the CSV again.")
    if not analysis["valid"]:
        raise HTTPException(status_code=400, detail="CSV has blocking errors. Please fix and analyze again.")

    rows = records_from_dataframe(analysis["df"])
    if not rows:
        raise HTTPException(status_code=400, detail="No rows to generate")

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "analysis_id": request.analysis_id,
        "status": "queued",
        "status_message": "Queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "episode_name": safe_episode_name(request.episode_name),
        "force_regenerate": bool(request.force_regenerate),
        "model_id": MODEL_ID,
        "output_format": OUTPUT_FORMAT,
        "progress": 0,
        "current": 0,
        "total": len(rows),
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "zipped_count": 0,
        "log_rows": [],
        "error_rows": [],
        "zip_path": "",
        "download_url": "",
    }

    with STATE_LOCK:
        JOBS[job_id] = job

    EXECUTOR.submit(generate_audio_job, job_id, rows, request.episode_name, api_key, request.force_regenerate)
    prune_state()
    return get_job_snapshot(job_id)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    return get_job_snapshot(job_id)


@app.get("/api/jobs/{job_id}/download")
def download_zip(job_id: str) -> FileResponse:
    job = get_job_snapshot(job_id)
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Generation is not complete yet")

    zip_path = Path(job["zip_path"])
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="ZIP file not found")

    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)
