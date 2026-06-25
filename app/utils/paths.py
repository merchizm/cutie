from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def open_folder(path: Path) -> bool:
    folder = path if path.is_dir() else path.parent
    try:
        subprocess.Popen(["xdg-open", str(folder)])
    except OSError:
        logger.exception("Failed to open folder: %s", folder)
        return False
    return True


def default_output_path(input_path: Path, extension: str = "mp4") -> Path:
    suffix = extension.lstrip(".")
    return input_path.with_name(f"{input_path.stem}-export.{suffix}")
