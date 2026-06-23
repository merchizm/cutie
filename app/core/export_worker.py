from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from app.core.ffmpeg_command_builder import ExportSettings, build_ffmpeg_commands
from app.core.ffmpeg_errors import summarize_ffmpeg_stderr


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
        temp_dir = Path(tempfile.gettempdir()) / "cutie"
        temp_dir.mkdir(parents=True, exist_ok=True)
        passlog_path = temp_dir / f"ffmpeg-pass-{threading.get_ident()}"
        concat_manifest_path = temp_dir / f"concat-{threading.get_ident()}.ffconcat"
        try:
            commands = build_ffmpeg_commands(
                self.settings,
                passlog_path=passlog_path,
                concat_manifest_path=concat_manifest_path,
            )
        except Exception as exc:
            logger.exception("Failed to build export command.")
            self.on_done(False, str(exc))
            return
        total_commands = len(commands)
        for index, command in enumerate(commands):
            success, message = self._run_command(command, index, total_commands)
            if not success:
                self._cleanup_passlog(passlog_path)
                self._cleanup_concat_manifest(concat_manifest_path)
                self.on_done(False, message)
                return
        self._cleanup_passlog(passlog_path)
        self._cleanup_concat_manifest(concat_manifest_path)
        self.on_progress(1.0)
        self.on_done(True, "Export finished.")

    def _run_command(self, command: list[str], command_index: int, total_commands: int) -> tuple[bool, str]:
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
            return False, "ffmpeg was not found. Install FFmpeg to export videos."
        except Exception as exc:
            logger.exception("Failed to start export process.")
            return False, str(exc)

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
                self._emit_progress(seconds, command_index, total_commands)
            elif key == "out_time":
                seconds = _parse_ffmpeg_time(value)
                self._emit_progress(seconds, command_index, total_commands)

        try:
            _, stderr = process.communicate(timeout=FFMPEG_EXPORT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Export command timed out: %s", self.settings.output_path)
            process.kill()
            process.communicate()
            return False, "Export timed out."
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
        if self._cancelled.is_set():
            return False, "Export cancelled."
        if process.returncode == 0:
            return True, "Export pass finished."
        message = summarize_ffmpeg_stderr(stderr, "Export failed.")
        logger.warning("Export failed for %s: %s\n%s", self.settings.output_path, message, stderr.strip())
        return False, message

    def _emit_progress(self, seconds: float, command_index: int, total_commands: int) -> None:
        pass_fraction = min(seconds / self.expected_duration, 1.0)
        overall = (command_index + pass_fraction) / max(total_commands, 1)
        self.on_progress(min(overall, 1.0))

    def _cleanup_passlog(self, passlog_path: Path) -> None:
        for path in passlog_path.parent.glob(f"{passlog_path.name}*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove FFmpeg pass log file: %s", path)

    def _cleanup_concat_manifest(self, concat_manifest_path: Path) -> None:
        try:
            concat_manifest_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove FFmpeg concat manifest: %s", concat_manifest_path)


def _parse_ffmpeg_time(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0
