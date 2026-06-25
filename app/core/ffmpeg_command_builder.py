from __future__ import annotations

import copy
import os
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
    video_encoder: str = "libx264"
    video_preset: str = "medium"
    target_video_bitrate_kbps: int | None = None
    framerate: int | None = None
    audio_codec: str | None = None
    audio_bitrate_kbps: int | None = None
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


class FilterGraphBuilder:
    def __init__(self) -> None:
        self._parts: list[str] = []

    def add_gap_video(self, label: str, width: int, height: int, duration: float) -> None:
        self._parts.append(
            f"color=c=black:s={width}x{height}:d={duration:.3f},"
            f"format=yuv420p[{label}]"
        )

    def add_gap_audio(self, label: str, duration: float) -> None:
        self._parts.append(
            f"anullsrc=channel_layout=stereo:sample_rate=48000,"
            f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[{label}]"
        )

    def add_video_trim(self, label: str, start: float, end: float) -> None:
        self._parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[{label}]"
        )

    def add_audio_trim(self, label: str, start: float, end: float) -> None:
        self._parts.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[{label}]"
        )

    def add_concat(self, labels: str, count: int, with_audio: bool) -> None:
        if with_audio:
            self._parts.append(f"{labels}concat=n={count}:v=1:a=1[vcat][aout]")
        else:
            self._parts.append(f"{labels}concat=n={count}:v=1:a=0[vcat]")

    def add_video_output(self, video_input: str, video_filter: str | None) -> None:
        if video_filter:
            self._parts.append(f"{video_input}{video_filter}[vout]")
        else:
            self._parts.append(f"{video_input}null[vout]")

    def add_raw(self, part: str) -> None:
        self._parts.append(part)

    def build(self) -> str:
        return ";".join(self._parts)


def build_ffmpeg_command(settings: ExportSettings) -> list[str]:
    return _build_ffmpeg_command(settings)


def build_ffmpeg_commands(
    settings: ExportSettings,
    passlog_path: Path | None = None,
    concat_manifest_path: Path | None = None,
) -> list[list[str]]:
    if _can_concat_demuxer_copy(settings):
        if concat_manifest_path is None:
            raise ValueError("concat_manifest_path is required for concat demuxer export")
        write_concat_manifest(settings, concat_manifest_path)
        return [_build_concat_demuxer_command(settings, concat_manifest_path)]
    if _needs_two_pass(settings):
        if passlog_path is None:
            raise ValueError("passlog_path is required for two-pass export")
        return [
            _build_ffmpeg_command(settings, pass_number=1, passlog_path=passlog_path),
            _build_ffmpeg_command(settings, pass_number=2, passlog_path=passlog_path),
        ]
    return [build_ffmpeg_command(settings)]


def write_concat_manifest(settings: ExportSettings, manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for start, end in settings.video_segments:
        lines.append(f"file '{_concat_escape(settings.input_path)}'")
        lines.append(f"inpoint {start:.6f}")
        lines.append(f"outpoint {end:.6f}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_ffmpeg_command(
    settings: ExportSettings,
    pass_number: int | None = None,
    passlog_path: Path | None = None,
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
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
    command.extend(
        _format_video_args(
            settings.format_name,
            settings.video_crf,
            settings.video_encoder,
            settings.video_preset,
            settings.target_video_bitrate_kbps,
        )
    )
    if settings.framerate:
        command.extend(["-r", str(settings.framerate)])
    if pass_number is not None:
        if passlog_path is None:
            raise ValueError("passlog_path is required for two-pass export")
        command.extend(["-pass", str(pass_number), "-passlogfile", str(passlog_path)])

    if pass_number == 1:
        command.extend(["-an", "-f", "null", "-progress", "pipe:1", os.devnull])
        return command

    if settings.audio_mode == "mute":
        command.append("-an")
    elif (
        settings.audio_mode == "replace"
        and not has_segments
        and len(settings.replacement_audio_clips) <= 1
    ):
        command.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest"])
        command.extend(_format_audio_args(settings.format_name, replacement=True, codec=settings.audio_codec, bitrate_kbps=settings.audio_bitrate_kbps))
    elif settings.audio_mode == "replace":
        command.extend(_format_audio_args(settings.format_name, replacement=True, codec=settings.audio_codec, bitrate_kbps=settings.audio_bitrate_kbps))
    else:
        command.extend(_format_audio_args(settings.format_name, replacement=False, codec=settings.audio_codec, bitrate_kbps=settings.audio_bitrate_kbps))

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
    video_encoder: str = "libx264",
    video_preset: str = "medium",
    target_video_bitrate_kbps: int | None = None,
    framerate: int | None = None,
    audio_codec: str | None = None,
    audio_bitrate_kbps: int | None = None,
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
        video_encoder=video_encoder,
        video_preset=video_preset,
        target_video_bitrate_kbps=target_video_bitrate_kbps,
        framerate=framerate,
        audio_codec=audio_codec,
        audio_bitrate_kbps=audio_bitrate_kbps,
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


def project_state_for_export(
    state: ProjectState,
    output_width: int | None,
    output_height: int | None,
    audio_mode: str,
    loop_timeline_audio: bool,
) -> ProjectState:
    export_state = copy.deepcopy(state)
    export_state.output_width = output_width
    export_state.output_height = output_height
    if audio_mode == "mute":
        export_state.original_audio_enabled = False
    elif audio_mode == "keep":
        export_state.original_audio_enabled = True
    export_state.original_audio_track.muted = not export_state.original_audio_enabled
    for clip in export_state.original_audio_track.clips:
        clip.muted = not export_state.original_audio_enabled
    for clip in export_state.audio_clips:
        clip.loop = loop_timeline_audio
    return export_state


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


def _format_video_args(
    format_name: str,
    video_crf: int | None,
    video_encoder: str,
    video_preset: str,
    target_video_bitrate_kbps: int | None = None,
) -> list[str]:
    bitrate_args = ["-b:v", f"{target_video_bitrate_kbps}k"] if target_video_bitrate_kbps else []
    if format_name == "webm":
        return ["-c:v", "libvpx-vp9", *bitrate_args] if bitrate_args else ["-c:v", "libvpx-vp9", "-crf", str(video_crf or 32), "-b:v", "0"]
    if video_encoder == "h264_nvenc":
        quality_args = bitrate_args if bitrate_args else ["-cq:v", str(video_crf or 23)]
        return ["-c:v", "h264_nvenc", *quality_args, "-preset", _nvenc_preset(video_preset)]
    if video_encoder == "h264_qsv":
        quality_args = bitrate_args if bitrate_args else ["-global_quality", str(video_crf or 23)]
        return ["-c:v", "h264_qsv", *quality_args, "-preset", video_preset]
    if video_encoder == "h264_amf":
        if bitrate_args:
            return ["-c:v", "h264_amf", "-quality", _amf_quality(video_preset), *bitrate_args]
        return ["-c:v", "h264_amf", "-quality", _amf_quality(video_preset), "-qp_i", str(video_crf or 23), "-qp_p", str(video_crf or 23)]
    quality_args = bitrate_args if bitrate_args else ["-crf", str(video_crf or 23)]
    return ["-c:v", "libx264", *quality_args, "-preset", video_preset]


def _nvenc_preset(preset: str) -> str:
    return {
        "ultrafast": "p1",
        "superfast": "p2",
        "veryfast": "p3",
        "faster": "p4",
        "fast": "p4",
        "medium": "p5",
        "slow": "p6",
        "slower": "p7",
        "veryslow": "p7",
    }.get(preset, "p5")


def _amf_quality(preset: str) -> str:
    if preset in {"slow", "slower", "veryslow"}:
        return "quality"
    if preset in {"ultrafast", "superfast", "veryfast", "faster"}:
        return "speed"
    return "balanced"


def _format_audio_args(
    format_name: str,
    replacement: bool,
    codec: str | None = None,
    bitrate_kbps: int | None = None,
) -> list[str]:
    resolved_codec = codec or ("libopus" if format_name == "webm" else "aac")
    bitrate = f"{bitrate_kbps or (192 if replacement else 128)}k"
    return ["-c:a", resolved_codec, "-b:a", bitrate]


def _format_stream_copy_args(audio_mode: str) -> list[str]:
    if audio_mode == "mute":
        return ["-c:v", "copy", "-an"]
    return ["-c", "copy"]


def _build_concat_demuxer_command(settings: ExportSettings, manifest_path: Path) -> list[str]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(manifest_path),
    ]
    command.extend(_format_stream_copy_args(settings.audio_mode))
    command.extend(["-progress", "pipe:1", str(settings.output_path)])
    return command


def _can_stream_copy(settings: ExportSettings, has_segments: bool, video_filter: str | None) -> bool:
    if settings.target_video_bitrate_kbps:
        return False
    if has_segments or video_filter:
        return False
    if settings.audio_mode not in {"keep", "mute"}:
        return False
    if settings.replacement_audio_path is not None or settings.replacement_audio_clips:
        return False
    if settings.format_name == "mkv":
        return True
    return settings.input_path.suffix.lower().lstrip(".") == settings.format_name


def _can_concat_demuxer_copy(settings: ExportSettings) -> bool:
    if len(settings.video_segments) <= 1:
        return False
    if settings.crop or settings.output_width or settings.output_height or settings.width:
        return False
    if settings.target_video_bitrate_kbps:
        return False
    if settings.audio_mode not in {"keep", "mute"}:
        return False
    if settings.replacement_audio_path is not None or settings.replacement_audio_clips:
        return False
    if any(settings.video_segment_audio_muted):
        return False
    if len(settings.video_segment_timeline_starts) != len(settings.video_segments):
        return False
    current = 0.0
    for timeline_start, (start, end) in zip(
        settings.video_segment_timeline_starts,
        settings.video_segments,
        strict=True,
    ):
        if end <= start:
            return False
        if abs(timeline_start - current) > 0.001:
            return False
        current += end - start
    if settings.format_name == "mkv":
        return True
    return settings.input_path.suffix.lower().lstrip(".") == settings.format_name


def _concat_escape(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _needs_two_pass(settings: ExportSettings) -> bool:
    return bool(
        settings.target_video_bitrate_kbps
        and settings.video_encoder == "libx264"
        and settings.format_name != "webm"
    )


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
    graph = FilterGraphBuilder()
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
            graph.add_gap_video(gap_video_label, settings.source_width, settings.source_height, gap)
            video_labels.append(f"[{gap_video_label}]")
            if settings.audio_mode == "keep":
                gap_audio_label = f"gapa{index}"
                graph.add_gap_audio(gap_audio_label, gap)
                audio_labels.append(f"[{gap_audio_label}]")
        video_label = f"v{index}"
        graph.add_video_trim(video_label, start, end)
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
                graph.add_gap_audio(audio_label, duration)
            else:
                graph.add_audio_trim(audio_label, start, end)
            audio_labels.append(f"[{audio_label}]")
        current_time = timeline_start + max(end - start, 0.0)

    if settings.audio_mode == "keep":
        graph.add_concat(
            "".join(label for pair in zip(video_labels, audio_labels, strict=True) for label in pair),
            len(video_labels),
            with_audio=True,
        )
        video_input = "[vcat]"
    else:
        graph.add_concat("".join(video_labels), len(video_labels), with_audio=False)
        video_input = "[vcat]"

    video_filter = _build_video_filter(settings)
    graph.add_video_output(video_input, video_filter)
    if settings.audio_mode == "replace" and len(settings.replacement_audio_clips) > 1:
        graph.add_raw(_build_audio_mix_filter(len(settings.replacement_audio_clips)))
    return graph.build()


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
