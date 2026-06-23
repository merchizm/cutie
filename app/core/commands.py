from __future__ import annotations

import copy
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from app.core.project_state import Clip, CropRecord, ProjectState


class StateCommand(Protocol):
    def execute(self, state: ProjectState) -> bool: ...
    def undo(self, state: ProjectState) -> None: ...


class ToggleOriginalAudioCommand:
    def execute(self, state: ProjectState) -> bool:
        if not state.source_has_audio:
            return False
        _set_original_audio_enabled(state, not state.original_audio_enabled)
        return True

    def undo(self, state: ProjectState) -> None:
        _set_original_audio_enabled(state, not state.original_audio_enabled)


class ToggleSelectedAudioMuteCommand:
    def __init__(self) -> None:
        self._previous_mute_state: dict[str, bool] = {}

    def execute(self, state: ProjectState) -> bool:
        targets = _selected_audio_mute_targets(state)
        if not targets:
            return False
        self._previous_mute_state = {clip.id: clip.muted for clip in targets}
        for clip in targets:
            clip.muted = not clip.muted
        return True

    def undo(self, state: ProjectState) -> None:
        for clip_id, muted in self._previous_mute_state.items():
            clip = state.find_clip(clip_id)
            if clip is not None:
                clip.muted = muted


class AddAudioClipCommand:
    def __init__(self, path: Path, duration: float, timeline_start: float) -> None:
        self.path = path
        self.duration = duration
        self.timeline_start = timeline_start
        self.added_clip_id: str | None = None
        self._added_clip: Clip | None = None
        self._music_clips: list[Clip] = []
        self._media_pool: list[Path] = []
        self._selected_clip_ids: list[str] = []
        self._selected_clip_id: str | None = None
        self._original_audio_enabled = True
        self._original_audio_track_muted = False
        self._original_audio_clip_mutes: dict[str, bool] = {}

    @property
    def added_clip(self) -> Clip | None:
        return self._added_clip

    def execute(self, state: ProjectState) -> bool:
        self._music_clips = copy.deepcopy(state.music_track.clips)
        self._media_pool = list(state.media_pool)
        self._selected_clip_ids = list(state.selected_clip_ids)
        self._selected_clip_id = state.selected_clip_id
        self._original_audio_enabled = state.original_audio_enabled
        self._original_audio_track_muted = state.original_audio_track.muted
        self._original_audio_clip_mutes = {
            clip.id: clip.muted
            for clip in state.original_audio_track.clips
        }
        self._added_clip = state.add_audio_clip(self.path, self.duration, self.timeline_start)
        self.added_clip_id = self._added_clip.id
        return True

    def undo(self, state: ProjectState) -> None:
        state.music_track.clips = copy.deepcopy(self._music_clips)
        state.media_pool = list(self._media_pool)
        state.selected_clip_ids = list(self._selected_clip_ids)
        state.selected_clip_id = self._selected_clip_id
        state.original_audio_enabled = self._original_audio_enabled
        state.original_audio_track.muted = self._original_audio_track_muted
        for clip in state.original_audio_track.clips:
            if clip.id in self._original_audio_clip_mutes:
                clip.muted = self._original_audio_clip_mutes[clip.id]
        state.refresh_duration()
        self._added_clip = None

    def referenced_paths(self) -> set[Path]:
        return set(self._media_pool) | _clip_paths(self._music_clips)


class ApplyWorkingVideoCommand:
    def __init__(
        self,
        path: Path,
        duration: float,
        width: int,
        height: int,
        crop_record: CropRecord | None = None,
    ) -> None:
        self.path = path
        self.duration = duration
        self.width = width
        self.height = height
        self.crop_record = crop_record
        self._snapshot: dict[str, object] = {}

    def execute(self, state: ProjectState) -> bool:
        self._snapshot = _source_snapshot(state)
        state.apply_working_video(
            self.path,
            self.duration,
            self.width,
            self.height,
            self.crop_record,
        )
        return True

    def undo(self, state: ProjectState) -> None:
        _restore_source_snapshot(state, self._snapshot)

    def referenced_paths(self) -> set[Path]:
        paths = _snapshot_paths(self._snapshot)
        paths.add(self.path)
        if self.crop_record is not None:
            paths.add(self.crop_record.source_path)
            paths.add(self.crop_record.output_path)
        return paths


class ResetToOriginalVideoCommand:
    def __init__(
        self,
        path: Path,
        duration: float,
        width: int,
        height: int,
        has_audio: bool,
    ) -> None:
        self.path = path
        self.duration = duration
        self.width = width
        self.height = height
        self.has_audio = has_audio
        self._snapshot: dict[str, object] = {}

    def execute(self, state: ProjectState) -> bool:
        if state.original_video_path is None or not state.is_cropped:
            return False
        self._snapshot = _source_snapshot(state)
        state.load_source(self.path, self.duration, self.width, self.height, self.has_audio)
        return True

    def undo(self, state: ProjectState) -> None:
        _restore_source_snapshot(state, self._snapshot)

    def referenced_paths(self) -> set[Path]:
        paths = _snapshot_paths(self._snapshot)
        paths.add(self.path)
        return paths


class MoveClipCommand:
    def __init__(self, clip_id: str, timeline_start: float) -> None:
        self.clip_id = clip_id
        self.timeline_start = timeline_start
        self._previous_starts: dict[str, float] = {}

    def execute(self, state: ProjectState) -> bool:
        targets = _move_targets(state, self.clip_id)
        if not targets:
            return False
        self._previous_starts = {clip.id: clip.timeline_start for clip in targets}
        if state.move_clip(self.clip_id, self.timeline_start):
            return True
        self._previous_starts = {}
        return False

    def undo(self, state: ProjectState) -> None:
        for clip_id, timeline_start in self._previous_starts.items():
            clip = state.find_clip(clip_id)
            if clip is not None:
                clip.timeline_start = timeline_start
        state.refresh_duration()


class TrimClipCommand:
    def __init__(self, clip_id: str, side: str, seconds: float) -> None:
        self.clip_id = clip_id
        self.side = side
        self.seconds = seconds
        self._previous_ranges: dict[str, tuple[float, float, float]] = {}

    def execute(self, state: ProjectState) -> bool:
        if self.side not in {"left", "right"}:
            return False
        targets = _trim_targets(state, self.clip_id)
        if not targets:
            return False
        self._previous_ranges = {
            clip.id: (clip.timeline_start, clip.source_in, clip.source_out)
            for clip in targets
        }
        changed = (
            state.trim_clip_left(self.clip_id, self.seconds)
            if self.side == "left"
            else state.trim_clip_right(self.clip_id, self.seconds)
        )
        if changed:
            return True
        self._previous_ranges = {}
        return False

    def undo(self, state: ProjectState) -> None:
        for clip_id, (timeline_start, source_in, source_out) in self._previous_ranges.items():
            clip = state.find_clip(clip_id)
            if clip is not None:
                clip.timeline_start = timeline_start
                clip.source_in = source_in
                clip.source_out = source_out
        state.refresh_duration()


class TrimVideoRangeCommand:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end
        self._track_clips: dict[str, list[Clip]] = {}
        self._selected_clip_ids: list[str] = []
        self._selected_clip_id: str | None = None

    def execute(self, state: ProjectState) -> bool:
        if self.end <= self.start:
            return False
        before = _track_signature(state)
        self._track_clips = {
            track.id: copy.deepcopy(track.clips)
            for track in state.tracks
        }
        self._selected_clip_ids = list(state.selected_clip_ids)
        self._selected_clip_id = state.selected_clip_id
        state.trim_video_range(self.start, self.end)
        if _track_signature(state) != before:
            return True
        self._track_clips = {}
        self._selected_clip_ids = []
        self._selected_clip_id = None
        return False

    def undo(self, state: ProjectState) -> None:
        for track in state.tracks:
            if track.id in self._track_clips:
                track.clips = copy.deepcopy(self._track_clips[track.id])
        state.selected_clip_ids = list(self._selected_clip_ids)
        state.selected_clip_id = self._selected_clip_id
        state.refresh_duration()

    def referenced_paths(self) -> set[Path]:
        return _clip_paths(
            clip
            for clips in self._track_clips.values()
            for clip in clips
        )


class DeleteSelectedClipCommand:
    def __init__(self) -> None:
        self._track_clips: dict[str, list[Clip]] = {}
        self._selected_clip_ids: list[str] = []
        self._selected_clip_id: str | None = None

    def execute(self, state: ProjectState) -> bool:
        if not state.selected_clip_ids:
            return False
        self._track_clips = {
            track.id: copy.deepcopy(track.clips)
            for track in state.tracks
        }
        self._selected_clip_ids = list(state.selected_clip_ids)
        self._selected_clip_id = state.selected_clip_id
        if state.delete_selected_clip():
            return True
        self._track_clips = {}
        self._selected_clip_ids = []
        self._selected_clip_id = None
        return False

    def undo(self, state: ProjectState) -> None:
        for track in state.tracks:
            if track.id in self._track_clips:
                track.clips = copy.deepcopy(self._track_clips[track.id])
        state.selected_clip_ids = list(self._selected_clip_ids)
        state.selected_clip_id = self._selected_clip_id
        state.refresh_duration()

    def referenced_paths(self) -> set[Path]:
        return _clip_paths(
            clip
            for clips in self._track_clips.values()
            for clip in clips
        )


class SplitAtCommand:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._track_clips: dict[str, list[Clip]] = {}
        self._selected_clip_ids: list[str] = []
        self._selected_clip_id: str | None = None

    def execute(self, state: ProjectState) -> bool:
        self._track_clips = {
            track.id: copy.deepcopy(track.clips)
            for track in state.tracks
        }
        self._selected_clip_ids = list(state.selected_clip_ids)
        self._selected_clip_id = state.selected_clip_id
        if state.split_at(self.seconds):
            return True
        self._track_clips = {}
        self._selected_clip_ids = []
        self._selected_clip_id = None
        return False

    def undo(self, state: ProjectState) -> None:
        for track in state.tracks:
            if track.id in self._track_clips:
                track.clips = copy.deepcopy(self._track_clips[track.id])
        state.selected_clip_ids = list(self._selected_clip_ids)
        state.selected_clip_id = self._selected_clip_id
        state.refresh_duration()

    def referenced_paths(self) -> set[Path]:
        return _clip_paths(
            clip
            for clips in self._track_clips.values()
            for clip in clips
        )


def _set_original_audio_enabled(state: ProjectState, enabled: bool) -> None:
    state.original_audio_enabled = enabled
    state.original_audio_track.muted = not enabled
    for clip in state.original_audio_track.clips:
        clip.muted = not enabled


def _selected_audio_mute_targets(state: ProjectState) -> list[Clip]:
    clip = state.selected_clip()
    if clip is None:
        return []
    if clip.type == "video" and clip.linked_group_id:
        return [
            linked
            for track in state.tracks
            for linked in track.clips
            if linked.linked_group_id == clip.linked_group_id and linked.type == "audio"
        ]
    return [clip]


def _move_targets(state: ProjectState, clip_id: str) -> list[Clip]:
    clip = state.find_clip(clip_id)
    if clip is None:
        return []
    if clip.type == "video" and clip.linked_group_id:
        return [
            linked
            for track in state.tracks
            for linked in track.clips
            if linked.linked_group_id == clip.linked_group_id
        ]
    return [clip]


def _trim_targets(state: ProjectState, clip_id: str) -> list[Clip]:
    return _move_targets(state, clip_id)


def _clip_paths(clips: Iterable[Clip]) -> set[Path]:
    paths: set[Path] = set()
    for clip in clips:
        paths.add(clip.source_path)
        if clip.waveform_path is not None:
            paths.add(clip.waveform_path)
        paths.update(clip.thumbnails)
    return paths


def _track_signature(state: ProjectState) -> tuple:
    return tuple(
        (
            track.id,
            tuple(
                (
                    clip.id,
                    clip.source_path,
                    clip.source_in,
                    clip.source_out,
                    clip.timeline_start,
                    clip.linked_group_id,
                )
                for clip in track.clips
            ),
        )
        for track in state.tracks
    )


def _source_snapshot(state: ProjectState) -> dict[str, object]:
    return {
        "original_video_path": state.original_video_path,
        "working_video_path": state.working_video_path,
        "source_video_path": state.source_video_path,
        "source_has_audio": state.source_has_audio,
        "source_duration": state.source_duration,
        "source_width": state.source_width,
        "source_height": state.source_height,
        "working_duration": state.working_duration,
        "working_width": state.working_width,
        "working_height": state.working_height,
        "is_cropped": state.is_cropped,
        "crop_history": copy.deepcopy(state.crop_history),
        "crop_enabled": state.crop_enabled,
        "crop_x": state.crop_x,
        "crop_y": state.crop_y,
        "crop_width": state.crop_width,
        "crop_height": state.crop_height,
        "media_pool": list(state.media_pool),
        "original_audio_enabled": state.original_audio_enabled,
        "track_state": {
            track.id: (track.muted, track.locked)
            for track in state.tracks
        },
        "track_clips": {track.id: copy.deepcopy(track.clips) for track in state.tracks},
        "selected_clip_ids": list(state.selected_clip_ids),
        "selected_clip_id": state.selected_clip_id,
        "playhead_time": state.playhead_time,
    }


def _restore_source_snapshot(state: ProjectState, snapshot: dict[str, object]) -> None:
    state.original_video_path = snapshot["original_video_path"]  # type: ignore[assignment]
    state.working_video_path = snapshot["working_video_path"]  # type: ignore[assignment]
    state.source_video_path = snapshot["source_video_path"]  # type: ignore[assignment]
    state.source_has_audio = snapshot["source_has_audio"]  # type: ignore[assignment]
    state.source_duration = snapshot["source_duration"]  # type: ignore[assignment]
    state.source_width = snapshot["source_width"]  # type: ignore[assignment]
    state.source_height = snapshot["source_height"]  # type: ignore[assignment]
    state.working_duration = snapshot["working_duration"]  # type: ignore[assignment]
    state.working_width = snapshot["working_width"]  # type: ignore[assignment]
    state.working_height = snapshot["working_height"]  # type: ignore[assignment]
    state.is_cropped = snapshot["is_cropped"]  # type: ignore[assignment]
    state.crop_history = copy.deepcopy(snapshot["crop_history"])  # type: ignore[assignment]
    state.crop_enabled = snapshot["crop_enabled"]  # type: ignore[assignment]
    state.crop_x = snapshot["crop_x"]  # type: ignore[assignment]
    state.crop_y = snapshot["crop_y"]  # type: ignore[assignment]
    state.crop_width = snapshot["crop_width"]  # type: ignore[assignment]
    state.crop_height = snapshot["crop_height"]  # type: ignore[assignment]
    state.media_pool = list(snapshot["media_pool"])  # type: ignore[arg-type]
    state.original_audio_enabled = snapshot["original_audio_enabled"]  # type: ignore[assignment]
    track_state = snapshot["track_state"]
    if isinstance(track_state, dict):
        for track in state.tracks:
            if track.id in track_state:
                muted, locked = track_state[track.id]
                track.muted = muted
                track.locked = locked
    track_clips = snapshot["track_clips"]
    if isinstance(track_clips, dict):
        for track in state.tracks:
            if track.id in track_clips:
                track.clips = copy.deepcopy(track_clips[track.id])
    state.selected_clip_ids = list(snapshot["selected_clip_ids"])  # type: ignore[arg-type]
    state.selected_clip_id = snapshot["selected_clip_id"]  # type: ignore[assignment]
    state.playhead_time = snapshot["playhead_time"]  # type: ignore[assignment]
    state.refresh_duration()


def _snapshot_paths(snapshot: dict[str, object]) -> set[Path]:
    paths = {path for path in snapshot.get("media_pool", []) if isinstance(path, Path)}
    crop_history = snapshot.get("crop_history", [])
    if isinstance(crop_history, list):
        for record in crop_history:
            if isinstance(record, CropRecord):
                paths.add(record.source_path)
                paths.add(record.output_path)
    track_clips = snapshot.get("track_clips", {})
    if isinstance(track_clips, dict):
        paths.update(
            _clip_paths(
                clip
                for clips in track_clips.values()
                if isinstance(clips, list)
                for clip in clips
                if isinstance(clip, Clip)
            )
        )
    return paths
