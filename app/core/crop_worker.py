from __future__ import annotations

import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.utils.timecode import seconds_to_timecode


DoneCallback = Callable[[bool, Path | None, str], None]


class CropWorker:
    def __init__(
        self,
        input_path: Path,
        crop: tuple[int, int, int, int],
        duration: float,
        on_done: DoneCallback,
    ) -> None:
        self.input_path = input_path
        self.crop = crop
        self.duration = max(duration, 0.001)
        self.on_done = on_done
        self.output_path = Path(tempfile.gettempdir()) / "cutie" / f"cropped_{uuid4().hex}.mp4"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        x, y, width, height = self.crop
        filter_arg = f"crop={width}:{height}:{x}:{y}"
        base = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-nostdin",
            "-i",
            str(self.input_path),
            "-vf",
            filter_arg,
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "veryfast",
        ]
        copy_command = [*base, "-c:a", "copy", str(self.output_path)]
        success, message = self._run_command(copy_command)
        if not success:
            fallback_command = [*base, "-c:a", "aac", "-b:a", "192k", str(self.output_path)]
            success, message = self._run_command(fallback_command)
        self.on_done(success, self.output_path if success else None, message)

    def _run_command(self, command: list[str]) -> tuple[bool, str]:
        try:
            process = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return False, "ffmpeg was not found. Install FFmpeg to crop videos."
        if process.returncode == 0:
            return True, f"Cropped working video generated at {seconds_to_timecode(self.duration)} source duration."
        message = process.stderr.strip().splitlines()[-1] if process.stderr.strip() else "Crop failed."
        return False, message
