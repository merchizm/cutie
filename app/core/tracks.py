from __future__ import annotations

from typing import Protocol

from app.core.track_ids import MUSIC_TRACK_ID, ORIGINAL_AUDIO_TRACK_ID, VIDEO_TRACK_ID


class TrackLike(Protocol):
    id: str


class TrackFactory(Protocol):
    def __call__(self, type: str, name: str, **kwargs: object) -> TrackLike: ...


def default_tracks(track_cls: TrackFactory, has_audio: bool) -> list[TrackLike]:
    return [
        track_cls("video", "Video", id=VIDEO_TRACK_ID),
        track_cls("audio", "Original audio", id=ORIGINAL_AUDIO_TRACK_ID, muted=not has_audio),
        track_cls("audio", "Music", id=MUSIC_TRACK_ID),
    ]


def ensure_default_tracks(tracks: list[TrackLike], track_cls: TrackFactory) -> bool:
    existing = {track.id for track in tracks}
    changed = False
    if VIDEO_TRACK_ID not in existing:
        tracks.append(track_cls("video", "Video", id=VIDEO_TRACK_ID))
        changed = True
    if ORIGINAL_AUDIO_TRACK_ID not in existing:
        tracks.append(track_cls("audio", "Original audio", id=ORIGINAL_AUDIO_TRACK_ID, muted=True))
        changed = True
    if MUSIC_TRACK_ID not in existing:
        tracks.append(track_cls("audio", "Music", id=MUSIC_TRACK_ID))
        changed = True
    return changed
