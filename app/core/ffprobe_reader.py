from __future__ import annotations

import json
import logging
import subprocess
from fractions import Fraction
from pathlib import Path

from app.core.media_info import MediaInfo

logger = logging.getLogger(__name__)
FFPROBE_TIMEOUT_SECONDS = 30
_info_cache: dict[tuple[Path, int, int], MediaInfo] = {}
_duration_cache: dict[tuple[Path, int, int], float] = {}


class FFprobeError(RuntimeError):
    pass


def _parse_fps(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def read_media_info(path: Path) -> MediaInfo:
    cache_key = _cache_key(path)
    cached = _info_cache.get(cache_key)
    if cached is not None:
        return cached
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        logger.exception("ffprobe executable was not found")
        raise FFprobeError("ffprobe was not found. Install FFmpeg to use Cutie.") from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or "ffprobe could not read this file."
        logger.warning("ffprobe failed for %s: %s", path, details)
        raise FFprobeError(details) from exc
    except subprocess.TimeoutExpired as exc:
        logger.warning("ffprobe timed out for %s", path)
        raise FFprobeError("ffprobe timed out while reading this file.") from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("ffprobe returned invalid JSON for %s", path)
        raise FFprobeError("ffprobe returned invalid metadata.") from exc

    video_stream = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"),
        {},
    )
    has_audio = any(stream.get("codec_type") == "audio" for stream in payload.get("streams", []))
    format_info = payload.get("format", {})
    duration = float(format_info.get("duration") or video_stream.get("duration") or 0)

    info = MediaInfo(
        path=path,
        duration=duration,
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=_parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        codec=str(video_stream.get("codec_name") or "Unknown"),
        has_audio=has_audio,
        size_bytes=path.stat().st_size,
    )
    _info_cache[cache_key] = info
    _duration_cache[cache_key] = info.duration
    return info


def read_media_duration(path: Path) -> float:
    cache_key = _cache_key(path)
    cached = _duration_cache.get(cache_key)
    if cached is not None:
        return cached
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
        payload = json.loads(result.stdout)
        duration = float(payload.get("format", {}).get("duration") or 0.0)
        _duration_cache[cache_key] = duration
        return duration
    except FileNotFoundError as exc:
        logger.exception("ffprobe executable was not found")
        raise FFprobeError("ffprobe was not found. Install FFmpeg to use Cutie.") from exc
    except subprocess.TimeoutExpired as exc:
        logger.warning("ffprobe duration read timed out for %s", path)
        raise FFprobeError("ffprobe timed out while reading media duration.") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Could not read media duration for %s", path)
        raise FFprobeError("Could not read media duration.") from exc


def clear_ffprobe_cache() -> None:
    _info_cache.clear()
    _duration_cache.clear()


def _cache_key(path: Path) -> tuple[Path, int, int]:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        logger.warning("Media file could not be accessed: %s", path)
        raise FFprobeError(f"Media file could not be accessed: {path}") from exc
    return resolved, stat.st_mtime_ns, stat.st_size
