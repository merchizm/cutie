from pathlib import Path

from app.core.ffmpeg_command_builder import (
    ExportSettings,
    build_ffmpeg_command,
    export_settings_from_project,
)
from app.core.project_state import ProjectState
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
    state.trim_video_range(1, 5)
    state.crop_enabled = True
    state.crop_x = 0.25
    state.crop_y = 0.0
    state.crop_width = 0.5
    state.crop_height = 1.0
    state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=0)
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


def test_global_trim_normalizes_video_to_timeline_start() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)

    state.trim_video_range(1, 5)

    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]
    assert video.timeline_start == 0
    assert audio.timeline_start == 0
    assert video.source_in == 1
    assert audio.source_in == 1
    assert video.timeline_end == 4


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


def test_timeline_audio_offset_is_exported() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=3)

    settings = export_settings_from_project(
        state,
        Path("out.mp4"),
        format_name="mp4",
        video_crf=23,
        output_width=None,
        output_height=None,
        audio_mode="timeline",
        loop_timeline_audio=True,
    )
    command = build_ffmpeg_command(settings)

    assert "-itsoffset" in command
    assert "00:00:03.000" in command


def test_multiple_timeline_audio_clips_are_mixed_for_export() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.add_audio_clip(Path("a.mp3"), duration=2, timeline_start=1)
    state.add_audio_clip(Path("b.mp3"), duration=2, timeline_start=3)

    settings = export_settings_from_project(
        state,
        Path("out.mp4"),
        format_name="mp4",
        video_crf=23,
        output_width=None,
        output_height=None,
        audio_mode="timeline",
        loop_timeline_audio=True,
    )
    command = build_ffmpeg_command(settings)
    filter_complex = command[command.index("-filter_complex") + 1]

    assert "a.mp3" in command
    assert "b.mp3" in command
    assert "amix=inputs=2" in filter_complex
    assert "[mixout]" in command


def test_audio_clip_can_be_added_before_video_tracks_exist() -> None:
    state = ProjectState()
    clip = state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=1.5)

    assert clip.timeline_start == 1.5
    assert state.music_track.clips == [clip]
    assert state.video_track.clips == []
    assert state.duration == 3.5


def test_muted_split_video_linked_audio_exports_segment_silence() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    assert state.split_at(4)
    right = state.video_segments[1]
    state.select_clip(right.id)
    assert state.toggle_selected_audio_mute()

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

    assert "-an" not in command
    assert "anullsrc=channel_layout=stereo" in filter_complex
    assert "concat=n=2:v=1:a=1" in filter_complex


def test_moved_video_segments_preserve_timeline_gap_for_export() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    assert state.split_at(4)
    first, second = state.video_segments

    assert state.move_clip(first.id, second.timeline_end)

    assert first.timeline_start == 10
    assert second.timeline_start == 4
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

    assert "color=c=black" in filter_complex
    assert "anullsrc=channel_layout=stereo" in filter_complex


def test_export_rejects_empty_video_timeline() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.select_clip(state.video_segments[0].id)
    assert state.delete_selected_clip()

    try:
        export_settings_from_project(
            state,
            Path("out.mp4"),
            format_name="mp4",
            video_crf=23,
            output_width=None,
            output_height=None,
            audio_mode="keep",
            loop_timeline_audio=True,
        )
    except ValueError as exc:
        assert "No video clips" in str(exc)
    else:
        raise AssertionError("empty video timeline should not export the original input")


def test_linked_original_audio_moves_and_trims_with_video() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]

    assert state.move_clip(video.id, 2)
    assert video.timeline_start == 2
    assert audio.timeline_start == 2

    assert state.trim_clip_left(video.id, 3)
    assert video.timeline_start == 3
    assert audio.timeline_start == 3
    assert video.source_in == 1
    assert audio.source_in == 1

    assert state.trim_clip_right(video.id, 8)
    assert video.timeline_end == 8
    assert audio.timeline_end == 8


def test_selected_music_clip_can_split_delete_and_mute() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=6, timeline_start=2)
    state.playhead_time = 4

    assert state.split_at(state.playhead_time)
    assert len(state.audio_clips) == 2
    left, right = state.audio_clips
    assert left.timeline_start == 2
    assert left.timeline_end == 4
    assert left.source_in == 0
    assert left.source_out == 2
    assert right.timeline_start == 4
    assert right.source_in == 2
    assert right.source_out == 6

    state.select_clip(right.id)
    assert state.toggle_selected_audio_mute()
    assert right.muted
    assert state.delete_selected_clip()
    assert state.audio_clips == [left]


def test_clip_move_snaps_to_playhead_and_neighbor_edges() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=12, width=1920, height=1080, has_audio=True)
    audio_a = state.add_audio_clip(Path("a.mp3"), duration=2, timeline_start=0)
    audio_b = state.add_audio_clip(Path("b.mp3"), duration=2, timeline_start=5)
    state.playhead_time = 3

    assert state.move_clip(audio_b.id, 3.08)
    assert audio_b.timeline_start == 3

    assert state.move_clip(audio_b.id, audio_a.timeline_end + 0.07)
    assert audio_b.timeline_start == audio_a.timeline_end


def test_export_dialog_resolution_math() -> None:
    from app.widgets.export_dialog import _even, _first_int, resolve_output_size

    assert _first_int("1280 width", 0) == 1280
    assert _first_int("Original", 7) == 7
    assert _even(853) == 854
    assert _even(854) == 854
    assert resolve_output_size("1080p", "9:16", 1280, 720) == (1080, 1920)
    assert resolve_output_size("1080 square", "1:1", 1280, 720) == (1080, 1080)
    assert resolve_output_size("1280 width", "16:9", 1280, 720) == (1280, 720)
