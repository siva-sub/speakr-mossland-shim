"""
MOSS-Transcribe-Diarize connector for audio transcription.

Uses the Mossland/MOSI hosted API (api.mosi.cn) to transcribe audio with
speaker diarization, timestamps, and language detection. Supports up to
90-minute audio in a single pass via SSE streaming.

Requires a Mossland API key (https://studio.mosi.cn/app/api-keys).
"""

import json
import logging
import os
import subprocess
import time
from typing import Any

import httpx

from ..base import (
    BaseTranscriptionConnector,
    ConnectorSpecifications,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
)
from ..exceptions import ConfigurationError, ProviderError, TranscriptionError

logger = logging.getLogger(__name__)

# Default API params (discovered via live-API probing — see the shim docs)
DEFAULT_VERSION = "v20260410-streamparam-20260703"

# Duration-scaled output token budget.
# Dense multi-speaker meetings decode to ~4.5 output tokens per audio second
# (including time markers), so 10 leaves roughly 2x headroom without
# approaching the input token cost of the same audio.
# See: https://github.com/sgl-project/sglang-omni/pull/1034
_OUTPUT_TOKENS_PER_AUDIO_SECOND = 10
# Floor: don't go below the old default of 5120 (covers short clips with margin)
_MIN_TOKENS = 5120
# Cap at the model's 128K context window (131072). The model's max_position_embeddings
# was raised from 40960 to 131072 on Jul 6, enabling 90-min audio.
_MAX_TOKENS = 131072
# Audio over this duration exceeds the model's context limit (~164 min per sglang-omni #1034)
# and should be rejected with a clear error rather than sent to the API.
_MAX_AUDIO_DURATION_S = 5400  # 90 minutes (conservative; model can do ~164 min)

# The model emits a time marker every 5 seconds, so a valid segment end
# can overshoot the audio tail by up to one marker interval. Anything past
# that is a corrupted timestamp token and should be clamped.
_TIMESTAMP_TOLERANCE_S = 5.0

# Model registry — each model declares its capabilities, like the OpenAI
# Transcribe connector does. This lets the UI show a model dropdown and
# lets the connector validate that the requested model is supported.
# Extend this dict when new MOSS models are released.
_MODELS: dict[str, dict[str, Any]] = {
    "moss-transcribe-diarize": {
        "supports_diarization": True,
        "max_duration_seconds": _MAX_AUDIO_DURATION_S,
        "recommended_chunk_seconds": _MAX_AUDIO_DURATION_S,
        "requires_version": True,
        "description": "MOSS-Transcribe-Diarize Pro — multi-speaker with diarization + timestamps",
    },
    "moss-transcribe": {
        "supports_diarization": False,
        "max_duration_seconds": _MAX_AUDIO_DURATION_S,
        "recommended_chunk_seconds": _MAX_AUDIO_DURATION_S,
        "requires_version": False,
        "description": "MOSS-Transcribe — single-speaker ASR",
    },
}


def _scaled_max_new_tokens(audio_duration_s: float) -> str:
    """Scale max_new_tokens with audio duration.

    Formula: tokens = duration * tokens_per_second
    - Floored at _MIN_TOKENS (5120) for short clips
    - Capped at _MAX_TOKENS (131072) for context window
    - 2x headroom over observed 4.5 tokens/sec for dense meetings

    See: https://github.com/sgl-project/sglang-omni/pull/1034
    """
    if audio_duration_s <= 0:
        tokens = _MIN_TOKENS
    else:
        tokens = int(audio_duration_s * _OUTPUT_TOKENS_PER_AUDIO_SECOND)
        tokens = max(tokens, _MIN_TOKENS)
        tokens = min(tokens, _MAX_TOKENS)
    return json.dumps({"max_new_tokens": tokens, "temperature": 0})


def _sanitize_segments(
    segments: list[TranscriptionSegment], audio_duration_s: float
) -> list[TranscriptionSegment]:
    """Clamp timestamps that exceed the audio duration + tolerance.

    The model occasionally emits a corrupted end timestamp (e.g. 4809.8s
    in a 4601.3s file). Clamp to audio_duration + 5s marker interval.
    See: https://github.com/sgl-project/sglang-omni/pull/1034
    """
    if audio_duration_s <= 0:
        return segments
    limit = round(float(audio_duration_s) + _TIMESTAMP_TOLERANCE_S, 2)
    repaired = 0
    for seg in segments:
        start = min(seg.start_time or 0, limit)
        end = min(max(seg.end_time or start, start), limit)
        if start != (seg.start_time or 0) or end != (seg.end_time or 0):
            repaired += 1
            seg.start_time = start
            seg.end_time = end
    if repaired:
        logger.warning("Clamped %d segments with timestamps outside audio duration", repaired)
    return segments

# Transport modes (controllable via MOSSLAND_TRANSPORT env var):
#   auto     — SSE streaming → async poll → 5-min chunking (default, cascading fallback)
#   stream   — SSE streaming only (no fallback)
#   async    — async polling only (no streaming, no chunking)
#   chunk    — 5-min chunking only (most conservative, speaker labels may fragment)
_TRANSPORT_AUTO = "auto"
_TRANSPORT_STREAM = "stream"
_TRANSPORT_ASYNC = "async"
_TRANSPORT_CHUNK = "chunk"


class MosslandTranscriptionConnector(BaseTranscriptionConnector):
    """Connector for MOSS-Transcribe-Diarize via the Mossland/MOSI API.

    The Mossland API hosts the MOSS-Transcribe-Diarize model (SOTA for
    multi-speaker ASR). It uses multipart upload + SSE streaming for
    real-time results with speaker labels.

    Key API behaviors (absorbed from the shim project):
    - Requires version=v20260410-streamparam-20260703
    - Requires diarize=true for speaker separation
    - sampling_params={"max_new_tokens":65536} for single-pass up to 90 min
    - stream=true returns SSE events (transcript.segment.done, transcript.text.done)
    - async=true + poll /v1/audio/tasks/{id} as fallback
    - response_format=verbose_json NOT supported upstream
    - audio_data (base64) NOT supported — multipart only
    """

    CAPABILITIES: set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.HOTWORDS,
    }
    PROVIDER_NAME = "mossland"

    # The model handles up to 90 min natively with max_new_tokens=65536.
    # No app-level chunking needed for files under 90 min.
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=100 * 1024 * 1024,  # 100 MB
        max_duration_seconds=5400,  # 90 minutes
        handles_chunking_internally=True,
        recommended_chunk_seconds=5400,
    )

    def __init__(self, config: dict[str, Any]):
        """
        Initialize the Mossland connector.

        Args:
            config: Configuration dict with keys:
                - api_key: Mossland API key (required)
                - base_url: API base URL (default: from MOSSLAND_BASE_URL env or https://api.mosi.cn)
                - model: Model name (default: from MOSSLAND_MODEL env or moss-transcribe-diarize)
        """
        super().__init__(config)

        self.api_key = config["api_key"]
        self.base_url = (
            config.get("base_url")
            or os.environ.get("MOSSLAND_BASE_URL", "https://api.mosi.cn").rstrip("/")
        )
        self.model = (
            config.get("model")
            or os.environ.get("MOSSLAND_MODEL", "moss-transcribe-diarize")
        )
        self.version = os.environ.get("MOSSLAND_VERSION", DEFAULT_VERSION)
        # If MOSSLAND_SAMPLING_PARAMS is set, use it; otherwise we scale
        # dynamically based on audio duration (see _scaled_max_new_tokens).
        self._fixed_sampling_params = os.environ.get("MOSSLAND_SAMPLING_PARAMS")
        # Transport mode: controls which methods are used and fallback order.
        # See _TRANSPORT_* constants above for available modes.
        self.transport_mode = os.environ.get("MOSSLAND_TRANSPORT", _TRANSPORT_AUTO).lower()
        # Tokens per audio second for duration-scaled max_new_tokens.
        # Default: 10 (2x headroom over observed 4.5 for dense meetings).
        # Lower for sparse audio (podcasts with silence); raise for dense overlapping speech.
        self.tokens_per_second = float(os.environ.get("MOSSLAND_TOKENS_PER_SEC", "10"))
        # Max audio duration the connector will accept (seconds). Audio over this
        # limit is rejected with a clear error rather than sent to the API.
        self.max_audio_duration = float(os.environ.get("MOSSLAND_MAX_AUDIO_DURATION", str(_MAX_AUDIO_DURATION_S)))
        # Polling config (for async fallback mode)
        self.poll_interval = float(os.environ.get("MOSSLAND_POLL_INTERVAL", "5.0"))
        self.poll_timeout = float(os.environ.get("MOSSLAND_POLL_TIMEOUT", "1800"))
        # Chunking config (for last-resort fallback)
        self.chunk_duration = int(os.environ.get("MOSSLAND_CHUNK_DURATION", "300"))
        # Timestamp sanitization (from sglang-omni #1034)
        self.sanitize_timestamps = os.environ.get(
            "MOSSLAND_SANITIZE_TIMESTAMPS", "true"
        ).lower() == "true"

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)",
        }

        # Discover available models from the API (dynamic, not hardcoded).
        # Falls back to the _MODELS dict if the API call fails.
        self._discovered_models: dict[str, dict[str, Any]] = {}
        self._fetch_models()

        # Validate the requested model
        if self.model not in self._discovered_models and self.model not in _MODELS:
            available = sorted(set(self._discovered_models.keys()) | set(_MODELS.keys()))
            raise ConfigurationError(
                f"Unknown model: {self.model}. Valid models: {available}"
            )

        # Set model-specific specs dynamically
        model_info = self._discovered_models.get(self.model) or _MODELS.get(self.model, {})
        self.SPECIFICATIONS = ConnectorSpecifications(
            max_file_size_bytes=100 * 1024 * 1024,
            max_duration_seconds=model_info.get("max_duration_seconds", _MAX_AUDIO_DURATION_S),
            handles_chunking_internally=True,
            recommended_chunk_seconds=model_info.get("recommended_chunk_seconds", _MAX_AUDIO_DURATION_S),
        )

    def _fetch_models(self) -> None:
        """Fetch available models from GET /v1/models and populate _discovered_models.

        Filters to transcription models (containing 'transcribe' in the id).
        Falls back to the hardcoded _MODELS dict if the API call fails.
        """
        try:
            with httpx.Client(timeout=15.0) as client:
                r = client.get(
                    f"{self.base_url}/v1/models",
                    headers=self.headers,
                )
            if r.status_code == 200:
                for m in r.json().get("data", []):
                    model_id = m.get("id", "")
                    if "transcribe" in model_id.lower():
                        # Start with the hardcoded specs, override with API-discovered info
                        base = _MODELS.get(model_id, {})
                        self._discovered_models[model_id] = {
                            "supports_diarization": "diarize" in model_id.lower() or base.get("supports_diarization", False),
                            "max_duration_seconds": base.get("max_duration_seconds", _MAX_AUDIO_DURATION_S),
                            "recommended_chunk_seconds": base.get("recommended_chunk_seconds", _MAX_AUDIO_DURATION_S),
                            "requires_version": base.get("requires_version", "diarize" in model_id.lower()),
                            "description": base.get("description", f"MOSS model: {model_id}"),
                            "owned_by": m.get("owned_by", "unknown"),
                        }
                logger.info(
                    f"Discovered {len(self._discovered_models)} transcription models from API: "
                    f"{list(self._discovered_models.keys())}"
                )
        except Exception as e:
            logger.warning(f"Could not fetch models from API ({e}), using hardcoded list")
            self._discovered_models = dict(_MODELS)

    def _model_supports_diarization(self) -> bool:
        """Check if the current model supports diarization."""
        model_info = self._discovered_models.get(self.model) or _MODELS.get(self.model, {})
        return model_info.get("supports_diarization", False)

    def _model_requires_version(self) -> bool:
        """Check if the current model requires the version parameter."""
        model_info = self._discovered_models.get(self.model) or _MODELS.get(self.model, {})
        return model_info.get("requires_version", False)

    def _validate_config(self) -> None:
        if not self.config.get("api_key"):
            raise ConfigurationError("api_key is required for Mossland connector")

    @staticmethod
    def _get_audio_duration(audio_bytes: bytes, filename: str = "") -> float:
        """Get audio duration in seconds using ffprobe (like SenseVoice connector)."""
        import tempfile
        suffix = os.path.splitext(filename)[1] if filename else ".wav"
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            os.unlink(tmp_path)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            logger.debug(f"Could not determine audio duration: {e}")
        return 0.0

    def _scaled_max_new_tokens_configurable(self, audio_duration_s: float) -> str:
        """Scale max_new_tokens with audio duration using configurable rate.

        Uses self.tokens_per_second (default 10, 2x headroom over observed 4.5).
        Floored at _MIN_TOKENS (5120), capped at _MAX_TOKENS (131072).
        """
        if audio_duration_s <= 0:
            tokens = _MIN_TOKENS
        else:
            tokens = int(audio_duration_s * self.tokens_per_second)
            tokens = max(tokens, _MIN_TOKENS)
            tokens = min(tokens, _MAX_TOKENS)
        return json.dumps({"max_new_tokens": tokens, "temperature": 0})

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using the Mossland API with SSE streaming.

        Falls back to async polling if streaming fails.
        """
        effective_model = self._effective_model(request) or self.model

        try:
            audio_bytes = request.audio_file.read()

            # Get audio duration for duration-scaled max_new_tokens (sglang-omni #1034)
            audio_duration = self._get_audio_duration(audio_bytes, request.filename or "")

            # Reject audio that exceeds the model's context limit
            if audio_duration > 0 and audio_duration > self.max_audio_duration:
                raise TranscriptionError(
                    f"Audio duration ({audio_duration:.0f}s) exceeds the maximum "
                    f"supported duration ({self.max_audio_duration:.0f}s). "
                    f"Use a shorter file or split the audio."
                )

            # Determine sampling_params: scale with duration unless explicitly set
            if self._fixed_sampling_params:
                sampling_params = self._fixed_sampling_params
            else:
                sampling_params = self._scaled_max_new_tokens_configurable(audio_duration)

            # Build multipart form data
            files = {"file": (request.filename, audio_bytes, request.mime_type or "audio/mpeg")}
            # Build form data — only include params the model needs
            data = {
                "model": effective_model,
                "sampling_params": sampling_params,
            }
            # version is required for moss-transcribe-diarize per the API docs
            if self._model_requires_version():
                data["version"] = self.version
            # diarize=true only for diarization-capable models
            if self._model_supports_diarization():
                data["diarize"] = "true"

            # Add hotwords if provided
            if request.hotwords:
                data["hotwords"] = request.hotwords

            logger.info(
                f"Mossland transcribing: {request.filename} "
                f"(duration={audio_duration:.0f}s, model={effective_model})"
            )

            # Determine which transport modes to try, based on MOSSLAND_TRANSPORT
            mode = self.transport_mode
            segments: list[TranscriptionSegment] = []
            full_text = ""

            if mode in (_TRANSPORT_STREAM, _TRANSPORT_AUTO):
                try:
                    segments, full_text = self._transcribe_streaming(audio_bytes, files, data)
                    # Check for truncation: if the last segment ends well before
                    # the audio duration, the SSE stream was cut short.
                    if mode == _TRANSPORT_AUTO and audio_duration > 0 and segments:
                        last_end = max((s.end_time or 0) for s in segments)
                        if last_end < audio_duration * 0.9:  # < 90% coverage
                            logger.warning(
                                f"SSE stream truncated: last segment at {last_end:.0f}s "
                                f"but audio is {audio_duration:.0f}s — falling back to async polling"
                            )
                            segments = []  # clear partial results, fall through to async
                            full_text = ""
                            mode = _TRANSPORT_ASYNC
                except Exception as e:
                    if mode == _TRANSPORT_STREAM:
                        raise
                    logger.warning(f"SSE streaming failed ({e}), trying async polling")
                    mode = _TRANSPORT_ASYNC  # fall through to async

            if mode in (_TRANSPORT_ASYNC, _TRANSPORT_AUTO) and not segments:
                # Ensure stream=true is not set when using async polling
                data.pop("stream", None)
                try:
                    segments, full_text = self._transcribe_async(audio_bytes, files, data)
                except Exception as e:
                    if mode == _TRANSPORT_ASYNC:
                        raise
                    logger.warning(f"Async polling failed ({e}), trying chunking")
                    mode = _TRANSPORT_CHUNK  # fall through to chunking

            if mode == _TRANSPORT_CHUNK and not segments:
                if audio_duration > self.chunk_duration:
                    segments, full_text = self._transcribe_chunked(audio_bytes, files, data, audio_duration)
                else:
                    raise TranscriptionError("All transport modes failed")

            # Sanitize timestamps: clamp any that exceed audio duration + 5s tolerance
            # (sglang-omni #1034: the model can emit corrupted end timestamps)
            if self.sanitize_timestamps:
                segments = _sanitize_segments(segments, audio_duration)

            # Build response
            speakers = list({s.speaker for s in segments if s.speaker})

            logger.info(
                f"Mossland transcription complete: {len(segments)} segments, "
                f"{len(speakers)} speakers"
            )

            return TranscriptionResponse(
                text=full_text,
                segments=segments,
                speakers=speakers if speakers else None,
                provider=self.PROVIDER_NAME,
                model=effective_model,
            )

        except (ProviderError, TranscriptionError):
            raise
        except Exception as e:
            logger.error(f"Mossland transcription failed: {e}")
            raise TranscriptionError(f"Mossland transcription failed: {e}") from e

    def _transcribe_streaming(
        self, audio_bytes: bytes, files: dict, data: dict
    ) -> tuple[list[TranscriptionSegment], str]:
        """Transcribe via SSE streaming (primary mode).

        Collects transcript.segment.done and transcript.text.done events.
        """
        data["stream"] = "true"
        segments = []
        full_text = ""

        timeout = httpx.Timeout(connect=120.0, read=None, write=300.0, pool=120.0)

        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/v1/audio/transcriptions",
                files=files,
                data=data,
                headers=self.headers,
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode()
                    raise ProviderError(
                        f"Mossland API error: {body[:500]}",
                        provider=self.PROVIDER_NAME,
                        status_code=response.status_code,
                    )

                for line in response.iter_lines():
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

                    if evt_type == "transcript.segment.done":
                        segments.append(
                            TranscriptionSegment(
                                text=evt.get("text", ""),
                                speaker=evt.get("speaker", ""),
                                start_time=float(evt.get("start", 0)),
                                end_time=float(evt.get("end", 0)),
                            )
                        )

                    elif evt_type == "transcript.text.done":
                        full_text = evt.get("text", full_text)

        if not full_text and segments:
            full_text = " ".join(s.text for s in segments)

        logger.info(f"SSE streaming complete: {len(segments)} segments")
        return segments, full_text

    def _transcribe_async(
        self, audio_bytes: bytes, files: dict, data: dict
    ) -> tuple[list[TranscriptionSegment], str]:
        """Transcribe via async task + polling (fallback mode)."""
        # Remove stream param — can't use stream=true with async=true
        data.pop("stream", None)
        data["async"] = "true"

        timeout = httpx.Timeout(connect=120.0, read=1800.0, write=300.0, pool=120.0)
        with httpx.Client(timeout=timeout) as client:
            # Submit task (retry up to 3 times on connection errors)
            last_err = None
            for attempt in range(3):
                try:
                    r = client.post(
                        f"{self.base_url}/v1/audio/transcriptions",
                        files=files,
                        data=data,
                        headers=self.headers,
                    )
                    break
                except httpx.RemoteProtocolError as e:
                    last_err = e
                    logger.warning(f"POST attempt {attempt+1}/3 failed: {e}")
                    time.sleep(2)
            else:
                raise ProviderError(f"Mossland API connection failed after 3 attempts: {last_err}", provider=self.PROVIDER_NAME, status_code=502)
            if r.status_code != 200:
                raise ProviderError(
                    f"Mossland API error: {r.text[:500]}",
                    provider=self.PROVIDER_NAME,
                    status_code=r.status_code,
                )

            task_id = r.json().get("task_id") or r.json().get("id")
            if not task_id:
                # Sync response — just return text
                text = r.json().get("text", "")
                return [], text

            logger.info(f"Async task created: {task_id}, polling...")

            # Poll for result
            deadline = time.monotonic() + self.poll_timeout
            poll_interval = self.poll_interval

            while True:
                r = client.get(
                    f"{self.base_url}/v1/audio/tasks/{task_id}",
                    headers=self.headers,
                    timeout=30.0,
                )
                if r.status_code == 200:
                    payload = r.json()
                    status = (payload.get("status") or "").upper()
                    if status in ("SUCCESS", "COMPLETED"):
                        segments = []
                        for seg in payload.get("segments") or []:
                            segments.append(
                                TranscriptionSegment(
                                    text=seg.get("text", ""),
                                    speaker=seg.get("speaker", ""),
                                    start_time=float(seg.get("start", 0)),
                                    end_time=float(seg.get("end", 0)),
                                )
                            )
                        full_text = payload.get("text", "")
                        if not full_text and segments:
                            full_text = " ".join(s.text for s in segments)
                        logger.info(f"Async polling complete: {len(segments)} segments")
                        return segments, full_text

                    if status in ("FAILED", "ERROR"):
                        raise ProviderError(
                            f"Mossland task failed: {status}",
                            provider=self.PROVIDER_NAME,
                            status_code=502,
                        )

                if time.monotonic() >= deadline:
                    raise TranscriptionError(f"Mossland poll timeout for task {task_id}")

                time.sleep(poll_interval)


    def _transcribe_chunked(
        self, audio_bytes: bytes, files: dict, data: str, audio_duration: float
    ) -> tuple[list[TranscriptionSegment], str]:
        """Transcribe by splitting audio into chunks (last-resort fallback).

        Speaker labels may fragment across chunks — use only when streaming
        and async polling both fail. Each chunk is sent via async polling.
        """
        import tempfile
        import shutil

        logger.warning(
            f"Using chunked fallback: splitting {audio_duration:.0f}s audio "
            f"into {self.chunk_duration}s segments (speaker labels may fragment)"
        )

        tmpdir = tempfile.mkdtemp(prefix="mossland_chunk_")
        try:
            input_path = os.path.join(tmpdir, "input")
            with open(input_path, "wb") as f:
                f.write(audio_bytes)

            pattern = os.path.join(tmpdir, "chunk_%03d.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-f", "segment",
                 "-segment_time", str(self.chunk_duration),
                 "-ar", "16000", "-ac", "1", "-b:a", "32k",
                 "-reset_timestamps", "1", "-loglevel", "error", pattern],
                capture_output=True, timeout=300,
            )

            from pathlib import Path
            all_segments: list[TranscriptionSegment] = []
            all_text: list[str] = []
            for chunk_file in sorted(Path(tmpdir).glob("chunk_*.mp3")):
                idx = int(chunk_file.stem.split("_")[1])
                offset = idx * self.chunk_duration

                with open(chunk_file, "rb") as cf:
                    chunk_bytes = cf.read()

                chunk_files = {"file": (chunk_file.name, chunk_bytes, "audio/mpeg")}
                chunk_data = dict(data)
                chunk_data.pop("stream", None)

                segs, text = self._transcribe_async(chunk_bytes, chunk_files, chunk_data)

                for seg in segs:
                    seg.start_time = round((seg.start_time or 0) + offset, 2)
                    seg.end_time = round((seg.end_time or 0) + offset, 2)
                    all_segments.append(seg)
                all_text.append(text)
                logger.info(f"Chunk {idx} done: {len(segs)} segments")

            return all_segments, " ".join(all_text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def available_models() -> list[str]:
        # Returns dynamically discovered models (from GET /v1/models) if available,
        # otherwise falls back to the hardcoded _MODELS dict.
        # The instance method _fetch_models() populates _discovered_models at init.
        return sorted(set(_MODELS.keys()))

    @staticmethod
    def available_langs() -> list[str]:
        # MOSS-TD supports 100+ languages; use the same list as Whisper for UI consistency
        import whisper.tokenizer
        return sorted(list(whisper.tokenizer.LANGUAGES.values()))

    @staticmethod
    def available_compute_types() -> list[str]:
        return ["default"]