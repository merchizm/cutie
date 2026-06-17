from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    has_audio: bool
    size_bytes: int

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def resolution(self) -> str:
        if self.width <= 0 or self.height <= 0:
            return "Unknown"
        return f"{self.width} x {self.height}"
