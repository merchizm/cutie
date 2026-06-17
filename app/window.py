from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gdk, Gio, Gtk

from app.core.export_worker import ExportWorker
from app.core.ffmpeg_command_builder import export_settings_from_project
from app.core.ffprobe_reader import FFprobeError, read_media_duration, read_media_info
from app.core.media_info import MediaInfo
from app.core.project_state import AudioClip, ProjectState
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
        self.last_output_path: Path | None = None

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
        self._set_empty_metadata()

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        open_button = Gtk.Button(label="Open")
        open_button.add_css_class("suggested-action")
        open_button.connect("clicked", self._choose_video)

        music_button = Gtk.Button(label="Add Music")
        music_button.connect("clicked", self._choose_audio)

        export_button = Gtk.Button()
        export_button.set_icon_name("document-send-symbolic")
        export_button.set_tooltip_text("Export")
        export_button.connect("clicked", self._start_export)

        header.pack_start(open_button)
        header.pack_start(music_button)
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
        self.original_audio_button = self._tool_button("audio-speakers-symbolic", "Toggle original audio")
        self.original_audio_button.connect("clicked", self._toggle_original_audio)
        self.zoom_out_button = self._tool_button("zoom-out-symbolic", "Zoom out timeline")
        self.zoom_out_button.connect("clicked", lambda _button: self.timeline.zoom_out())
        self.zoom_in_button = self._tool_button("zoom-in-symbolic", "Zoom in timeline")
        self.zoom_in_button.connect("clicked", lambda _button: self.timeline.zoom_in())
        self.fit_button = Gtk.Button(label="Fit")
        self.fit_button.set_tooltip_text("Fit timeline to view")
        self.fit_button.connect("clicked", lambda _button: self.timeline.fit())
        for child in (
            self.add_video_button,
            self.add_audio_button,
            self.split_button,
            self.delete_button,
            self.original_audio_button,
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

    def _handle_drop(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        try:
            files = value.get_files()
        except AttributeError:
            return False
        if not files:
            return False
        path = files[0].get_path()
        if path:
            self._load_video(Path(path))
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
        if self.media_info is None:
            self._toast("Open a video before adding audio.")
            return
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
                clip = AudioClip(
                    path=path,
                    filename=path.name,
                    timeline_start=0.0,
                    duration=duration,
                    loop=True,
                )
                self.state.audio_clips = [clip]
                self.state.selected_clip_id = clip.id
                self.state.original_audio_enabled = False
                self._sync_timeline_audio()
                self._toast("Audio added to timeline.")
        dialog.destroy()

    def _load_video(self, path: Path) -> None:
        try:
            info = read_media_info(path)
        except FFprobeError as exc:
            self._toast(str(exc))
            return
        self.media_info = info
        self.state.load_source(path, info.duration, info.width, info.height, info.has_audio)
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
        for clip in self.state.audio_clips:
            clip.loop = options.loop_timeline_audio
        self._sync_timeline_audio()
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
        self.state.trim_start = start
        self.state.trim_end = end
        if self.state.video_segments:
            self.state.video_segments = [
                segment for segment in self.state.video_segments if segment.end > start and segment.start < end
            ]
            for segment in self.state.video_segments:
                segment.start = max(segment.start, start)
                segment.end = min(segment.end, end)
            if not self.state.video_segments:
                self.state.video_segments = []
        self.timeline.set_video_segments(self.state.video_segments)
        current = self.preview.get_position()
        if current < start:
            self._seek_preview(start)
        elif current > end:
            self._seek_preview(end)

    def _crop_changed(self, enabled: bool, x: float, y: float, width: float, height: float) -> None:
        self.state.crop_enabled = enabled
        self.state.crop_x = x
        self.state.crop_y = y
        self.state.crop_width = width
        self.state.crop_height = height

    def _clip_selected(self, clip_id: str) -> None:
        self.state.selected_clip_id = clip_id
        self.timeline.set_audio_clips(self.state.audio_clips, self.state.selected_clip_id)

    def _delete_selected_clip(self, _button: Gtk.Button) -> None:
        if self.state.delete_selected_clip():
            self._sync_timeline_audio()
            self._toast("Audio clip removed.")

    def _split_at_playhead(self, _button: Gtk.Button) -> None:
        if self.media_info is None:
            self._toast("Open a video first.")
            return
        if self.state.split_video_at(self.state.playhead_time):
            self.timeline.set_video_segments(self.state.video_segments)
            self._sync_timeline_audio()
            self._toast(f"Split at {seconds_to_label(self.state.playhead_time)}.")
        else:
            self._toast("Move the playhead inside a video segment to split.")

    def _toggle_original_audio(self, _button: Gtk.Button) -> None:
        if not self.state.source_has_audio:
            self._toast("This video has no original audio.")
            return
        self.state.original_audio_enabled = not self.state.original_audio_enabled
        self._sync_timeline_audio()
        self._toast("Original audio on." if self.state.original_audio_enabled else "Original audio muted.")

    def _sync_timeline_audio(self) -> None:
        mode = "keep" if self.state.original_audio_enabled else "mute"
        self.timeline.set_audio_state(mode, None, self.state.source_has_audio)
        self.timeline.set_audio_clips(self.state.audio_clips, self.state.selected_clip_id)

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


def main() -> int:
    app = CutieApplication()
    return app.run(None)
