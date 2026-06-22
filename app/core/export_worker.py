from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable

from app.core.ffmpeg_command_builder import ExportSettings, build_ffmpeg_command


ProgressCallback = Callable[[float], None]
DoneCallback = Callable[[bool, str], None]
FFMPEG_EXPORT_TIMEOUT_SECONDS = 60 * 60 * 4
logger = logging.getLogger(__name__)


class ExportWorker:
    def __init__(
        self,
        settings: ExportSettings,
        expected_duration: float,
        on_progress: ProgressCallback,
        on_done: DoneCallback,
    ) -> None:
        self.settings = settings
        self.expected_duration = max(expected_duration, 0.001)
        self.on_progress = on_progress
        self.on_done = on_done
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()

    def _run(self) -> None:
        command = build_ffmpeg_command(self.settings)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            with self._lock:
                self._process = process
        except FileNotFoundError:
            logger.exception("ffmpeg executable was not found for export.")
            self.on_done(False, "ffmpeg was not found. Install FFmpeg to export videos.")
            return
        except Exception as exc:
            logger.exception("Failed to start export process.")
            self.on_done(False, str(exc))
            return

        assert process.stdout is not None
        for line in process.stdout:
            if self._cancelled.is_set():
                break
            key, _, value = line.strip().partition("=")
            if key == "out_time_ms":
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                self.on_progress(min(seconds / self.expected_duration, 1.0))
            elif key == "out_time":
                seconds = _parse_ffmpeg_time(value)
                self.on_progress(min(seconds / self.expected_duration, 1.0))

        try:
            _, stderr = process.communicate(timeout=FFMPEG_EXPORT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Export command timed out: %s", self.settings.output_path)
            process.kill()
            process.communicate()
            self.on_done(False, "Export timed out.")
            return
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
        if self._cancelled.is_set():
            self.on_done(False, "Export cancelled.")
        elif process.returncode == 0:
            self.on_progress(1.0)
            self.on_done(True, "Export finished.")
        else:
            message = stderr.strip().splitlines()[-1] if stderr.strip() else "Export failed."
            logger.warning("Export failed for %s: %s", self.settings.output_path, message)
            self.on_done(False, message)


def _parse_ffmpeg_time(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0
