from pathlib import Path

from app.core.ffmpeg_command_builder import (
    ExportSettings,
    build_ffmpeg_command,
    export_settings_from_project,
)
from app.core.project_state import AudioClip, ProjectState
from app.utils.timecode import seconds_to_label, seconds_to_timecode


def test_seconds_to_timecode() -> None:
    assert seconds_to_timecode(12.3) == "00:00:12.300"
    assert seconds_to_timecode(62.5) == "00:01:02.500"


def test_seconds_to_label() -> None:
    assert seconds_to_label(62) == "1:02"
    assert seconds_to_label(3661) == "1:01:01"


def test_mute_resize_command() -> None:
    command = build_ffmpeg_command(
        ExportSettings(
            input_path=Path("input.mp4"),
            output_path=Path("output.mp4"),
            trim_start=12.3,
            trim_end=42,
            width=1280,
            audio_mode="mute",
            video_crf=18,
        )
    )
    assert "-ss" in command
    assert "00:00:12.300" in command
    assert "-to" in command
    assert "scale=1280:-2" in command
    assert "-an" in command
    assert "18" in command
    assert "output.mp4" == command[-1]


def test_replace_audio_command() -> None:
    command = build_ffmpeg_command(
        ExportSettings(
            input_path=Path("input.mp4"),
            output_path=Path("output.webm"),
            audio_mode="replace",
            replacement_audio_path=Path("music.mp3"),
            format_name="webm",
        )
    )
    assert "-stream_loop" in command
    assert "-1" in command
    assert "music.mp3" in command
    assert "-shortest" in command
    assert "libopus" in command


def test_project_state_crop_and_timeline_audio_export() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.trim_start = 1
    state.trim_end = 5
    state.crop_enabled = True
    state.crop_x = 0.25
    state.crop_y = 0.0
    state.crop_width = 0.5
    state.crop_height = 1.0
    state.audio_clips.append(
        AudioClip(Path("music.mp3"), "music.mp3", timeline_start=0, duration=2, loop=True)
    )
    settings = export_settings_from_project(
        state,
        Path("out.mp4"),
        format_name="mp4",
        video_crf=23,
        output_width=1080,
        output_height=1080,
        audio_mode="timeline",
        loop_timeline_audio=True,
    )
    command = build_ffmpeg_command(settings)
    assert "crop=960:1080:480:0,scale=1080:1080" in command
    assert "-stream_loop" in command
    assert "music.mp3" in command
    assert "-shortest" in command


def test_split_segments_export_uses_concat_filter() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)

    assert state.split_video_at(4)

    settings = export_settings_from_project(
        state,
        Path("out.mp4"),
        format_name="mp4",
        video_crf=23,
        output_width=None,
        output_height=None,
        audio_mode="keep",
        loop_timeline_audio=True,
    )
    command = build_ffmpeg_command(settings)
    filter_complex = command[command.index("-filter_complex") + 1]

    assert "-ss" not in command
    assert "trim=start=0.000:end=4.000" in filter_complex
    assert "trim=start=4.000:end=10.000" in filter_complex
    assert "concat=n=2:v=1:a=1" in filter_complex
    assert "[vout]" in command
    assert "[aout]" in command


def test_export_dialog_resolution_math() -> None:
    from app.widgets.export_dialog import _even, _first_int, resolve_output_size

    assert _first_int("1280 width", 0) == 1280
    assert _first_int("Original", 7) == 7
    assert _even(853) == 854
    assert _even(854) == 854
    assert resolve_output_size("1080p", "9:16", 1280, 720) == (1080, 1920)
    assert resolve_output_size("1080 square", "1:1", 1280, 720) == (1080, 1080)
    assert resolve_output_size("1280 width", "16:9", 1280, 720) == (1280, 720)
