---
name: speakr-shim
description: "How to build, modify, and swap the OpenAI-compatible transcription shim that sits between speakr and any STT provider. Use when changing the transcription backend (Mossland, WhisperX, Parakeet, Deepgram, etc.), adding model aliases, adjusting polling/timeout behavior, or debugging the transcription pipeline."
license: MIT
metadata:
  author: siva-sub
  version: '0.3.0'
---

# Speakr Shim — Building and Swapping Transcription Backends

## When to use this skill

Use this skill when the user wants to:

- **Change the STT backend** — switch from Mossland to WhisperX, Parakeet, Deepgram, Soniox, or any other provider
- **Add a new model alias** — make speakr accept a model name it doesn't know by default
- **Fix transcription errors** — debug the shim, upstream API, or speakr connector
- **Build a new shim from scratch** — wrap a non-OpenAI API behind the standard `/v1/audio/transcriptions` shape
- **Adjust timeouts, polling, streaming, file-size limits** — tune the shim for longer/shorter audio
- **Understand the transcription pipeline** — trace a request from speakr through the shim to the upstream API

## How the shim pattern works

```
speakr (port 8899)                   mossland-shim v0.3 (port 8001)          Mossland API
┌─────────────────┐                 ┌──────────────────────┐              ┌──────────────┐
│ openai_transcribe│  multipart     │ FastAPI proxy        │  multipart   │ api.mosi.cn  │
│ connector       │ ──────────────> │                      │ ───────────> │              │
│                 │                 │ 1. validate model    │              │ stream=true  │
│ Sends:          │                 │ 2. validate size     │              │ (SSE stream) │
│  - file (audio) │                 │ 3. SSE stream OR     │ <─────────── │ Returns:     │
│  - model=...    │                 │    async+poll OR     │  SSE events  │  segments +  │
│  - response_fmt │                 │    5-min chunking    │              │  speakers    │
│                 │ <────────────── │ 4. collect events    │              │              │
│                 │  OpenAI-shaped  │ 5. return JSON       │              │              │
│                 │  JSON response  │                      │              │              │
└─────────────────┘                 └──────────────────────┘              └──────────────┘
```

**Three transport modes** (cascading fallback):

1. **SSE streaming** (primary, `USE_STREAMING=true`): sends `stream=true`, collects `transcript.segment.done` and `transcript.text.done` SSE events in real-time. No polling. Fastest.
2. **Async polling** (fallback): sends `async=true`, polls `GET /v1/audio/tasks/{id}` every 5s. Used if SSE fails.
3. **5-min chunking** (last resort): splits audio into 5-min segments, processes each separately, merges with offset timestamps. Used if both SSE and async fail.

**Key API params** (sent on every request):

| Param | Value | Purpose |
|---|---|---|
| `model` | `moss-transcribe-diarize` | The Pro model, hosted via API |
| `version` | `v20260410-streamparam-20260703` | Required for moss-transcribe-diarize |
| `diarize` | `true` | Enables multi-speaker separation |
| `sampling_params` | `{"max_new_tokens":65536,"temperature":0}` | Single-pass for up to 90-min audio |
| `stream` | `true` (SSE mode) | Real-time streaming transcription |
| `async` | `true` (poll mode) | Async task submission |

**The core idea:** speakr (and any OpenAI-compatible client) sends a standard multipart POST to `/v1/audio/transcriptions`. The shim receives it, translates it to whatever shape the upstream API expects, calls the upstream, collects the result (potentially polling), translates the response back to the OpenAI shape, and returns it.

This means **speakr never needs to know which STT provider is actually doing the work**. You can swap providers by changing only the shim — no speakr code changes.

## The current shim: mossland-shim

**Location:** `./shim/`

**Upstream:** Mossland API (`https://api.mosi.cn/v1`)  
**Model:** `moss-transcribe-diarize` (MOSS-Transcribe-Diarize 0.9B)

**Key shim behaviors (learned from live-API probing):**

| Behavior | Why |
|----------|-----|
| Uses `stream=true` (SSE streaming) as primary mode | Real-time results via `transcript.segment.done` events with speaker labels. No polling needed. Falls back to `async=true` + polling, then 5-min chunking. |
| Sends `version=v20260410-streamparam-20260703` | Required for moss-transcribe-diarize per the official API docs. |
| Sends `diarize=true` | Explicitly enables multi-speaker diarization. |
| Sends `sampling_params={"max_new_tokens":65536}` | Enables single-pass transcription up to 90 min. Without this, default max_new_tokens=5120 truncates at ~20 min (see [sglang-omni #1034](https://github.com/sgl-project/sglang-omni/issues/1034)). |
| Accepts `gpt-4o-transcribe-diarize` as an alias | speakr's connector validates model names against an OpenAI allowlist. The shim translates the alias to `moss-transcribe-diarize` before forwarding. |
| Returns verbose_json shape regardless of upstream | The shim collects SSE events or polls the task endpoint and always returns segments with speaker labels in the OpenAI format. |
| Validates model name and file size before forwarding | Failed requests still charge Mossland credits. Pre-validation saves money. |
| Auto-starts Docker containers if down | The MCP server's `_ensure_containers()` function checks and starts mossland-shim and speakr before every upload and health check. |

## How to swap the backend

### Option A: Switch to a different cloud API (e.g., Deepgram, AssemblyAI, Soniox)

1. **Create a new shim directory:**

   ```bash
   mkdir -p ./stt-shim-deepgram
   ```

2. **Write the shim** — copy the mossland-shim structure and change:
   - `MOSI_BASE_URL` → the new provider's base URL
   - `MOSI_API_KEY` env var name → provider's key name
   - `_post_to_mosiland()` → adapt the request shape (some providers use JSON, not multipart; some use WebSocket)
   - `_poll_task()` → adapt the polling endpoint (some providers are synchronous, no polling needed)
   - `_format_openai_segments()` → map the provider's segment format to OpenAI's `{id, start, end, text, speaker}`

3. **Write a Dockerfile** (same pattern — python:3.12-slim + fastapi + httpx + python-multipart)

4. **Build and run:**

   ```bash
   docker build -t stt-shim-deepgram:local ./stt-shim-deepgram
   docker run -d --name stt-shim-deepgram \
     -p 127.0.0.1:8002:8000 \
     --env-file ./stt-shim-deepgram/.env \
     stt-shim-deepgram:local
   ```

5. **Update speakr's env** to point at the new shim:

   ```bash
   # In speakr.env, change:
   TRANSCRIPTION_BASE_URL=http://stt-shim-deepgram:8000/v1
   TRANSCRIPTION_MODEL=deepgram-nova-2   # or whatever model name
   # Then restart speakr:
   docker restart speakr
   ```

6. **Update the MCP config** if needed (the speakr MCP server doesn't change — it talks to speakr, not the shim directly).

### Option B: Switch to a local model (e.g., WhisperX, Parakeet via vLLM/SGLang)

For local inference you don't need a shim — the inference server IS OpenAI-compatible:

1. **Start the inference server** (e.g., vLLM or sglang-omni with a Whisper/Parakeet model)

2. **Point speakr directly at it:**

   ```bash
   # In speakr.env:
   TRANSCRIPTION_BASE_URL=http://whisperx-server:9000/v1
   TRANSCRIPTION_MODEL=whisper-1
   # Or use the ASR endpoint connector:
   # ASR_BASE_URL=http://whisperx-server:9000
   # USE_ASR_ENDPOINT=true
   ```

3. No shim needed — the local server speaks OpenAI natively.

### Option C: Switch to speakr's built-in connectors (no shim at all)

speakr supports several connectors natively (no custom shim):

| Connector | Env vars | Notes |
|-----------|----------|-------|
| `openai_whisper` | `TRANSCRIPTION_API_KEY`, `TRANSCRIPTION_MODEL=whisper-1` | OpenAI Whisper API |
| `openai_transcribe` | `TRANSCRIPTION_API_KEY`, `TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize` | GPT-4o Transcribe |
| `asr_endpoint` | `ASR_BASE_URL=http://your-asr:9000` | WhisperX / whisper-asr-webservice |
| `mistral` | `TRANSCRIPTION_CONNECTOR=mistral`, `TRANSCRIPTION_API_KEY=...` | Mistral Voxtral |
| `vibevoice` | `TRANSCRIPTION_CONNECTOR=vibevoice`, `TRANSCRIPTION_BASE_URL=http://vllm:8000` | VibeVoice via vLLM |
| `azure_openai_transcribe` | Azure-specific env vars | Azure OpenAI |

To use any of these, remove `TRANSCRIPTION_BASE_URL` (shim) from speakr.env and set the connector-specific vars instead.

## How to modify the existing shim

### Add a new model alias

In `./shim/app.py`, edit `MODEL_ALIASES`:

```python
MODEL_ALIASES = {
    "gpt-4o-transcribe-diarize": "moss-transcribe-diarize",
    "gpt-4o-transcribe": "moss-transcribe",
    # Add new aliases here:
    "whisper-1": "moss-transcribe",        # make whisper-1 route to Mossland
    "my-custom-model": "moss-transcribe-diarize",
}
```

Then rebuild:

```bash
docker build -t mossland-shim:local ./shim
docker restart mossland-shim
```

### Change the polling interval / timeout

In `./shim/.env` or the docker run command:

```env
POLL_INTERVAL_SECONDS=5.0    # poll every 5s instead of 2s (less API load)
POLL_TIMEOUT_SECONDS=1800    # wait up to 30 min for long audio
REQUEST_TIMEOUT_SECONDS=900  # 15 min for the initial POST
```

### Change the file size limit

```env
MAX_UPLOAD_BYTES=52428800    # 50 MB instead of 25 MB
```

### Change the upstream model

```env
MOSI_DEFAULT_MODEL=moss-transcribe    # use the non-diarize variant
```

Or change speakr's `TRANSCRIPTION_MODEL` in `speakr.env`.

## How to build a new shim from scratch (template)

The minimal shim is ~100 lines. Here's the skeleton:

```python
"""Shim: translate OpenAI /v1/audio/transcriptions → [your provider]'s API."""
import os, time, httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from typing import Any

UPSTREAM_URL = os.environ["UPSTREAM_URL"]
UPSTREAM_KEY = os.environ["UPSTREAM_KEY"]

app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "upstream": UPSTREAM_URL}

@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str | None = Form(default="default"),
    response_format: str | None = Form(default="json"),
):
    audio = await file.read()

    # 1. Call your upstream API (adapt the shape)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{UPSTREAM_URL}/transcribe",
            headers={"Authorization": f"Bearer {UPSTREAM_KEY}"},
            files={"audio": (file.filename, audio)},
            timeout=300,
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    result = r.json()

    # 2. Translate to OpenAI shape
    segments = []
    for seg in result.get("segments", []):
        segments.append({
            "id": seg.get("id", 0),
            "start": seg.get("start", 0.0),
            "end": seg.get("end", 0.0),
            "text": seg.get("text", ""),
        })
        if seg.get("speaker"):
            segments[-1]["speaker"] = seg["speaker"]

    # 3. Return
    rf = (response_format or "json").lower()
    if rf == "json":
        return JSONResponse({"text": result.get("text", "")})
    return JSONResponse({"text": result.get("text", ""), "segments": segments})
```

**Dockerfile (universal):**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx python-multipart
COPY app.py .
RUN useradd --create-home --shell /usr/sbin/nologin appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Anti-patterns

### DO NOT skip the healthz endpoint

Every shim must have `GET /healthz` returning `{"status": "ok", "key_configured": bool}`. Without it, Docker healthchecks and manual debugging are blind. speakr's connector init also benefits from being able to probe the shim.

### DO NOT hardcode the API key in the source

Keys go in `.env` (gitignored) or Docker secrets. The shim reads from `os.environ`. Never commit a key.

### DO NOT send `response_format=verbose_json` to the upstream if it doesn't support it

The Mossland API rejects it (`unsupported_response_format`). Always check what the upstream supports. The shim should absorb the difference: fetch segments however the upstream allows, then return the OpenAI verbose_json shape to the client.

### DO NOT forget `python-multipart` in requirements

FastAPI's `Form(...)` and `File(...)` parameters require `python-multipart`. Without it, the app crashes at import time with `RuntimeError: Form data requires "python-multipart" to be installed`.

### DO NOT assume the upstream's docs are accurate

Live-API probing revealed multiple discrepancies in the Mossland API (task endpoint, async behavior, audio_data rejection). Always test with a real request before trusting the docs. Budget a few credits/tokens for probing.

### DO NOT poll faster than `retry_after`

The Mossland API returns `retry_after: 3` in the async response. Polling every 2s is close to the limit. For providers with different retry windows, adjust `POLL_INTERVAL_SECONDS`.

### DO NOT silently swallow upstream errors

Surface the upstream status code and body in the HTTPException detail. This makes debugging 10x easier when something goes wrong. The mossland-shim includes `upstream_status` and `upstream_body` in every error response.

## Operations guide

### Rebuild the shim after code changes

```bash
docker build -t mossland-shim:local ./shim
docker restart mossland-shim
# Verify:
curl -sS http://127.0.0.1:8001/healthz
```

### Test the shim directly (bypassing speakr)

```bash
# Quick test with verbose_json (shows segments + speakers):
curl -sS -X POST http://127.0.0.1:8001/v1/audio/transcriptions \
  -F "file=@/path/to/audio.mp3" \
  -F "model=gpt-4o-transcribe-diarize" \
  -F "response_format=verbose_json" | python3 -m json.tool

# Plain text:
curl -sS -X POST http://127.0.0.1:8001/v1/audio/transcriptions \
  -F "file=@/path/to/audio.mp3" \
  -F "response_format=text"
```

### Debug the full pipeline

```bash
# Watch the shim's logs (shows upstream calls + polling):
docker logs -f mossland-shim

# Watch speakr's logs (shows connector init + ASR calls):
docker logs -f speakr

# Test each hop independently:
# Hop 1: speakr → shim
docker exec speakr curl -sS http://mossland-shim:8000/healthz

# Hop 2: shim → Mossland
curl -sS -H "Authorization: Bearer $MOSI_API_KEY" https://api.mosi.cn/v1/models | python3 -m json.tool | head -5
```

### Run multiple shims side-by-side

You can run multiple shims on different ports for A/B testing:

```bash
# Mossland on :8001
docker run -d --name mossland-shim -p 127.0.0.1:8001:8000 ...

# Deepgram on :8002
docker run -d --name deepgram-shim -p 127.0.0.1:8002:8000 ...

# WhisperX (local) on :8003
docker run -d --name whisperx-shim -p 127.0.0.1:8003:8000 ...
```

Switch speakr between them by changing `TRANSCRIPTION_BASE_URL` in speakr.env and restarting.

## File locations

| What | Path |
|------|------|
| Mossland shim source | `./shim/app.py` |
| Mossland shim Dockerfile | `./shim/Dockerfile` |
| Mossland shim requirements | `./shim/requirements.txt` |
| Mossland shim secrets | `./shim/.env` |
| speakr env (connector + LLM config) | `./shim/speakr.env` |
| Docker compose (optional) | `./shim/docker-compose.mossland-shim.yml` |
| Shim README | `./shim/README.md` |
| speakr skill | [../speakr/SKILL.md](../speakr/SKILL.md) |
| References | [references.md](references.md) |
