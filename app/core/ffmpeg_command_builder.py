from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.project_state import ProjectState
from app.utils.numbers import even_floor
from app.utils.timecode import seconds_to_timecode


@dataclass(frozen=True)
class TimelineAudioClip:
    path: Path
    timeline_start: float
    loop: bool = True


@dataclass(frozen=True)
class ExportSettings:
    input_path: Path
    output_path: Path
    trim_start: float = 0.0
    trim_end: float | None = None
    width: int | None = None
    audio_mode: str = "keep"
    replacement_audio_path: Path | None = None
    replacement_audio_clips: tuple[TimelineAudioClip, ...] = ()
    format_name: str = "mp4"
    video_crf: int | None = None
    crop: tuple[int, int, int, int] | None = None
    output_width: int | None = None
    output_height: int | None = None
    loop_audio: bool = True
    video_segments: tuple[tuple[float, float], ...] = ()
    video_segment_timeline_starts: tuple[float, ...] = ()
    video_segment_audio_muted: tuple[bool, ...] = ()
    audio_timeline_start: float = 0.0
    source_width: int = 1280
    source_height: int = 720


def build_ffmpeg_command(settings: ExportSettings) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-y", "-nostdin"]
    has_segments = _needs_complex_video_filter(settings)
    video_filter = None

    if settings.trim_start > 0 and not has_segments:
        command.extend(["-ss", seconds_to_timecode(settings.trim_start)])
    if settings.trim_end is not None and settings.trim_end > settings.trim_start and not has_segments:
        command.extend(["-to", seconds_to_timecode(settings.trim_end)])

    command.extend(["-i", str(settings.input_path)])

    if settings.audio_mode == "replace":
        replacement_clips = settings.replacement_audio_clips
        if not replacement_clips and settings.replacement_audio_path is not None:
            replacement_clips = (
                TimelineAudioClip(settings.replacement_audio_path, settings.audio_timeline_start, settings.loop_audio),
            )
        if not replacement_clips:
            raise ValueError("replacement_audio_path is required when audio_mode is replace")
        for clip in replacement_clips:
            if clip.timeline_start > 0:
                command.extend(["-itsoffset", seconds_to_timecode(clip.timeline_start)])
            if clip.loop:
                command.extend(["-stream_loop", "-1"])
            command.extend(["-i", str(clip.path)])

    if has_segments:
        command.extend(["-filter_complex", _build_segment_filter(settings)])
    elif settings.audio_mode == "replace" and len(settings.replacement_audio_clips) > 1:
        command.extend(["-filter_complex", _build_audio_mix_filter(len(settings.replacement_audio_clips))])
    else:
        video_filter = _build_video_filter(settings)
        if video_filter:
            command.extend(["-vf", video_filter])

    if _can_stream_copy(settings, has_segments, video_filter):
        command.extend(_format_stream_copy_args(settings.audio_mode))
        command.extend(["-progress", "pipe:1", str(settings.output_path)])
        return command

    if has_segments:
        command.extend(["-map", "[vout]"])
        if settings.audio_mode == "keep":
            command.extend(["-map", "[aout]"])
        elif settings.audio_mode == "replace":
            if len(settings.replacement_audio_clips) > 1:
                command.extend(["-map", "[mixout]", "-shortest"])
            else:
                command.extend(["-map", "1:a:0", "-shortest"])
    elif settings.audio_mode == "replace" and len(settings.replacement_audio_clips) > 1:
        command.extend(["-map", "0:v:0", "-map", "[mixout]", "-shortest"])
    command.extend(_format_video_args(settings.format_name, settings.video_crf))

    if settings.audio_mode == "mute":
        command.append("-an")
    elif (
        settings.audio_mode == "replace"
        and not has_segments
        and len(settings.replacement_audio_clips) <= 1
    ):
        command.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest"])
        command.extend(_format_audio_args(settings.format_name, replacement=True))
    elif settings.audio_mode == "replace":
        command.extend(_format_audio_args(settings.format_name, replacement=True))
    else:
        command.extend(_format_audio_args(settings.format_name, replacement=False))

    command.extend(["-progress", "pipe:1", str(settings.output_path)])
    return command


def export_settings_from_project(
    state: ProjectState,
    output_path: Path,
    format_name: str,
    video_crf: int,
    output_width: int | None,
    output_height: int | None,
    audio_mode: str,
    loop_timeline_audio: bool,
) -> ExportSettings:
    if state.source_video_path is None:
        raise ValueError("No source video loaded")

    audio_clips = tuple(
        TimelineAudioClip(clip.path, clip.timeline_start, loop_timeline_audio and clip.loop)
        for clip in state.active_audio_clips()
    )
    audio_clip = state.active_audio_clip()
    replacement_audio = audio_clip.path if audio_mode == "timeline" and audio_clip else None
    resolved_audio_mode = "replace" if audio_mode == "timeline" and audio_clips else audio_mode
    if resolved_audio_mode == "timeline":
        resolved_audio_mode = "mute"
    if resolved_audio_mode == "keep" and (not state.original_audio_enabled or state.original_audio_track.muted):
        resolved_audio_mode = "mute"
    video_clips = sorted(state.active_video_segments(), key=lambda item: item.timeline_start)
    if not video_clips:
        raise ValueError("No video clips remain on the timeline")
    video_segments = tuple((clip.source_in, clip.source_out) for clip in video_clips)
    video_segment_timeline_starts = tuple(clip.timeline_start for clip in video_clips)
    segment_audio_muted = tuple(_linked_audio_muted(state, clip) for clip in video_clips)
    if resolved_audio_mode == "keep" and len(video_clips) == 1 and segment_audio_muted[0]:
        resolved_audio_mode = "mute"

    return ExportSettings(
        input_path=state.working_video_path or state.source_video_path,
        output_path=output_path,
        trim_start=video_segments[0][0] if len(video_segments) == 1 else state.trim_start,
        trim_end=video_segments[0][1] if len(video_segments) == 1 else state.trim_end,
        audio_mode=resolved_audio_mode,
        replacement_audio_path=replacement_audio,
        replacement_audio_clips=audio_clips if resolved_audio_mode == "replace" else (),
        format_name=format_name,
        video_crf=video_crf,
        crop=_source_crop_pixels(state),
        output_width=output_width,
        output_height=output_height,
        loop_audio=loop_timeline_audio and (audio_clip.loop if audio_clip else True),
        video_segments=video_segments,
        video_segment_timeline_starts=video_segment_timeline_starts,
        video_segment_audio_muted=segment_audio_muted,
        audio_timeline_start=audio_clip.timeline_start if audio_clip else 0.0,
        source_width=state.working_width or state.source_width or 1280,
        source_height=state.working_height or state.source_height or 720,
    )


def _linked_audio_muted(state: ProjectState, video_clip: object) -> bool:
    linked_group_id = getattr(video_clip, "linked_group_id", None)
    if linked_group_id is None:
        return not state.original_audio_enabled
    linked_audio = [
        clip
        for clip in state.original_audio_track.clips
        if clip.linked_group_id == linked_group_id and clip.type == "audio"
    ]
    return not linked_audio or any(clip.muted for clip in linked_audio)


def _format_video_args(format_name: str, video_crf: int | None) -> list[str]:
    if format_name == "webm":
        return ["-c:v", "libvpx-vp9", "-crf", str(video_crf or 32), "-b:v", "0"]
    return ["-c:v", "libx264", "-crf", str(video_crf or 23), "-preset", "medium"]


def _format_audio_args(format_name: str, replacement: bool) -> list[str]:
    bitrate = "192k" if replacement else "128k"
    if format_name == "webm":
        return ["-c:a", "libopus", "-b:a", bitrate]
    return ["-c:a", "aac", "-b:a", bitrate]


def _format_stream_copy_args(audio_mode: str) -> list[str]:
    if audio_mode == "mute":
        return ["-c:v", "copy", "-an"]
    return ["-c", "copy"]


def _can_stream_copy(settings: ExportSettings, has_segments: bool, video_filter: str | None) -> bool:
    if has_segments or video_filter:
        return False
    if settings.audio_mode not in {"keep", "mute"}:
        return False
    if settings.replacement_audio_path is not None or settings.replacement_audio_clips:
        return False
    if settings.format_name == "mkv":
        return True
    return settings.input_path.suffix.lower().lstrip(".") == settings.format_name


def _build_audio_mix_filter(input_count: int) -> str:
    labels = "".join(f"[{index}:a:0]" for index in range(1, input_count + 1))
    return f"{labels}amix=inputs={input_count}:duration=longest:dropout_transition=0[mixout]"


def _needs_complex_video_filter(settings: ExportSettings) -> bool:
    if len(settings.video_segments) > 1:
        return True
    if settings.video_segments and settings.video_segment_timeline_starts:
        return settings.video_segment_timeline_starts[0] > 0.001
    return False


def _build_video_filter(settings: ExportSettings) -> str | None:
    filters: list[str] = []
    if settings.crop is not None:
        x, y, width, height = settings.crop
        filters.append(f"crop={width}:{height}:{x}:{y}")
    if settings.output_width and settings.output_height:
        filters.append(f"scale={settings.output_width}:{settings.output_height}")
    elif settings.output_width:
        filters.append(f"scale={settings.output_width}:-2")
    elif settings.width:
        filters.append(f"scale={settings.width}:-2")
    return ",".join(filters) if filters else None


def _build_segment_filter(settings: ExportSettings) -> str:
    parts: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    current_time = 0.0
    for index, (start, end) in enumerate(settings.video_segments):
        timeline_start = (
            settings.video_segment_timeline_starts[index]
            if index < len(settings.video_segment_timeline_starts)
            else current_time
        )
        gap = max(timeline_start - current_time, 0.0)
        if gap > 0.001:
            gap_video_label = f"gapv{index}"
            parts.append(
                f"color=c=black:s={settings.source_width}x{settings.source_height}:d={gap:.3f},"
                f"format=yuv420p[{gap_video_label}]"
            )
            video_labels.append(f"[{gap_video_label}]")
            if settings.audio_mode == "keep":
                gap_audio_label = f"gapa{index}"
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={gap:.3f},asetpts=PTS-STARTPTS[{gap_audio_label}]"
                )
                audio_labels.append(f"[{gap_audio_label}]")
        video_label = f"v{index}"
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[{video_label}]"
        )
        video_labels.append(f"[{video_label}]")
        if settings.audio_mode == "keep":
            audio_label = f"a{index}"
            audio_muted = (
                settings.video_segment_audio_muted[index]
                if index < len(settings.video_segment_audio_muted)
                else False
            )
            if audio_muted:
                duration = max(end - start, 0.001)
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[{audio_label}]"
                )
            else:
                parts.append(
                    f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[{audio_label}]"
                )
            audio_labels.append(f"[{audio_label}]")
        current_time = timeline_start + max(end - start, 0.0)

    if settings.audio_mode == "keep":
        parts.append(
            "".join(label for pair in zip(video_labels, audio_labels, strict=True) for label in pair)
            + f"concat=n={len(video_labels)}:v=1:a=1[vcat][aout]"
        )
        video_input = "[vcat]"
    else:
        parts.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vcat]")
        video_input = "[vcat]"

    video_filter = _build_video_filter(settings)
    if video_filter:
        parts.append(f"{video_input}{video_filter}[vout]")
    else:
        parts.append(f"{video_input}null[vout]")
    if settings.audio_mode == "replace" and len(settings.replacement_audio_clips) > 1:
        parts.append(_build_audio_mix_filter(len(settings.replacement_audio_clips)))
    return ";".join(parts)


def _source_crop_pixels(state: ProjectState) -> tuple[int, int, int, int] | None:
    if not state.crop_enabled or state.source_width <= 0 or state.source_height <= 0:
        return None
    x = even_floor(int(round(state.crop_x * state.source_width)))
    y = even_floor(int(round(state.crop_y * state.source_height)))
    width = even_floor(int(round(state.crop_width * state.source_width)))
    height = even_floor(int(round(state.crop_height * state.source_height)))
    width = max(2, min(width, state.source_width - x))
    height = max(2, min(height, state.source_height - y))
    return x, y, width, height
