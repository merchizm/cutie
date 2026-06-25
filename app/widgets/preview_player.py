from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cairo import Context as CairoContext
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk

Gst.init(None)


class PreviewPlayer(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        self._duration = 0.0
        self._source_width = 0
        self._source_height = 0
        self._is_playing = False
        self._crop_enabled = False
        self._crop = [0.0, 0.0, 1.0, 1.0]
        self._applied_crop_enabled = False
        self._applied_crop = self._crop.copy()
        self._aspect_ratio: float | None = None
        self._drag_mode: str | None = None
        self._drag_origin: tuple[float, float] = (0.0, 0.0)
        self._drag_crop = self._crop.copy()
        self._timeline_audio_clips: list[object] = []
        self._active_audio_clip_id: str | None = None
        self._audio_player: Gst.Element | None = None
        self.on_position_changed: Callable[[float, bool], None] | None = None
        self.on_crop_changed: Callable[[bool, float, float, float, float], None] | None = None
        self.on_crop_mode_changed: Callable[[bool], None] | None = None

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_size_request(640, 360)
        overlay.add_css_class("card")

        self.video = Gtk.Video(hexpand=True, vexpand=True)
        self.video.set_size_request(640, 360)
        self.video.set_autoplay(False)
        overlay.set_child(self.video)

        self.crop_area = Gtk.DrawingArea()
        self.crop_area.set_hexpand(True)
        self.crop_area.set_vexpand(True)
        self.crop_area.set_draw_func(self._draw_crop_overlay)
        overlay.add_overlay(self.crop_area)
        self.crop_area.set_can_target(False)

        preview_click = Gtk.GestureClick()
        preview_click.connect("released", self._preview_clicked)
        overlay.add_controller(preview_click)

        click = Gtk.GestureClick()
        click.connect("pressed", self._crop_click_pressed)
        self.crop_area.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._crop_drag_begin)
        drag.connect("drag-update", self._crop_drag_update)
        drag.connect("drag-end", self._crop_drag_end)
        self.crop_area.add_controller(drag)

        self.crop_tools_revealer = Gtk.Revealer()
        self.crop_tools_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        crop_tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        crop_tools.set_halign(Gtk.Align.CENTER)
        self.apply_crop_button = Gtk.Button(label="Apply")
        self.apply_crop_button.connect("clicked", self._apply_crop)
        self.apply_crop_button.set_visible(False)
        self.cancel_crop_button = Gtk.Button(label="Decline")
        self.cancel_crop_button.connect("clicked", self._cancel_crop)
        self.cancel_crop_button.set_visible(False)
        self.aspect_combo = Gtk.ComboBoxText()
        for item in ("Free", "1:1", "16:9", "9:16", "4:3"):
            self.aspect_combo.append_text(item)
        self.aspect_combo.set_active(0)
        self.aspect_combo.connect("changed", self._aspect_changed)
        fit_button = Gtk.Button(label="Fit")
        fit_button.connect("clicked", self._fit_crop)
        center_button = Gtk.Button(label="Center")
        center_button.connect("clicked", self._center_crop)
        reset_button = Gtk.Button(label="Reset")
        reset_button.connect("clicked", self._reset_crop)
        for child in (
            self.apply_crop_button,
            self.cancel_crop_button,
            self.aspect_combo,
            fit_button,
            center_button,
            reset_button,
        ):
            crop_tools.append(child)
        self.crop_tools_revealer.set_child(crop_tools)

        self.append(overlay)

        GLib.timeout_add(100, self._refresh_position)

    def load_file(self, path: str, duration: float) -> None:
        self._duration = max(duration, 0.0)
        self._is_playing = False
        self._stop_timeline_audio()
        self.video.set_file(Gio.File.new_for_path(path))
        self._emit_position()

    def set_source_size(self, width: int, height: int) -> None:
        self._source_width = max(width, 0)
        self._source_height = max(height, 0)
        self.crop_area.queue_draw()

    def play(self) -> None:
        stream = self.video.get_media_stream()
        if stream is None:
            return
        stream.play()
        self._is_playing = True
        self._sync_timeline_audio(self.get_position())
        self._emit_position()

    def pause(self) -> None:
        stream = self.video.get_media_stream()
        if stream is not None:
            stream.pause()
        self._is_playing = False
        self._pause_timeline_audio()
        self._emit_position()

    def toggle_playback(self) -> None:
        if self._is_playing:
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float) -> None:
        stream = self.video.get_media_stream()
        if stream is None:
            return
        seconds = min(max(seconds, 0.0), self._duration)
        stream.seek(int(seconds * 1_000_000))
        self._sync_timeline_audio(seconds)
        self._emit_position(seconds)

    def get_position(self) -> float:
        stream = self.video.get_media_stream()
        if stream is None:
            return 0.0
        timestamp = stream.get_timestamp()
        return max(timestamp / 1_000_000, 0.0) if timestamp >= 0 else 0.0

    def get_duration(self) -> float:
        return self._duration

    def is_playing(self) -> bool:
        return self._is_playing

    def _refresh_position(self) -> bool:
        if self._duration:
            current = self.get_position()
            if current >= self._duration and self._is_playing:
                self._is_playing = False
                self._pause_timeline_audio()
            elif self._is_playing:
                self._sync_timeline_audio(current)
            self._emit_position(current)
        return True

    def _emit_position(self, position: float | None = None) -> None:
        if self.on_position_changed is not None:
            self.on_position_changed(self.get_position() if position is None else position, self._is_playing)

    def set_crop(self, enabled: bool, x: float, y: float, width: float, height: float) -> None:
        self._crop_enabled = enabled
        self._crop = [x, y, width, height]
        self._applied_crop_enabled = enabled
        self._applied_crop = self._crop.copy()
        self.crop_area.set_can_target(enabled)
        self.crop_tools_revealer.set_reveal_child(enabled)
        self._set_crop_approval_visible(False)
        self.crop_area.queue_draw()
        self._emit_crop_mode()

    def set_crop_mode(self, enabled: bool) -> None:
        self._crop_enabled = enabled
        self.crop_area.set_can_target(enabled)
        self.crop_tools_revealer.set_reveal_child(enabled)
        if not enabled:
            self._drag_mode = None
            self._set_crop_approval_visible(False)
        else:
            self._stage_crop()
        self.crop_area.queue_draw()
        self._emit_crop_mode()

    def set_timeline_audio_clips(self, clips: list[object]) -> None:
        self._timeline_audio_clips = list(clips)
        self._sync_timeline_audio(self.get_position())

    def set_original_audio_preview_enabled(self, enabled: bool) -> None:
        stream = self.video.get_media_stream()
        if stream is not None and hasattr(stream, "set_muted"):
            stream.set_muted(not enabled)

    def _draw_crop_overlay(self, _area: Gtk.DrawingArea, cr: CairoContext, width: int, height: int) -> None:
        if not self._crop_enabled:
            return
        x, y, crop_w, crop_h = self._crop_rect_pixels(width, height)
        cr.set_source_rgba(0, 0, 0, 0.42)
        cr.rectangle(0, 0, width, y)
        cr.rectangle(0, y, x, crop_h)
        cr.rectangle(x + crop_w, y, width - x - crop_w, crop_h)
        cr.rectangle(0, y + crop_h, width, height - y - crop_h)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.96)
        cr.set_line_width(2)
        cr.rectangle(x, y, crop_w, crop_h)
        cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.set_line_width(1)
        for i in (1, 2):
            gx = x + crop_w * i / 3
            gy = y + crop_h * i / 3
            cr.move_to(gx, y)
            cr.line_to(gx, y + crop_h)
            cr.move_to(x, gy)
            cr.line_to(x + crop_w, gy)
        cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.95)
        for hx, hy in self._handle_points(x, y, crop_w, crop_h):
            cr.rectangle(hx - 4, hy - 4, 8, 8)
            cr.fill()

        cr.select_font_face("Sans")
        cr.set_font_size(12)
        if self._source_width and self._source_height:
            label = f"{int(self._crop[2] * self._source_width)} x {int(self._crop[3] * self._source_height)}"
        else:
            label = f"{int(self._crop[2] * 100)}% x {int(self._crop[3] * 100)}%"
        cr.move_to(x + 8, max(y + 18, 18))
        cr.show_text(label)

    def _crop_click_pressed(self, _gesture: Gtk.GestureClick, _n_press: int, x: float, y: float) -> None:
        if not self._crop_enabled:
            return
        self._drag_mode = self._hit_crop(x, y) or "draw"
        if self._drag_mode == "draw":
            px, py = self._point_to_crop(x, y)
            self._crop = [px, py, 0.01, 0.01]
            self._stage_crop()

    def _crop_drag_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if not self._crop_enabled:
            return
        self._drag_origin = (x, y)
        self._drag_crop = self._crop.copy()
        self._drag_mode = self._hit_crop(x, y) or "move"

    def _crop_drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if not self._crop_enabled:
            return
        _vx, _vy, video_w, video_h = self._video_rect(self.crop_area.get_width(), self.crop_area.get_height())
        dx = offset_x / max(video_w, 1)
        dy = offset_y / max(video_h, 1)
        x, y, crop_w, crop_h = self._drag_crop

        if self._drag_mode == "move":
            x = min(max(x + dx, 0.0), 1.0 - crop_w)
            y = min(max(y + dy, 0.0), 1.0 - crop_h)
        elif self._drag_mode in ("nw", "w", "sw"):
            new_x = min(max(x + dx, 0.0), x + crop_w - 0.05)
            crop_w = crop_w + (x - new_x)
            x = new_x
        elif self._drag_mode in ("ne", "e", "se"):
            crop_w = min(max(crop_w + dx, 0.05), 1.0 - x)

        if self._drag_mode in ("nw", "n", "ne"):
            new_y = min(max(y + dy, 0.0), y + crop_h - 0.05)
            crop_h = crop_h + (y - new_y)
            y = new_y
        elif self._drag_mode in ("sw", "s", "se"):
            crop_h = min(max(crop_h + dy, 0.05), 1.0 - y)

        if self._drag_mode == "draw":
            ox, oy = self._drag_origin
            start_x, start_y = self._point_to_crop(ox, oy)
            end_x, end_y = self._point_to_crop(ox + offset_x, oy + offset_y)
            x = min(start_x, end_x)
            y = min(start_y, end_y)
            crop_w = abs(end_x - start_x)
            crop_h = abs(end_y - start_y)

        self._crop = self._apply_aspect([x, y, crop_w, crop_h])
        self._stage_crop()

    def _crop_drag_end(self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float) -> None:
        self._drag_mode = None

    def _hit_crop(self, x: float, y: float) -> str | None:
        rx, ry, rw, rh = self._crop_rect_pixels(self.crop_area.get_width(), self.crop_area.get_height())
        handles = {
            "nw": (rx, ry),
            "n": (rx + rw / 2, ry),
            "ne": (rx + rw, ry),
            "e": (rx + rw, ry + rh / 2),
            "se": (rx + rw, ry + rh),
            "s": (rx + rw / 2, ry + rh),
            "sw": (rx, ry + rh),
            "w": (rx, ry + rh / 2),
        }
        for name, (hx, hy) in handles.items():
            if abs(x - hx) <= 12 and abs(y - hy) <= 12:
                return name
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return "move"
        return None

    def _handle_points(self, x: float, y: float, width: float, height: float) -> list[tuple[float, float]]:
        return [
            (x, y),
            (x + width / 2, y),
            (x + width, y),
            (x + width, y + height / 2),
            (x + width, y + height),
            (x + width / 2, y + height),
            (x, y + height),
            (x, y + height / 2),
        ]

    def _crop_rect_pixels(self, width: int, height: int) -> tuple[float, float, float, float]:
        video_x, video_y, video_w, video_h = self._video_rect(width, height)
        return (
            video_x + self._crop[0] * video_w,
            video_y + self._crop[1] * video_h,
            self._crop[2] * video_w,
            self._crop[3] * video_h,
        )

    def _video_rect(self, width: int, height: int) -> tuple[float, float, float, float]:
        if self._source_width <= 0 or self._source_height <= 0:
            return 0.0, 0.0, float(width), float(height)
        source_ratio = self._source_width / self._source_height
        area_ratio = max(width, 1) / max(height, 1)
        if area_ratio > source_ratio:
            video_h = float(height)
            video_w = video_h * source_ratio
            video_x = (width - video_w) / 2
            return video_x, 0.0, video_w, video_h
        video_w = float(width)
        video_h = video_w / source_ratio
        video_y = (height - video_h) / 2
        return 0.0, video_y, video_w, video_h

    def _point_to_crop(self, x: float, y: float) -> tuple[float, float]:
        video_x, video_y, video_w, video_h = self._video_rect(
            self.crop_area.get_width(),
            self.crop_area.get_height(),
        )
        return (
            min(max((x - video_x) / max(video_w, 1), 0.0), 1.0),
            min(max((y - video_y) / max(video_h, 1), 0.0), 1.0),
        )

    def _aspect_changed(self, combo: Gtk.ComboBoxText) -> None:
        label = combo.get_active_text() or "Free"
        self._aspect_ratio = {
            "1:1": 1.0,
            "16:9": 16 / 9,
            "9:16": 9 / 16,
            "4:3": 4 / 3,
        }.get(label)
        self._crop = self._apply_aspect(self._crop)
        self._stage_crop()

    def _fit_crop(self, _button: Gtk.Button) -> None:
        self.set_crop_mode(True)
        self._crop = self._apply_aspect([0.0, 0.0, 1.0, 1.0])
        self._stage_crop()

    def _center_crop(self, _button: Gtk.Button) -> None:
        self.set_crop_mode(True)
        self._crop[0] = (1.0 - self._crop[2]) / 2
        self._crop[1] = (1.0 - self._crop[3]) / 2
        self._stage_crop()

    def _reset_crop(self, _button: Gtk.Button) -> None:
        self._crop_enabled = False
        self.crop_area.set_can_target(False)
        self.crop_tools_revealer.set_reveal_child(False)
        self._crop = [0.0, 0.0, 1.0, 1.0]
        self._stage_crop()
        self._emit_crop_mode()

    def _apply_aspect(self, crop: list[float]) -> list[float]:
        x, y, width, height = crop
        width = min(max(width, 0.05), 1.0)
        height = min(max(height, 0.05), 1.0)
        if self._aspect_ratio is not None:
            if self._source_width and self._source_height:
                normalized_ratio = self._aspect_ratio * self._source_height / self._source_width
            else:
                area_ratio = max(self.crop_area.get_width(), 1) / max(self.crop_area.get_height(), 1)
                normalized_ratio = self._aspect_ratio / area_ratio
            if width / height > normalized_ratio:
                width = height * normalized_ratio
            else:
                height = width / normalized_ratio
        width = min(width, 1.0)
        height = min(height, 1.0)
        x = min(max(x, 0.0), 1.0 - width)
        y = min(max(y, 0.0), 1.0 - height)
        return [x, y, width, height]

    def _stage_crop(self) -> None:
        self.crop_area.queue_draw()
        self._set_crop_approval_visible(self._crop_is_pending())

    def _apply_crop(self, _button: Gtk.Button) -> None:
        enabled = self._crop_enabled
        crop = self._crop.copy()
        self._applied_crop_enabled = self._crop_enabled
        self._applied_crop = self._crop.copy()
        self._set_crop_approval_visible(False)
        if self.on_crop_changed is not None:
            self.on_crop_changed(enabled, *crop)
        self._crop_enabled = False
        self._crop = [0.0, 0.0, 1.0, 1.0]
        self._applied_crop_enabled = False
        self._applied_crop = self._crop.copy()
        self.crop_area.set_can_target(False)
        self.crop_tools_revealer.set_reveal_child(False)
        self._emit_crop_mode()
        self.crop_area.queue_draw()

    def _cancel_crop(self, _button: Gtk.Button) -> None:
        self._crop_enabled = False
        self._crop = [0.0, 0.0, 1.0, 1.0]
        self._applied_crop_enabled = False
        self._applied_crop = self._crop.copy()
        self.crop_area.set_can_target(False)
        self.crop_tools_revealer.set_reveal_child(False)
        self._set_crop_approval_visible(False)
        self._emit_crop_mode()
        self.crop_area.queue_draw()

    def _crop_is_pending(self) -> bool:
        return self._crop_enabled != self._applied_crop_enabled or self._crop != self._applied_crop

    def _set_crop_approval_visible(self, visible: bool) -> None:
        self.apply_crop_button.set_visible(visible)
        self.cancel_crop_button.set_visible(visible)

    def _emit_crop_mode(self) -> None:
        if self.on_crop_mode_changed is not None:
            self.on_crop_mode_changed(self._crop_enabled)

    def _preview_clicked(self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float) -> None:
        if not self._crop_enabled:
            self.toggle_playback()

    def _sync_timeline_audio(self, position: float) -> None:
        clip = self._audio_clip_at(position)
        if clip is None:
            self._pause_timeline_audio()
            self._active_audio_clip_id = None
            return
        clip_id = getattr(clip, "id", None)
        if self._audio_player is None or self._active_audio_clip_id != clip_id:
            self._stop_timeline_audio()
            self._audio_player = Gst.ElementFactory.make("playbin")
            if self._audio_player is None:
                return
            self._audio_player.set_property("uri", Path(getattr(clip, "source_path")).resolve().as_uri())
            self._active_audio_clip_id = clip_id
        clip_offset = max(position - float(getattr(clip, "timeline_start")), 0.0)
        source_position = float(getattr(clip, "source_in", 0.0)) + clip_offset
        should_seek = True
        if self._audio_player.current_state in {Gst.State.PLAYING, Gst.State.PAUSED}:
            success, current = self._audio_player.query_position(Gst.Format.TIME)
            if success:
                current_seconds = current / Gst.SECOND
                should_seek = abs(current_seconds - source_position) > 0.35
        if should_seek:
            self._audio_player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(source_position * Gst.SECOND),
            )
        self._audio_player.set_state(Gst.State.PLAYING if self._is_playing else Gst.State.PAUSED)

    def _audio_clip_at(self, position: float) -> object | None:
        for clip in self._timeline_audio_clips:
            if getattr(clip, "muted", False):
                continue
            if float(getattr(clip, "timeline_start")) <= position < float(getattr(clip, "timeline_end")):
                return clip
        return None

    def _pause_timeline_audio(self) -> None:
        if self._audio_player is not None:
            self._audio_player.set_state(Gst.State.PAUSED)

    def _stop_timeline_audio(self) -> None:
        if self._audio_player is not None:
            self._audio_player.set_state(Gst.State.NULL)
        self._audio_player = None
        self._active_audio_clip_id = None
