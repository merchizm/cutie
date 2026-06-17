from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

from app.core.ffmpeg_command_builder import ExportSettings, build_ffmpeg_command


ProgressCallback = Callable[[float], None]
DoneCallback = Callable[[bool, str], None]


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

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _run(self) -> None:
        command = build_ffmpeg_command(self.settings)
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self.on_done(False, "ffmpeg was not found. Install FFmpeg to export videos.")
            return
        except Exception as exc:
            self.on_done(False, str(exc))
            return

        assert self._process.stdout is not None
        for line in self._process.stdout:
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

        _, stderr = self._process.communicate()
        if self._process.returncode == 0:
            self.on_progress(1.0)
            self.on_done(True, "Export finished.")
        else:
            message = stderr.strip().splitlines()[-1] if stderr.strip() else "Export failed."
            self.on_done(False, message)


def _parse_ffmpeg_time(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0
