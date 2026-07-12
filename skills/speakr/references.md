# Speakr MCP — References

Comprehensive reference for all upstream projects, models, APIs, and resources
that power the speakr + mossland-shim + MCP stack.

---

## Local file layout

| Component | Path |
|-----------|------|
| MCP server (35 tools, Python/FastMCP) | `./mcp/server.py` |
| MCP launcher (sets env, exec python3) | `./mcp/run.sh` |
| MCP secrets (speakr API token) | `./mcp/.env` |
| Mossland shim (FastAPI, OpenAI-compat proxy) | `./shim/app.py` |
| Mossland shim Dockerfile | `./shim/Dockerfile` |
| Mossland shim secrets (Mossland key) | `./shim/.env` |
| speakr container env (LLM + ASR config) | `./shim/speakr.env` |
| Pi MCP config (registers speakr server) | `~/.pi/agent/mcp.json` |
| Pi skill (this document set) | `~/.pi/agent/skills/speakr/` |

---

## Docker containers

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `mossland-shim` | `mossland-shim:local` (built from `./shim/`) | `127.0.0.1:8001` | OpenAI-compat proxy → Mossland API. Accepts multipart, sends `async=true`, polls `/v1/audio/tasks/{id}`, returns segments with speaker labels. |
| `speakr` | `learnedmachine/speakr:lite` | `127.0.0.1:8899` | Web UI + REST API v1. Uses `openai_transcribe` connector pointed at the shim. LLM via Ollama cloud (glm-5.2). |

---

## Upstream projects

### Core

| Project | URL | Stars | Role |
|---------|-----|------:|------|
| **speakr** | <https://github.com/murtaza-nasir/speakr> | 3,518 | Self-hosted transcription web app. Connector architecture supports OpenAI, WhisperX, Mistral Voxtral, VibeVoice, and our custom shim. |
| **MOSS-Transcribe-Diarize** | <https://github.com/OpenMOSS/MOSS-Transcribe-Diarize> | 311 | The official model repo. Contains `mtd-subtitle-web` Gradio app, Python inference helpers, CLI tools. |
| **MOSS-TTS Family** | <https://github.com/OpenMOSS/MOSS-TTS> | 3,762 | The broader model family (TTS, TTSD, VoiceGenerator, SoundEffect). Same team (OpenMOSS / MOSI.AI). |

### Inference engines (not used locally — cloud API instead)

| Project | URL | Stars | Role |
|---------|-----|------:|------|
| sglang-omni | <https://github.com/sgl-project/sglang-omni> | 613 | Production server framework for MOSS models. OpenAI-compatible `/v1/audio/transcriptions`. The recommended serving path for GPUs. |
| vLLM | <https://github.com/vllm-project/vllm> | 85.7k | Added MOSS-Transcribe-Diarize support ([issue #47729](https://github.com/vllm-project/vllm/issues/47729)). |
| moss-transcribe.cpp | <https://github.com/mudler/moss-transcribe.cpp> | 15 | C++17/ggml from-scratch port. CPU inference. GGUF weights at <https://huggingface.co/mudler/moss-transcribe.cpp-gguf>. |
| CrispASR | <https://github.com/CrispStrobe/CrispASR> | 422 | Another C++/ggml port. GGUF weights at <https://huggingface.co/cstr/MOSS-Transcribe-Diarize-GGUF>. |
| LocalAI | <https://github.com/mudler/LocalAI> | 47,495 | Has a built-in `moss-transcribe-cpp` backend ([PR #10756](https://github.com/mudler/LocalAI/pull/10756)). "Ollama for speech" — pluggable engines behind OpenAI-compatible API. |

### MCP ecosystem

| Project | URL | Role |
|---------|-----|------|
| MCP Python SDK | <https://github.com/modelcontextprotocol/python-sdk> | The `mcp` package (v1.28.1). `FastMCP` class used by our server. |
| OpenAPI-to-MCP bridges | Various (taskade/mcp, criteo/openapi-to-mcp, etc.) | Generic bridges that could wrap speakr's OpenAPI if you want auto-generated tools instead of hand-written. Not used — we hand-wrote for control. |

---

## Models

### MOSS-Transcribe-Diarize (the STT model)

| Variant | URL | Size | Notes |
|---------|-----|-----:|-------|
| Official (BF16 safetensors) | <https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize> | 1.73 GB | The canonical weights. Custom transformers code (`trust_remote_code=True`). Needs CUDA. |
| GGUF (mudler port) | <https://huggingface.co/mudler/moss-transcribe.cpp-gguf> | 511 MB – 1.8 GB | q4_0 through f16. CPU inference via moss-transcribe.cpp. q5_k = byte-identical to reference. |
| GGUF (CrispASR port) | <https://huggingface.co/cstr/MOSS-Transcribe-Diarize-GGUF> | 1.1 – 1.7 GB | q4_k, q8_0, f16. CPU inference via CrispASR. |
| MLX (Apple Silicon) | <https://huggingface.co/vanch007/mlx-MOSS-Transcribe-Diarize> | — | FP/8bit/4bit. macOS only. |
| ONNX (browser/sherpa) | <https://huggingface.co/Luigi/moss-transcribe-diarize-zhtw-onnx> | 356 – 718 MB | Runs in browsers via `onnxruntime-web`. Fine-tuned for zh-TW. |

### Architecture (from the model card)

| Component | Spec |
|-----------|------|
| Text backbone | Qwen3-0.6B style causal decoder |
| Audio encoder | Whisper-Medium encoder (24 layers, d_model=1024) |
| Audio frontend | WhisperFeatureExtractor, 16 kHz, 80 mel bins, 30s chunks |
| Audio-text bridge | 4x temporal merge + MLP adaptor |
| Context window | 128K tokens (up to ~90 min audio) |
| Output format | `[start][Sxx]text[end]` with speaker tags |

### GLM-5.2 (the LLM model)

Accessed via Ollama cloud at `https://ollama.com/v1`. Model ID: `glm-5.2`.
Reasoning model — fills `reasoning` field before `content`. Token budgets set to 16000 per operation.

---

## API documentation

### Mossland / MOSI API (STT)

| Resource | URL |
|----------|-----|
| API base | `https://api.mosi.cn/v1` |
| Models endpoint | `GET https://api.mosi.cn/v1/models` |
| Transcription (sync/async/stream) | `POST https://api.mosi.cn/v1/audio/transcriptions` |
| Task query | `GET https://api.mosi.cn/v1/audio/tasks/{task_id}` |
| API key management | <https://studio.mosi.cn/app/api-keys> |
| Docs (overview) | <https://studio.mosi.cn/docs/moss-transcribe-diarize> |

**Key API behaviors discovered during integration:**

- **SSE streaming** (`stream=true`): the API returns a real-time SSE event stream. Events: `task.created`, `transcript.text.delta` (word-by-word), `transcript.segment.done` (with `speaker`, `start`, `end`, `text`), `transcript.text.done`. The shim collects these and returns as JSON. This is the primary mode.
- **Async polling** (`async=true`): POST returns `{task_id, status: PENDING, retry_after: 3}` immediately. Poll `GET /v1/audio/tasks/{task_id}` for result. Fallback mode.
- **Required params**: `version=v20260410-streamparam-20260703` and `diarize=true` are required for moss-transcribe-diarize.
- **Long audio**: `sampling_params={"max_new_tokens":65536}` enables single-pass for up to 90 min. Without this, default `max_new_tokens=5120` truncates at ~20 min (see [sglang-omni #1034](https://github.com/sgl-project/sglang-omni/issues/1034)).
- The rich result (segments with speaker labels + timestamps) is at `GET /v1/audio/tasks/{task_id}` — NOT at `/v1/audio/transcriptions/{task_id}` (which 404s despite what the docs say).
- `response_format=verbose_json` is NOT supported on the live API. The shim always collects segments from SSE events or the task endpoint.
- `audio_data` (base64 in JSON) is NOT supported. Must use multipart file upload, `file_id`, or `audio_url`.
- Failed requests can still charge credits. The shim validates model + file size before forwarding.
- **`platform.mosi.cn`** is a dashboard for monitoring API usage; the actual API gateway is `api.mosi.cn` (confirmed via the platform's `agwOrigin` config).
- Cost: ~31.73 credits per 2-minute file ≈ ~16 credits/min.
- **Speaker accuracy** (single-pass vs ElevenLabs Scribe 2 on 55-min recording): 95.4% agreement. Per-speaker: Speaker 1 87.8%, Speaker 2 91.2%, Speaker 3 76.9%.

### Ollama cloud API (LLM)

| Resource | URL |
|----------|-----|
| API base | `https://ollama.com/v1` |
| Models endpoint | `GET https://ollama.com/v1/models` (34 models available) |
| Chat completions | `POST https://ollama.com/v1/chat/completions` |
| API key | Ollama account settings |

**Key behaviors:**

- GLM-5.2 is a reasoning model: `message.reasoning` fills first, then `message.content`.
- With `max_tokens < ~300`, content comes back empty (all budget consumed by reasoning).
- Working budget for short answers: ~300-500 tokens. For summaries of long transcripts: 2000-8000.
- speakr env sets all four budgets (TITLE, SUMMARY, CHAT, EVENT) to 16000 per [issue #264](https://github.com/murtaza-nasir/speakr/issues/264).

### speakr REST API v1

| Resource | URL |
|----------|-----|
| OpenAPI spec | `GET http://127.0.0.1:8899/api/v1/openapi.json` |
| User profile | `GET /api/v1/users/me` |
| Recordings CRUD | `/api/v1/recordings`, `/api/v1/recordings/{id}` |
| Upload | `POST /api/v1/recordings/upload` (multipart) |
| Transcript | `GET /api/v1/recordings/{id}/transcript` |
| Summary | `GET /api/v1/recordings/{id}/summary`, `POST /api/v1/recordings/{id}/summarize` |
| Chat | `POST /api/v1/recordings/{id}/chat` |
| Speakers | `GET /api/v1/recordings/{id}/speakers`, `PUT /api/v1/recordings/{id}/speakers/assign` |
| Webhooks | `/api/v1/webhooks` (CRUD, HMAC-SHA256 signed) |
| Auth | Bearer token in `Authorization` header |

Full docs: <https://murtaza-nasir.github.io/speakr/>

---

## Key GitHub issues (research findings)

### speakr issues (informed our integration)

| Issue | Title | Takeaway |
|-------|-------|----------|
| [#264](https://github.com/murtaza-nasir/speakr/issues/264) | LLM returned empty content (Kimi-K2.5) | Reasoning models need 16000 token budgets. **Directly informed our GLM-5.2 config.** |
| [#209](https://github.com/murtaza-nasir/speakr/issues/209) | Switch to OpenAI compatible endpoint for ASR | Community wants `/v1/audio/transcriptions` support. Our shim provides exactly this. |
| [#180](https://github.com/murtaza-nasir/speakr/issues/180) | ASR parameters ignored (query params vs form data) | speakr sends ASR params as query params; some backends need form data. |
| [#275](https://github.com/murtaza-nasir/speakr/issues/275) | Webhooks or other means to notify | Added webhooks with HMAC-SHA256 signing + exponential retry. |
| [#221](https://github.com/murtaza-nasir/speakr/issues/221) | Speaker identification via REST API | Added Bearer-auth speaker assignment endpoints. |
| [#169](https://github.com/murtaza-nasir/speakr/issues/169) | Upload API request | Added `POST /api/v1/recordings/upload`. |
| [#105](https://github.com/murtaza-nasir/speakr/issues/105) | Add token authentication | Added Bearer token auth for the REST API. |
| [#168](https://github.com/murtaza-nasir/speakr/issues/168) | Auto chunking for gpt-4o-transcribe-diarize | 1400s (~23min) max duration for diarize models; chunking improvements landed. |
| [#296](https://github.com/murtaza-nasir/speakr/issues/296) | Add SenseVoice as ASR backend | SenseVoice connector added (faster, non-autoregressive, OpenAI-compatible). |

### MOSS-Transcribe-Diarize issues

| Issue | Title | Takeaway |
|-------|-------|----------|
| [#3](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/issues/3) | Improving accuracy | Team says fine-tuning code coming soon. |
| [#8](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/issues/8) | How many languages supported? | Unanswered — no official language list published. |

### sglang-omni MOSS-TD PRs (performance work)

| PR | Title | Takeaway |
|----|-------|----------|
| [#965](https://github.com/sgl-project/sglang-omni/pull/965) | Stream output builder for MOSS-TD | SSE streaming with ~40ms TTFT. Merged. |
| [#955](https://github.com/sgl-project/sglang-omni/pull/955) | MOSS-TD ASR CI stages | Full CI pipeline for MOSS-TD evaluation. |
| [#962](https://github.com/sgl-project/sglang-omni/issues/962) | Encoder torch.compile | Ongoing perf optimization. |

---

## Research papers

| Paper | URL | Notes |
|-------|-----|-------|
| MOSS Transcribe Diarize Technical Report | <https://arxiv.org/abs/2601.01554> | Architecture, training data, evaluation benchmarks. Shows SOTA on AISHELL-4, Alimeeting, Podcast, Movies. |

### Benchmark highlights (from the paper)

| Model | AISHELL-4 CER | Alimeeting CER | Podcast CER | Movies CER |
|-------|:---:|:---:|:---:|:---:|
| MOSS-TD 0.9B | 14.84 | 24.86 | 5.97 | 6.36 |
| MOSS-TD Pro | 13.78 | 18.22 | 4.46 | 5.86 |
| Gemini 3 Pro | 22.75 | 26.75 | — | 8.62 |
| GPT-4o | — | — | — | 14.37 |
| Whisper-large-v3 | higher | higher | higher | higher |

---

## Community resources

### Reddit (r/LocalLLaMA)

| Resource | URL | Key insight |
|----------|-----|-------------|
| Release thread | [old.reddit.com/.../1uru6wf](https://old.reddit.com/r/LocalLLaMA/comments/1uru6wf/openmossteammosstranscribediarize_hugging_face/) | Real user testing: beats Whisper-large-v3, handles podcasts/accents/overlapping speech. GGUF quants not always byte-identical in practice. VRAM: 310GB for 90min without chunking, 40GB with. |

### HuggingFace Spaces

| Space | URL |
|-------|-----|
| Official demo | <https://huggingface.co/spaces/OpenMOSS-Team/MOSS-transcribe-diarize> |

### Community integrations (MOSS-Transcribe-Diarize + MOSI API)

| Project | URL | Status |
|---------|-----|--------|
| voice-pro PR #71 | <https://github.com/abus-aikorea/voice-pro/pull/71> | Open. 336-line MOSI ASR engine for the 11k-star Gradio TTS app. Uses base64 JSON (broken on live API — our shim uses multipart). |
| Termo/ClawHub skill | <https://termo.ai/skills/moss-transcribe-diarize> | CLI skill. `scripts/transcribe.py` calls MOSI API. |
| hehehai/voxt (macOS) | <https://github.com/hehehai/voxt> | macOS voice input app listing MOSI as compatible endpoint. |
| mlx-audio | <https://github.com/Blaizzy/mlx-audio> | Apple Silicon runtime with MOSS-TD support. |

---

## Hardware context

This stack runs on:

| Component | Spec |
|-----------|------|
| GPU | NVIDIA RTX 2050 (4 GB VRAM, Turing) — NOT used for inference; all compute is cloud |
| CPU | 16 cores |
| RAM | 14 GB |
| Disk | 77 GB free |
| OS | Arch Linux |

The RTX 2050's 4 GB VRAM rules out local GPU inference of MOSS-TD (needs 10+ GB even with chunking). The Mossland hosted API sidesteps this entirely.

---

## Docker image sizes

| Image | Size |
|-------|------|
| `learnedmachine/speakr:lite` | 887 MB |
| `mossland-shim:local` | ~55 MB (python:3.12-slim + fastapi + httpx + python-multipart) |

---

## Cost summary

| Operation | Cost | Notes |
|-----------|------|-------|
| Transcription (Mossland) | ~16 credits / min of audio | Per-task billing. Failed requests can still charge. |
| LLM summary/chat (Ollama cloud) | Ollama cloud quota | GLM-5.2 reasoning tokens count toward quota. ~300 tokens per short answer, 2000-8000 per summary. |
| MCP server (speakr-mcp) | Free | Runs locally, no external calls except through speakr's API. |
| mossland-shim | Free | Pass-through proxy. |

Credit-to-currency conversion is not publicly listed. Check the Mossland console at <https://studio.mosi.cn> for current balance and pricing.
