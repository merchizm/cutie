from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from app.utils.timecode import seconds_to_label


class PlaybackControls(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        self.on_seek: Callable[[float], None] | None = None
        self.on_toggle_playback: Callable[[], None] | None = None
        self._duration = 0.0
        self._updating = False

        self.jump_start_button = self._icon_button("media-skip-backward-symbolic", "Jump to start")
        self.back_button = self._icon_button("media-seek-backward-symbolic", "Seek backward")
        self.play_button = self._icon_button("media-playback-start-symbolic", "Play or pause")
        self.forward_button = self._icon_button("media-seek-forward-symbolic", "Seek forward")
        self.jump_end_button = self._icon_button("media-skip-forward-symbolic", "Jump to end")

        self.time_spin = Gtk.SpinButton.new_with_range(0, 1, 0.1)
        self.time_spin.set_digits(1)
        self.time_spin.set_width_chars(7)
        self.time_spin.set_tooltip_text("Current time in seconds")
        self.time_spin.connect("value-changed", self._time_changed)

        self.time_label = Gtk.Label(label="0:00 / 0:00")
        self.time_label.add_css_class("monospace")

        self.jump_start_button.connect("clicked", lambda _button: self._seek(0))
        self.back_button.connect("clicked", lambda _button: self._seek_delta(-1))
        self.play_button.connect("clicked", self._toggle)
        self.forward_button.connect("clicked", lambda _button: self._seek_delta(1))
        self.jump_end_button.connect("clicked", lambda _button: self._seek(self._duration))

        for child in (
            self.jump_start_button,
            self.back_button,
            self.play_button,
            self.forward_button,
            self.jump_end_button,
            self.time_spin,
            self.time_label,
        ):
            self.append(child)

        self.set_sensitive(False)

    def set_duration(self, duration: float) -> None:
        self._duration = max(duration, 0.0)
        self.time_spin.get_adjustment().set_upper(max(self._duration, 0.1))
        self.set_sensitive(self._duration > 0)
        self.update_position(0, False)

    def update_position(self, seconds: float, is_playing: bool) -> None:
        seconds = min(max(seconds, 0.0), self._duration)
        self._updating = True
        self.time_spin.set_value(seconds)
        self._updating = False
        self.time_label.set_text(f"{seconds_to_label(seconds)} / {seconds_to_label(self._duration)}")
        self.play_button.set_icon_name(
            "media-playback-pause-symbolic" if is_playing else "media-playback-start-symbolic"
        )

    def _seek_delta(self, delta: float) -> None:
        self._seek(self.time_spin.get_value() + delta)

    def _seek(self, seconds: float) -> None:
        if self.on_seek is not None:
            self.on_seek(min(max(seconds, 0.0), self._duration))

    def _toggle(self, _button: Gtk.Button) -> None:
        if self.on_toggle_playback is not None:
            self.on_toggle_playback()

    def _time_changed(self, spin: Gtk.SpinButton) -> None:
        if not self._updating:
            self._seek(spin.get_value())

    def _icon_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button()
        button.set_icon_name(icon_name)
        button.set_tooltip_text(tooltip)
        return button
