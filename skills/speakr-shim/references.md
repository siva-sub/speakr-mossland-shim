# Speakr Shim — References

Detailed reference for all STT providers, API shapes, speakr connectors, and resources for building/modifying transcription shims.

---

## Provider comparison matrix

### Cloud APIs (no local GPU needed)

| Provider | API shape | Diarization | Speaker labels | Cost (/min) | OpenAI-compat? | Shim needed? |
|----------|-----------|:-----------:|:--------------:|:-----------:|:--------------:|:------------:|
| **Mossland (MOSI)** | Multipart + async + poll | Yes | Yes (S01, S02) | ~16 credits | No (custom) | **Yes** (current) |
| OpenAI Whisper | Multipart, sync | No | No | $0.006 | Yes (native) | No |
| OpenAI gpt-4o-transcribe-diarize | Multipart, sync | Yes | Yes (A, B, C) | $0.006+ | Yes (native) | No |
| Deepgram Nova-3 | JSON/WebSocket | Yes | Yes | $0.0043 | No | Yes |
| AssemblyAI Universal | Multipart, sync+webhook | Yes | Yes | $0.012 | No | Yes |
| Soniox | Multipart/WS, token-based | Yes | Yes | ~$0.0017 | No | Yes |
| Speechmatics | Multipart, async | Yes | Yes | Custom | No | Yes |
| Gladia | Multipart, async | Yes | Yes | Custom | No | Yes |
| Google Speech-to-Text | gRPC/REST | Yes (diarization config) | Yes | $0.024 | No | Yes |
| AWS Transcribe | AWS SDK | Yes | Yes | $0.024 | No | Yes |
| Azure Speech | REST/SDK | Yes | Yes | $0.016 | No | Yes |

### Local / self-hosted (requires GPU)

| Provider | API shape | Diarization | OpenAI-compat? | Shim needed? |
|----------|-----------|:-----------:|:--------------:|:------------:|
| WhisperX (via whisperx-asr-service) | `/asr` endpoint | Yes (pyannote) | No | Yes (or use speakr's `asr_endpoint` connector) |
| Faster-Whisper Server | `/v1/audio/transcriptions` | No | Yes | No |
| vLLM (with MOSS-TD) | `/v1/audio/transcriptions` | Yes | Yes | No |
| SGLang-Omni (with MOSS-TD) | `/v1/audio/transcriptions` | Yes | Yes | No |
| LocalAI (moss-transcribe-cpp backend) | `/v1/audio/transcriptions` | Yes | Yes | No |
| Parakeet (via nemo) | Custom | No | No | Yes |
| Voxtral (via vLLM) | `/v1/audio/transcriptions` | Yes | Yes | No |
| VibeVoice (via vLLM) | `/v1/audio/transcriptions` | Yes | Yes | No |
| SenseVoice (via funasr-server) | `/v1/audio/transcriptions` | Yes (cam++) | Yes | No |

---

## API shape reference (for building new shims)

### Mossland / MOSI API

```
POST /v1/audio/transcriptions
  multipart: file=@audio.mp3, model=moss-transcribe-diarize, async=true
  headers: Authorization: Bearer <key>
→ 200: {task_id, status: "PENDING", retry_after: 3}

GET /v1/audio/tasks/{task_id}
  headers: Authorization: Bearer <key>
→ 200: {id, status: "SUCCESS", duration: 119.99, text: "...",
        segments: [{type, id, start, end, text, speaker: "S01"}]}
```

**Gotchas:**

- `async=true` is mandatory for getting segments (sync only returns text).
- `/v1/audio/transcriptions/{task_id}` 404s — use `/v1/audio/tasks/{task_id}`.
- `response_format=verbose_json` not supported.
- `audio_data` (base64 JSON) not supported — multipart only.
- Failed requests still charge credits.

### OpenAI Whisper API (reference shape — the target)

```
POST /v1/audio/transcriptions
  multipart: file=@audio.mp3, model=whisper-1, response_format=verbose_json
  headers: Authorization: Bearer <key>
→ 200: {text: "...", segments: [{id, start, end, text}]}
```

This is the shape every shim must produce. No speaker field in standard OpenAI, but speakr and our shim add it.

### Deepgram API

```
POST https://api.deepgram.com/v1/listen
  headers: Authorization: Token <key>, Content-Type: audio/*
  body: raw audio bytes (not multipart!)
  params: ?model=nova-3&diarize=true&smart_format=true
→ 200: {results: {channels: [{alternatives: [{transcript, words, confidence}]}]},
        metadata: {duration}}
```

**Shim differences:**

- Audio sent as raw body, not multipart.
- Auth uses `Token` prefix, not `Bearer`.
- Response is deeply nested — needs flattening.
- Diarization via query param `diarize=true`.
- Speaker labels in `words[].speaker`.

### AssemblyAI API

```
POST https://api.assemblyai.com/v2/upload
  headers: authorization: <key>
  multipart: file=@audio.mp3
→ {upload_url: "..."}

POST https://api.assemblyai.com/v2/transcript
  headers: authorization: <key>
  json: {audio_url: "<upload_url>", speaker_labels: true}
→ {id: "..."}

GET https://api.assemblyai.com/v2/transcript/{id}
→ {text, utterances: [{speaker: "A", text, start, end}], words: [...]}
```

**Shim differences:**

- Two-step: upload first, then submit transcript job.
- Async with polling.
- Speaker labels via `speaker_labels: true` config.
- Webhook callbacks supported.

### Soniox API

```
POST https://api.soniox.com/v1/transcriptions
  headers: Authorization: Bearer <key>
  multipart: file=@audio.mp3
  json: {model: "stt-sonix-1"}
→ {id: "..."}

GET https://api.soniox.com/v1/transcriptions/{id}
→ {text, segments: [{text, speaker, start_ms, end_ms}]}
```

**Shim differences:**

- Very cheap (~$0.0017/min).
- Speaker labels built in.
- Timestamps in milliseconds (need /1000 for seconds).

---

## speakr connector reference

speakr's connector system is in `/app/src/services/transcription/connectors/`. Each connector has the same interface:

```python
class MyConnector(BaseTranscriptionConnector):
    CAPABILITIES = {TranscriptionCapability.DIARIZATION, ...}
    PROVIDER_NAME = "my_connector"

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        # Send audio to the provider
        # Parse the response
        # Return segments + speaker info
```

### Available connectors (from the source)

| File | Connector name | Auto-detect trigger |
|------|---------------|-------------------|
| `openai_transcribe.py` | `openai_transcribe` | Model contains `gpt-4o` |
| `openai_whisper.py` | `openai_whisper` | Default (fallback) |
| `asr_endpoint.py` | `asr_endpoint` | `ASR_BASE_URL` is set |
| `azure_openai_transcribe.py` | `azure_openai_transcribe` | Azure-specific env vars |
| `mistral.py` | `mistral` | `TRANSCRIPTION_CONNECTOR=mistral` |
| `vibevoice.py` | `vibevoice` | `TRANSCRIPTION_CONNECTOR=vibevoice` |

### Auto-detection priority

1. `TRANSCRIPTION_CONNECTOR` (explicit)
2. `ASR_BASE_URL` (if set, uses `asr_endpoint`)
3. Model name contains `gpt-4o` (uses `openai_transcribe`)
4. Default: `openai_whisper` with `whisper-1`

### Capabilities flags

```python
class TranscriptionCapability(Enum):
    DIARIZATION              # can identify speakers
    TIMESTAMPS               # produces word/segment timestamps
    LANGUAGE_DETECTION       # auto-detects language
    SPEAKER_COUNT_CONTROL    # accepts min/max speaker hints
    HOTWORDS                 # accepts custom vocabulary
    INITIAL_PROMPT           # accepts a prompt to guide transcription
    KNOWN_SPEAKERS           # can match against enrolled speaker profiles
    SPEAKER_EMBEDDINGS       # returns voice embeddings for cross-recording matching
```

### How speakr sends requests (the `openai_transcribe` connector)

```python
# Simplified from src/services/transcription/connectors/openai_transcribe.py
client = OpenAI(
    api_key=config['api_key'],
    base_url=config.get('base_url', 'https://api.openai.com/v1'),
)
result = client.audio.transcriptions.create(
    model=self.model,           # e.g. "gpt-4o-transcribe-diarize"
    file=audio_file,
    response_format="verbose_json",
    language=language,           # optional
)
# Parses result.segments for display
```

The shim sits at `base_url` and intercepts this call.

---

## Docker image sizes (for reference)

| Image | Size | Notes |
|-------|------|-------|
| `mossland-shim:local` | ~55 MB | python:3.12-slim + fastapi + httpx + multipart |
| `learnedmachine/speakr:lite` | 887 MB | No PyTorch (Inquire Mode falls back to text search) |
| `learnedmachine/speakr` (full) | ~4.4 GB | Includes PyTorch for Inquire Mode embeddings |
| `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` | 686 MB | For comparison |

---

## Mossland shim env vars (complete reference)

| Variable | Default | Description |
|----------|---------|-------------|
| `MOSI_API_KEY` | (required) | Mossland API key |
| `MOSI_BASE_URL` | `https://api.mosi.cn` | Upstream base URL |
| `MOSI_DEFAULT_MODEL` | `moss-transcribe-diarize` | Model if client doesn't specify |
| `MODEL_VERSION` | `v20260410-streamparam-20260703` | Required version for moss-transcribe-diarize |
| `SAMPLING_PARAMS` | `{"max_new_tokens":65536,"temperature":0}` | JSON string for sampling params |
| `USE_STREAMING` | `true` | SSE streaming (primary), async poll (fallback) |
| `MAX_UPLOAD_BYTES` | `104857600` (100 MB) | File size cap |
| `POLL_INTERVAL_SECONDS` | `5.0` | Poll interval (fallback mode) |
| `POLL_TIMEOUT_SECONDS` | `1800` (30 min) | Max poll time (fallback mode) |
| `CHUNK_DURATION` | `300` (5 min) | Fallback chunk size (last resort) |

---

## speakr env vars (transcription-related)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_CONNECTOR` | (auto-detect) | Force a specific connector |
| `TRANSCRIPTION_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (the shim) |
| `TRANSCRIPTION_API_KEY` | (required) | API key for the connector |
| `TRANSCRIPTION_MODEL` | `whisper-1` | Model name |
| `TRANSCRIPTION_REQUEST_TIMEOUT` | varies | Per-request timeout |
| `TRANSCRIPTION_MODELS_AVAILABLE` | (empty) | CSV of allowed models for the UI dropdown |
| `ASR_BASE_URL` | (empty) | If set, switches to the `asr_endpoint` connector |
| `ASR_DIARIZE` | `true` | Enable/disable diarization on the ASR endpoint |
| `ASR_TIMEOUT` | `1800` | ASR request timeout in seconds |
| `ENABLE_CHUNKING` | `true` | Split long audio into chunks |
| `CHUNK_SIZE_MB` | `20` | Chunk size for splitting |
| `CHUNK_OVERLAP_SECONDS` | `3` | Overlap between chunks |
| `AUDIO_COMPRESS_UPLOADS` | `true` | Compress audio before sending |
| `AUDIO_CODEC` | `mp3` | Codec for compression |
| `AUDIO_BITRATE` | `128k` | Bitrate for compression |
| `VIDEO_PASSTHROUGH_ASR` | `false` | Send video directly to ASR (skip audio extraction) |

---

## speakr env vars (LLM-related, for context)

| Variable | Default | Description |
|----------|---------|-------------|
| `TEXT_MODEL_PROVIDER` | `openai` | LLM provider |
| `TEXT_MODEL_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible LLM endpoint |
| `TEXT_MODEL_API_KEY` | (required) | LLM API key |
| `TEXT_MODEL_NAME` | `openai/gpt-3.5-turbo` | LLM model name |
| `TITLE_MODEL_NAME` | (same as TEXT_MODEL_NAME) | Model for title generation |
| `SUMMARY_MODEL_NAME` | (same as TEXT_MODEL_NAME) | Model for summarization |
| `TITLE_MAX_TOKENS` | `5000` | Max tokens for titles |
| `SUMMARY_MAX_TOKENS` | `8000` | Max tokens for summaries |
| `CHAT_MAX_TOKENS` | `5000` | Max tokens for chat |
| `EVENT_MAX_TOKENS` | `3000` | Max tokens for event extraction |
| `LLM_REQUEST_TIMEOUT` | `600` | LLM request timeout |

**For reasoning models (GLM-5.2, Kimi-K2.5, GPT-5):** set all four `*_MAX_TOKENS` to `16000`. See [speakr issue #264](https://github.com/murtaza-nasir/speakr/issues/264).

---

## Upstream documentation links

### Mossland / MOSI

| Resource | URL |
|----------|-----|
| API base | `https://api.mosi.cn/v1` |
| Models list | `GET https://api.mosi.cn/v1/models` |
| API key management | <https://studio.mosi.cn/app/api-keys> |
| Transcription docs | <https://studio.mosi.cn/docs/moss-transcribe-diarize> |
| Model page | <https://mosi.cn/models/moss-transcribe-diarize> |
| Paper | <https://arxiv.org/abs/2601.01554> |

### speakr

| Resource | URL |
|----------|-----|
| GitHub | <https://github.com/murtaza-nasir/speakr> |
| Docs | <https://murtaza-nasir.github.io/speakr/> |
| Installation guide | <https://murtaza-nasir.github.io/speakr/getting-started/installation> |
| Model configuration | <https://murtaza-nasir.github.io/speakr/admin-guide/model-configuration/> |
| Troubleshooting | <https://murtaza-nasir.github.io/speakr/troubleshooting> |
| Docker compose template | <https://github.com/murtaza-nasir/speakr/blob/master/config/docker-compose.example.yml> |
| Env template | <https://github.com/murtaza-nasir/speakr/blob/master/config/env.transcription.example> |
| WhisperX ASR service | <https://github.com/murtaza-nasir/whisperx-asr-service> |

### Alternative STT providers

| Provider | API docs | Notes |
|----------|----------|-------|
| OpenAI Whisper | <https://platform.openai.com/docs/api-reference/audio> | The reference shape |
| Deepgram | <https://developers.deepgram.com/api/> | Raw body, not multipart |
| AssemblyAI | <https://www.assemblyai.com/docs/api-v/2> | Two-step upload + submit |
| Soniox | <https://soniox.com/docs> | Cheapest per-minute |
| Google STT | <https://cloud.google.com/speech-to-text/docs> | gRPC/REST |
| AWS Transcribe | <https://docs.aws.amazon.com/transcribe/> | AWS SDK |
| Azure Speech | <https://learn.microsoft.com/azure/ai-services/speech-service/> | REST/SDK |

### Local inference engines

| Engine | URL | MOSS-TD support |
|--------|-----|:---------------:|
| sglang-omni | <https://github.com/sgl-project/sglang-omni> | Yes (recommended) |
| vLLM | <https://github.com/vllm-project/vllm> | Yes |
| LocalAI | <https://github.com/mudler/LocalAI> | Yes (moss-transcribe-cpp backend) |
| moss-transcribe.cpp | <https://github.com/mudler/moss-transcribe.cpp> | Yes (CPU, GGUF) |
| CrispASR | <https://github.com/CrispStrobe/CrispASR> | Yes (CPU, GGUF) |
| faster-whisper-server | <https://github.com/fedirz/faster-whisper-server> | No (Whisper only) |

### GGUF model weights

| Repo | URL | Sizes |
|------|-----|-------|
| mudler GGUF | <https://huggingface.co/mudler/moss-transcribe.cpp-gguf> | q4_0 (511 MB) – f16 (1.8 GB) |
| CrispASR GGUF | <https://huggingface.co/cstr/MOSS-Transcribe-Diarize-GGUF> | q4_k (1.1 GB) – f16 (1.7 GB) |
| ONNX (browser) | <https://huggingface.co/Luigi/moss-transcribe-diarize-zhtw-onnx> | 356 – 718 MB |

---

## Key GitHub issues for shim builders

| Issue | URL | Takeaway |
|-------|-----|----------|
| speakr #209 | <https://github.com/murtaza-nasir/speakr/issues/209> | Community wants OpenAI-compatible ASR endpoints (our shim provides this) |
| speakr #180 | <https://github.com/murtaza-nasir/speakr/issues/180> | speakr sends params as query vs form data — know which your shim receives |
| speakr #264 | <https://github.com/murtaza-nasir/speakr/issues/264> | Reasoning model token budgets (16000 for all operations) |
| speakr #168 | <https://github.com/murtaza-nasir/speakr/issues/168> | Duration limits and chunking behavior for diarize models |
| speakr #156 | <https://github.com/murtaza-nasir/speakr/issues/156> | Format/codec compatibility issues with non-OpenAI endpoints |
| sglang-omni #959 | <https://github.com/sgl-project/sglang-omni/issues/959> | SGLang serving beats vLLM for MOSS-TD (performance comparison) |
| LocalAI #10756 | <https://github.com/mudler/LocalAI/pull/10756> | Built-in moss-transcribe-cpp backend for LocalAI |

---

## Cost comparison (per hour of audio)

| Provider | Cost/hour | Diarization? | Notes |
|----------|-----------|:------------:|-------|
| Soniox | ~$0.10 | Yes | Cheapest |
| Whisper API | $0.36 | No | No diarization |
| OpenAI gpt-4o-transcribe-diarize | ~$0.40 | Yes | Token-based pricing |
| Deepgram Nova-3 | $0.26 | Yes | Fast, streaming |
| Mossland (MOSS-TD) | ~952 credits | Yes | Best quality; credit-to-¥ unknown |
| AssemblyAI | $0.75 | Yes | Good API |
| Google STT | $1.44 | Yes | Enterprise |
| AWS Transcribe | $1.44 | Yes | Enterprise |
| Self-hosted (A100, sglang-omni) | ~$0.05 | Yes | Daniel van Strien's Apollo 11 test: 174h for $9.46 |

---

## speakr skill cross-reference

This skill is part of the speakr skill family:

- [../speakr/SKILL.md](../speakr/SKILL.md) — Main speakr skill (35 MCP tools, workflows, anti-patterns)
- [../speakr/references.md](../speakr/references.md) — speakr references (models, papers, community)
- [SKILL.md](SKILL.md) — This skill (shim building, backend swapping)
- [references.md](references.md) — This file (provider comparison, API shapes, env vars)
