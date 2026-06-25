from __future__ import annotations

import copy
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gdk, Gio, Gtk

from app.core.crop_worker import CropWorker
from app.core.commands import (
    AddAudioClipCommand,
    ApplyWorkingVideoCommand,
    DeleteSelectedClipCommand,
    MoveClipCommand,
    ResetToOriginalVideoCommand,
    SplitAtCommand,
    ToggleOriginalAudioCommand,
    TrimClipCommand,
    TrimVideoRangeCommand,
)
from app.core.export_worker import ExportWorker
from app.core.ffmpeg_command_builder import export_settings_from_project, project_state_for_export
from app.core.ffprobe_reader import FFprobeError, read_media_duration, read_media_info
from app.core.media_info import MediaInfo
from app.core.project_references import referenced_media_paths
from app.core.project_state import AudioClip, CropRecord, ProjectState
from app.core.undo_history import UndoHistory
from app.utils.numbers import even_floor
from app.utils.logging_config import configure_logging
from app.utils.paths import default_output_path, open_folder
from app.utils.timecode import seconds_to_label
from app.widgets.export_dialog import DialogExportOptions, ExportSettingsDialog
from app.widgets.playback_controls import PlaybackControls
from app.widgets.preview_player import PreviewPlayer
from app.widgets.trim_timeline import TrimTimeline


logger = logging.getLogger(__name__)
UNDO_LIMIT = 50
AUDIO_SUFFIXES = {".mp3", ".aac", ".ogg", ".opus", ".flac", ".wav", ".m4a", ".wma", ".aiff", ".aif"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts"}


class CutieWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Cutie")
        self.set_default_size(1100, 720)

        self.media_info: MediaInfo | None = None
        self.state = ProjectState()
        self.worker: ExportWorker | None = None
        self.crop_worker: CropWorker | None = None
        self.probe_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cutie-probe")
        self.last_output_path: Path | None = None
        self.owned_temp_files: set[Path] = set()
        self.undo_history = UndoHistory(UNDO_LIMIT)
        self.video_has_audio = False

        self.toast_overlay = Adw.ToastOverlay()
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self._build_header())
        toolbar_view.set_content(self.toast_overlay)
        self.set_content(toolbar_view)

        self.preview = PreviewPlayer()
        self.playback = PlaybackControls()
        self.timeline = TrimTimeline()
        self.preview.on_position_changed = self._preview_position_changed
        self.preview.on_crop_changed = self._crop_changed
        self.preview.on_crop_mode_changed = self._crop_mode_changed
        self.playback.on_seek = self._seek_preview
        self.playback.on_toggle_playback = self.preview.toggle_playback
        self.timeline.on_seek = self._seek_preview
        self.timeline.on_trim_changed = self._trim_changed
        self.timeline.on_clip_selected = self._clip_selected
        self.timeline.on_clip_moved = self._clip_moved
        self.timeline.on_clip_trimmed = self._clip_trimmed
        self.timeline.on_files_dropped = self._timeline_files_dropped

        self.metadata_group = Adw.PreferencesGroup(title="Video")
        self.metadata_rows: dict[str, Adw.ActionRow] = {}
        for label in ("Filename", "Duration", "Resolution", "FPS", "Codec", "File size"):
            row = Adw.ActionRow(title=label)
            self.metadata_group.add(row)
            self.metadata_rows[label] = row

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("Idle")
        self.open_folder_button = Gtk.Button(label="Open Folder")
        self.open_folder_button.set_sensitive(False)
        self.open_folder_button.connect("clicked", self._open_output_folder)
        self.cancel_export_button = Gtk.Button(label="Cancel Export")
        self.cancel_export_button.set_sensitive(False)
        self.cancel_export_button.connect("clicked", self._cancel_export)
        progress_group = Adw.PreferencesGroup(title="Export")
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        progress_box.append(self.progress)
        progress_box.append(self.open_folder_button)
        progress_box.append(self.cancel_export_button)
        progress_group.add(progress_box)

        side_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        side_panel.set_margin_top(18)
        side_panel.set_margin_bottom(18)
        side_panel.set_margin_start(18)
        side_panel.set_margin_end(18)
        side_panel.append(self.metadata_group)
        side_panel.append(progress_group)

        main_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        main_column.set_margin_top(18)
        main_column.set_margin_bottom(18)
        main_column.set_margin_start(18)
        main_column.set_margin_end(0)
        main_column.append(self.preview)
        main_column.append(self.playback)
        main_column.append(self._build_timeline_toolbar())
        main_column.append(self.timeline)

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_start_child(main_column)
        split.set_end_child(side_panel)
        split.set_resize_start_child(True)
        split.set_resize_end_child(False)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)
        split.set_position(760)

        self.toast_overlay.set_child(split)
        self._install_drop_target()
        self._install_shortcuts()
        self._set_empty_metadata()
        self.connect("close-request", self._close_request)

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        open_button = Gtk.Button(label="Open")
        open_button.add_css_class("suggested-action")
        open_button.connect("clicked", self._choose_video)

        media_button = Gtk.Button(label="Add Media")
        media_button.set_icon_name("list-add-symbolic")
        media_button.set_tooltip_text("Add a video or audio file to the timeline")
        media_button.connect("clicked", self._choose_media)

        export_button = Gtk.Button()
        export_button.set_icon_name("document-send-symbolic")
        export_button.set_tooltip_text("Export")
        export_button.connect("clicked", self._start_export)

        header.pack_start(open_button)
        header.pack_start(media_button)
        header.pack_end(export_button)
        return header

    def _build_timeline_toolbar(self) -> Gtk.Box:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_hexpand(True)
        self.add_media_button = self._tool_button("list-add-symbolic", "Add a video or audio file to the timeline")
        self.add_media_button.connect("clicked", self._choose_media)
        self.crop_button = Gtk.ToggleButton(label="Crop")
        self.crop_button.set_tooltip_text("Crop video resolution")
        self.crop_button.connect("toggled", self._crop_button_toggled)
        self.split_button = self._tool_button("edit-cut-symbolic", "Split at playhead")
        self.split_button.connect("clicked", self._split_at_playhead)
        self.delete_button = self._tool_button("edit-delete-symbolic", "Delete selected clip")
        self.delete_button.connect("clicked", self._delete_selected_clip)
        self.original_audio_button = self._tool_button("audio-volume-high-symbolic", "Toggle original audio")
        self.original_audio_button.connect("clicked", self._toggle_original_audio)
        self.reset_original_button = self._tool_button("edit-clear-symbolic", "Reset to original video")
        self.reset_original_button.connect("clicked", self._reset_to_original)
        self.zoom_out_button = self._tool_button("zoom-out-symbolic", "Zoom out timeline")
        self.zoom_out_button.connect("clicked", lambda _button: self.timeline.zoom_out())
        self.zoom_in_button = self._tool_button("zoom-in-symbolic", "Zoom in timeline")
        self.zoom_in_button.connect("clicked", lambda _button: self.timeline.zoom_in())
        self.fit_button = Gtk.Button(label="Fit")
        self.fit_button.set_tooltip_text("Fit timeline to view")
        self.fit_button.connect("clicked", lambda _button: self.timeline.fit())
        self.undo_button = self._tool_button("edit-undo-symbolic", "Undo")
        self.undo_button.connect("clicked", lambda _button: self._undo())
        self.redo_button = self._tool_button("edit-redo-symbolic", "Redo")
        self.redo_button.connect("clicked", lambda _button: self._redo())
        for child in (
            self.add_media_button,
            self.crop_button,
            self.preview.crop_tools_revealer,
            self.split_button,
            self.delete_button,
            self.original_audio_button,
            self.reset_original_button,
            self.undo_button,
            self.redo_button,
            self.zoom_out_button,
            self.zoom_in_button,
            self.fit_button,
        ):
            toolbar.append(child)
        return toolbar

    def _tool_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button()
        button.set_icon_name(icon_name)
        button.set_tooltip_text(tooltip)
        return button

    def _install_drop_target(self) -> None:
        try:
            target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        except AttributeError:
            logger.debug("Gtk.DropTarget FileList is not available; disabling window drop target.")
            return
        target.connect("drop", self._handle_drop)
        self.add_controller(target)

    def _install_shortcuts(self) -> None:
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._key_pressed)
        self.add_controller(controller)

    def _key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        key = Gdk.keyval_name(keyval) or ""
        if key == "space":
            self.preview.toggle_playback()
            return True
        if key in ("Delete", "BackSpace"):
            self._delete_selected_clip(self.delete_button)
            return True
        if key in ("s", "S") or (ctrl and key.lower() == "b"):
            self._split_at_playhead(self.split_button)
            return True
        if ctrl and key.lower() == "z" and not shift:
            self._undo()
            return True
        if (ctrl and shift and key.lower() == "z") or (ctrl and key.lower() == "y"):
            self._redo()
            return True
        if key in ("plus", "KP_Add", "asterisk"):
            self.timeline.zoom_in()
            return True
        if key in ("minus", "KP_Subtract"):
            self.timeline.zoom_out()
            return True
        if key == "Home":
            self._seek_preview(0.0)
            return True
        if key == "End":
            self._seek_preview(self.state.duration)
            return True
        return False

    def _handle_drop(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        try:
            files = value.get_files()
        except AttributeError:
            logger.debug("Drop value does not expose get_files().")
            return False
        if not files:
            return False
        path = files[0].get_path()
        if path:
            dropped_path = Path(path)
            suffix = dropped_path.suffix.lower()
            if suffix in AUDIO_SUFFIXES:
                self._timeline_files_dropped([dropped_path], self.state.playhead_time, "audio")
            elif suffix in VIDEO_SUFFIXES:
                self._timeline_files_dropped([dropped_path], self.state.playhead_time, "video")
            else:
                self._toast("Unsupported file type.")
            return True
        return False

    def _choose_video(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative(
            title="Open Video",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Open",
            cancel_label="Cancel",
        )
        video_filter = Gtk.FileFilter()
        video_filter.set_name("Video files")
        for pattern in ("*.mp4", "*.mkv", "*.mov", "*.avi", "*.webm", "*.flv", "*.wmv", "*.m4v", "*.ts"):
            video_filter.add_pattern(pattern)
        dialog.add_filter(video_filter)
        dialog.connect("response", self._video_dialog_response)
        dialog.show()

    def _video_dialog_response(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            path = Path(file.get_path()) if file and file.get_path() else None
            if path:
                self._load_video(path)
        dialog.destroy()

    def _choose_media(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative(
            title="Add Media",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Add",
            cancel_label="Cancel",
        )
        media_filter = Gtk.FileFilter()
        media_filter.set_name("Audio and video files")
        for pattern in (
            "*.mp3", "*.aac", "*.ogg", "*.opus", "*.flac", "*.wav", "*.m4a", "*.wma", "*.aiff", "*.aif",
            "*.mp4", "*.mkv", "*.mov", "*.avi", "*.webm", "*.flv", "*.wmv", "*.m4v", "*.ts",
        ):
            media_filter.add_pattern(pattern)
        dialog.add_filter(media_filter)
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        for pattern in ("*.mp3", "*.aac", "*.ogg", "*.opus", "*.flac", "*.wav", "*.m4a", "*.wma", "*.aiff", "*.aif"):
            audio_filter.add_pattern(pattern)
        dialog.add_filter(audio_filter)
        video_filter = Gtk.FileFilter()
        video_filter.set_name("Video files")
        for pattern in ("*.mp4", "*.mkv", "*.mov", "*.avi", "*.webm", "*.flv", "*.wmv", "*.m4v", "*.ts"):
            video_filter.add_pattern(pattern)
        dialog.add_filter(video_filter)
        dialog.connect("response", self._media_dialog_response)
        dialog.show()

    def _media_dialog_response(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            path = Path(file.get_path()) if file and file.get_path() else None
            if path is not None:
                suffix = path.suffix.lower()
                if suffix in AUDIO_SUFFIXES:
                    self._load_audio_clip(path, self.state.playhead_time)
                elif suffix in VIDEO_SUFFIXES:
                    if self.media_info is None:
                        self._load_video(path)
                    else:
                        self._toast("Only one video source is supported for now.")
                else:
                    self._toast("Unsupported file type.")
        dialog.destroy()

    def _load_video(self, path: Path) -> None:
        pending_audio = copy.deepcopy(self.state.audio_clips) if self.state.original_video_path is None else []
        self._toast("Reading video metadata.")
        future = self.probe_executor.submit(read_media_info, path)
        future.add_done_callback(
            lambda done: GLib.idle_add(self._video_info_loaded, done, path, pending_audio)
        )

    def _video_info_loaded(
        self,
        future: Future[MediaInfo],
        path: Path,
        pending_audio: list[AudioClip],
    ) -> bool:
        try:
            info = future.result()
        except FFprobeError as exc:
            self._toast(str(exc))
            return False
        except Exception:
            logger.exception("Failed to read video metadata: %s", path)
            self._toast("Could not read video metadata.")
            return False
        self.media_info = info
        self.video_has_audio = info.has_audio
        self.state.load_source(path, info.duration, info.width, info.height, info.has_audio)
        if pending_audio:
            self.state.audio_clips = pending_audio
            self.state.original_audio_enabled = False
            self.state.original_audio_track.muted = True
            self.state.select_clip(pending_audio[0].id)
        self.preview.load_file(str(path), info.duration)
        self.preview.set_source_size(info.width, info.height)
        self.preview.set_crop(False, 0.0, 0.0, 1.0, 1.0)
        self.playback.set_duration(info.duration)
        self.timeline.set_duration(info.duration)
        self.timeline.set_video_name(info.filename)
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        self._sync_original_audio_controls()
        self.last_output_path = default_output_path(path, "mp4")
        self._set_metadata(info)
        self._toast("Video loaded.")
        return False

    def _load_audio_clip(self, path: Path, seconds: float) -> None:
        self._toast("Reading audio metadata.")
        future = self.probe_executor.submit(read_media_duration, path)
        future.add_done_callback(
            lambda done: GLib.idle_add(self._audio_duration_loaded, done, path, seconds)
        )

    def _audio_duration_loaded(
        self,
        future: Future[float],
        path: Path,
        seconds: float,
    ) -> bool:
        try:
            duration = future.result()
        except FFprobeError as exc:
            self._toast(str(exc))
            return False
        except Exception:
            logger.exception("Failed to read audio metadata: %s", path)
            self._toast("Could not read audio metadata.")
            return False
        self._add_audio_clip(path, duration, seconds)
        self._toast("Audio added to timeline.")
        return False

    def _reset_to_original(self, _button: Gtk.Button) -> None:
        if self.state.original_video_path is None:
            self._toast("Open a video first.")
            return
        if not self.state.is_cropped:
            self._toast("Already using the original video.")
            return
        path = self.state.original_video_path
        self._toast("Reading original video metadata.")
        future = self.probe_executor.submit(read_media_info, path)
        future.add_done_callback(
            lambda done: GLib.idle_add(self._original_video_info_loaded, done, path)
        )

    def _original_video_info_loaded(self, future: Future[MediaInfo], path: Path) -> bool:
        try:
            info = future.result()
        except FFprobeError as exc:
            self._toast(str(exc))
            return False
        except Exception:
            logger.exception("Failed to read original video metadata: %s", path)
            self._toast("Could not read original video metadata.")
            return False
        if not self.undo_history.execute(
            self.state,
            ResetToOriginalVideoCommand(path, info.duration, info.width, info.height, info.has_audio),
        ):
            self._toast("Already using the original video.")
            return False
        self.media_info = info
        self.video_has_audio = info.has_audio
        self.preview.load_file(str(path), info.duration)
        self.preview.set_source_size(info.width, info.height)
        self.preview.set_crop(False, 0.0, 0.0, 1.0, 1.0)
        self.playback.set_duration(info.duration)
        self.timeline.update_duration(info.duration)
        self.timeline.set_video_name(path.name)
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        self._sync_original_audio_controls()
        self.last_output_path = default_output_path(path, "mp4")
        self._set_metadata(info)
        self._cleanup_unreferenced_temp_files()
        self._toast("Restored original video.")
        return False

    def _start_export(self, _button: Gtk.Button) -> None:
        if self.media_info is None:
            self._toast("Open a video first.")
            return
        default_path = self.last_output_path or default_output_path(self.media_info.path, "mp4")
        dialog = ExportSettingsDialog(
            self,
            default_path,
            bool(self.state.active_audio_clip()),
            self.state.original_audio_enabled,
            self._begin_export,
        )
        dialog.present()

    def _begin_export(self, options: DialogExportOptions) -> None:
        export_state = project_state_for_export(
            self.state,
            options.output_width,
            options.output_height,
            options.audio_mode,
            options.loop_timeline_audio,
        )
        export_state.output_aspect_mode = options.aspect_mode
        try:
            settings = export_settings_from_project(
                export_state,
                options.output_path,
                options.format_name,
                options.video_crf,
                options.output_width,
                options.output_height,
                options.audio_mode,
                options.loop_timeline_audio,
                video_encoder=options.video_encoder,
                video_preset=options.video_preset,
                target_video_bitrate_kbps=options.target_video_bitrate_kbps,
                framerate=options.framerate,
                audio_codec=options.audio_codec,
                audio_bitrate_kbps=options.audio_bitrate_kbps,
            )
        except ValueError as exc:
            self._toast(str(exc))
            return
        expected_duration = max(
            sum(segment.end - segment.start for segment in export_state.active_video_segments()),
            0.001,
        )
        self.progress.set_fraction(0)
        self.progress.set_text("Exporting")
        self.open_folder_button.set_sensitive(False)
        self.cancel_export_button.set_sensitive(True)
        self.last_output_path = options.output_path
        self.worker = ExportWorker(
            settings,
            expected_duration,
            lambda progress: GLib.idle_add(self._set_progress, progress),
            lambda success, message: GLib.idle_add(self._export_done, success, message),
        )
        self.worker.start()

    def _export_done(self, success: bool, message: str) -> bool:
        self.progress.set_text("Done" if success else "Failed")
        self.open_folder_button.set_sensitive(success and self.last_output_path is not None)
        self.cancel_export_button.set_sensitive(False)
        self.worker = None
        self._toast(message)
        return False

    def _set_progress(self, fraction: float) -> bool:
        fraction = max(0.0, min(fraction, 1.0))
        self.progress.set_fraction(fraction)
        self.progress.set_text(f"{fraction * 100:.0f}%")
        return False

    def _seek_preview(self, seconds: float) -> None:
        self.state.playhead_time = seconds
        self.preview.seek(seconds)
        self.timeline.set_position(seconds)
        self.playback.update_position(seconds, self.preview.is_playing())

    def _preview_position_changed(self, seconds: float, is_playing: bool) -> None:
        self.state.playhead_time = seconds
        self.timeline.set_position(seconds)
        self.playback.update_position(seconds, is_playing)

    def _trim_changed(self, start: float, end: float) -> None:
        changed = self.undo_history.execute(self.state, TrimVideoRangeCommand(start, end))
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        if changed:
            self._cleanup_unreferenced_temp_files()
        current = self.preview.get_position()
        if current < start:
            self._seek_preview(start)
        elif current > end:
            self._seek_preview(end)

    def _crop_changed(self, enabled: bool, x: float, y: float, width: float, height: float) -> None:
        if not enabled:
            self.state.clear_pending_crop()
            return
        crop = self._crop_pixels(x, y, width, height)
        if crop is None or self.state.working_video_path is None:
            self.state.clear_pending_crop()
            self._toast("Open a video before cropping.")
            return
        self.state.set_pending_crop(x, y, width, height)
        self.progress.set_fraction(0)
        self.progress.set_text("Cropping")
        source_path = self.state.working_video_path

        def on_crop_done(success: bool, path: Path | None, message: str) -> None:
            self._crop_done(success, path, message, source_path, x, y, width, height)

        self.crop_worker = CropWorker(
            source_path,
            crop,
            self.state.working_duration,
            on_crop_done,
            dispatch_done=lambda callback, success, path, message: GLib.idle_add(callback, success, path, message),
        )
        self.crop_worker.start()

    def _crop_done(
        self,
        success: bool,
        path: Path | None,
        message: str,
        source_path: Path,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> bool:
        self.crop_worker = None
        if not success or path is None:
            self.progress.set_text("Idle")
            self.state.clear_pending_crop()
            self._toast(message)
            return False
        self.owned_temp_files.add(path)
        self.progress.set_text("Reading crop metadata")
        future = self.probe_executor.submit(read_media_info, path)
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._crop_output_info_loaded,
                done,
                path,
                source_path,
                x,
                y,
                width,
                height,
            )
        )
        return False

    def _crop_output_info_loaded(
        self,
        future: Future[MediaInfo],
        path: Path,
        source_path: Path,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> bool:
        self.progress.set_text("Idle")
        try:
            info = future.result()
        except FFprobeError as exc:
            logger.exception("Failed to inspect cropped video: %s", path)
            self.state.clear_pending_crop()
            self._cleanup_unreferenced_temp_files()
            self._toast(str(exc))
            return False
        except Exception:
            logger.exception("Failed to inspect cropped video: %s", path)
            self.state.clear_pending_crop()
            self._cleanup_unreferenced_temp_files()
            self._toast("Could not read cropped video metadata.")
            return False
        self.media_info = info
        self.undo_history.execute(
            self.state,
            ApplyWorkingVideoCommand(
                path,
                info.duration,
                info.width,
                info.height,
                CropRecord(source_path, path, x, y, width, height),
            ),
        )
        self._cleanup_unreferenced_temp_files()
        self.preview.load_file(str(path), info.duration)
        self.preview.set_source_size(info.width, info.height)
        self.preview.set_crop(False, 0.0, 0.0, 1.0, 1.0)
        self.playback.set_duration(info.duration)
        self.timeline.set_duration(info.duration)
        self.timeline.set_video_name(path.name)
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        self._sync_original_audio_controls()
        self._set_metadata(info)
        self._toast("Crop applied to working video.")
        return False

    def _clip_selected(self, clip_id: str) -> None:
        self.state.select_clip(clip_id)
        self.timeline.set_video_segments(self.state.video_segments)
        self.timeline.set_audio_clips(self.state.audio_clips, self.state.selected_clip_id)

    def _clip_moved(self, clip_id: str, timeline_start: float) -> None:
        if self.undo_history.execute(self.state, MoveClipCommand(clip_id, timeline_start)):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._cleanup_unreferenced_temp_files()
        else:
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()

    def _clip_trimmed(self, clip_id: str, side: str, seconds: float) -> None:
        if self.undo_history.execute(self.state, TrimClipCommand(clip_id, side, seconds)):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._cleanup_unreferenced_temp_files()
        else:
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()

    def _delete_selected_clip(self, _button: Gtk.Button) -> None:
        if self.undo_history.execute(self.state, DeleteSelectedClipCommand()):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._cleanup_unreferenced_temp_files()
            self._toast("Selected clip removed.")
        else:
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()

    def _split_at_playhead(self, _button: Gtk.Button) -> None:
        if self.media_info is None:
            self._toast("Open a video first.")
            return
        if self.undo_history.execute(self.state, SplitAtCommand(self.state.playhead_time)):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._cleanup_unreferenced_temp_files()
            self._toast(f"Split at {seconds_to_label(self.state.playhead_time)}.")
        else:
            self._toast("Move the playhead inside a video segment to split.")

    def _toggle_original_audio(self, _button: Gtk.Button) -> None:
        if not self.video_has_audio:
            self._toast("This video has no original audio.")
            return
        if not self.undo_history.execute(self.state, ToggleOriginalAudioCommand()):
            self._toast("This video has no original audio.")
            return
        self._sync_timeline_audio()
        self._cleanup_unreferenced_temp_files()
        self._toast("Original audio on." if self.state.original_audio_enabled else "Original audio muted.")

    def _sync_timeline_audio(self) -> None:
        mode = "keep" if self.state.original_audio_enabled else "mute"
        self.timeline.update_duration(self.state.duration or self.state.working_duration)
        self.timeline.set_audio_state(mode, None, self.state.source_has_audio)
        self.timeline.set_original_audio_clips(self.state.original_audio_track.clips)
        self.timeline.set_audio_clips(self.state.audio_clips, self.state.selected_clip_id)
        self.preview.set_timeline_audio_clips(self.state.active_audio_clips())
        self.preview.set_original_audio_preview_enabled(self.state.original_audio_enabled and self.state.source_has_audio)
        self._sync_original_audio_controls()

    def _sync_original_audio_controls(self) -> None:
        enabled = self.video_has_audio and self.state.source_has_audio
        self.original_audio_button.set_sensitive(enabled)
        self.original_audio_button.set_tooltip_text(
            "Toggle original audio" if enabled else "This video has no original audio"
        )

    def _timeline_files_dropped(self, paths: list[Path], seconds: float, track_type: str) -> None:
        path = paths[0]
        if track_type == "audio" or path.suffix.lower() in AUDIO_SUFFIXES:
            self._load_audio_clip(path, seconds)
            return
        if path.suffix.lower() in VIDEO_SUFFIXES:
            if self.media_info is None:
                self._load_video(path)
            else:
                self._toast("Only one video source is supported for now.")

    def _crop_button_toggled(self, button: Gtk.ToggleButton) -> None:
        self.preview.set_crop_mode(button.get_active())

    def _crop_mode_changed(self, enabled: bool) -> None:
        if self.crop_button.get_active() != enabled:
            self.crop_button.set_active(enabled)

    def _add_audio_clip(self, path: Path, duration: float, seconds: float) -> AudioClip:
        command = AddAudioClipCommand(path, duration, seconds)
        if not self.undo_history.execute(self.state, command):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            raise RuntimeError("Could not add audio clip.")
        clip = command.added_clip
        if clip is None:
            clip = self.state.audio_clips[-1]
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        self._cleanup_unreferenced_temp_files()
        return clip

    def _undo(self) -> None:
        previous = self.undo_history.undo(self.state)
        if previous is None:
            self._toast("Nothing to undo.")
            return
        self.state = previous
        self._cleanup_unreferenced_temp_files()
        self._restore_state_to_ui()
        self._toast("Undo.")

    def _redo(self) -> None:
        next_state = self.undo_history.redo(self.state)
        if next_state is None:
            self._toast("Nothing to redo.")
            return
        self.state = next_state
        self._cleanup_unreferenced_temp_files()
        self._restore_state_to_ui()
        self._toast("Redo.")

    def _restore_state_to_ui(self) -> None:
        if self.state.working_video_path:
            path = self.state.working_video_path
            future = self.probe_executor.submit(read_media_info, path)
            future.add_done_callback(
                lambda done: GLib.idle_add(self._restore_metadata_loaded, done, path)
            )
            self.preview.load_file(str(self.state.working_video_path), self.state.working_duration)
            self.preview.set_source_size(self.state.working_width, self.state.working_height)
            self.playback.set_duration(self.state.working_duration)
            self.timeline.set_duration(self.state.duration or self.state.working_duration)
            self.timeline.set_video_name(self.state.working_video_path.name)
        self.preview.set_crop(False, 0.0, 0.0, 1.0, 1.0)
        self.timeline.set_video_segments(self.state.video_segments)
        self.timeline.set_position(self.state.playhead_time)
        self._sync_timeline_audio()

    def _restore_metadata_loaded(self, future: Future[MediaInfo], path: Path) -> bool:
        if self.state.working_video_path != path:
            return False
        try:
            self.media_info = future.result()
        except FFprobeError:
            logger.exception("Failed to refresh media metadata during state restore: %s", path)
            return False
        except Exception:
            logger.exception("Failed to refresh media metadata during state restore: %s", path)
            return False
        self._set_metadata(self.media_info)
        return False

    def _crop_pixels(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> tuple[int, int, int, int] | None:
        if self.state.working_width <= 0 or self.state.working_height <= 0:
            return None
        pixel_x = even_floor(int(round(x * self.state.working_width)))
        pixel_y = even_floor(int(round(y * self.state.working_height)))
        pixel_width = even_floor(int(round(width * self.state.working_width)))
        pixel_height = even_floor(int(round(height * self.state.working_height)))
        pixel_width = max(2, min(pixel_width, self.state.working_width - pixel_x))
        pixel_height = max(2, min(pixel_height, self.state.working_height - pixel_y))
        return pixel_x, pixel_y, pixel_width, pixel_height

    def _open_output_folder(self, _button: Gtk.Button) -> None:
        if self.last_output_path is not None:
            if not open_folder(self.last_output_path):
                self._toast("Could not open output folder.")

    def _cancel_export(self, _button: Gtk.Button) -> None:
        if self.worker is None:
            return
        self.cancel_export_button.set_sensitive(False)
        self.progress.set_text("Cancelling")
        self.worker.cancel()

    def _set_empty_metadata(self) -> None:
        for row in self.metadata_rows.values():
            row.set_subtitle("Open a video")

    def _set_metadata(self, info: MediaInfo) -> None:
        rows = {
            "Filename": info.filename,
            "Duration": seconds_to_label(info.duration),
            "Resolution": info.resolution,
            "FPS": f"{info.fps:.2f}" if info.fps else "Unknown",
            "Codec": info.codec,
            "File size": _format_size(info.size_bytes),
        }
        for label, value in rows.items():
            self.metadata_rows[label].set_subtitle(value)

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))

    def _close_request(self, *_args: object) -> bool:
        if self.worker is not None:
            self.worker.cancel()
        if self.crop_worker is not None:
            self.crop_worker.cancel()
        self.probe_executor.shutdown(wait=False, cancel_futures=True)
        self._cleanup_temp_files()
        return False

    def _cleanup_temp_files(self) -> None:
        for path in list(self.owned_temp_files):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove temporary crop file: %s", path)
            finally:
                self.owned_temp_files.discard(path)

    def _cleanup_unreferenced_temp_files(self) -> None:
        referenced = referenced_media_paths([self.state, *self.undo_history.states()])
        referenced.update(self.undo_history.referenced_paths())
        for path in list(self.owned_temp_files):
            if path in referenced:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove unreferenced temporary crop file: %s", path)
            finally:
                self.owned_temp_files.discard(path)


class CutieApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.mce.Cutie", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = CutieWindow(self)
        window.present()


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size_bytes} B"


def main() -> int:
    configure_logging()
    app = CutieApplication()
    return app.run(None)
