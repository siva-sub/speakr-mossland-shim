# speakr-mossland-shim

Use the hosted **MOSS-Transcribe-Diarize** model (via the Mossland/MOSI API) as the transcription backend for [speakr](https://github.com/murtaza-nasir/speakr) — or any OpenAI-compatible transcription client.

## What this gives you

- **Speaker diarization + timestamps** in a single model pass (up to 90 min audio)
- **95.4% speaker accuracy** vs ElevenLabs Scribe 2 (benchmark on a 55-min, 3-speaker recording)
- **Real-time SSE streaming** from the Mossland API (no polling)
- **Single-pass long audio** — no chunking, no speaker label fragmentation
- **AI summaries + chat** via GLM-5.2 (or any OpenAI-compatible LLM)
- **MCP server** with 37 tools for programmatic access from Pi, Claude Desktop, or any MCP client

## Architecture

```
Your audio file
    ↓
speakr (web UI + REST API, port 8899)
    ├── Transcription → mossland-shim (port 8001)
    │                     ├── SSE streaming (primary)
    │                     ├── Async polling (fallback)
    │                     └── 5-min chunking (last resort)
    │                     ↓
    │                 Mossland API (api.mosi.cn)
    │                 model: moss-transcribe-diarize (= Pro)
    │                 version: v20260410-streamparam-20260703
    │                 stream=true, diarize=true, max_new_tokens=65536
    │
    └── Summary/Chat  → Your LLM (Ollama, OpenAI, OpenRouter, etc.)
```

The shim translates between speakr's OpenAI-compatible requests and the Mossland API's specific requirements (SSE streaming, `version` param, `sampling_params` for long audio, `diarize=true`).

## Quick start

### Prerequisites

- Docker + Docker Compose
- A Mossland API key ([get one here](https://studio.mosi.cn/app/api-keys))
- An LLM API key for summaries (optional — speakr works without it, just no AI summaries)

### 1. Clone and configure

```bash
git clone https://github.com/siva-sub/speakr-mossland-shim.git
cd speakr-mossland-shim

# Create your env files from the examples
cp shim/.env.example shim/.env          # edit: paste your Mossland API key
cp speakr.env.example speakr.env        # edit: paste your LLM API key
```

### 2. Start the stack

```bash
docker compose up -d --build
```

### 3. Verify

```bash
# Check the shim is healthy
curl http://localhost:8001/healthz
# → {"status":"ok","streaming":true,"model_version":"v20260410-streamparam-20260703",...}

# Open speakr
open http://localhost:8899
# Register an account, upload an audio file
```

### 4. Transcribe

Upload any audio file via the speakr web UI or REST API. The shim handles everything:
- Compresses audio to 16kHz mono (the model's native format)
- Sends to Mossland with SSE streaming
- Collects segments with speaker labels and timestamps
- Returns as OpenAI-compatible JSON to speakr
- speakr triggers AI summarization via your configured LLM

## Using the MCP server

The MCP server exposes 37 tools for programmatic access from any MCP client (Pi, Claude Desktop, Cursor, etc.).

### Register in Pi

Add to `~/.pi/agent/mcp.json`:

```json
{
  "speakr": {
    "command": "bash",
    "args": ["/path/to/speakr-mossland-shim/mcp/run.sh"],
    "lifecycle": "lazy",
    "idleTimeout": 30
  }
}
```

Create `mcp/.env` with:

```
SPEAKR_TOKEN=your-speakr-api-token
SPEAKR_URL=http://127.0.0.1:8899
SHIM_URL=http://127.0.0.1:8001
```

### Available tools

- **Recording management**: upload, list, get, update, delete, status
- **Transcript**: get full transcript with speakers and timestamps
- **Summary**: get, generate, replace AI summaries
- **Chat**: ask questions about any recording
- **Speakers**: get, rename, auto-identify, global library CRUD
- **Organization**: folders, tags, notes
- **Shim**: health check, direct transcription (bypass speakr)
- **Auto-start**: Docker containers start automatically if down

See `skills/speakr/SKILL.md` for the full 37-tool reference.

## Configuration

### Shim environment (`shim/.env`)

| Variable | Default | Description |
|---|---|---|
| `MOSI_API_KEY` | (required) | Mossland API key |
| `MOSI_BASE_URL` | `https://api.mosi.cn` | Upstream API |
| `MOSI_DEFAULT_MODEL` | `moss-transcribe-diarize` | Model ID |
| `MODEL_VERSION` | `v20260410-streamparam-20260703` | Required version tag |
| `SAMPLING_PARAMS` | `{"max_new_tokens":65536,"temperature":0}` | JSON for long audio |
| `USE_STREAMING` | `true` | SSE streaming (primary mode) |
| `MAX_UPLOAD_BYTES` | `104857600` | 100 MB file size limit |
| `CHUNK_DURATION` | `300` | Fallback chunk size (seconds) |

### speakr environment (`speakr.env`)

See `speakr.env.example` for all options. Key settings:

| Variable | Value | Why |
|---|---|---|
| `TRANSCRIPTION_CONNECTOR` | `openai_transcribe` | Uses speakr's OpenAI client |
| `TRANSCRIPTION_BASE_URL` | `http://mossland-shim:8000/v1` | Points at the shim |
| `TRANSCRIPTION_MODEL` | `gpt-4o-transcribe-diarize` | Alias the shim translates to `moss-transcribe-diarize` |
| `ENABLE_CHUNKING` | `false` | The shim handles long audio; speakr shouldn't chunk |
| `TEXT_MODEL_*` | your LLM config | Summaries, titles, chat |

## Benchmark

55-min recording, 3 speakers (Maha, Siva, Jael), compared against ElevenLabs Scribe 2:

| Metric | ElevenLabs Scribe 2 | MOSS-TD (this shim) |
|---|---:|---:|
| Speaker agreement | baseline | **95.4%** |
| Segments | 908 | 1,474 |
| Word-level timestamps | Yes | No |
| Speaker labels | 4 (consistent) | 3 (consistent) |
| Single-pass | Yes | Yes |
| Cost | Per-minute SaaS | Mossland credits (~16/min) |

Per-speaker accuracy: Maha 87.8%, Siva 91.2%, Jael 76.9%.

## Why this exists

The Mossland API hosts the MOSS-Transcribe-Diarize model (SOTA for multi-speaker ASR), but its API shape differs from what OpenAI-compatible clients expect:

1. **No `response_format=verbose_json`** — the API returns SSE events or a bare `{"text": "..."}` sync response
2. **Requires `version` param** — `v20260410-streamparam-20260703`
3. **Requires `diarize=true`** — explicitly enable speaker separation
4. **Long audio needs `sampling_params`** — default `max_new_tokens=5120` truncates at ~20 min; `65536` enables 90-min single-pass
5. **No `audio_data` base64** — multipart upload only
6. **Task query endpoint mismatch** — docs say `/v1/audio/transcriptions/{id}` but it 404s; `/v1/audio/tasks/{id}` works

The shim absorbs all of these so any OpenAI-compatible client (speakr, Whisper API clients, etc.) can use the Mossland API without code changes.

## Skills (for Pi / MCP agents)

Two Pi skills are included for agents that need to understand the system:

- **`speakr`** — when to use the MCP tools, workflows, anti-patterns, benchmark data
- **`speakr-shim`** — how the shim works, how to swap backends, how to build new shims

## License

MIT. The underlying models and services have their own licenses:
- [MOSS-Transcribe-Diarize](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize) — Apache 2.0
- [speakr](https://github.com/murtaza-nasir/speakr) — AGPL-3.0
- Mossland API — commercial (per-minute credits)