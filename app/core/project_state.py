from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


@dataclass
class AudioClip:
    path: Path
    filename: str
    timeline_start: float
    duration: float
    muted: bool = False
    loop: bool = True
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class VideoSegment:
    start: float
    end: float
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class ProjectState:
    source_video_path: Path | None = None
    source_duration: float = 0.0
    source_width: int = 0
    source_height: int = 0
    source_has_audio: bool = False
    trim_start: float = 0.0
    trim_end: float = 0.0
    crop_enabled: bool = False
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_width: float = 1.0
    crop_height: float = 1.0
    output_aspect_mode: str = "original"
    output_width: int | None = None
    output_height: int | None = None
    video_segments: list[VideoSegment] = field(default_factory=list)
    original_audio_enabled: bool = True
    audio_clips: list[AudioClip] = field(default_factory=list)
    selected_clip_id: str | None = None
    playhead_time: float = 0.0

    def load_source(
        self,
        path: Path,
        duration: float,
        width: int,
        height: int,
        has_audio: bool,
    ) -> None:
        self.source_video_path = path
        self.source_duration = max(duration, 0.0)
        self.source_width = max(width, 0)
        self.source_height = max(height, 0)
        self.source_has_audio = has_audio
        self.trim_start = 0.0
        self.trim_end = self.source_duration
        self.crop_enabled = False
        self.crop_x = 0.0
        self.crop_y = 0.0
        self.crop_width = 1.0
        self.crop_height = 1.0
        self.output_aspect_mode = "original"
        self.output_width = None
        self.output_height = None
        self.video_segments = [VideoSegment(0.0, self.source_duration)]
        self.original_audio_enabled = has_audio
        self.audio_clips.clear()
        self.selected_clip_id = "video"
        self.playhead_time = 0.0

    def selected_audio_clip(self) -> AudioClip | None:
        return next((clip for clip in self.audio_clips if clip.id == self.selected_clip_id), None)

    def active_audio_clip(self) -> AudioClip | None:
        return next((clip for clip in self.audio_clips if not clip.muted), None)

    def split_video_at(self, seconds: float) -> bool:
        if self.source_duration <= 0:
            return False
        seconds = min(max(seconds, 0.0), self.source_duration)
        for index, segment in enumerate(self.video_segments):
            if segment.start + 0.05 < seconds < segment.end - 0.05:
                self.video_segments[index : index + 1] = [
                    VideoSegment(segment.start, seconds),
                    VideoSegment(seconds, segment.end),
                ]
                self.selected_clip_id = "video"
                return True
        return False

    def active_video_segments(self) -> list[VideoSegment]:
        if self.video_segments:
            return [segment for segment in self.video_segments if segment.end > segment.start]
        if self.trim_end > self.trim_start:
            return [VideoSegment(self.trim_start, self.trim_end)]
        return []

    def selected_clip_is_audio(self) -> bool:
        return self.selected_audio_clip() is not None

    def delete_selected_clip(self) -> bool:
        if self.selected_clip_id is None:
            return False
        before = len(self.audio_clips)
        self.audio_clips = [clip for clip in self.audio_clips if clip.id != self.selected_clip_id]
        deleted = len(self.audio_clips) != before
        if deleted:
            self.selected_clip_id = "video"
        return deleted

    def toggle_selected_audio_mute(self) -> bool:
        clip = self.selected_audio_clip()
        if clip is None:
            return False
        clip.muted = not clip.muted
        return True
