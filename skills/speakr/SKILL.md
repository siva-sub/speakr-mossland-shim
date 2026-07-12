---
name: speakr
description: "Self-hosted audio transcription, speaker diarization, and AI summarization via the speakr MCP server. Use when the user wants to transcribe audio/video, rename speakers, generate summaries, or chat about recordings. Powered by MOSS-Transcribe-Diarize (via Mossland API) for STT and GLM-5.2 (via Ollama cloud) for summaries/chat."
license: MIT
metadata:
  author: siva-sub
  version: '0.3.0'
---

# Speakr — Audio Transcription & AI Summarization

## When to use

Use the speakr MCP tools when the user wants to:

- **Transcribe audio or video** — meetings, calls, podcasts, interviews, lectures, voice notes
- **Identify and rename speakers** — "who said what" with labels like S01, S02
- **Generate AI summaries** — meeting minutes, action items, key decisions
- **Chat about a recording** — ask questions grounded in the transcript
- **Manage recordings** — list, search, organize into folders, tag, annotate
- **Batch process** — upload multiple files, queue transcriptions

**Do NOT use for:**

- Real-time/streaming transcription (speakr is async/batch only)
- Translating audio (use a translation service)
- Text-to-speech (speakr is STT only; for TTS use the Mossland TTS API directly)
- Transcribing audio shorter than ~10 seconds (the MOSS model may drop the first 60-90s; very short clips produce empty or partial transcripts)

## Architecture

```
User → Pi agent → speakr MCP tools (37 tools, stdio)
                      ↓
                 speakr web app (Docker, port 8899)
                   ├── Transcription → mossland-shim v0.3 (Docker, port 8001)
                   │                     ↓
                   │                 Mossland API (api.mosi.cn)
                   │                 model: moss-transcribe-diarize (= Pro)
                   │                 version: v20260410-streamparam-20260703
                   │                 stream=true (SSE) → real-time segments
                   │                 diarize=true, max_new_tokens=65536
                   │                 (single-pass, up to 90 min)
                   │
                   └── Summary/Chat  → Ollama cloud (ollama.com/v1)
                                       model: glm-5.2
                                       (reasoning model, budgets at 16000)
```

- **STT model:** MOSS-Transcribe-Diarize (= Pro, hosted via API) — joint transcription + speaker diarization + timestamps in one pass. SOTA on multi-speaker benchmarks, beats Whisper-large-v3 and Gemini.
- **Transport:** SSE streaming (primary) → async poll (fallback) → 5-min chunking (last resort). The shim automatically cascades.
- **LLM model:** GLM-5.2 (reasoning model) — for summaries, titles, chat, speaker identification.
- **Cost:** ~16 Mossland credits per minute of audio (transcription only); LLM is via Ollama cloud (quota-based).
- **Auto-start:** The MCP server automatically starts Docker containers (mossland-shim, speakr) if they are down. No manual intervention needed.

## Key workflows

### 1. Upload and transcribe

```
speakr_upload(file_path="/path/to/audio.mp3", title="Team Meeting")
  → returns recording_id, status="PENDING"
  → transcription queued automatically via mossland-shim → Mossland API
```

Transcription is async. Poll status:

```
speakr_get_status(recording_id=1)
  → {"status": "COMPLETED"} when ready
```

Typical times: 2-min clip ≈ 20-40s, 30-min meeting ≈ 2-4 min, 90-min podcast ≈ 5-10 min.

### 2. Get the transcript

```
speakr_get_transcript(recording_id=1)
  → {segments: [{start_time, end_time, speaker: "S01", sentence: "..."}, ...]}
```

Segments include timestamps and speaker labels. The first 60-90 seconds of audio may be missing (known MOSS model behavior — see anti-patterns).

### 3. Rename speakers

This is the primary tool for editing speaker names:

```
speakr_assign_speakers(
  recording_id=1,
  speaker_map={"S01": "Alice", "S02": {"name": "Bob", "isMe": true}}
)
```

Or auto-identify via LLM (GLM-5.2 reads the transcript and guesses names from context):

```
speakr_identify_speakers(recording_id=1)
```

### 4. Get or generate a summary

Summaries are auto-generated after transcription (if auto-summarization is on):

```
speakr_get_summary(recording_id=1)
  → {summary: "### Minutes\n\n..."}
```

Regenerate with a custom prompt:

```
speakr_summarize(recording_id=1, prompt="Extract action items only")
```

### 5. Chat about a recording

```
speakr_chat(recording_id=1, message="What were the key decisions?")
  → {response: "The team decided to..."}
```

Chat is grounded in the transcript only — it does not browse the web or access other recordings.

### 6. Organize

```
speakr_create_folder(name="Q3 Meetings")
speakr_create_tag(name="Important")
speakr_add_tag(recording_id=1, tag_id=2)
```

## Tool reference (37 tools)

### Recordings (7)

| Tool | Purpose |
|------|---------|
| `speakr_upload` | Upload audio/video file, auto-queues transcription |
| `speakr_list_recordings` | List with pagination, filters (folder, tag, status, search) |
| `speakr_get_recording` | Get full details for one recording |
| `speakr_update_recording` | Update title, meeting_date, folder, note |
| `speakr_delete_recording` | Delete a recording |
| `speakr_get_status` | Poll processing status |

### Transcript (1)

| Tool | Purpose |
|------|---------|
| `speakr_get_transcript` | Get full transcript with speaker labels and timestamps |

### Summary (3)

| Tool | Purpose |
|------|---------|
| `speakr_get_summary` | Get the AI-generated summary |
| `speakr_summarize` | Queue/regenerate summary (optional custom prompt) |
| `speakr_replace_summary` | Replace summary text manually |

### Chat (1)

| Tool | Purpose |
|------|---------|
| `speakr_chat` | Ask questions about a recording (uses transcript as context) |

### Speakers — per recording (3)

| Tool | Purpose |
|------|---------|
| `speakr_get_speakers` | Get speakers detected in a recording |
| `speakr_assign_speakers` | **Rename speaker labels** (e.g. S01 to Alice) |
| `speakr_identify_speakers` | Auto-identify speakers via LLM |

### Speakers — global library (4)

| Tool | Purpose |
|------|---------|
| `speakr_list_speakers` | List all known speakers |
| `speakr_create_speaker` | Create a new speaker profile |
| `speakr_update_speaker` | Update a speaker's name/description |
| `speakr_delete_speaker` | Delete a speaker profile |

### Transcription control (2)

| Tool | Purpose |
|------|---------|
| `speakr_transcribe` | Queue or re-queue transcription |
| `speakr_batch_transcribe` | Queue multiple recordings at once |

### Folders (4)

| Tool | Purpose |
|------|---------|
| `speakr_list_folders` | List all folders |
| `speakr_create_folder` | Create a folder |
| `speakr_update_folder` | Rename a folder |
| `speakr_delete_folder` | Delete a folder |

### Tags (4)

| Tool | Purpose |
|------|---------|
| `speakr_list_tags` | List all tags |
| `speakr_create_tag` | Create a tag |
| `speakr_add_tag` | Add a tag to a recording |
| `speakr_remove_tag` | Remove a tag from a recording |

### Notes (2)

| Tool | Purpose |
|------|---------|
| `speakr_get_notes` | Get notes for a recording |
| `speakr_update_notes` | Replace notes for a recording |

### Meta (4)

| Tool | Purpose |
|------|---------|
| `speakr_get_stats` | System statistics (storage, queue, recordings) |
| `speakr_get_user` | Current user profile and preferences |
| `speakr_get_transcription_info` | Active connector and capabilities |
| `speakr_toggle_auto_summarization` | Toggle auto-summarization on/off |

## Anti-patterns

### DO NOT upload without checking the file exists first

The upload tool reads from `file_path` locally. If the path is wrong, the MCP call fails. Always verify the path with `ls` or `file` before calling `speakr_upload`.

### DO NOT assume speakers are the same person across recordings

`S01` in recording A and `S01` in recording B are **not** the same person. The labels are assigned independently per audio file. Use `speakr_assign_speakers` in each recording separately, or use the global speaker library (`speakr_create_speaker` + `speakr_identify_speakers`) for cross-recording voice matching.

### DO NOT expect the first 60-90 seconds of audio to be transcribed

The MOSS-Transcribe-Diarize model has a known behavior where it may skip the first chunk of audio. Observed: a 2-minute file returned segments starting at 78 seconds. For important content in the first minute, consider prepending 60 seconds of silence or splitting the audio.

### DO NOT set token budgets below 16000 for GLM-5.2

GLM-5.2 is a reasoning model. It consumes `max_tokens` on hidden thinking before producing visible output. With the default 8000/5000 budgets, summaries and titles come back **empty** with `finish_reason: length`. The env is set to 16000 across all four operations (SUMMARY, CHAT, TITLE, EVENT). See [speakr issue #264](https://github.com/murtaza-nasir/speakr/issues/264).

### DO NOT call `speakr_chat` or `speakr_summarize` in a tight loop

Each LLM call takes 10-30s (GLM-5.2 reasoning phase). Polling these endpoints rapidly wastes API quota and produces no faster results. Queue once, then check `speakr_get_status`.

### DO NOT use `speakr_transcribe` on a recording that is already transcribed without good reason

Re-transcription charges Mossland credits again (~16/min). Only re-transcribe if the original failed, the audio was updated, or you changed the model/hotwords.

### DO NOT assume `speakr_get_transcription_info` works

The `/api/v1/transcription` endpoint returns HTTP 500 with the current openai_transcribe connector pointing at the mossland-shim. This is a known speakr bug with non-standard connectors. Use `speakr_get_stats` or `speakr_list_recordings` for health checks instead.

### DO NOT forget that failed transcription requests can still charge credits

The Mossland API bills the task at submission time. If the audio is malformed or the codec is unsupported, you still pay. The mossland-shim validates model name and file size before forwarding, but cannot prevent upstream codec rejections.

### DO NOT trust speaker labels across chunk boundaries (FALLBACK MODE ONLY)

**UPDATE:** This issue is now **fixed**. The shim sends `sampling_params={"max_new_tokens":65536}` which enables single-pass transcription of audio up to ~90 minutes. Speaker labels are consistent across the entire file.

The chunking fallback (5-min segments) only activates if single-pass fails. When it does, each chunk gets independent speaker labels that may not correspond to the same person across chunks.

**Benchmark (55-min recording vs ElevenLabs Scribe 2):**

| Mode | Speaker agreement | Speaker labels | Notes |
|---|---:|---|---|
| **Single-pass (current)** | **95.4%** | 3 (S01/S02/S03, consistent) | Errors evenly distributed (2.7–10% per 5-min) |
| Chunked fallback | 56.9% | 10 (fragmented) | 94% error at chunk boundaries |

Root cause of the chunking issue: the MOSS-TD model supports 90-min audio (128K context), but the default `max_new_tokens=5120` truncates at ~20 min. The fix passes `max_new_tokens=65536` via `sampling_params`. See [sglang-omni #1034](https://github.com/sgl-project/sglang-omni/issues/1034).

**Per-speaker accuracy (single-pass vs Scribe 2):**

| Speaker | Accuracy | Segments |
|---|---:|---:|
| Speaker 1 | 87.8% | 626 |
| Speaker 2 | 91.2% | 614 |
| Speaker 3 | 76.9% | 234 |

## Benchmark: MOSS-TD vs ElevenLabs Scribe 2

Head-to-head comparison on a 55-min, 3-speaker recording:

| Metric | ElevenLabs Scribe 2 | MOSS-TD single-pass | MOSS-TD chunked (fallback) |
|---|---:|---:|---:|
| Segments | 908 | 1,474 | 1,117 |
| Words transcribed | 12,161 | ~12,000+ | 11,248 (92%) |
| Word-level timestamps | Yes | No | No |
| Speaker labels (raw) | 4 (named) | 3 (S01-S03) | 10 (fragmented) |
| Speaker agreement vs Scribe | — | **95.4%** | 56.9% |
| Single-pass | Yes | Yes | No (5-min chunks) |
| Cost | Per-minute SaaS | Mossland credits | Mossland credits |

**Text quality:** Comparable. MOSS-TD drops some filler words (`um`, `uh`) but captures the substantive content accurately.

**Speaker diarization:** Single-pass MOSS-TD achieves 95.4% agreement with Scribe 2. The remaining 4.6% is mostly at speaker transitions (overlapping speech, brief interjections).

**When to use which:**

- **ElevenLabs Scribe / Soniox**: long recordings (> 5 min) where speaker accuracy matters
- **MOSS-TD (Mossland)**: short recordings (≤ 5 min), budget-sensitive use, or when the model is self-hosted (sglang-omni with no chunking needed)

## Operations guide

### Start everything (after a reboot)

```bash
# 1. Start the mossland-shim (OpenAI-compat proxy for Mossland API)
docker start mossland-shim

# 2. Start speakr (web UI + REST API)
docker start speakr

# 3. Verify
curl -sS http://127.0.0.1:8001/healthz  # shim health
curl -sS http://127.0.0.1:8899/         # speakr health (should 302 to /login)
```

The MCP server (speakr-mcp) is launched automatically by Pi on first tool call (`lifecycle: lazy`). No manual start needed. The MCP also auto-starts Docker containers (mossland-shim, speakr) if they are down — the `_ensure_containers()` function runs before every upload and health check.

### Stop everything

```bash
docker stop speakr mossland-shim
```

### Restart after config changes

```bash
# If you changed speakr.env (LLM, transcription config):
docker restart speakr

# If you changed the shim (.env, app.py):
docker restart mossland-shim

# If you changed the MCP server (server.py, .env):
# No restart needed — Pi relaunches the lazy server on next tool call.
# To force a reload, use mcp({ connect: "speakr" }) in a Pi session.
``### Rebuild from scratch (full teardown + rebuild)

```bash
# Stop and remove containers
docker rm -f speakr mossland-shim
docker volume rm speakr-data

# Rebuild the shim image
docker build -t mossland-shim:local ./shim

# Start the shim
docker run -d --name mossland-shim --restart unless-stopped \
  -p 127.0.0.1:8001:8000 \
  --env-file ./shim/.env \
  -e MOSI_BASE_URL=https://api.mosi.cn \
  -e MOSI_DEFAULT_MODEL=moss-transcribe-diarize \
  mossland-shim:local

# Start speakr
docker run -d --name speakr --restart unless-stopped \
  --link mossland-shim:mossland-shim \
  -p 127.0.0.1:8899:8899 \
  --env-file ./shim/speakr.env \
  -v speakr-data:/data \
  --memory 2g \
  learnedmachine/speakr:lite
```

### Health checks

```bash
# Shim: should return {"status":"ok","key_configured":true}
curl -sS http://127.0.0.1:8001/healthz

# speakr: should return HTML (302 redirect to /login)
curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8899/

# MCP: test from Pi
# mcp({ server: "speakr" })  → should list 35 tools

# Docker logs
docker logs --tail 20 speakr
docker logs --tail 20 mossland-shim
```

### Rotating API keys

Three keys are in use, stored in gitignored files:

| Key | File | Used by | Where to rotate |
|-----|------|---------|-----------------|
| Mossland API | `./shim/.env` | mossland-shim container | [studio.mosi.cn/app/api-keys](https://studio.mosi.cn/app/api-keys) |
| Ollama cloud | `./shim/speakr.env` | speakr container | [ollama.com/settings](https://ollama.com/settings) |
| speakr API token | `./mcp/.env` | speakr MCP server | speakr UI: Account > API Keys |

After rotating, update the file and restart the corresponding service.

## File locations

| What | Path |
|------|------|
| MCP server source | `./mcp/server.py` |
| MCP server launcher | `./mcp/run.sh` |
| MCP server secrets | `./mcp/.env` |
| Mossland shim source | `./shim/app.py` |
| Mossland shim Dockerfile | `./shim/Dockerfile` |
| Mossland shim secrets | `./shim/.env` |
| speakr env (LLM + transcription) | `./shim/speakr.env` |
| Pi MCP config | `~/.pi/agent/mcp.json` |
| This skill | `~/.pi/agent/skills/speakr/SKILL.md` |
| References | [references.md](references.md) |

## References

See [references.md](references.md) for the full list of upstream projects, model cards, API docs, Docker images, GitHub issues, research papers, and community resources.
