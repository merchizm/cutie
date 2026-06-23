from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app.core.project_state import ProjectState


def referenced_media_paths(states: Iterable[ProjectState]) -> set[Path]:
    paths: set[Path] = set()
    for state in states:
        for candidate in (state.original_video_path, state.working_video_path, state.source_video_path):
            if candidate is not None:
                paths.add(candidate)
        paths.update(state.media_pool)
        paths.update(record.output_path for record in state.crop_history)
        for track in state.tracks:
            paths.update(clip.source_path for clip in track.clips)
    return paths

