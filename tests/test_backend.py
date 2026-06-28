import tempfile
import copy
import logging
from types import SimpleNamespace
from pathlib import Path

from app.core.commands import (
    AddAudioClipCommand,
    ApplyWorkingVideoCommand,
    DeleteSelectedClipCommand,
    MoveClipCommand,
    ResetToOriginalVideoCommand,
    SplitAtCommand,
    ToggleOriginalAudioCommand,
    ToggleSelectedAudioMuteCommand,
    TrimClipCommand,
    TrimVideoRangeCommand,
)
from app.core.crop_worker import CropWorker
import app.core.ffprobe_reader as ffprobe_reader
from app.core.ffmpeg_command_builder import (
    ExportSettings,
    build_ffmpeg_command,
    build_ffmpeg_commands,
    export_settings_from_project,
    project_state_for_export,
    write_concat_manifest,
)
from app.core.ffmpeg_errors import summarize_ffmpeg_stderr
from app.core.project_references import referenced_media_paths
from app.core.project_state import CropRecord, ProjectState
from app.core.undo_history import UndoHistory
import app.utils.paths as path_utils
from app.utils.numbers import even_floor
from app.utils.timecode import seconds_to_label, seconds_to_timecode


def test_seconds_to_timecode() -> None:
    assert seconds_to_timecode(12.3) == "00:00:12.300"
    assert seconds_to_timecode(62.5) == "00:01:02.500"


def test_seconds_to_label() -> None:
    assert seconds_to_label(62) == "1:02"
    assert seconds_to_label(3661) == "1:01:01"


def test_summarize_ffmpeg_stderr_skips_trailing_conversion_noise() -> None:
    stderr = """
ffmpeg version 6.1 Copyright
configuration: --enable-gpl
[mov,mp4,m4a,3gp,3g2,mj2 @ 0x123] moov atom not found
input.mp4: Invalid data found when processing input
Conversion failed!
"""

    assert summarize_ffmpeg_stderr(stderr, "Export failed.") == "input.mp4: Invalid data found when processing input"


def test_summarize_ffmpeg_stderr_uses_fallback_for_empty_output() -> None:
    assert summarize_ffmpeg_stderr("", "Crop failed.") == "Crop failed."


def test_ffprobe_reader_wraps_missing_media_file_errors() -> None:
    ffprobe_reader.clear_ffprobe_cache()
    missing = Path("missing-input-file.mp4")
    logger = logging.getLogger("app.core.ffprobe_reader")
    previous_disabled = logger.disabled

    try:
        logger.disabled = True
        try:
            ffprobe_reader.read_media_info(missing)
        except ffprobe_reader.FFprobeError as exc:
            assert "could not be accessed" in str(exc)
        else:
            raise AssertionError("missing media info should raise FFprobeError")

        try:
            ffprobe_reader.read_media_duration(missing)
        except ffprobe_reader.FFprobeError as exc:
            assert "could not be accessed" in str(exc)
        else:
            raise AssertionError("missing media duration should raise FFprobeError")
    finally:
        logger.disabled = previous_disabled


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
    assert command[command.index("-loglevel") + 1] == "error"
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


def test_trim_only_same_container_uses_stream_copy() -> None:
    command = build_ffmpeg_command(
        ExportSettings(
            input_path=Path("input.mp4"),
            output_path=Path("output.mp4"),
            trim_start=1,
            trim_end=4,
            audio_mode="keep",
            format_name="mp4",
        )
    )

    assert "-ss" in command
    assert "-to" in command
    assert command[command.index("-c") + 1] == "copy"
    assert "libx264" not in command
    assert "-progress" in command


def test_trim_with_resize_reencodes_instead_of_stream_copy() -> None:
    command = build_ffmpeg_command(
        ExportSettings(
            input_path=Path("input.mp4"),
            output_path=Path("output.mp4"),
            trim_start=1,
            trim_end=4,
            output_width=1280,
            output_height=720,
            audio_mode="keep",
            format_name="mp4",
        )
    )

    assert "scale=1280:720" in command
    assert "libx264" in command
    assert "-c" not in command


def test_hardware_encoder_settings_are_reflected_in_command() -> None:
    command = build_ffmpeg_command(
        ExportSettings(
            input_path=Path("input.mkv"),
            output_path=Path("output.mkv"),
            output_width=1280,
            output_height=720,
            format_name="mkv",
            video_encoder="h264_nvenc",
            video_preset="slow",
        )
    )

    assert command[command.index("-c:v") + 1] == "h264_nvenc"
    assert command[command.index("-preset") + 1] == "p6"
    assert "-cq:v" in command


def test_target_bitrate_libx264_uses_two_pass_commands() -> None:
    commands = build_ffmpeg_commands(
        ExportSettings(
            input_path=Path("input.mp4"),
            output_path=Path("output.mp4"),
            output_width=1280,
            output_height=720,
            format_name="mp4",
            target_video_bitrate_kbps=6000,
        ),
        passlog_path=Path("passlog"),
    )

    assert len(commands) == 2
    first, second = commands
    assert first[first.index("-pass") + 1] == "1"
    assert second[second.index("-pass") + 1] == "2"
    assert first[first.index("-b:v") + 1] == "6000k"
    assert second[second.index("-b:v") + 1] == "6000k"
    assert "-an" in first
    assert first[-1] != "output.mp4"
    assert second[-1] == "output.mp4"


def test_ffprobe_reader_caches_media_info_until_file_changes() -> None:
    calls = 0
    payload = (
        '{"streams":[{"codec_type":"video","width":1280,"height":720,'
        '"avg_frame_rate":"30/1","codec_name":"h264"}],'
        '"format":{"duration":"3.5"}}'
    )

    def fake_run(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return SimpleNamespace(stdout=payload)

    original_run = ffprobe_reader.subprocess.run
    ffprobe_reader.clear_ffprobe_cache()
    try:
        ffprobe_reader.subprocess.run = fake_run
        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            path = Path(handle.name)
            first = ffprobe_reader.read_media_info(path)
            second = ffprobe_reader.read_media_info(path)
    finally:
        ffprobe_reader.subprocess.run = original_run
        ffprobe_reader.clear_ffprobe_cache()

    assert first == second
    assert first.duration == 3.5
    assert calls == 1


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


def test_project_state_for_export_does_not_mutate_project_audio_flags() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=0)
    state.original_audio_enabled = True
    state.original_audio_track.muted = False
    for original_clip in state.original_audio_track.clips:
        original_clip.muted = False
    clip.loop = True

    export_state = project_state_for_export(
        state,
        output_width=1280,
        output_height=720,
        audio_mode="mute",
        loop_timeline_audio=False,
    )

    assert state.original_audio_enabled
    assert not state.original_audio_track.muted
    assert not state.original_audio_track.clips[0].muted
    assert clip.loop
    assert state.output_width is None
    assert state.output_height is None

    assert not export_state.original_audio_enabled
    assert export_state.original_audio_track.muted
    assert export_state.original_audio_track.clips[0].muted
    assert not export_state.audio_clips[0].loop
    assert export_state.output_width == 1280
    assert export_state.output_height == 720


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


def test_project_state_pending_crop_can_be_cleared_after_failed_crop() -> None:
    state = ProjectState()
    state.set_pending_crop(0.25, 0.1, 0.5, 0.8)

    assert state.crop_enabled
    assert state.crop_x == 0.25
    assert state.crop_y == 0.1
    assert state.crop_width == 0.5
    assert state.crop_height == 0.8

    state.clear_pending_crop()

    assert not state.crop_enabled
    assert state.crop_x == 0.0
    assert state.crop_y == 0.0
    assert state.crop_width == 1.0
    assert state.crop_height == 1.0


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


def test_split_segments_can_use_concat_demuxer_fast_path() -> None:
    manifest = Path(tempfile.gettempdir()) / "cutie-test-concat.ffconcat"
    settings = ExportSettings(
        input_path=Path("input.mp4"),
        output_path=Path("out.mp4"),
        format_name="mp4",
        audio_mode="keep",
        video_segments=((0, 4), (4, 10)),
        video_segment_timeline_starts=(0, 4),
        video_segment_audio_muted=(False, False),
    )
    try:
        commands = build_ffmpeg_commands(settings, concat_manifest_path=manifest)
        command = commands[0]
        manifest_text = manifest.read_text(encoding="utf-8")
    finally:
        manifest.unlink(missing_ok=True)

    assert len(commands) == 1
    assert "-filter_complex" not in command
    assert command[command.index("-loglevel") + 1] == "error"
    assert command[command.index("-f") + 1] == "concat"
    assert command[command.index("-c") + 1] == "copy"
    assert str(manifest) in command
    assert "file 'input.mp4'" in manifest_text
    assert "inpoint 0.000000" in manifest_text
    assert "outpoint 10.000000" in manifest_text


def test_concat_manifest_escapes_single_quotes() -> None:
    manifest = Path(tempfile.gettempdir()) / "cutie-test-escaped.ffconcat"
    try:
        write_concat_manifest(
            ExportSettings(
                input_path=Path("clip's.mp4"),
                output_path=Path("out.mp4"),
                video_segments=((0, 1),),
            ),
            manifest,
        )
        text = manifest.read_text(encoding="utf-8")
    finally:
        manifest.unlink(missing_ok=True)

    assert "clip'\\''s.mp4" in text


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


def test_trimmed_timeline_audio_source_start_is_exported() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=6, timeline_start=2)

    assert history.execute(state, TrimClipCommand(clip.id, "left", 4))

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
    music_input = command.index("music.mp3")

    assert "-ss" in command[:music_input]
    assert "00:00:02.000" in command[:music_input]


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


def test_project_state_deepcopy_excludes_track_lookup_cache() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    assert state.video_track.id == "video"
    assert state._track_lookup

    clone = copy.deepcopy(state)

    assert clone._track_lookup == {}
    assert clone.video_track.id == "video"
    assert clone.video_track is not state.video_track


def test_undo_history_limits_and_restores_project_states() -> None:
    history = UndoHistory(limit=2)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)

    history.push(state)
    state.playhead_time = 1
    history.push(state)
    state.playhead_time = 2
    history.push(state)
    state.playhead_time = 3

    assert len(history.undo_stack) == 2
    restored = history.undo(state)
    assert restored is not None
    assert restored.playhead_time == 2

    redone = history.redo(restored)
    assert redone is not None
    assert redone.playhead_time == 3

    history.push(redone)
    assert history.redo(redone) is None


def test_undo_history_executes_and_reverts_state_command() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)

    assert history.execute(state, ToggleOriginalAudioCommand())
    assert not state.original_audio_enabled
    assert state.original_audio_track.muted
    assert all(clip.muted for clip in state.original_audio_track.clips)

    restored = history.undo(state)
    assert restored is state
    assert state.original_audio_enabled
    assert not state.original_audio_track.muted

    redone = history.redo(state)
    assert redone is state
    assert not state.original_audio_enabled


def test_toggle_selected_audio_mute_command_reverts_music_clip() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=3, timeline_start=0)

    assert history.execute(state, ToggleSelectedAudioMuteCommand())
    assert clip.muted

    restored = history.undo(state)
    assert restored is state
    assert not clip.muted

    redone = history.redo(state)
    assert redone is state
    assert clip.muted


def test_toggle_selected_audio_mute_command_reverts_linked_video_audio() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    audio = state.original_audio_track.clips[0]
    state.select_clip(state.video_segments[0].id)

    assert history.execute(state, ToggleSelectedAudioMuteCommand())
    assert audio.muted

    restored = history.undo(state)
    assert restored is state
    assert not audio.muted

    redone = history.redo(state)
    assert redone is state
    assert audio.muted


def test_add_audio_clip_command_reverts_added_clip_and_audio_state() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    original_audio = state.original_audio_track.clips[0]

    command = AddAudioClipCommand(Path("music.mp3"), duration=3, timeline_start=1)
    assert history.execute(state, command)
    clip_id = command.added_clip_id
    assert clip_id is not None
    assert state.find_clip(clip_id) is not None
    assert Path("music.mp3") in state.media_pool
    assert state.selected_clip_id == clip_id
    assert not state.original_audio_enabled
    assert state.original_audio_track.muted

    restored = history.undo(state)
    assert restored is state
    assert state.find_clip(clip_id) is None
    assert Path("music.mp3") not in state.media_pool
    assert state.selected_clip_id == "video"
    assert state.original_audio_enabled
    assert not state.original_audio_track.muted
    assert not original_audio.muted

    redone = history.redo(state)
    assert redone is state
    assert command.added_clip_id is not None
    assert state.find_clip(command.added_clip_id) is not None
    assert not state.original_audio_enabled


def test_apply_working_video_command_reverts_crop_source_change() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=1)
    record = CropRecord(Path("input.mp4"), Path("cropped.mp4"), 0.1, 0.2, 0.8, 0.6)

    command = ApplyWorkingVideoCommand(Path("cropped.mp4"), 8, 1280, 720, record)
    assert history.execute(state, command)
    assert state.working_video_path == Path("cropped.mp4")
    assert state.working_duration == 8
    assert state.working_width == 1280
    assert state.working_height == 720
    assert state.is_cropped
    assert state.crop_history == [record]
    assert state.video_segments[0].source_path == Path("cropped.mp4")
    assert len(state.audio_clips) == 1
    assert state.audio_clips[0].source_path == Path("music.mp3")

    restored = history.undo(state)
    assert restored is state
    assert state.working_video_path == Path("input.mp4")
    assert state.working_duration == 10
    assert state.working_width == 1920
    assert state.working_height == 1080
    assert not state.is_cropped
    assert state.crop_history == []
    assert state.video_segments[0].source_path == Path("input.mp4")
    assert len(state.audio_clips) == 1
    assert state.audio_clips[0].source_path == Path("music.mp3")

    redone = history.redo(state)
    assert redone is state
    assert state.working_video_path == Path("cropped.mp4")
    assert state.crop_history == [record]
    assert state.video_segments[0].source_path == Path("cropped.mp4")


def test_apply_working_video_command_keeps_source_paths_for_cleanup() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=1)
    record = CropRecord(Path("input.mp4"), Path("cropped.mp4"), 0.1, 0.2, 0.8, 0.6)

    assert history.execute(state, ApplyWorkingVideoCommand(Path("cropped.mp4"), 8, 1280, 720, record))
    paths = history.referenced_paths()

    assert Path("input.mp4") in paths
    assert Path("music.mp3") in paths
    assert Path("cropped.mp4") in paths


def test_reset_to_original_video_command_reverts_to_cropped_state() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=1)
    record = CropRecord(Path("input.mp4"), Path("cropped.mp4"), 0.1, 0.2, 0.8, 0.6)
    assert history.execute(state, ApplyWorkingVideoCommand(Path("cropped.mp4"), 8, 1280, 720, record))

    assert history.execute(state, ResetToOriginalVideoCommand(Path("input.mp4"), 10, 1920, 1080, True))
    assert state.working_video_path == Path("input.mp4")
    assert not state.is_cropped
    assert state.crop_history == []
    assert state.audio_clips == []
    assert state.original_audio_enabled

    restored = history.undo(state)
    assert restored is state
    assert state.working_video_path == Path("cropped.mp4")
    assert state.is_cropped
    assert state.crop_history == [record]
    assert len(state.audio_clips) == 1
    assert state.audio_clips[0].source_path == Path("music.mp3")
    assert not state.original_audio_enabled

    redone = history.redo(state)
    assert redone is state
    assert state.working_video_path == Path("input.mp4")
    assert not state.is_cropped


def test_reset_to_original_video_command_rejects_non_cropped_state() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)

    assert not history.execute(state, ResetToOriginalVideoCommand(Path("input.mp4"), 10, 1920, 1080, True))
    assert history.undo_stack == ()


def test_move_clip_command_reverts_music_clip_position() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=3, timeline_start=0)

    assert history.execute(state, MoveClipCommand(clip.id, 5))
    assert clip.timeline_start == 5

    restored = history.undo(state)
    assert restored is state
    assert clip.timeline_start == 0

    redone = history.redo(state)
    assert redone is state
    assert clip.timeline_start == 5


def test_move_clip_command_reverts_linked_video_audio_position() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]

    assert history.execute(state, MoveClipCommand(video.id, 2))
    assert video.timeline_start == 2
    assert audio.timeline_start == 2

    restored = history.undo(state)
    assert restored is state
    assert video.timeline_start == 0
    assert audio.timeline_start == 0


def test_trim_clip_command_reverts_music_clip_left_trim() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=6, timeline_start=2)

    assert history.execute(state, TrimClipCommand(clip.id, "left", 4))
    assert clip.timeline_start == 4
    assert clip.source_in == 2
    assert clip.source_out == 6

    restored = history.undo(state)
    assert restored is state
    assert clip.timeline_start == 2
    assert clip.source_in == 0
    assert clip.source_out == 6

    redone = history.redo(state)
    assert redone is state
    assert clip.timeline_start == 4
    assert clip.source_in == 2


def test_trim_clip_command_reverts_linked_video_audio_right_trim() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]

    assert history.execute(state, TrimClipCommand(video.id, "right", 7))
    assert video.source_out == 7
    assert audio.source_out == 7

    restored = history.undo(state)
    assert restored is state
    assert video.source_out == 10
    assert audio.source_out == 10

    redone = history.redo(state)
    assert redone is state
    assert video.source_out == 7
    assert audio.source_out == 7


def test_trim_video_range_command_reverts_global_trim() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]

    assert history.execute(state, TrimVideoRangeCommand(1, 5))
    assert video.timeline_start == 0
    assert audio.timeline_start == 0
    assert video.source_in == 1
    assert audio.source_in == 1
    assert video.source_out == 5
    assert audio.source_out == 5

    restored = history.undo(state)
    assert restored is state
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]
    assert video.timeline_start == 0
    assert audio.timeline_start == 0
    assert video.source_in == 0
    assert audio.source_in == 0
    assert video.source_out == 10
    assert audio.source_out == 10

    redone = history.redo(state)
    assert redone is state
    video = state.video_segments[0]
    assert video.source_in == 1
    assert video.source_out == 5


def test_trim_video_range_command_rejects_noop_range() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)

    assert not history.execute(state, TrimVideoRangeCommand(0, 10))
    assert history.undo_stack == ()


def test_delete_selected_clip_command_reverts_music_clip_delete() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=3, timeline_start=1)

    assert history.execute(state, DeleteSelectedClipCommand())
    assert state.audio_clips == []
    assert state.selected_clip_id is None

    restored = history.undo(state)
    assert restored is state
    restored_clip = state.find_clip(clip.id)
    assert restored_clip is not None
    assert restored_clip.timeline_start == 1
    assert state.selected_clip_id == clip.id

    redone = history.redo(state)
    assert redone is state
    assert state.find_clip(clip.id) is None


def test_command_history_keeps_deleted_clip_media_references_for_cleanup() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("cropped-temp.mp4"), duration=3, timeline_start=1)

    assert history.execute(state, DeleteSelectedClipCommand())
    assert state.find_clip(clip.id) is None
    assert Path("cropped-temp.mp4") in history.referenced_paths()


def test_delete_selected_clip_command_reverts_linked_video_audio_delete() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]
    state.select_clip(video.id)

    assert history.execute(state, DeleteSelectedClipCommand())
    assert state.video_segments == []
    assert state.original_audio_track.clips == []

    restored = history.undo(state)
    assert restored is state
    assert state.find_clip(video.id) is not None
    assert state.find_clip(audio.id) is not None
    assert state.selected_clip_id == video.id


def test_split_at_command_reverts_music_clip_split() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    clip = state.add_audio_clip(Path("music.mp3"), duration=6, timeline_start=2)
    state.select_clip(clip.id)

    assert history.execute(state, SplitAtCommand(4))
    assert len(state.audio_clips) == 2

    restored = history.undo(state)
    assert restored is state
    restored_clip = state.find_clip(clip.id)
    assert restored_clip is not None
    assert len(state.audio_clips) == 1
    assert restored_clip.timeline_start == 2
    assert restored_clip.source_in == 0
    assert restored_clip.source_out == 6
    assert state.selected_clip_id == clip.id

    redone = history.redo(state)
    assert redone is state
    assert len(state.audio_clips) == 2


def test_split_at_command_reverts_linked_video_audio_split() -> None:
    history = UndoHistory(limit=5)
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    video = state.video_segments[0]
    audio = state.original_audio_track.clips[0]
    state.select_clip(video.id)

    assert history.execute(state, SplitAtCommand(4))
    assert len(state.video_segments) == 2
    assert len(state.original_audio_track.clips) == 2

    restored = history.undo(state)
    assert restored is state
    assert state.find_clip(video.id) is not None
    assert state.find_clip(audio.id) is not None
    assert len(state.video_segments) == 1
    assert len(state.original_audio_track.clips) == 1
    assert state.selected_clip_id == video.id


def test_referenced_media_paths_collects_project_media() -> None:
    state = ProjectState()
    state.load_source(Path("input.mp4"), duration=10, width=1920, height=1080, has_audio=True)
    state.add_audio_clip(Path("music.mp3"), duration=2, timeline_start=0)

    paths = referenced_media_paths([state])

    assert Path("input.mp4") in paths
    assert Path("music.mp3") in paths


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


def test_even_floor_clamps_and_rounds_to_lower_even() -> None:
    assert even_floor(-3) == 0
    assert even_floor(853) == 852
    assert even_floor(854) == 854


def test_open_folder_returns_false_when_process_cannot_start() -> None:
    original_popen = path_utils.subprocess.Popen
    logger = logging.getLogger("app.utils.paths")
    previous_disabled = logger.disabled

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("xdg-open unavailable")

    try:
        logger.disabled = True
        path_utils.subprocess.Popen = fail_popen
        assert not path_utils.open_folder(Path("output.mp4"))
    finally:
        logger.disabled = previous_disabled
        path_utils.subprocess.Popen = original_popen


def test_open_folder_uses_parent_for_file_path() -> None:
    calls: list[list[str]] = []
    original_popen = path_utils.subprocess.Popen

    def fake_popen(command: list[str], *_args: object, **_kwargs: object) -> object:
        calls.append(command)
        return object()

    try:
        path_utils.subprocess.Popen = fake_popen
        assert path_utils.open_folder(Path("render/output.mp4"))
    finally:
        path_utils.subprocess.Popen = original_popen

    assert calls == [["xdg-open", "render"]]


def test_crop_worker_cleanup_removes_owned_temp_file() -> None:
    worker = CropWorker(Path("input.mp4"), (0, 0, 100, 100), 1, lambda *_args: None)
    worker.output_path.parent.mkdir(parents=True, exist_ok=True)
    worker.output_path.write_text("temporary crop", encoding="utf-8")

    worker.cleanup()

    assert not worker.output_path.exists()
