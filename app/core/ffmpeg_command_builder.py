from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.project_state import ProjectState
from app.utils.timecode import seconds_to_timecode


@dataclass(frozen=True)
class ExportSettings:
    input_path: Path
    output_path: Path
    trim_start: float = 0.0
    trim_end: float | None = None
    width: int | None = None
    audio_mode: str = "keep"
    replacement_audio_path: Path | None = None
    format_name: str = "mp4"
    video_crf: int | None = None
    crop: tuple[int, int, int, int] | None = None
    output_width: int | None = None
    output_height: int | None = None
    loop_audio: bool = True
    video_segments: tuple[tuple[float, float], ...] = ()


def build_ffmpeg_command(settings: ExportSettings) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-y", "-nostdin"]
    has_segments = len(settings.video_segments) > 1
    video_filter = None

    if settings.trim_start > 0 and not has_segments:
        command.extend(["-ss", seconds_to_timecode(settings.trim_start)])
    if settings.trim_end is not None and settings.trim_end > settings.trim_start and not has_segments:
        command.extend(["-to", seconds_to_timecode(settings.trim_end)])

    command.extend(["-i", str(settings.input_path)])

    if settings.audio_mode == "replace":
        if settings.replacement_audio_path is None:
            raise ValueError("replacement_audio_path is required when audio_mode is replace")
        if settings.loop_audio:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(settings.replacement_audio_path)])

    if has_segments:
        command.extend(["-filter_complex", _build_segment_filter(settings)])
    else:
        video_filter = _build_video_filter(settings)
        if video_filter:
            command.extend(["-vf", video_filter])

    if has_segments:
        command.extend(["-map", "[vout]"])
        if settings.audio_mode == "keep":
            command.extend(["-map", "[aout]"])
        elif settings.audio_mode == "replace":
            command.extend(["-map", "1:a:0", "-shortest"])
    command.extend(_format_video_args(settings.format_name, settings.video_crf))

    if settings.audio_mode == "mute":
        command.append("-an")
    elif settings.audio_mode == "replace" and not has_segments:
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

    audio_clip = state.active_audio_clip()
    replacement_audio = audio_clip.path if audio_mode == "timeline" and audio_clip else None
    resolved_audio_mode = "replace" if replacement_audio is not None else audio_mode
    if resolved_audio_mode == "timeline":
        resolved_audio_mode = "mute"

    return ExportSettings(
        input_path=state.source_video_path,
        output_path=output_path,
        trim_start=state.trim_start,
        trim_end=state.trim_end,
        audio_mode=resolved_audio_mode,
        replacement_audio_path=replacement_audio,
        format_name=format_name,
        video_crf=video_crf,
        crop=_source_crop_pixels(state),
        output_width=output_width,
        output_height=output_height,
        loop_audio=loop_timeline_audio and (audio_clip.loop if audio_clip else True),
        video_segments=tuple((segment.start, segment.end) for segment in state.active_video_segments()),
    )


def _format_video_args(format_name: str, video_crf: int | None) -> list[str]:
    if format_name == "webm":
        return ["-c:v", "libvpx-vp9", "-crf", str(video_crf or 32), "-b:v", "0"]
    return ["-c:v", "libx264", "-crf", str(video_crf or 23), "-preset", "medium"]


def _format_audio_args(format_name: str, replacement: bool) -> list[str]:
    bitrate = "192k" if replacement else "128k"
    if format_name == "webm":
        return ["-c:a", "libopus", "-b:a", bitrate]
    return ["-c:a", "aac", "-b:a", bitrate]


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
    for index, (start, end) in enumerate(settings.video_segments):
        video_label = f"v{index}"
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[{video_label}]"
        )
        video_labels.append(f"[{video_label}]")
        if settings.audio_mode == "keep":
            audio_label = f"a{index}"
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[{audio_label}]"
            )
            audio_labels.append(f"[{audio_label}]")

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
    return ";".join(parts)


def _source_crop_pixels(state: ProjectState) -> tuple[int, int, int, int] | None:
    if not state.crop_enabled or state.source_width <= 0 or state.source_height <= 0:
        return None
    x = _even(int(round(state.crop_x * state.source_width)))
    y = _even(int(round(state.crop_y * state.source_height)))
    width = _even(int(round(state.crop_width * state.source_width)))
    height = _even(int(round(state.crop_height * state.source_height)))
    width = max(2, min(width, state.source_width - x))
    height = max(2, min(height, state.source_height - y))
    return x, y, width, height


def _even(value: int) -> int:
    value = max(value, 0)
    return value if value % 2 == 0 else value - 1
