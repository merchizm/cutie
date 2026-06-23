from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)
FFMPEG_CAPABILITIES_TIMEOUT_SECONDS = 5

_encoder_cache: set[str] | None = None


def available_encoders() -> set[str]:
    global _encoder_cache
    if _encoder_cache is not None:
        return _encoder_cache
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_CAPABILITIES_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.warning("Could not read FFmpeg encoder capabilities.", exc_info=True)
        _encoder_cache = set()
        return _encoder_cache
    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    _encoder_cache = encoders
    return encoders


def available_h264_encoder_options() -> dict[str, str]:
    encoders = available_encoders()
    options = {"CPU H.264": "libx264"}
    hardware = {
        "NVIDIA NVENC": "h264_nvenc",
        "Intel QSV": "h264_qsv",
        "AMD AMF": "h264_amf",
    }
    for label, encoder in hardware.items():
        if encoder in encoders:
            options[label] = encoder
    return options

