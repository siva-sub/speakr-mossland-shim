#!/usr/bin/env python3
"""
speakr-mcp: a Model Context Protocol server for speakr.

Exposes the full speakr REST API (v1) as MCP tools so any MCP client
(Pi, Claude Desktop, Cursor, etc.) can upload audio, fetch transcripts,
rename speakers, chat about recordings, manage folders/tags, and more.

Config (env vars):
  SPEAKR_URL    – base URL of the speakr instance (default: http://127.0.0.1:8899)
  SPEAKR_TOKEN  – Bearer API token (from speakr → Account → API Keys)

The server runs over stdio, launched on demand by the MCP client.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SPEAKR_URL = os.environ.get("SPEAKR_URL", "http://127.0.0.1:8899").rstrip("/")
SPEAKR_TOKEN = os.environ.get("SPEAKR_TOKEN", "")
SHIM_URL = os.environ.get("SHIM_URL", "http://127.0.0.1:8001").rstrip("/")
TIMEOUT = float(os.environ.get("SPEAKR_TIMEOUT", "300"))

if not SPEAKR_TOKEN:
    raise RuntimeError("SPEAKR_TOKEN is required. Get one from speakr → Account → API Keys.")

HEADERS = {"Authorization": f"Bearer {SPEAKR_TOKEN}"}

mcp = FastMCP("speakr")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
    timeout: float | None = None,
) -> Any:
    """Make an authenticated request to speakr and return parsed JSON."""
    url = f"{SPEAKR_URL}/api/v1{path}"
    t = timeout or TIMEOUT
    with httpx.Client(timeout=t) as client:
        r = client.request(
            method,
            url,
            headers=HEADERS,
            params=params,
            json=json_body,
            files=files,
            data=data,
        )
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:500]
        raise RuntimeError(f"speakr {method} {path} → {r.status_code}: {detail}")
    # Some endpoints return plain text or empty
    if r.status_code == 204 or not r.content:
        return {"status": "ok"}
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        return r.json()
    return {"raw": r.text[:2000]}


# ---------------------------------------------------------------------------
# Audio compression helper
# ---------------------------------------------------------------------------


def _compress_audio(file_path: str) -> str:
    """
    Compress audio to 16kHz mono 32k mp3 using ffmpeg.

    The MOSS-Transcribe-Diarize model uses WhisperFeatureExtractor at 16kHz
    internally, so anything above 16kHz is wasted bandwidth. Downsampling to
    16kHz mono 32k mp3 reduces file size by ~10x with zero quality loss from
    the model's perspective. This also avoids triggering speakr's file-size
    based chunking and the shim's upload limit.

    Returns the path to the compressed temp file.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="speakr_mcp_")
    os.close(fd)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        file_path,
        "-ar",
        "16000",  # 16kHz sample rate (model's native)
        "-ac",
        "1",  # mono
        "-b:a",
        "32k",  # 32 kbps bitrate
        "-loglevel",
        "error",
        tmp_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        os.unlink(tmp_path)
        raise RuntimeError(f"ffmpeg compression failed: {result.stderr[:500]}")
    return tmp_path


# ---------------------------------------------------------------------------
# Upload & Recordings
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_upload(
    file_path: str,
    title: str | None = None,
    folder_id: int | None = None,
    hotwords: str | None = None,
    initial_prompt: str | None = None,
    language: str | None = None,
    transcription_model: str | None = None,
    compress: bool = True,
) -> dict:
    """
    Upload an audio/video file to speakr. Automatically queues transcription.

    By default, compresses the audio to 16kHz mono 32k mp3 before uploading.
    This is the MOSS model's native format — no quality loss, ~10x smaller,
    avoids chunking issues. Set compress=False to upload the original file.

    Args:
        file_path: Local path to the audio/video file.
        title: Optional title for the recording.
        folder_id: Optional folder to place the recording in.
        hotwords: Custom vocabulary hints for transcription.
        initial_prompt: Optional prompt for the transcription model.
        language: Language hint (e.g. 'en', 'zh').
        transcription_model: Override the transcription model.
        compress: If True (default), compress with ffmpeg to 16kHz mono first.

    Returns:
        Recording object with id, status, and metadata.

    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    _ensure_containers()

    tmp_compressed = None
    upload_path = file_path
    try:
        if compress:
            tmp_compressed = _compress_audio(file_path)
            upload_path = tmp_compressed
            orig_mb = p.stat().st_size / 1024 / 1024
            comp_mb = os.path.getsize(tmp_compressed) / 1024 / 1024
            log.info("Compressed %.1f MB → %.1f MB", orig_mb, comp_mb)

        with open(upload_path, "rb") as f:
            files = {"file": (p.name, f)}
            data: dict[str, Any] = {}
            if title:
                data["title"] = title
            if folder_id is not None:
                data["folder_id"] = str(folder_id)
            if hotwords:
                data["hotwords"] = hotwords
            if initial_prompt:
                data["initial_prompt"] = initial_prompt
            if language:
                data["language"] = language
            if transcription_model:
                data["transcription_model"] = transcription_model
            return _request("POST", "/recordings/upload", files=files, data=data, timeout=600)
    finally:
        if tmp_compressed and os.path.exists(tmp_compressed):
            os.unlink(tmp_compressed)


@mcp.tool()
def speakr_list_recordings(
    page: int = 1,
    per_page: int = 25,
    folder_id: int | None = None,
    tag_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
) -> dict:
    """
    List recordings with optional filters.

    Args:
        page: Page number (default 1).
        per_page: Items per page (default 25).
        folder_id: Filter by folder.
        tag_id: Filter by tag.
        status: Filter by processing status.
        search: Full-text search in transcript/title.

    """
    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if folder_id is not None:
        params["folder_id"] = folder_id
    if tag_id is not None:
        params["tag_id"] = tag_id
    if status:
        params["status"] = status
    if search:
        params["search"] = search
    return _request("GET", "/recordings", params=params)


@mcp.tool()
def speakr_get_recording(recording_id: int) -> dict:
    """Get full details for a single recording."""
    return _request("GET", f"/recordings/{recording_id}")


@mcp.tool()
def speakr_update_recording(
    recording_id: int,
    title: str | None = None,
    meeting_date: str | None = None,
    folder_id: int | None = None,
    note: str | None = None,
) -> dict:
    """Update a recording's metadata."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if meeting_date is not None:
        body["meeting_date"] = meeting_date
    if folder_id is not None:
        body["folder_id"] = folder_id
    if note is not None:
        body["note"] = note
    return _request("PATCH", f"/recordings/{recording_id}", json_body=body)


@mcp.tool()
def speakr_delete_recording(recording_id: int) -> dict:
    """Delete a recording."""
    return _request("DELETE", f"/recordings/{recording_id}")


@mcp.tool()
def speakr_get_status(recording_id: int) -> dict:
    """Get the processing status of a recording (transcription, summary, etc.)."""
    return _request("GET", f"/recordings/{recording_id}/status")


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_get_transcript(recording_id: int, format: str = "json") -> Any:
    """
    Get the full transcript for a recording, including speaker labels and timestamps.

    Args:
        recording_id: The recording ID.
        format: Output format — 'json' (default, with segments) or 'text'.

    """
    return _request("GET", f"/recordings/{recording_id}/transcript", params={"format": format})


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_get_summary(recording_id: int) -> dict:
    """Get the AI-generated summary for a recording."""
    return _request("GET", f"/recordings/{recording_id}/summary")


@mcp.tool()
def speakr_summarize(recording_id: int, prompt: str | None = None) -> dict:
    """
    Queue summarization for a recording. Optionally override the summary prompt.

    Args:
        recording_id: The recording ID.
        prompt: Custom prompt for the summarization model. If omitted, uses the default.

    """
    body: dict[str, Any] = {}
    if prompt:
        body["prompt"] = prompt
    return _request("POST", f"/recordings/{recording_id}/summarize", json_body=body or None)


@mcp.tool()
def speakr_replace_summary(recording_id: int, summary: str) -> dict:
    """Replace the summary text for a recording."""
    return _request("PUT", f"/recordings/{recording_id}/summary", json_body={"summary": summary})


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_chat(recording_id: int, message: str, conversation_history: list | None = None) -> dict:
    """
    Ask a question about a recording. Uses the transcript as context.

    Args:
        recording_id: The recording ID.
        message: Your question or instruction.
        conversation_history: Optional prior messages for multi-turn chat.

    """
    body: dict[str, Any] = {"message": message}
    if conversation_history:
        body["conversation_history"] = conversation_history
    return _request("POST", f"/recordings/{recording_id}/chat", json_body=body, timeout=600)


# ---------------------------------------------------------------------------
# Speaker management (per-recording)
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_get_speakers(recording_id: int) -> dict:
    """Get the speakers detected in a recording, with their segment counts and talk time."""
    return _request("GET", f"/recordings/{recording_id}/speakers")


@mcp.tool()
def speakr_assign_speakers(
    recording_id: int,
    speaker_map: dict[str, Any],
    regenerate_summary: bool = False,
) -> dict:
    """
    Rename / assign speaker labels in a transcript.

    This is the primary tool for editing speaker names.

    Args:
        recording_id: The recording ID.
        speaker_map: Map of speaker labels to names.
            Values can be a plain string (the name) or an object {"name": "...", "isMe": true}.
            Example: {"S01": "Alice", "S02": {"name": "Bob", "isMe": true}}
        regenerate_summary: If True, regenerate the summary after renaming.

    """
    return _request(
        "PUT",
        f"/recordings/{recording_id}/speakers/assign",
        json_body={"speaker_map": speaker_map, "regenerate_summary": regenerate_summary},
    )


@mcp.tool()
def speakr_identify_speakers(recording_id: int) -> dict:
    """Auto-identify speakers in a recording using the LLM (GLM-5.2)."""
    return _request("POST", f"/recordings/{recording_id}/speakers/identify", timeout=600)


# ---------------------------------------------------------------------------
# Speaker library (global, cross-recording)
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_list_speakers() -> dict:
    """List all speakers in the global speaker library."""
    return _request("GET", "/speakers")


@mcp.tool()
def speakr_create_speaker(name: str, description: str | None = None) -> dict:
    """Create a new speaker in the global library."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    return _request("POST", "/speakers", json_body=body)


@mcp.tool()
def speakr_update_speaker(
    speaker_id: int, name: str | None = None, description: str | None = None
) -> dict:
    """Update a speaker in the global library."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    return _request("PUT", f"/speakers/{speaker_id}", json_body=body)


@mcp.tool()
def speakr_delete_speaker(speaker_id: int) -> dict:
    """Delete a speaker from the global library."""
    return _request("DELETE", f"/speakers/{speaker_id}")


# ---------------------------------------------------------------------------
# Transcription control
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_transcribe(
    recording_id: int,
    language: str | None = None,
    model: str | None = None,
    hotwords: str | None = None,
    diarize: bool | None = None,
) -> dict:
    """Queue (re-)transcription for a recording."""
    body: dict[str, Any] = {}
    if language:
        body["language"] = language
    if model:
        body["model"] = model
    if hotwords:
        body["hotwords"] = hotwords
    if diarize is not None:
        body["diarize"] = diarize
    return _request("POST", f"/recordings/{recording_id}/transcribe", json_body=body or None)


@mcp.tool()
def speakr_batch_transcribe(recording_ids: list[int]) -> dict:
    """Queue transcription for multiple recordings at once."""
    return _request(
        "POST", "/recordings/batch/transcribe", json_body={"recording_ids": recording_ids}
    )


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_list_folders() -> dict:
    """List all folders."""
    return _request("GET", "/folders")


@mcp.tool()
def speakr_create_folder(name: str, parent_id: int | None = None) -> dict:
    """Create a new folder."""
    body: dict[str, Any] = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    return _request("POST", "/folders", json_body=body)


@mcp.tool()
def speakr_update_folder(folder_id: int, name: str | None = None) -> dict:
    """Update a folder's name."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    return _request("PATCH", f"/folders/{folder_id}", json_body=body)


@mcp.tool()
def speakr_delete_folder(folder_id: int) -> dict:
    """Delete a folder."""
    return _request("DELETE", f"/folders/{folder_id}")


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_list_tags() -> dict:
    """List all tags."""
    return _request("GET", "/tags")


@mcp.tool()
def speakr_create_tag(name: str, color: str | None = None) -> dict:
    """Create a new tag."""
    body: dict[str, Any] = {"name": name}
    if color:
        body["color"] = color
    return _request("POST", "/tags", json_body=body)


@mcp.tool()
def speakr_add_tag(recording_id: int, tag_id: int) -> dict:
    """Add a tag to a recording."""
    return _request("POST", f"/recordings/{recording_id}/tags", json_body={"tag_id": tag_id})


@mcp.tool()
def speakr_remove_tag(recording_id: int, tag_id: int) -> dict:
    """Remove a tag from a recording."""
    return _request("DELETE", f"/recordings/{recording_id}/tags/{tag_id}")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_get_notes(recording_id: int) -> dict:
    """Get notes for a recording."""
    return _request("GET", f"/recordings/{recording_id}/notes")


@mcp.tool()
def speakr_update_notes(recording_id: int, notes: str) -> dict:
    """Replace notes for a recording."""
    return _request("PUT", f"/recordings/{recording_id}/notes", json_body={"notes": notes})


# ---------------------------------------------------------------------------
# Events (calendar)
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_get_events(recording_id: int) -> dict:
    """Get calendar events extracted from a recording."""
    return _request("GET", f"/recordings/{recording_id}/events")


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_get_stats() -> dict:
    """Get system statistics (storage, recordings count, etc.)."""
    return _request("GET", "/stats")


@mcp.tool()
def speakr_get_user() -> dict:
    """Get the current user's profile and preferences."""
    return _request("GET", "/users/me")


@mcp.tool()
def speakr_get_transcription_info() -> dict:
    """Get info about the active transcription connector and its capabilities."""
    return _request("GET", "/transcription")


@mcp.tool()
def speakr_toggle_auto_summarization(enabled: bool) -> dict:
    """Toggle auto-summarization on or off."""
    return _request("PUT", "/settings/auto-summarization", json_body={"enabled": enabled})


# ---------------------------------------------------------------------------
# Docker auto-start helpers
# ---------------------------------------------------------------------------


def _ensure_containers() -> None:
    """
    Check if mossland-shim and speakr containers are running; start them if down.

    Called automatically by speakr_shim_health and speakr_upload.
    Uses the docker CLI — assumes docker is available on the host.
    """
    import subprocess as sp

    for name in ("mossland-shim", "speakr"):
        try:
            result = sp.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True, text=True, timeout=10,
            )
            running = result.stdout.strip() == "true"
        except Exception:
            running = False

        if not running:
            log.info("Container %s is down, starting...", name)
            try:
                # Try simple start first (works if container exists but is stopped)
                sp.run(["docker", "start", name], capture_output=True, timeout=30)
                log.info("Started %s", name)
            except Exception as e:
                log.warning("Could not start %s: %s", name, e)


# ---------------------------------------------------------------------------
# Shim tools (direct access to the mossland-shim, bypassing speakr)
# ---------------------------------------------------------------------------


@mcp.tool()
def speakr_shim_health() -> dict:
    """
    Check the mossland-shim health and configuration.

    Returns the shim's upstream URL, model name, model version, streaming mode,
    and API key status. Automatically starts Docker containers if they are down.
    Use this to verify the transcription backend is healthy.
    """
    _ensure_containers()
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{SHIM_URL}/healthz")
            return r.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def speakr_shim_transcribe(
    file_path: str,
    model: str | None = None,
    response_format: str = "verbose_json",
) -> dict:
    """
    Transcribe audio directly through the mossland-shim, bypassing speakr.

    This sends the file straight to the shim which uses SSE streaming +
    version=v20260410-streamparam-20260703 + diarize=true +
    sampling_params={max_new_tokens:65536} for single-pass transcription.
    Use this for testing the backend or when you don't need speakr's
    storage/summary/chat features.

    Args:
        file_path: Local path to the audio file.
        model: Model name (default: from shim config).
        response_format: 'json' (text only), 'verbose_json' (segments+speakers), or 'text'.

    Returns:
        Transcription result with text and optionally segments with speakers.

    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(p, "rb") as f, httpx.Client(timeout=1800) as client:
        r = client.post(
            f"{SHIM_URL}/v1/audio/transcriptions",
            files={"file": (p.name, f)},
            data={
                "model": model or "",
                "response_format": response_format,
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(f"shim error {r.status_code}: {r.text[:500]}")
    return r.json()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
