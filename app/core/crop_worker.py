from __future__ import annotations

import subprocess
import tempfile
import threading
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.core.ffmpeg_errors import summarize_ffmpeg_stderr
from app.utils.timecode import seconds_to_timecode


DoneCallback = Callable[[bool, Path | None, str], None]
CallbackDispatcher = Callable[[DoneCallback, bool, Path | None, str], None]
FFMPEG_CROP_TIMEOUT_SECONDS = 60 * 30
logger = logging.getLogger(__name__)


class CropWorker:
    def __init__(
        self,
        input_path: Path,
        crop: tuple[int, int, int, int],
        duration: float,
        on_done: DoneCallback,
        dispatch_done: CallbackDispatcher | None = None,
    ) -> None:
        self.input_path = input_path
        self.crop = crop
        self.duration = max(duration, 0.001)
        self.on_done = on_done
        self.dispatch_done = dispatch_done
        self.output_path = Path(tempfile.gettempdir()) / "cutie" / f"cropped_{uuid4().hex}.mp4"
        self.temp_files = [self.output_path]
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()

    def cleanup(self, keep: set[Path] | None = None) -> None:
        keep = keep or set()
        for path in self.temp_files:
            if path not in keep:
                path.unlink(missing_ok=True)

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
        if not success and not self._cancelled.is_set():
            fallback_command = [*base, "-c:a", "aac", "-b:a", "192k", str(self.output_path)]
            success, message = self._run_command(fallback_command)
        if self._cancelled.is_set():
            self.output_path.unlink(missing_ok=True)
            self._emit_done(False, None, "Crop cancelled.")
        else:
            self._emit_done(success, self.output_path if success else None, message)

    def _run_command(self, command: list[str]) -> tuple[bool, str]:
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._lock:
                self._process = process
            try:
                _stdout, stderr = process.communicate(timeout=FFMPEG_CROP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning("Crop command timed out: %s", self.input_path)
                process.kill()
                process.communicate()
                return False, "Crop timed out."
        except FileNotFoundError:
            logger.exception("ffmpeg executable was not found for crop.")
            return False, "ffmpeg was not found. Install FFmpeg to crop videos."
        except Exception as exc:
            logger.exception("Failed to start crop process.")
            return False, str(exc)
        finally:
            with self._lock:
                self._process = None
        if process is None:
            return False, "Crop failed."
        if process.returncode == 0:
            return True, f"Cropped working video generated at {seconds_to_timecode(self.duration)} source duration."
        message = summarize_ffmpeg_stderr(stderr, "Crop failed.")
        logger.warning("Crop command failed for %s: %s\n%s", self.input_path, message, stderr.strip())
        return False, message

    def _emit_done(self, success: bool, path: Path | None, message: str) -> None:
        if self.dispatch_done is not None:
            self.dispatch_done(self.on_done, success, path, message)
        else:
            self.on_done(success, path, message)
