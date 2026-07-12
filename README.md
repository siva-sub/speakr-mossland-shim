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

Create `mcp/.env` (gitignored — never commit your token):

```bash
cp mcp/.env.example mcp/.env  # edit: paste your speakr API token
```

```env
SPEAKR_TOKEN=your-speakr-api-token
SPEAKR_URL=http://127.0.0.1:8899
SHIM_URL=http://127.0.0.1:8001
```

The `run.sh` launcher sources `mcp/.env` and starts the Python MCP server. Pi launches it on first tool call (`lifecycle: lazy`).

### Register in Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "speakr": {
      "command": "bash",
      "args": ["/path/to/speakr-mossland-shim/mcp/run.sh"],
      "env": {
        "SPEAKR_TOKEN": "your-speakr-api-token",
        "SPEAKR_URL": "http://127.0.0.1:8899",
        "SHIM_URL": "http://127.0.0.1:8001"
      }
    }
  }
}
```

### Getting a speakr API token

1. Start speakr (`docker compose up -d`)
2. Open `http://localhost:8899` and register an account
3. Go to **Account → API Keys** and create a token
4. Paste it into `mcp/.env`

### Available tools (37 total)

#### Recordings (7)

| Tool | Purpose |
|------|---------|
| `speakr_upload` | Upload audio/video, auto-compress to 16kHz mono, auto-start containers, auto-queue transcription |
| `speakr_list_recordings` | List with pagination, filters (folder, tag, status, search) |
| `speakr_get_recording` | Get full details for one recording |
| `speakr_update_recording` | Update title, meeting_date, folder, note |
| `speakr_delete_recording` | Delete a recording |
| `speakr_get_status` | Poll processing status |

#### Transcript (1)

| Tool | Purpose |
|------|---------|
| `speakr_get_transcript` | Get full transcript with speaker labels and timestamps |

#### Summary (3)

| Tool | Purpose |
|------|---------|
| `speakr_get_summary` | Get the AI-generated summary |
| `speakr_summarize` | Queue/regenerate summary (optional custom prompt) |
| `speakr_replace_summary` | Replace summary text manually |

#### Chat (1)

| Tool | Purpose |
|------|---------|
| `speakr_chat` | Ask questions about a recording (uses transcript as context) |

#### Speakers — per recording (3)

| Tool | Purpose |
|------|---------|
| `speakr_get_speakers` | Get speakers detected in a recording |
| `speakr_assign_speakers` | **Rename speaker labels** (e.g. S01 → "Alice") |
| `speakr_identify_speakers` | Auto-identify speakers via LLM |

#### Speakers — global library (4)

| Tool | Purpose |
|------|---------|
| `speakr_list_speakers` | List all known speakers |
| `speakr_create_speaker` | Create a new speaker profile |
| `speakr_update_speaker` | Update a speaker's name/description |
| `speakr_delete_speaker` | Delete a speaker profile |

#### Transcription control (2)

| Tool | Purpose |
|------|---------|
| `speakr_transcribe` | Queue or re-queue transcription |
| `speakr_batch_transcribe` | Queue multiple recordings at once |

#### Folders (4)

| Tool | Purpose |
|------|---------|
| `speakr_list_folders` | List all folders |
| `speakr_create_folder` | Create a folder |
| `speakr_update_folder` | Rename a folder |
| `speakr_delete_folder` | Delete a folder |

#### Tags (4)

| Tool | Purpose |
|------|---------|
| `speakr_list_tags` | List all tags |
| `speakr_create_tag` | Create a tag |
| `speakr_add_tag` | Add a tag to a recording |
| `speakr_remove_tag` | Remove a tag from a recording |

#### Notes (2)

| Tool | Purpose |
|------|---------|
| `speakr_get_notes` | Get notes for a recording |
| `speakr_update_notes` | Replace notes for a recording |

#### Meta (4)

| Tool | Purpose |
|------|---------|
| `speakr_get_stats` | System statistics (storage, queue, recordings) |
| `speakr_get_user` | Current user profile and preferences |
| `speakr_get_transcription_info` | Active connector and capabilities |
| `speakr_toggle_auto_summarization` | Toggle auto-summarization on/off |

#### Shim tools (2)

| Tool | Purpose |
|------|---------|
| `speakr_shim_health` | Check the mossland-shim health (streaming mode, model version, key status). Auto-starts containers if down. |
| `speakr_shim_transcribe` | Transcribe audio directly through the shim, bypassing speakr. SSE streaming + single-pass. |

### Auto-start behavior

The MCP server automatically checks if the `mossland-shim` and `speakr` Docker containers are running before every upload and health check. If either is stopped, it runs `docker start <name>` to bring it back up. No manual intervention needed after reboots or crashes.

### Example: upload and rename speakers

```python
# Via MCP tool call (from Pi, Claude Desktop, etc.):

# 1. Upload (auto-compresses to 16kHz mono, auto-starts containers)
speakr_upload(file_path="/path/to/meeting.mp3", title="Team Meeting")
# → {"id": 1, "status": "PENDING"}

# 2. Wait for transcription (poll status)
speakr_get_status(recording_id=1)
# → {"status": "COMPLETED"}

# 3. Get the transcript with speakers
speakr_get_transcript(recording_id=1)
# → {"segments": [{"speaker": "S01", "start": 0.1, "end": 5.2, "text": "..."}, ...]}

# 4. Rename speakers
speakr_assign_speakers(
    recording_id=1,
    speaker_map={
        "S01": "Alice",
        "S02": {"name": "Bob", "isMe": true},
        "S03": "Charlie"
    }
)
# → {"success": true, "participants": "Alice, Bob, Charlie"}

# 5. Get the AI summary
speakr_get_summary(recording_id=1)
# → {"summary": "### Minutes\n\n..."}

# 6. Chat about the recording
speakr_chat(recording_id=1, message="What were the key decisions?")
# → {"response": "The team decided to..."}
```

See `skills/speakr/SKILL.md` for the full skill with anti-patterns, tips, and troubleshooting.

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