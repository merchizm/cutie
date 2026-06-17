from __future__ import annotations

import copy
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gdk, Gio, Gtk

from app.core.crop_worker import CropWorker
from app.core.export_worker import ExportWorker
from app.core.ffmpeg_command_builder import export_settings_from_project
from app.core.ffprobe_reader import FFprobeError, read_media_duration, read_media_info
from app.core.media_info import MediaInfo
from app.core.project_state import AudioClip, CropRecord, ProjectState
from app.utils.paths import default_output_path, open_folder
from app.utils.timecode import seconds_to_label
from app.widgets.export_dialog import DialogExportOptions, ExportSettingsDialog
from app.widgets.playback_controls import PlaybackControls
from app.widgets.preview_player import PreviewPlayer
from app.widgets.trim_timeline import TrimTimeline


class CutieWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Cutie")
        self.set_default_size(1100, 720)

        self.media_info: MediaInfo | None = None
        self.state = ProjectState()
        self.worker: ExportWorker | None = None
        self.crop_worker: CropWorker | None = None
        self.last_output_path: Path | None = None
        self.undo_stack: list[ProjectState] = []
        self.redo_stack: list[ProjectState] = []

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
        progress_group = Adw.PreferencesGroup(title="Export")
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        progress_box.append(self.progress)
        progress_box.append(self.open_folder_button)
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

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        open_button = Gtk.Button(label="Open")
        open_button.add_css_class("suggested-action")
        open_button.connect("clicked", self._choose_video)

        music_button = Gtk.Button(label="Add Music")
        music_button.connect("clicked", self._choose_audio)

        reset_original_button = Gtk.Button(label="Original")
        reset_original_button.set_tooltip_text("Reset to original video")
        reset_original_button.connect("clicked", self._reset_to_original)

        export_button = Gtk.Button()
        export_button.set_icon_name("document-send-symbolic")
        export_button.set_tooltip_text("Export")
        export_button.connect("clicked", self._start_export)

        header.pack_start(open_button)
        header.pack_start(music_button)
        header.pack_start(reset_original_button)
        header.pack_end(export_button)
        return header

    def _build_timeline_toolbar(self) -> Gtk.Box:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_hexpand(True)
        self.add_video_button = self._tool_button("list-add-symbolic", "Add video")
        self.add_video_button.connect("clicked", self._choose_video)
        self.add_audio_button = self._tool_button("audio-x-generic-symbolic", "Add audio/music")
        self.add_audio_button.connect("clicked", self._choose_audio)
        self.split_button = self._tool_button("edit-cut-symbolic", "Split at playhead")
        self.split_button.connect("clicked", self._split_at_playhead)
        self.delete_button = self._tool_button("edit-delete-symbolic", "Delete selected clip")
        self.delete_button.connect("clicked", self._delete_selected_clip)
        self.mute_button = self._tool_button("audio-volume-muted-symbolic", "Mute selected")
        self.mute_button.connect("clicked", self._mute_selected_clip)
        self.original_audio_button = self._tool_button("audio-speakers-symbolic", "Toggle original audio")
        self.original_audio_button.connect("clicked", self._toggle_original_audio)
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
            self.add_video_button,
            self.add_audio_button,
            self.split_button,
            self.delete_button,
            self.mute_button,
            self.original_audio_button,
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
            return False
        if not files:
            return False
        path = files[0].get_path()
        if path:
            dropped_path = Path(path)
            suffix = dropped_path.suffix.lower()
            if suffix in {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}:
                self._timeline_files_dropped([dropped_path], self.state.playhead_time, "audio")
            elif suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
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
        for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.avi", "*.m4v"):
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

    def _choose_audio(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative(
            title="Choose Audio",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Choose",
            cancel_label="Cancel",
        )
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        for pattern in ("*.mp3", "*.wav", "*.flac", "*.ogg", "*.m4a", "*.aac"):
            audio_filter.add_pattern(pattern)
        dialog.add_filter(audio_filter)
        dialog.connect("response", self._audio_dialog_response)
        dialog.show()

    def _audio_dialog_response(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            path = Path(file.get_path()) if file and file.get_path() else None
            if path is not None:
                try:
                    duration = read_media_duration(path)
                except FFprobeError as exc:
                    self._toast(str(exc))
                    dialog.destroy()
                    return
                self._add_audio_clip(path, duration, self.state.playhead_time)
                self._toast("Audio added to timeline.")
        dialog.destroy()

    def _load_video(self, path: Path) -> None:
        pending_audio = copy.deepcopy(self.state.audio_clips) if self.state.original_video_path is None else []
        try:
            info = read_media_info(path)
        except FFprobeError as exc:
            self._toast(str(exc))
            return
        self.media_info = info
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
        self.last_output_path = default_output_path(path, "mp4")
        self._set_metadata(info)
        self._toast("Video loaded.")

    def _reset_to_original(self, _button: Gtk.Button) -> None:
        if self.state.original_video_path is None:
            self._toast("Open a video first.")
            return
        if not self.state.is_cropped:
            self._toast("Already using the original video.")
            return
        self._push_undo()
        self._load_video(self.state.original_video_path)
        self._toast("Restored original video.")

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
        self.state.output_aspect_mode = options.aspect_mode
        self.state.output_width = options.output_width
        self.state.output_height = options.output_height
        if options.audio_mode == "mute":
            self.state.original_audio_enabled = False
        elif options.audio_mode == "keep":
            self.state.original_audio_enabled = True
        self.state.original_audio_track.muted = not self.state.original_audio_enabled
        for clip in self.state.original_audio_track.clips:
            clip.muted = not self.state.original_audio_enabled
        for clip in self.state.audio_clips:
            clip.loop = options.loop_timeline_audio
        self._sync_timeline_audio()
        try:
            settings = export_settings_from_project(
                self.state,
                options.output_path,
                options.format_name,
                options.video_crf,
                options.output_width,
                options.output_height,
                options.audio_mode,
                options.loop_timeline_audio,
            )
        except ValueError as exc:
            self._toast(str(exc))
            return
        expected_duration = max(
            sum(segment.end - segment.start for segment in self.state.active_video_segments()),
            0.001,
        )
        self.progress.set_fraction(0)
        self.progress.set_text("Exporting")
        self.open_folder_button.set_sensitive(False)
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
        self.state.trim_video_range(start, end)
        self.timeline.set_video_segments(self.state.video_segments)
        current = self.preview.get_position()
        if current < start:
            self._seek_preview(start)
        elif current > end:
            self._seek_preview(end)

    def _crop_changed(self, enabled: bool, x: float, y: float, width: float, height: float) -> None:
        if not enabled:
            self.state.crop_enabled = False
            return
        crop = self._crop_pixels(x, y, width, height)
        if crop is None or self.state.working_video_path is None:
            self._toast("Open a video before cropping.")
            return
        self._push_undo()
        self.state.crop_enabled = True
        self.state.crop_x = x
        self.state.crop_y = y
        self.state.crop_width = width
        self.state.crop_height = height
        self.progress.set_fraction(0)
        self.progress.set_text("Cropping")
        self.crop_worker = CropWorker(
            self.state.working_video_path,
            crop,
            self.state.working_duration,
            lambda success, path, message: GLib.idle_add(
                self._crop_done,
                success,
                path,
                message,
                self.state.working_video_path,
                x,
                y,
                width,
                height,
            ),
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
        self.progress.set_text("Idle")
        if not success or path is None:
            self._discard_empty_undo()
            self._toast(message)
            return False
        try:
            info = read_media_info(path)
        except FFprobeError as exc:
            self._toast(str(exc))
            return False
        self.media_info = info
        self.state.apply_working_video(
            path,
            info.duration,
            info.width,
            info.height,
            CropRecord(source_path, path, x, y, width, height),
        )
        self.preview.load_file(str(path), info.duration)
        self.preview.set_source_size(info.width, info.height)
        self.preview.set_crop(False, 0.0, 0.0, 1.0, 1.0)
        self.playback.set_duration(info.duration)
        self.timeline.set_duration(info.duration)
        self.timeline.set_video_name(path.name)
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        self._set_metadata(info)
        self._toast("Crop applied to working video.")
        return False

    def _clip_selected(self, clip_id: str) -> None:
        self.state.select_clip(clip_id)
        self.timeline.set_video_segments(self.state.video_segments)
        self.timeline.set_audio_clips(self.state.audio_clips, self.state.selected_clip_id)

    def _clip_moved(self, clip_id: str, timeline_start: float) -> None:
        self._push_undo()
        if self.state.move_clip(clip_id, timeline_start):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
        else:
            self._discard_empty_undo()

    def _clip_trimmed(self, clip_id: str, side: str, seconds: float) -> None:
        self._push_undo()
        changed = (
            self.state.trim_clip_left(clip_id, seconds)
            if side == "left"
            else self.state.trim_clip_right(clip_id, seconds)
        )
        if changed:
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
        else:
            self._discard_empty_undo()

    def _delete_selected_clip(self, _button: Gtk.Button) -> None:
        self._push_undo()
        if self.state.delete_selected_clip():
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._toast("Selected clip removed.")
        else:
            self._discard_empty_undo()

    def _mute_selected_clip(self, _button: Gtk.Button) -> None:
        self._push_undo()
        if self.state.toggle_selected_audio_mute():
            self._sync_timeline_audio()
            self._toast("Mute toggled.")
        else:
            self._discard_empty_undo()
            self._toast("Select an audio clip or video with linked audio.")

    def _split_at_playhead(self, _button: Gtk.Button) -> None:
        if self.media_info is None:
            self._toast("Open a video first.")
            return
        self._push_undo()
        if self.state.split_at(self.state.playhead_time):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._toast(f"Split at {seconds_to_label(self.state.playhead_time)}.")
        else:
            self._discard_empty_undo()
            self._toast("Move the playhead inside a video segment to split.")

    def _toggle_original_audio(self, _button: Gtk.Button) -> None:
        if not self.state.source_has_audio:
            self._toast("This video has no original audio.")
            return
        self._push_undo()
        self.state.original_audio_enabled = not self.state.original_audio_enabled
        self.state.original_audio_track.muted = not self.state.original_audio_enabled
        for clip in self.state.original_audio_track.clips:
            clip.muted = not self.state.original_audio_enabled
        self._sync_timeline_audio()
        self._toast("Original audio on." if self.state.original_audio_enabled else "Original audio muted.")

    def _sync_timeline_audio(self) -> None:
        mode = "keep" if self.state.original_audio_enabled else "mute"
        self.timeline.update_duration(self.state.duration or self.state.working_duration)
        self.timeline.set_audio_state(mode, None, self.state.source_has_audio)
        self.timeline.set_original_audio_clips(self.state.original_audio_track.clips)
        self.timeline.set_audio_clips(self.state.audio_clips, self.state.selected_clip_id)

    def _timeline_files_dropped(self, paths: list[Path], seconds: float, track_type: str) -> None:
        path = paths[0]
        if track_type == "audio" or path.suffix.lower() in {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}:
            try:
                duration = read_media_duration(path)
            except FFprobeError as exc:
                self._toast(str(exc))
                return
            self._add_audio_clip(path, duration, seconds)
            self._toast("Audio added to timeline.")
            return
        if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            if self.media_info is None:
                self._load_video(path)
            else:
                self._toast("Only one video source is supported for now.")

    def _add_audio_clip(self, path: Path, duration: float, seconds: float) -> AudioClip:
        self._push_undo()
        clip = self.state.add_audio_clip(path, duration, seconds)
        self.timeline.set_video_segments(self.state.video_segments)
        self._sync_timeline_audio()
        return clip

    def _push_undo(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.state))
        self.redo_stack.clear()

    def _discard_empty_undo(self) -> None:
        if self.undo_stack:
            self.undo_stack.pop()

    def _undo(self) -> None:
        if not self.undo_stack:
            self._toast("Nothing to undo.")
            return
        self.redo_stack.append(copy.deepcopy(self.state))
        self.state = self.undo_stack.pop()
        self._restore_state_to_ui()
        self._toast("Undo.")

    def _redo(self) -> None:
        if not self.redo_stack:
            self._toast("Nothing to redo.")
            return
        self.undo_stack.append(copy.deepcopy(self.state))
        self.state = self.redo_stack.pop()
        self._restore_state_to_ui()
        self._toast("Redo.")

    def _restore_state_to_ui(self) -> None:
        if self.state.working_video_path:
            try:
                self.media_info = read_media_info(self.state.working_video_path)
                self._set_metadata(self.media_info)
            except FFprobeError:
                pass
            self.preview.load_file(str(self.state.working_video_path), self.state.working_duration)
            self.preview.set_source_size(self.state.working_width, self.state.working_height)
            self.playback.set_duration(self.state.working_duration)
            self.timeline.set_duration(self.state.duration or self.state.working_duration)
            self.timeline.set_video_name(self.state.working_video_path.name)
        self.preview.set_crop(False, 0.0, 0.0, 1.0, 1.0)
        self.timeline.set_video_segments(self.state.video_segments)
        self.timeline.set_position(self.state.playhead_time)
        self._sync_timeline_audio()

    def _crop_pixels(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> tuple[int, int, int, int] | None:
        if self.state.working_width <= 0 or self.state.working_height <= 0:
            return None
        pixel_x = _even(int(round(x * self.state.working_width)))
        pixel_y = _even(int(round(y * self.state.working_height)))
        pixel_width = _even(int(round(width * self.state.working_width)))
        pixel_height = _even(int(round(height * self.state.working_height)))
        pixel_width = max(2, min(pixel_width, self.state.working_width - pixel_x))
        pixel_height = max(2, min(pixel_height, self.state.working_height - pixel_y))
        return pixel_x, pixel_y, pixel_width, pixel_height

    def _open_output_folder(self, _button: Gtk.Button) -> None:
        if self.last_output_path is not None:
            open_folder(self.last_output_path)

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


def _even(value: int) -> int:
    value = max(value, 0)
    return value if value % 2 == 0 else value - 1


def main() -> int:
    app = CutieApplication()
    return app.run(None)
