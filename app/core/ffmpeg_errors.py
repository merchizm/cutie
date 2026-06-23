from __future__ import annotations


_NOISY_PREFIXES = (
    "built with ",
    "configuration:",
    "copyright ",
    "ffmpeg version ",
    "libav",
    "libpostproc",
    "libsw",
)
_NOISY_EXACT = {
    "conversion failed!",
}


def summarize_ffmpeg_stderr(stderr: str, fallback: str) -> str:
    candidates: list[str] = []
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered in _NOISY_EXACT:
            continue
        if lowered.startswith(_NOISY_PREFIXES):
            continue
        candidates.append(line)
    if not candidates:
        return fallback
    for line in reversed(candidates):
        lowered = line.lower()
        if "error" in lowered or "failed" in lowered or "invalid" in lowered:
            return line
    return candidates[-1]
