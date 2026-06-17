from __future__ import annotations

import subprocess
from pathlib import Path


def open_folder(path: Path) -> None:
    folder = path if path.is_dir() else path.parent
    subprocess.Popen(["xdg-open", str(folder)])


def default_output_path(input_path: Path, extension: str = "mp4") -> Path:
    suffix = extension.lstrip(".")
    return input_path.with_name(f"{input_path.stem}-export.{suffix}")
