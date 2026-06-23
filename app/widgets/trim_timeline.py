from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from cairo import Context as CairoContext
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

from app.core.project_state import AudioClip, VideoSegment
from app.utils.timecode import clamp_time, seconds_to_label


logger = logging.getLogger(__name__)


class TimelineLayout:
    LEFT_MARGIN = 88
    RIGHT_MARGIN = 18
    RULER_Y = 20
    VIDEO_Y = 48
    AUDIO_Y = 108
    TRACK_HEIGHT = 40
    ORIGINAL_AUDIO_HEIGHT = 18
    MUSIC_TRACK_OFFSET = 22


class TrimTimeline(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_hexpand(True)

        self.on_seek: Callable[[float], None] | None = None
        self.on_trim_changed: Callable[[float, float], None] | None = None
        self.on_clip_selected: Callable[[str], None] | None = None
        self.on_clip_moved: Callable[[str, float], None] | None = None
        self.on_clip_trimmed: Callable[[str, str, float], None] | None = None
        self.on_files_dropped: Callable[[list[Path], float, str], None] | None = None

        self._duration = 0.0
        self._position = 0.0
        self._trim_start = 0.0
        self._trim_end = 0.0
        self._video_name = "Video"
        self._audio_mode = "keep"
        self._audio_name: str | None = None
        self._has_original_audio = False
        self._video_segments: list[VideoSegment] = []
        self._original_audio_clips: list[AudioClip] = []
        self._audio_clips: list[AudioClip] = []
        self._video_segment_items: list[tuple[int, VideoSegment]] = []
        self._original_audio_clips_by_start: list[AudioClip] = []
        self._audio_clips_by_start: list[AudioClip] = []
        self._selected_clip_id: str | None = "video"
        self._zoom = 1.0
        self._last_position_draw_x: float | None = None
        self._drag_mode: str | None = None
        self._drag_start_x = 0.0
        self._drag_clip_id: str | None = None

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Timeline")
        title.add_css_class("heading")
        title.set_xalign(0)
        title.set_hexpand(True)

        self.zoom_out_button = Gtk.Button()
        self.zoom_out_button.set_icon_name("zoom-out-symbolic")
        self.zoom_out_button.set_tooltip_text("Zoom out")
        self.zoom_in_button = Gtk.Button()
        self.zoom_in_button.set_icon_name("zoom-in-symbolic")
        self.zoom_in_button.set_tooltip_text("Zoom in")
        self.fit_button = Gtk.Button(label="Fit")
        self.fit_button.set_tooltip_text("Fit timeline")
        self.zoom_out_button.connect("clicked", self._zoom_out)
        self.zoom_in_button.connect("clicked", self._zoom_in)
        self.fit_button.connect("clicked", self._fit)

        header.append(title)
        header.append(self.zoom_out_button)
        header.append(self.zoom_in_button)
        header.append(self.fit_button)

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(720)
        self.area.set_content_height(178)
        self.area.set_hexpand(True)
        self.area.set_draw_func(self._draw)
        self.area.add_css_class("card")

        click = Gtk.GestureClick()
        click.connect("pressed", self._click_pressed)
        self.area.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)

        try:
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", self._files_dropped)
            self.area.add_controller(drop)
        except AttributeError:
            logger.debug("Gtk.DropTarget FileList is not available; timeline file drop disabled.")

        self.append(header)
        self.append(self.area)
        self.set_sensitive(True)

    def set_duration(self, duration: float) -> None:
        self._duration = max(duration, 0.0)
        self._position = 0.0
        self._trim_start = 0.0
        self._trim_end = self._duration
        self.set_sensitive(True)
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def update_duration(self, duration: float) -> None:
        self._duration = max(duration, 0.0)
        self._position = clamp_time(self._position, 0, self._duration)
        self._trim_end = max(self._trim_end, self._duration)
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def set_video_name(self, name: str) -> None:
        self._video_name = name
        self.area.queue_draw()

    def set_position(self, seconds: float) -> None:
        previous_x = self._last_position_draw_x if self._last_position_draw_x is not None else self._position_draw_x()
        self._position = clamp_time(seconds, 0, self._duration)
        next_x = self._position_draw_x()
        if previous_x is None or next_x is None or abs(next_x - previous_x) >= 0.5:
            self._last_position_draw_x = next_x
            self.area.queue_draw()

    def set_audio_state(
        self,
        mode: str,
        replacement_audio_path: Path | None = None,
        has_original_audio: bool | None = None,
    ) -> None:
        self._audio_mode = mode
        self._audio_name = replacement_audio_path.name if replacement_audio_path else None
        if has_original_audio is not None:
            self._has_original_audio = has_original_audio
        self.area.queue_draw()

    def set_audio_clips(self, clips: list[AudioClip], selected_clip_id: str | None) -> None:
        self._audio_clips = list(clips)
        self._audio_clips_by_start = sorted(self._audio_clips, key=lambda clip: clip.timeline_start)
        self._selected_clip_id = selected_clip_id
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def set_original_audio_clips(self, clips: list[AudioClip]) -> None:
        self._original_audio_clips = list(clips)
        self._original_audio_clips_by_start = sorted(
            self._original_audio_clips,
            key=lambda clip: clip.timeline_start,
        )
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def set_video_segments(self, segments: list[VideoSegment]) -> None:
        self._video_segments = list(segments)
        self._video_segment_items = sorted(
            enumerate(self._video_segments),
            key=lambda item: item[1].timeline_start,
        )
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def select_video(self) -> None:
        self._selected_clip_id = "video"
        self.area.queue_draw()

    def zoom_in(self) -> None:
        self._zoom = min(self._zoom * 1.5, 6.0)
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def zoom_out(self) -> None:
        self._zoom = max(self._zoom / 1.5, 0.5)
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def fit(self) -> None:
        self._zoom = 1.0
        self._invalidate_position_draw_cache()
        self.area.queue_draw()

    def get_trim(self) -> tuple[float, float]:
        return self._trim_start, self._trim_end

    def _draw(self, _area: Gtk.DrawingArea, cr: CairoContext, width: int, height: int) -> None:
        left = TimelineLayout.LEFT_MARGIN
        right = TimelineLayout.RIGHT_MARGIN
        ruler_y = TimelineLayout.RULER_Y
        video_y = TimelineLayout.VIDEO_Y
        audio_y = TimelineLayout.AUDIO_Y
        track_h = TimelineLayout.TRACK_HEIGHT
        timeline_w = max(width - left - right, 1)

        bg = _rgba("window_bg_color", (0.11, 0.11, 0.12, 1.0))
        fg = _rgba("window_fg_color", (0.92, 0.92, 0.94, 1.0))
        muted = _rgba("insensitive_fg_color", (0.55, 0.55, 0.58, 1.0))
        accent = _rgba("accent_color", (0.24, 0.48, 0.96, 1.0))

        _rounded_rect(cr, 0, 0, width, height, 8)
        cr.set_source_rgba(*bg)
        cr.fill()

        self._draw_label(cr, "Time", 14, ruler_y + 4, muted)
        self._draw_label(cr, "Video", 14, video_y + 25, fg)
        self._draw_label(cr, "Audio", 14, audio_y + 25, fg)

        cr.set_line_width(1)
        cr.set_source_rgba(muted[0], muted[1], muted[2], 0.35)
        cr.move_to(left, ruler_y + 12)
        cr.line_to(width - right, ruler_y + 12)
        cr.stroke()

        if self._duration <= 0:
            self._draw_label(cr, "Open a video to build a timeline", left + 16, video_y + 25, muted)
            return

        tick_step = self._tick_step()
        visible_duration = self._visible_duration()
        tick = 0.0
        while tick <= visible_duration + 0.01:
            x = self._time_to_x(tick, left, timeline_w)
            cr.set_source_rgba(muted[0], muted[1], muted[2], 0.4)
            cr.move_to(x, ruler_y + 4)
            cr.line_to(x, ruler_y + 16)
            cr.stroke()
            self._draw_label(cr, seconds_to_label(tick), x + 4, ruler_y - 2, muted, 11)
            tick += tick_step

        self._draw_track_backplate(cr, left, video_y, timeline_w, track_h)
        self._draw_track_backplate(cr, left, audio_y, timeline_w, track_h)

        trim_x = self._time_to_x(self._trim_start, left, timeline_w)
        trim_w = max(self._time_to_x(self._trim_end, left, timeline_w) - trim_x, 8)
        for index, segment in self._visible_video_segments():
            segment_start = max(segment.start, self._trim_start)
            segment_end = min(segment.end, self._trim_end)
            if segment_end <= segment_start:
                continue
            segment_x = self._time_to_x(segment_start, left, timeline_w)
            segment_w = max(self._time_to_x(segment_end, left, timeline_w) - segment_x, 4)
            label = self._video_name if index == 0 else ""
            self._draw_clip(
                cr,
                segment_x,
                video_y,
                segment_w,
                track_h,
                accent,
                label,
                self._selected_clip_id == segment.id,
            )
            self._draw_trim_handle(cr, segment_x, video_y, track_h)
            self._draw_trim_handle(cr, segment_x + segment_w, video_y, track_h)

        audio_color = (0.12, 0.58, 0.44, 1.0)
        original_audio_h = TimelineLayout.ORIGINAL_AUDIO_HEIGHT
        music_y = audio_y + TimelineLayout.MUSIC_TRACK_OFFSET
        if self._audio_mode == "mute" or not self._has_original_audio or not self._original_audio_clips:
            original_label = "Original audio muted" if self._has_original_audio else "No original audio"
            self._draw_muted_audio(cr, left, audio_y, timeline_w, original_audio_h, muted, original_label)
        else:
            for clip in self._visible_clips(self._original_audio_clips_by_start):
                clip_x = self._time_to_x(clip.timeline_start, left, timeline_w)
                clip_w = max(self._time_to_x(clip.timeline_end, left, timeline_w) - clip_x, 6)
                color = audio_color if not clip.muted else (0.5, 0.5, 0.55, 1.0)
                self._draw_audio_clip(
                    cr,
                    clip_x,
                    audio_y,
                    min(clip_w, left + timeline_w - clip_x),
                    original_audio_h,
                    color,
                    clip.label,
                    self._selected_clip_id == clip.id,
                )

        if self._audio_clips:
            for clip in self._visible_clips(self._audio_clips_by_start):
                clip_x = self._time_to_x(clip.timeline_start, left, timeline_w)
                clip_w = max(self._time_to_x(clip.timeline_end, left, timeline_w) - clip_x, 32)
                color = (0.38, 0.32, 0.82, 1.0) if not clip.muted else (0.5, 0.5, 0.55, 1.0)
                label = f"{clip.filename}  {seconds_to_label(clip.duration)}"
                if clip.muted:
                    label += " muted"
                self._draw_audio_clip(
                    cr,
                    clip_x,
                    music_y,
                    min(clip_w, left + timeline_w - clip_x),
                    18,
                    color,
                    label,
                    self._selected_clip_id == clip.id,
                )
                self._draw_trim_handle(cr, clip_x, music_y, 18)
                self._draw_trim_handle(cr, min(clip_x + clip_w, left + timeline_w), music_y, 18)
        elif self._audio_mode == "replace" and self._audio_name:
            self._draw_audio_clip(cr, trim_x, music_y, trim_w, 18, audio_color, self._audio_name, False)

        playhead_x = self._time_to_x(self._position, left, timeline_w)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(2)
        cr.move_to(playhead_x, ruler_y + 2)
        cr.line_to(playhead_x, audio_y + track_h + 10)
        cr.stroke()
        cr.arc(playhead_x, ruler_y + 1, 5, 0, 6.283)
        cr.fill()

    def _draw_track_backplate(self, cr: CairoContext, x: float, y: float, width: float, height: float) -> None:
        _rounded_rect(cr, x, y, width, height, 7)
        cr.set_source_rgba(1, 1, 1, 0.07)
        cr.fill()

    def _draw_clip(
        self,
        cr: CairoContext,
        x: float,
        y: float,
        width: float,
        height: float,
        color: tuple[float, float, float, float],
        label: str,
        selected: bool = False,
    ) -> None:
        _rounded_rect(cr, x, y, width, height, 7)
        cr.set_source_rgba(color[0], color[1], color[2], 0.82)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.98 if selected else 0.72)
        cr.set_line_width(3 if selected else 1.5)
        cr.stroke()
        self._draw_thumbnail_stripes(cr, x + 8, y + 7, max(width - 16, 0), height - 14)
        self._draw_label(cr, label, x + 14, y + 25, (1, 1, 1, 0.96), 12)

    def _draw_audio_clip(
        self,
        cr: CairoContext,
        x: float,
        y: float,
        width: float,
        height: float,
        color: tuple[float, float, float, float],
        label: str,
        selected: bool = False,
    ) -> None:
        _rounded_rect(cr, x, y, width, height, 7)
        cr.set_source_rgba(color[0], color[1], color[2], 0.78)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.95 if selected else 0.35)
        cr.set_line_width(2 if selected else 1)
        cr.stroke()
        self._draw_waveform(cr, x + 10, y + 8, max(width - 20, 0), height - 16)
        self._draw_label(cr, label, x + 14, y + 25, (1, 1, 1, 0.96), 12)

    def _draw_muted_audio(
        self,
        cr: CairoContext,
        x: float,
        y: float,
        width: float,
        height: float,
        muted: tuple[float, float, float, float],
        label: str = "Original audio muted",
    ) -> None:
        _rounded_rect(cr, x, y, width, height, 7)
        cr.set_source_rgba(muted[0], muted[1], muted[2], 0.18)
        cr.fill()
        self._draw_label(cr, label, x + 14, y + 25, muted, 12)

    def _draw_trim_handle(self, cr: CairoContext, x: float, y: float, height: float) -> None:
        _rounded_rect(cr, x - 5, y + 2, 10, height - 4, 4)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.set_line_width(1)
        cr.move_to(x - 1.5, y + 12)
        cr.line_to(x - 1.5, y + height - 12)
        cr.move_to(x + 1.5, y + 12)
        cr.line_to(x + 1.5, y + height - 12)
        cr.stroke()

    def _draw_thumbnail_stripes(self, cr: CairoContext, x: float, y: float, width: float, height: float) -> None:
        if width <= 0:
            return
        step = 34
        current = x
        while current < x + width:
            _rounded_rect(cr, current, y, min(24, x + width - current), height, 4)
            cr.set_source_rgba(1, 1, 1, 0.12)
            cr.fill()
            current += step

    def _draw_waveform(self, cr: CairoContext, x: float, y: float, width: float, height: float) -> None:
        if width <= 0:
            return
        cr.set_source_rgba(1, 1, 1, 0.42)
        cr.set_line_width(2)
        mid = y + height / 2
        current = x
        index = 0
        while current < x + width:
            amp = 4 + (index % 5) * 3
            cr.move_to(current, mid - amp)
            cr.line_to(current, mid + amp)
            current += 7
            index += 1
        cr.stroke()

    def _draw_label(
        self,
        cr: CairoContext,
        text: str,
        x: float,
        y: float,
        color: tuple[float, float, float, float],
        size: int = 12,
    ) -> None:
        cr.set_source_rgba(*color)
        cr.select_font_face("Sans")
        cr.set_font_size(size)
        cr.move_to(x, y)
        cr.show_text(text[:56])

    def _click_pressed(self, _gesture: Gtk.GestureClick, _n_press: int, x: float, y: float) -> None:
        mode = self._hit_test(x, y)
        if mode == "playhead":
            self._drag_mode = mode
        elif mode and mode.startswith("clip:"):
            self._select_clip(mode.removeprefix("clip:"))
        elif mode and (mode.startswith("left:") or mode.startswith("right:")):
            side, clip_id = mode.split(":", 1)
            self._drag_mode = side
            self._drag_clip_id = clip_id
            self._select_clip(clip_id)
        else:
            self._seek_from_x(x)

    def _drag_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_start_x = x
        mode = self._hit_test(x, y)
        if mode and mode.startswith("clip:"):
            self._drag_mode = "move-clip"
            self._drag_clip_id = mode.removeprefix("clip:")
            self._select_clip(self._drag_clip_id)
        elif mode and (mode.startswith("left:") or mode.startswith("right:")):
            side, clip_id = mode.split(":", 1)
            self._drag_mode = side
            self._drag_clip_id = clip_id
            self._select_clip(clip_id)
        else:
            self._drag_mode = mode or "playhead"
            self._drag_clip_id = None

    def _drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
        self._apply_drag(self._drag_start_x + offset_x)

    def _drag_end(self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float) -> None:
        self._drag_mode = None
        self._drag_clip_id = None

    def _apply_drag(self, x: float) -> None:
        seconds = self._x_to_time(x)
        if self._drag_mode == "left-handle":
            self._trim_start = clamp_time(seconds, 0, max(self._trim_end - 0.1, 0))
            self._position = max(self._position, self._trim_start)
            self._emit_trim()
        elif self._drag_mode == "right-handle":
            self._trim_end = clamp_time(seconds, min(self._trim_start + 0.1, self._duration), self._duration)
            self._position = min(self._position, self._trim_end)
            self._emit_trim()
        elif self._drag_mode == "move-clip" and self._drag_clip_id is not None:
            start_time = self._x_to_time(self._drag_start_x)
            delta = seconds - start_time
            current_start = self._clip_start(self._drag_clip_id)
            if current_start is not None and self.on_clip_moved is not None:
                self.on_clip_moved(self._drag_clip_id, max(current_start + delta, 0.0))
                self._drag_start_x = x
        elif self._drag_mode in ("left", "right") and self._drag_clip_id is not None:
            if self.on_clip_trimmed is not None:
                self.on_clip_trimmed(self._drag_clip_id, self._drag_mode, seconds)
        else:
            self._seek(seconds)
        self.area.queue_draw()

    def _hit_test(self, x: float, y: float) -> str | None:
        left, timeline_w = self._geometry()
        video_y = TimelineLayout.VIDEO_Y
        audio_y = TimelineLayout.AUDIO_Y
        track_h = TimelineLayout.TRACK_HEIGHT
        playhead_x = self._time_to_x(self._position, left, timeline_w)
        if video_y - 6 <= y <= video_y + track_h + 6:
            for _index, segment in self._visible_video_segments():
                segment_start = max(segment.start, self._trim_start)
                segment_end = min(segment.end, self._trim_end)
                segment_start_x = self._time_to_x(segment_start, left, timeline_w)
                segment_end_x = self._time_to_x(segment_end, left, timeline_w)
                if abs(x - segment_start_x) <= 8:
                    return f"left:{segment.id}"
                if abs(x - segment_end_x) <= 8:
                    return f"right:{segment.id}"
                if segment_start_x <= x <= segment_end_x:
                    return f"clip:{segment.id}"
        for clip in self._visible_clips(self._original_audio_clips_by_start):
            clip_x = self._time_to_x(clip.timeline_start, left, timeline_w)
            clip_w = max(self._time_to_x(clip.timeline_end, left, timeline_w) - clip_x, 6)
            if audio_y <= y <= audio_y + 20 and clip_x <= x <= clip_x + clip_w:
                return f"clip:{clip.id}"
        for clip in self._visible_clips(self._audio_clips_by_start):
            clip_x = self._time_to_x(clip.timeline_start, left, timeline_w)
            clip_w = max(self._time_to_x(clip.timeline_end, left, timeline_w) - clip_x, 32)
            if audio_y + 22 <= y <= audio_y + 42 and clip_x <= x <= clip_x + clip_w:
                if abs(x - clip_x) <= 8:
                    return f"left:{clip.id}"
                if abs(x - (clip_x + clip_w)) <= 8:
                    return f"right:{clip.id}"
                return f"clip:{clip.id}"
        if 12 <= y <= audio_y + track_h + 16 and abs(x - playhead_x) <= 10:
            return "playhead"
        if 12 <= y <= audio_y + track_h + 16:
            return "timeline"
        return None

    def _seek_from_x(self, x: float) -> None:
        self._seek(self._x_to_time(x))

    def _seek(self, seconds: float) -> None:
        self._position = clamp_time(seconds, 0, self._duration)
        self.area.queue_draw()
        if self.on_seek is not None:
            self.on_seek(self._position)

    def _emit_trim(self) -> None:
        if self.on_trim_changed is not None:
            self.on_trim_changed(self._trim_start, self._trim_end)

    def _clip_start(self, clip_id: str) -> float | None:
        for clip in [*self._video_segments, *self._original_audio_clips, *self._audio_clips]:
            if clip.id == clip_id:
                return clip.timeline_start
        return None

    def _select_clip(self, clip_id: str) -> None:
        self._selected_clip_id = clip_id
        self.area.queue_draw()
        if self.on_clip_selected is not None:
            self.on_clip_selected(clip_id)

    def _files_dropped(self, _target: Gtk.DropTarget, value: object, x: float, y: float) -> bool:
        if self.on_files_dropped is None:
            return False
        try:
            files = value.get_files()
        except AttributeError:
            logger.debug("Timeline drop value does not expose get_files().")
            return False
        paths = [Path(file.get_path()) for file in files if file.get_path()]
        if not paths:
            return False
        track_type = "audio" if y >= 96 else "video"
        self.on_files_dropped(paths, self._x_to_time(x), track_type)
        return True

    def _time_to_x(self, seconds: float, left: float, timeline_w: float) -> float:
        if self._duration <= 0:
            return left
        return left + (seconds / self._visible_duration()) * timeline_w

    def _x_to_time(self, x: float) -> float:
        left, timeline_w = self._geometry()
        fraction = (x - left) / timeline_w
        return clamp_time(fraction * self._visible_duration(), 0, self._duration)

    def _geometry(self) -> tuple[int, int]:
        width = max(self.area.get_width(), 1)
        left = TimelineLayout.LEFT_MARGIN
        right = TimelineLayout.RIGHT_MARGIN
        return left, max(width - left - right, 1)

    def _tick_step(self) -> float:
        visible_duration = self._visible_duration()
        if visible_duration <= 12:
            base = 1
        elif visible_duration <= 60:
            base = 5
        elif visible_duration <= 300:
            base = 30
        else:
            base = 60
        return max(base, 0.5)

    def _visible_duration(self) -> float:
        return max(self._duration / self._zoom, 0.001)

    def _visible_range(self) -> tuple[float, float]:
        return 0.0, self._visible_duration()

    def _visible_video_segments(self) -> list[tuple[int, VideoSegment]]:
        start, end = self._visible_range()
        visible: list[tuple[int, VideoSegment]] = []
        for index, segment in self._video_segment_items:
            if segment.timeline_start > end:
                break
            if segment.timeline_end >= start:
                visible.append((index, segment))
        return visible

    def _visible_clips(self, clips_by_start: list[AudioClip]) -> list[AudioClip]:
        start, end = self._visible_range()
        visible: list[AudioClip] = []
        for clip in clips_by_start:
            if clip.timeline_start > end:
                break
            if clip.timeline_end >= start:
                visible.append(clip)
        return visible

    def _position_draw_x(self) -> float | None:
        if self._duration <= 0:
            return None
        left, timeline_w = self._geometry()
        return self._time_to_x(self._position, left, timeline_w)

    def _invalidate_position_draw_cache(self) -> None:
        self._last_position_draw_x = None

    def _zoom_in(self, _button: Gtk.Button) -> None:
        self.zoom_in()

    def _zoom_out(self, _button: Gtk.Button) -> None:
        self.zoom_out()

    def _fit(self, _button: Gtk.Button) -> None:
        self.fit()


def _rounded_rect(cr: CairoContext, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
    cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
    cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
    cr.close_path()


def _rgba(name: str, fallback: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    color = Gdk.RGBA()
    if color.parse(f"@{name}"):
        return color.red, color.green, color.blue, color.alpha
    return fallback
