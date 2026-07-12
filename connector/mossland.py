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
_DEFAULT_MAX_NEW_TOKENS = 65536  # fallback for short clips

# The model emits a time marker every 5 seconds, so a valid segment end
# can overshoot the audio tail by up to one marker interval. Anything past
# that is a corrupted timestamp token and should be clamped.
_TIMESTAMP_TOLERANCE_S = 5.0


def _scaled_max_new_tokens(audio_duration_s: float) -> str:
    """Scale max_new_tokens with audio duration, capped at the 128K context."""
    if audio_duration_s <= 0:
        tokens = _DEFAULT_MAX_NEW_TOKENS
    else:
        tokens = int(audio_duration_s * _OUTPUT_TOKENS_PER_AUDIO_SECOND)
        tokens = max(tokens, 5120)  # floor: don't go below the old default
        tokens = min(tokens, 131072)  # cap at context window
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
DEFAULT_SAMPLING_PARAMS = '{"max_new_tokens":65536,"temperature":0}'


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
                - base_url: API base URL (default: https://api.mosi.cn)
                - model: Model name (default: moss-transcribe-diarize)
        """
        super().__init__(config)

        self.api_key = config["api_key"]
        self.base_url = config.get("base_url", "https://api.mosi.cn").rstrip("/")
        self.model = config.get("model", "moss-transcribe-diarize")
        self.version = os.environ.get("MOSSLAND_VERSION", DEFAULT_VERSION)
        # If MOSSLAND_SAMPLING_PARAMS is set, use it; otherwise we scale
        # dynamically based on audio duration (see _scaled_max_new_tokens).
        self._fixed_sampling_params = os.environ.get("MOSSLAND_SAMPLING_PARAMS")

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)",
        }

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

            # Determine sampling_params: scale with duration unless explicitly set
            if self._fixed_sampling_params:
                sampling_params = self._fixed_sampling_params
            else:
                sampling_params = _scaled_max_new_tokens(audio_duration)

            # Build multipart form data
            files = {"file": (request.filename, audio_bytes, request.mime_type or "audio/mpeg")}
            data = {
                "model": effective_model,
                "version": self.version,
                "diarize": "true",
                "sampling_params": sampling_params,
            }

            # Add hotwords if provided
            if request.hotwords:
                data["hotwords"] = request.hotwords

            logger.info(
                f"Mossland transcribing: {request.filename} "
                f"(duration={audio_duration:.0f}s, model={effective_model})"
            )

            # Try SSE streaming first, fall back to async polling
            try:
                segments, full_text = self._transcribe_streaming(audio_bytes, files, data)
            except Exception as e:
                logger.warning(f"SSE streaming failed ({e}), falling back to async polling")
                segments, full_text = self._transcribe_async(audio_bytes, files, data)

            # Sanitize timestamps: clamp any that exceed audio duration + 5s tolerance
            # (sglang-omni #1034: the model can emit corrupted end timestamps)
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

        timeout = httpx.Timeout(connect=120.0, read=None, write=120.0, pool=120.0)

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
        data["async"] = "true"

        with httpx.Client(timeout=httpx.Timeout(connect=60.0, read=1800.0)) as client:
            # Submit task
            r = client.post(
                f"{self.base_url}/v1/audio/transcriptions",
                files=files,
                data=data,
                headers=self.headers,
            )
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
            deadline = time.monotonic() + 1800  # 30 min max
            poll_interval = 5.0

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

    @staticmethod
    def available_models() -> list[str]:
        return ["moss-transcribe-diarize", "moss-transcribe"]

    @staticmethod
    def available_langs() -> list[str]:
        # MOSS-TD supports 100+ languages; use the same list as Whisper for UI consistency
        import whisper.tokenizer
        return sorted(list(whisper.tokenizer.LANGUAGES.values()))

    @staticmethod
    def available_compute_types() -> list[str]:
        return ["default"]