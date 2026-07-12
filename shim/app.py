"""
Mossland -> OpenAI-compatible transcription shim.

Supports two modes:
  1. SSE streaming (default): stream=true, collects transcript.segment.done
     and transcript.text.done events. Real-time, no polling.
  2. Async polling (fallback): async=true + poll /v1/audio/tasks/{id}.

Both modes send version=v20260410-streamparam-20260703, diarize=true, and
sampling_params={"max_new_tokens":65536} for single-pass long audio support.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MOSI_BASE_URL = os.environ.get("MOSI_BASE_URL", "https://api.mosi.cn").rstrip("/")
MOSI_API_KEY = os.environ.get("MOSI_API_KEY", "")
DEFAULT_MODEL = os.environ.get("MOSI_DEFAULT_MODEL", "moss-transcribe-diarize")
ALLOWED_MODELS = {"moss-transcribe-diarize", "moss-transcribe"}
MODEL_ALIASES = {
    "gpt-4o-transcribe-diarize": "moss-transcribe-diarize",
    "gpt-4o-transcribe": "moss-transcribe",
    "gpt-4o-mini-transcribe": "moss-transcribe",
    "gpt-4o-mini-transcribe-2025-12-15": "moss-transcribe",
}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
MODEL_VERSION = os.environ.get("MODEL_VERSION", "v20260410-streamparam-20260703")
SAMPLING_PARAMS = os.environ.get("SAMPLING_PARAMS", '{"max_new_tokens":65536,"temperature":0}')
USE_STREAMING = os.environ.get("USE_STREAMING", "true").lower() == "true"
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5.0"))
POLL_TIMEOUT_SECONDS = float(os.environ.get("POLL_TIMEOUT_SECONDS", "1800"))
CHUNK_DURATION = int(os.environ.get("CHUNK_DURATION", "300"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mossland-shim")

app = FastAPI(title="Mossland Shim", version="0.3.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "upstream": MOSI_BASE_URL,
        "default_model": DEFAULT_MODEL,
        "model_version": MODEL_VERSION,
        "streaming": USE_STREAMING,
        "key_configured": bool(MOSI_API_KEY),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_model(model: str | None) -> str:
    chosen = (model or DEFAULT_MODEL).strip()
    if chosen in MODEL_ALIASES:
        chosen = MODEL_ALIASES[chosen]
    if chosen not in ALLOWED_MODELS:
        raise HTTPException(400, detail={"error": {"message": f"model '{chosen}' not supported"}})
    return chosen


def _get_duration(file_path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _split_audio(file_path: str, chunk_dur: int, tmpdir: str) -> list[tuple[str, float]]:
    pattern = os.path.join(tmpdir, "chunk_%03d.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            file_path,
            "-f",
            "segment",
            "-segment_time",
            str(chunk_dur),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-b:a",
            "32k",
            "-reset_timestamps",
            "1",
            "-loglevel",
            "error",
            pattern,
        ],
        capture_output=True,
        timeout=300,
    )
    chunks = []
    for f in sorted(Path(tmpdir).glob("chunk_*.mp3")):
        idx = int(f.stem.split("_")[1])
        chunks.append((str(f), float(idx * chunk_dur)))
    return chunks


def _form_data(model: str, **extra: str) -> dict[str, str]:
    """Build the standard form data with all required Mossland params."""
    data = {
        "model": model,
        "version": MODEL_VERSION,
        "diarize": "true",
        "sampling_params": SAMPLING_PARAMS,
    }
    data.update(extra)
    return data


def _format_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise segment format from various Mossland response shapes."""
    out = []
    for seg in segments:
        try:
            start = float(seg.get("start", seg.get("start_time", 0)))
            end = float(seg.get("end", seg.get("end_time", 0)))
        except (TypeError, ValueError):
            continue
        item = {
            "id": seg.get("id") or seg.get("content_index") or f"seg_{len(out)}",
            "start": round(start, 2),
            "end": round(end, 2),
            "text": seg.get("text", seg.get("sentence", "")),
        }
        if seg.get("speaker"):
            item["speaker"] = seg["speaker"]
        out.append(item)
    return out


def _build_response(
    text: str, segments: list[dict[str, Any]], duration: float = 0
) -> dict[str, Any]:
    resp = {"text": text, "segments": segments}
    if duration:
        resp["duration"] = duration
    return resp


# ---------------------------------------------------------------------------
# SSE Streaming mode (primary)
# ---------------------------------------------------------------------------


async def _submit_streaming(
    client: httpx.AsyncClient, file_path: str, model: str
) -> dict[str, Any]:
    """
    Submit with stream=true, collect SSE events, return as JSON.

    Events:
      task.created            → task metadata
      transcript.text.delta   → incremental word(s)
      transcript.segment.done → {speaker, start, end, text}
      transcript.text.done    → full text
    """
    if not MOSI_API_KEY:
        raise HTTPException(500, detail={"error": {"message": "MOSI_API_KEY missing"}})

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    log.info("SSE streaming: %s size=%d", os.path.basename(file_path), len(file_bytes))

    segments = []
    full_text = ""
    task_id = None

    async with client.stream(
        "POST",
        f"{MOSI_BASE_URL}/v1/audio/transcriptions",
        files={"file": (os.path.basename(file_path), file_bytes)},
        data=_form_data(model, stream="true"),
        headers={"Authorization": f"Bearer {MOSI_API_KEY}"},
        timeout=httpx.Timeout(connect=120.0, read=None, write=120.0, pool=120.0),
    ) as response:
        if response.status_code != 200:
            body = await response.aread()
            raise HTTPException(response.status_code, detail=body.decode()[:500])

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                evt = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            evt_type = evt.get("type", "")

            if evt_type == "task.created":
                task_id = evt.get("task_id")
                log.info("Stream task created: %s", task_id)

            elif evt_type == "transcript.segment.done":
                segments.append(
                    {
                        "id": evt.get("content_index", len(segments)),
                        "start": round(float(evt.get("start", 0)), 2),
                        "end": round(float(evt.get("end", 0)), 2),
                        "text": evt.get("text", ""),
                        "speaker": evt.get("speaker", ""),
                    }
                )

            elif evt_type == "transcript.text.done":
                full_text = evt.get("text", full_text)
                log.info("Stream complete: %d segments, %d chars", len(segments), len(full_text))

    if not full_text and segments:
        full_text = " ".join(s["text"] for s in segments)

    return _build_response(full_text, segments)


# ---------------------------------------------------------------------------
# Async polling mode (fallback)
# ---------------------------------------------------------------------------


async def _submit_async(client: httpx.AsyncClient, file_path: str, model: str) -> dict[str, Any]:
    """Submit with async=true, poll until complete."""
    if not MOSI_API_KEY:
        raise HTTPException(500, detail={"error": {"message": "MOSI_API_KEY missing"}})

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    r = await client.post(
        f"{MOSI_BASE_URL}/v1/audio/transcriptions",
        files={"file": (os.path.basename(file_path), file_bytes)},
        data=_form_data(model, **{"async": "true"}),
        headers={"Authorization": f"Bearer {MOSI_API_KEY}"},
        timeout=120.0,
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, detail=r.text[:500])

    task_id = r.json().get("task_id") or r.json().get("id")
    if not task_id:
        return r.json()

    log.info("Async task: %s, polling...", task_id)
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        r = await client.get(
            f"{MOSI_BASE_URL}/v1/audio/tasks/{task_id}",
            headers={"Authorization": f"Bearer {MOSI_API_KEY}"},
            timeout=30.0,
        )
        if r.status_code == 200:
            payload = r.json()
            status = (payload.get("status") or "").upper()
            if status in {"SUCCESS", "COMPLETED"}:
                segments = _format_segments(payload.get("segments") or [])
                return _build_response(
                    payload.get("text", ""), segments, payload.get("duration", 0)
                )
            if status in {"FAILED", "ERROR"}:
                raise HTTPException(502, detail={"error": {"message": f"task {status}"}})
        if time.monotonic() >= deadline:
            raise HTTPException(504, detail={"error": {"message": f"poll timeout for {task_id}"}})
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _merge_chunked(results: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    all_segments = []
    all_text = []
    for payload, offset in results:
        all_text.append(payload.get("text", ""))
        for seg in payload.get("segments") or []:
            try:
                start = float(seg.get("start", 0)) + offset
                end = float(seg.get("end", 0)) + offset
            except (TypeError, ValueError):
                continue
            item = {
                "id": seg.get("id", f"seg_{len(all_segments)}"),
                "start": round(start, 2),
                "end": round(end, 2),
                "text": seg.get("text", ""),
            }
            if seg.get("speaker"):
                item["speaker"] = seg["speaker"]
            all_segments.append(item)
    return {"text": " ".join(all_text), "segments": all_segments}


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str | None = Form(default=None),
    response_format: str | None = Form(default="json"),
) -> JSONResponse:
    chosen = _resolve_model(model)

    body = await file.read()
    await file.close()

    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, detail={"error": {"message": "file too large"}})

    tmpdir = tempfile.mkdtemp(prefix="shim_")
    input_path = os.path.join(tmpdir, "input")
    with open(input_path, "wb") as f:
        f.write(body)

    try:
        duration = _get_duration(input_path)
        log.info(
            "File: %s size=%d duration=%.0fs streaming=%s",
            file.filename,
            len(body),
            duration,
            USE_STREAMING,
        )

        async with httpx.AsyncClient() as client:
            submit_fn = _submit_streaming if USE_STREAMING else _submit_async

            try:
                result = await submit_fn(client, input_path, chosen)
                log.info("Done: %d segments", len(result.get("segments", [])))
            except Exception as e:
                log.warning("Primary mode failed (%s), trying fallback", e)
                # Try the other mode
                fallback_fn = _submit_async if USE_STREAMING else _submit_streaming
                try:
                    result = await fallback_fn(client, input_path, chosen)
                except Exception:
                    # Last resort: chunk into 5-min segments
                    if duration > CHUNK_DURATION:
                        log.warning("Both modes failed, trying %ds chunking", CHUNK_DURATION)
                        chunks = _split_audio(input_path, CHUNK_DURATION, tmpdir)
                        results = []
                        for chunk_path, offset in chunks:
                            payload = await _submit_async(client, chunk_path, chosen)
                            results.append((payload, offset))
                        result = _merge_chunked(results)
                    else:
                        raise

    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)

    rf = (response_format or "json").lower()
    if rf == "json":
        return JSONResponse({"text": result["text"]})
    if rf == "text":
        return JSONResponse(content=result["text"], media_type="text/plain")
    return JSONResponse(result)
