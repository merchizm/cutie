from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app.core.track_ids import MUSIC_TRACK_ID, ORIGINAL_AUDIO_TRACK_ID, PRIMARY_VIDEO_CLIP_ID, VIDEO_TRACK_ID
from app.core.tracks import default_tracks, ensure_default_tracks


def _id() -> str:
    return uuid4().hex


@dataclass
class Clip:
    type: str
    source_path: Path
    source_in: float
    source_out: float
    timeline_start: float
    label: str
    track_id: str
    linked_group_id: str | None = None
    selected: bool = False
    muted: bool = False
    color: str = ""
    id: str = field(default_factory=_id)
    loop: bool = False
    waveform_path: Path | None = None
    thumbnails: list[Path] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(self.source_out - self.source_in, 0.0)

    @duration.setter
    def duration(self, value: float) -> None:
        self.source_out = self.source_in + max(value, 0.0)

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration

    @timeline_end.setter
    def timeline_end(self, value: float) -> None:
        self.source_out = self.source_in + max(value - self.timeline_start, 0.0)

    @property
    def start(self) -> float:
        return self.timeline_start

    @start.setter
    def start(self, value: float) -> None:
        self.timeline_start = max(value, 0.0)

    @property
    def end(self) -> float:
        return self.timeline_end

    @end.setter
    def end(self, value: float) -> None:
        self.timeline_end = value

    @property
    def path(self) -> Path:
        return self.source_path

    @property
    def filename(self) -> str:
        return self.label


@dataclass
class Track:
    type: str
    name: str
    id: str = field(default_factory=_id)
    clips: list[Clip] = field(default_factory=list)
    muted: bool = False
    locked: bool = False


@dataclass
class CropRecord:
    source_path: Path
    output_path: Path
    x: float
    y: float
    width: float
    height: float


@dataclass
class ProjectState:
    original_video_path: Path | None = None
    working_video_path: Path | None = None
    source_video_path: Path | None = None
    source_duration: float = 0.0
    source_width: int = 0
    source_height: int = 0
    source_has_audio: bool = False
    working_width: int = 0
    working_height: int = 0
    working_duration: float = 0.0
    is_cropped: bool = False
    crop_history: list[CropRecord] = field(default_factory=list)
    crop_enabled: bool = False
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_width: float = 1.0
    crop_height: float = 1.0
    output_aspect_mode: str = "original"
    output_width: int | None = None
    output_height: int | None = None
    media_pool: list[Path] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    selected_clip_ids: list[str] = field(default_factory=list)
    selected_clip_id: str | None = None
    playhead_time: float = 0.0
    duration: float = 0.0
    original_audio_enabled: bool = True
    snap_threshold: float = 0.15
    _track_lookup: dict[str, Track] = field(default_factory=dict, init=False, repr=False)
    _track_lookup_ids: tuple[str, ...] = field(default_factory=tuple, init=False, repr=False)

    def __deepcopy__(self, memo: dict[int, object]) -> ProjectState:
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone
        for key, value in self.__dict__.items():
            if key in {"_track_lookup", "_track_lookup_ids"}:
                continue
            setattr(clone, key, copy.deepcopy(value, memo))
        clone._track_lookup = {}
        clone._track_lookup_ids = ()
        return clone

    def load_source(
        self,
        path: Path,
        duration: float,
        width: int,
        height: int,
        has_audio: bool,
    ) -> None:
        self.original_video_path = path
        self.working_video_path = path
        self.source_video_path = path
        self.source_duration = max(duration, 0.0)
        self.source_width = max(width, 0)
        self.source_height = max(height, 0)
        self.source_has_audio = has_audio
        self.working_width = self.source_width
        self.working_height = self.source_height
        self.working_duration = self.source_duration
        self.is_cropped = False
        self.crop_history.clear()
        self.crop_enabled = False
        self.crop_x = 0.0
        self.crop_y = 0.0
        self.crop_width = 1.0
        self.crop_height = 1.0
        self.output_aspect_mode = "original"
        self.output_width = None
        self.output_height = None
        self.media_pool = [path]
        self.playhead_time = 0.0
        self.original_audio_enabled = has_audio
        self.tracks = default_tracks(Track, has_audio)
        self._invalidate_track_lookup()
        group_id = _id()
        self.video_track.clips.append(
            Clip(
                type="video",
                source_path=path,
                source_in=0.0,
                source_out=self.source_duration,
                timeline_start=0.0,
                label=path.name,
                track_id=VIDEO_TRACK_ID,
                linked_group_id=group_id if has_audio else None,
                selected=True,
                color="accent",
                id=PRIMARY_VIDEO_CLIP_ID,
            )
        )
        if has_audio:
            self.original_audio_track.clips.append(
                Clip(
                    type="audio",
                    source_path=path,
                    source_in=0.0,
                    source_out=self.source_duration,
                    timeline_start=0.0,
                    label="Original audio",
                    track_id=ORIGINAL_AUDIO_TRACK_ID,
                    linked_group_id=group_id,
                    color="original",
                )
            )
        self.selected_clip_ids = [PRIMARY_VIDEO_CLIP_ID]
        self.selected_clip_id = PRIMARY_VIDEO_CLIP_ID
        self._refresh_duration()

    @property
    def video_track(self) -> Track:
        self._ensure_tracks()
        return self._track(VIDEO_TRACK_ID)

    @property
    def original_audio_track(self) -> Track:
        self._ensure_tracks()
        return self._track(ORIGINAL_AUDIO_TRACK_ID)

    @property
    def music_track(self) -> Track:
        self._ensure_tracks()
        return self._track(MUSIC_TRACK_ID)

    @property
    def video_segments(self) -> list[Clip]:
        return self.video_track.clips

    @video_segments.setter
    def video_segments(self, clips: list[Clip]) -> None:
        self.video_track.clips = clips
        self._refresh_duration()

    @property
    def audio_clips(self) -> list[Clip]:
        return self.music_track.clips

    @audio_clips.setter
    def audio_clips(self, clips: list[Clip]) -> None:
        self.music_track.clips = clips
        self._refresh_duration()

    @property
    def trim_start(self) -> float:
        clips = self.video_track.clips if self.tracks else []
        return min((clip.timeline_start for clip in clips), default=0.0)

    @trim_start.setter
    def trim_start(self, value: float) -> None:
        self._trim_video_range(value, self.trim_end)

    @property
    def trim_end(self) -> float:
        clips = self.video_track.clips if self.tracks else []
        return max((clip.timeline_end for clip in clips), default=self.duration)

    @trim_end.setter
    def trim_end(self, value: float) -> None:
        self._trim_video_range(self.trim_start, value)

    def trim_video_range(self, start: float, end: float) -> None:
        self._trim_video_range(start, end)

    def set_pending_crop(self, x: float, y: float, width: float, height: float) -> None:
        self.crop_enabled = True
        self.crop_x = x
        self.crop_y = y
        self.crop_width = width
        self.crop_height = height

    def clear_pending_crop(self) -> None:
        self.crop_enabled = False
        self.crop_x = 0.0
        self.crop_y = 0.0
        self.crop_width = 1.0
        self.crop_height = 1.0

    def apply_working_video(
        self,
        path: Path,
        duration: float,
        width: int,
        height: int,
        crop_record: CropRecord | None = None,
    ) -> None:
        self.working_video_path = path
        self.source_video_path = path
        self.source_duration = max(duration, 0.0)
        self.source_width = max(width, 0)
        self.source_height = max(height, 0)
        self.working_duration = self.source_duration
        self.working_width = self.source_width
        self.working_height = self.source_height
        self.is_cropped = True
        if crop_record is not None:
            self.crop_history.append(crop_record)
        self.crop_enabled = False
        self.crop_x = 0.0
        self.crop_y = 0.0
        self.crop_width = 1.0
        self.crop_height = 1.0
        self.media_pool.append(path)
        self.load_timeline_from_working_source()

    def load_timeline_from_working_source(self) -> None:
        if self.working_video_path is None:
            return
        has_audio = self.source_has_audio
        group_id = _id()
        self.video_track.clips = [
            Clip(
                type="video",
                source_path=self.working_video_path,
                source_in=0.0,
                source_out=self.working_duration,
                timeline_start=0.0,
                label=self.working_video_path.name,
                track_id=VIDEO_TRACK_ID,
                linked_group_id=group_id if has_audio else None,
                selected=True,
                color="accent",
                id=PRIMARY_VIDEO_CLIP_ID,
            )
        ]
        self.original_audio_track.clips = []
        if has_audio:
            self.original_audio_track.clips.append(
                Clip(
                    type="audio",
                    source_path=self.working_video_path,
                    source_in=0.0,
                    source_out=self.working_duration,
                    timeline_start=0.0,
                    label="Original audio",
                    track_id=ORIGINAL_AUDIO_TRACK_ID,
                    linked_group_id=group_id,
                    color="original",
                    muted=not self.original_audio_enabled,
                )
            )
        self.selected_clip_ids = [PRIMARY_VIDEO_CLIP_ID]
        self.selected_clip_id = PRIMARY_VIDEO_CLIP_ID
        self.playhead_time = 0.0
        self._refresh_duration()

    def add_audio_clip(self, path: Path, duration: float, timeline_start: float = 0.0) -> Clip:
        self._ensure_tracks()
        clip = Clip(
            type="audio",
            source_path=path,
            source_in=0.0,
            source_out=max(duration, 0.0),
            timeline_start=max(timeline_start, 0.0),
            label=path.name,
            track_id=MUSIC_TRACK_ID,
            color="music",
            loop=True,
        )
        clip.timeline_start = self._non_overlapping_start(self.music_track, clip, clip.timeline_start)
        self.music_track.clips.append(clip)
        self.media_pool.append(path)
        self.select_clip(clip.id)
        self.original_audio_enabled = False
        self.original_audio_track.muted = True
        self._refresh_duration()
        return clip

    def selected_audio_clip(self) -> Clip | None:
        clip = self.selected_clip()
        return clip if clip and clip.type == "audio" and clip.track_id == MUSIC_TRACK_ID else None

    def active_audio_clip(self) -> Clip | None:
        return next((clip for clip in self.music_track.clips if not clip.muted), None)

    def active_audio_clips(self) -> list[Clip]:
        return [clip for clip in self.music_track.clips if not clip.muted]

    def selected_clip(self) -> Clip | None:
        if self.selected_clip_id is None:
            return None
        return self.find_clip(self.selected_clip_id)

    def find_clip(self, clip_id: str) -> Clip | None:
        for track in self.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
        return None

    def select_clip(self, clip_id: str) -> None:
        self.selected_clip_ids = [clip_id]
        self.selected_clip_id = clip_id
        for track in self.tracks:
            for clip in track.clips:
                clip.selected = clip.id == clip_id

    def selected_clip_is_audio(self) -> bool:
        return self.selected_audio_clip() is not None

    def split_at(self, seconds: float) -> bool:
        if self.selected_clip_id and self.find_clip(self.selected_clip_id):
            clip_ids = list(self.selected_clip_ids)
        else:
            clip_ids = [
                clip.id
                for track in self.tracks
                if not track.locked
                for clip in track.clips
                if clip.timeline_start + 0.05 < seconds < clip.timeline_end - 0.05
            ]
        changed = False
        selection_after_split: str | None = None
        for clip_id in clip_ids:
            clip = self.find_clip(clip_id)
            if clip is None or not (clip.timeline_start + 0.05 < seconds < clip.timeline_end - 0.05):
                continue
            if clip.type == "video" and clip.linked_group_id:
                linked_group_id = clip.linked_group_id
                left_group_id = _id()
                right_group_id = _id()
                left_id = self._split_clip(clip, seconds, left_group_id, right_group_id)
                if left_id is not None:
                    selection_after_split = left_id
                    changed = True
                for linked in self._linked_clips(linked_group_id):
                    if linked.type == "audio" and linked.id != clip.id:
                        changed = self._split_clip(linked, seconds, left_group_id, right_group_id) is not None or changed
            else:
                left_id = self._split_clip(clip, seconds)
                if left_id is not None:
                    selection_after_split = left_id
                    changed = True
        if changed:
            if selection_after_split is not None:
                self.select_clip(selection_after_split)
            self._refresh_duration()
        return changed

    def split_video_at(self, seconds: float) -> bool:
        return self.split_at(seconds)

    def delete_selected_clip(self) -> bool:
        if not self.selected_clip_ids:
            return False
        removed = False
        selected = set(self.selected_clip_ids)
        linked_groups = {
            clip.linked_group_id
            for clip in (self.find_clip(clip_id) for clip_id in selected)
            if clip is not None and clip.type == "video" and clip.linked_group_id
        }
        for track in self.tracks:
            before = len(track.clips)
            track.clips = [
                clip
                for clip in track.clips
                if clip.id not in selected and clip.linked_group_id not in linked_groups
            ]
            removed = removed or len(track.clips) != before
        if removed:
            self.selected_clip_ids = []
            self.selected_clip_id = None
            self._ripple_video_gaps()
            self._refresh_duration()
        return removed

    def toggle_selected_audio_mute(self) -> bool:
        clip = self.selected_clip()
        if clip is None:
            return False
        changed = False
        targets = [clip]
        if clip.type == "video" and clip.linked_group_id:
            targets = [linked for linked in self._linked_clips(clip.linked_group_id) if linked.type == "audio"]
        for target in targets:
            target.muted = not target.muted
            changed = True
        return changed

    def move_clip(self, clip_id: str, timeline_start: float) -> bool:
        clip = self.find_clip(clip_id)
        if clip is None:
            return False
        track = self._track(clip.track_id)
        timeline_start = self._snap_start(clip, max(timeline_start, 0.0))
        new_start = self._non_overlapping_start(track, clip, timeline_start)
        delta = new_start - clip.timeline_start
        if abs(delta) < 0.001:
            return False
        move_group = [clip]
        if clip.type == "video" and clip.linked_group_id:
            move_group = self._linked_clips(clip.linked_group_id)
        for item in move_group:
            item.timeline_start = max(item.timeline_start + delta, 0.0)
        self._refresh_duration()
        return True

    def trim_clip_left(self, clip_id: str, timeline_start: float) -> bool:
        clip = self.find_clip(clip_id)
        if clip is None:
            return False
        timeline_start = max(timeline_start, 0.0)
        if not (clip.timeline_start < timeline_start < clip.timeline_end - 0.05):
            return False
        delta = timeline_start - clip.timeline_start
        for item in self._trim_group(clip):
            item.timeline_start += delta
            item.source_in += delta
        self._refresh_duration()
        return True

    def trim_clip_right(self, clip_id: str, timeline_end: float) -> bool:
        clip = self.find_clip(clip_id)
        if clip is None:
            return False
        if not (clip.timeline_start + 0.05 < timeline_end < clip.timeline_end):
            return False
        delta = clip.timeline_end - timeline_end
        for item in self._trim_group(clip):
            item.source_out -= delta
        self._refresh_duration()
        return True

    def active_video_segments(self) -> list[Clip]:
        return [clip for clip in self.video_track.clips if clip.duration > 0]

    def refresh_duration(self) -> None:
        self._refresh_duration()

    def _split_clip(
        self,
        clip: Clip,
        seconds: float,
        left_group_id: str | None = None,
        right_group_id: str | None = None,
    ) -> str | None:
        track = self._track(clip.track_id)
        index = track.clips.index(clip)
        offset = seconds - clip.timeline_start
        if offset <= 0.05 or clip.duration - offset <= 0.05:
            return None
        left = Clip(
            type=clip.type,
            source_path=clip.source_path,
            source_in=clip.source_in,
            source_out=clip.source_in + offset,
            timeline_start=clip.timeline_start,
            label=clip.label,
            track_id=clip.track_id,
            linked_group_id=left_group_id if left_group_id is not None else clip.linked_group_id,
            selected=clip.selected,
            muted=clip.muted,
            color=clip.color,
            loop=clip.loop,
            waveform_path=clip.waveform_path,
            thumbnails=list(clip.thumbnails),
        )
        right = Clip(
            type=clip.type,
            source_path=clip.source_path,
            source_in=clip.source_in + offset,
            source_out=clip.source_out,
            timeline_start=seconds,
            label=clip.label,
            track_id=clip.track_id,
            linked_group_id=right_group_id if right_group_id is not None else clip.linked_group_id,
            selected=False,
            muted=clip.muted,
            color=clip.color,
            loop=clip.loop,
            waveform_path=clip.waveform_path,
            thumbnails=list(clip.thumbnails),
        )
        track.clips[index : index + 1] = [left, right]
        return left.id

    def _trim_video_range(self, start: float, end: float) -> None:
        if not self.tracks or end <= start:
            return
        for clip in self.video_track.clips:
            if clip.timeline_start < start:
                delta = start - clip.timeline_start
                for item in self._trim_group(clip):
                    item.timeline_start = max(item.timeline_start + delta, start)
                    item.source_in = min(item.source_in + delta, item.source_out)
            if clip.timeline_end > end:
                delta = clip.timeline_end - end
                for item in self._trim_group(clip):
                    item.source_out = max(item.source_in, item.source_out - delta)
        self.video_track.clips = [clip for clip in self.video_track.clips if clip.duration > 0]
        first_start = min((clip.timeline_start for clip in self.video_track.clips), default=0.0)
        if first_start > 0:
            linked_groups = {clip.linked_group_id for clip in self.video_track.clips if clip.linked_group_id}
            for track in self.tracks:
                for clip in track.clips:
                    if clip.track_id == VIDEO_TRACK_ID or clip.linked_group_id in linked_groups:
                        clip.timeline_start = max(clip.timeline_start - first_start, 0.0)
        self._refresh_duration()

    def _track(self, track_id: str) -> Track:
        current_ids = tuple(track.id for track in self.tracks)
        if current_ids != self._track_lookup_ids:
            self._track_lookup = {track.id: track for track in self.tracks}
            self._track_lookup_ids = current_ids
        return self._track_lookup[track_id]

    def _ensure_tracks(self) -> None:
        if ensure_default_tracks(self.tracks, Track):
            self._invalidate_track_lookup()

    def _invalidate_track_lookup(self) -> None:
        self._track_lookup = {}
        self._track_lookup_ids = ()

    def _linked_clips(self, linked_group_id: str) -> list[Clip]:
        return [clip for track in self.tracks for clip in track.clips if clip.linked_group_id == linked_group_id]

    def _trim_group(self, clip: Clip) -> list[Clip]:
        if clip.type == "video" and clip.linked_group_id:
            return self._linked_clips(clip.linked_group_id)
        return [clip]

    def _non_overlapping_start(self, track: Track, clip: Clip, desired_start: float) -> float:
        desired_start = max(desired_start, 0.0)
        desired_end = desired_start + clip.duration
        for other in sorted(track.clips, key=lambda item: item.timeline_start):
            if other.id == clip.id:
                continue
            if desired_start < other.timeline_end and desired_end > other.timeline_start:
                if desired_start < other.timeline_start:
                    desired_start = max(0.0, other.timeline_start - clip.duration)
                else:
                    desired_start = other.timeline_end
                desired_end = desired_start + clip.duration
        return desired_start

    def _snap_start(self, clip: Clip, desired_start: float) -> float:
        desired_start = max(desired_start, 0.0)
        desired_end = desired_start + clip.duration
        candidates = [0.0, self.playhead_time]
        for track in self.tracks:
            for other in track.clips:
                if other.id == clip.id:
                    continue
                candidates.extend([other.timeline_start, other.timeline_end])
                candidates.extend([other.timeline_start - clip.duration, other.timeline_end - clip.duration])
        if clip.linked_group_id:
            for linked in self._linked_clips(clip.linked_group_id):
                if linked.id != clip.id:
                    candidates.extend([linked.timeline_start, linked.timeline_end - clip.duration])
        for candidate in candidates:
            if candidate >= 0 and abs(desired_start - candidate) <= self.snap_threshold:
                return candidate
            candidate_end_start = candidate - clip.duration
            if candidate_end_start >= 0 and abs(desired_end - candidate) <= self.snap_threshold:
                return candidate_end_start
        return desired_start

    def _ripple_video_gaps(self) -> None:
        current = 0.0
        ordered = sorted(self.video_track.clips, key=lambda item: item.timeline_start)
        self.video_track.clips = ordered
        for clip in ordered:
            delta = current - clip.timeline_start
            clip.timeline_start = current
            if clip.linked_group_id:
                for linked in self._linked_clips(clip.linked_group_id):
                    if linked.id != clip.id:
                        linked.timeline_start = max(linked.timeline_start + delta, 0.0)
            current = clip.timeline_end

    def _refresh_duration(self) -> None:
        self.duration = max((clip.timeline_end for track in self.tracks for clip in track.clips), default=0.0)


AudioClip = Clip
VideoSegment = Clip
