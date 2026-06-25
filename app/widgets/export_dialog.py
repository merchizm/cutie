from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk

from app.core.ffmpeg_capabilities import available_h264_encoder_options


@dataclass(frozen=True)
class DialogExportOptions:
    output_path: Path
    format_name: str
    extension: str
    output_width: int | None
    output_height: int | None
    aspect_mode: str
    video_crf: int
    video_encoder: str
    video_preset: str
    target_video_bitrate_kbps: int | None
    framerate: int | None
    audio_codec: str
    audio_bitrate_kbps: int
    audio_mode: str
    loop_timeline_audio: bool


class ExportSettingsDialog(Gtk.Window):
    FORMATS = {
        "MP4": ("mp4", "mp4"),
        "WebM": ("webm", "webm"),
        "MKV": ("mkv", "mkv"),
    }
    RESOLUTIONS = ("Original", "1920 width", "1280 width", "1080 square", "1080p", "720p", "480p", "854 width", "640 width", "Custom")
    ASPECTS = {
        "Original": "original",
        "1:1": "1:1",
        "16:9": "16:9",
        "9:16": "9:16",
        "Custom": "custom",
    }
    QUALITY = {
        "High quality": 18,
        "Balanced": 23,
        "Small file": 28,
        "Custom CRF": -1,
    }
    PRESETS = ("ultrafast", "veryfast", "fast", "medium", "slow")
    FRAMERATES = {
        "Source": None,
        "24 fps": 24,
        "30 fps": 30,
        "60 fps": 60,
    }
    AUDIO_CODECS = {
        "AAC": "aac",
        "Opus": "libopus",
        "MP3": "libmp3lame",
    }

    def __init__(
        self,
        parent: Gtk.Window,
        default_output_path: Path,
        has_timeline_audio: bool,
        original_audio_enabled: bool,
        on_export: Callable[[DialogExportOptions], None],
    ) -> None:
        super().__init__(title="Export Settings", transient_for=parent, modal=True)
        self.set_default_size(440, 520)
        self._output_path = default_output_path
        self._on_export = on_export
        self._default_audio_mode = "timeline" if has_timeline_audio else ("keep" if original_audio_enabled else "mute")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.set_margin_top(18)
        root.set_margin_bottom(18)
        root.set_margin_start(18)
        root.set_margin_end(18)

        title = Gtk.Label(label="Export")
        title.add_css_class("title-2")
        title.set_xalign(0)

        self.output_label = Gtk.Label(label=str(self._output_path))
        self.output_label.set_wrap(True)
        self.output_label.set_xalign(0)
        output_button = Gtk.Button(label="Choose File")
        output_button.connect("clicked", self._choose_output)

        self.format_combo = self._combo(list(self.FORMATS), "MP4")
        self.quality_combo = self._combo(list(self.QUALITY), "Balanced")
        self.quality_combo.connect("changed", self._quality_changed)
        self.encoder_options = available_h264_encoder_options()
        self.encoder_combo = self._combo(list(self.encoder_options), "CPU H.264")
        self.preset_combo = self._combo(list(self.PRESETS), "medium")
        self.framerate_combo = self._combo(list(self.FRAMERATES), "Source")
        self.audio_codec_combo = self._combo(list(self.AUDIO_CODECS), "AAC")
        self.audio_bitrate = Gtk.SpinButton.new_with_range(64, 512, 32)
        self.audio_bitrate.set_value(192)

        self.custom_crf = Gtk.SpinButton.new_with_range(0, 51, 1)
        self.custom_crf.set_value(23)
        self.custom_crf.set_sensitive(False)
        self.target_bitrate_switch = Gtk.Switch()
        self.target_bitrate_switch.set_active(False)
        self.target_bitrate_switch.connect("notify::active", self._target_bitrate_changed)
        self.target_bitrate = Gtk.SpinButton.new_with_range(256, 100_000, 256)
        self.target_bitrate.set_value(6000)
        self.target_bitrate.set_sensitive(False)

        self.loop_audio = Gtk.Switch()
        self.loop_audio.set_active(True)

        group = Adw.PreferencesGroup()
        group.add(self._row("Output file", output_button))
        group.add(self._row("Format", self.format_combo))
        group.add(self._row("Encoder", self.encoder_combo))
        group.add(self._row("Preset", self.preset_combo))
        group.add(self._row("Framerate", self.framerate_combo))
        group.add(self._row("Quality", self.quality_combo))
        group.add(self._row("Custom CRF", self.custom_crf))
        group.add(self._row("Target bitrate", self.target_bitrate_switch))
        group.add(self._row("Bitrate kbps", self.target_bitrate))
        group.add(self._row("Audio codec", self.audio_codec_combo))
        group.add(self._row("Audio bitrate kbps", self.audio_bitrate))
        group.add(self._row("Loop timeline audio", self.loop_audio))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _button: self.close())
        export_button = Gtk.Button(label="Export")
        export_button.add_css_class("suggested-action")
        export_button.connect("clicked", self._export)
        actions.append(cancel_button)
        actions.append(export_button)

        root.append(title)
        root.append(self.output_label)
        root.append(group)
        root.append(actions)
        self.set_child(root)

    def _combo(self, values: list[str], active: str) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for value in values:
            combo.append_text(value)
        combo.set_active(values.index(active))
        return combo

    def _row(self, title: str, suffix: Gtk.Widget) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        suffix.set_valign(Gtk.Align.CENTER)
        row.add_suffix(suffix)
        row.set_activatable_widget(suffix)
        return row

    def _choose_output(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative(
            title="Save Export",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
            accept_label="Save",
            cancel_label="Cancel",
        )
        dialog.set_current_name(self._output_path.name)
        dialog.connect("response", self._output_response)
        dialog.show()

    def _output_response(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            path = Path(file.get_path()) if file and file.get_path() else None
            if path is not None:
                self._output_path = path
                self.output_label.set_text(str(path))
        dialog.destroy()

    def _quality_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self.custom_crf.set_sensitive(self.quality_combo.get_active_text() == "Custom CRF")

    def _target_bitrate_changed(self, _switch: Gtk.Switch, _pspec: object) -> None:
        self.target_bitrate.set_sensitive(self.target_bitrate_switch.get_active())

    def _export(self, _button: Gtk.Button) -> None:
        format_label = self.format_combo.get_active_text() or "MP4"
        format_name, extension = self.FORMATS[format_label]
        quality_label = self.quality_combo.get_active_text() or "Balanced"
        crf = int(self.custom_crf.get_value()) if quality_label == "Custom CRF" else self.QUALITY[quality_label]
        encoder_label = self.encoder_combo.get_active_text() or "CPU H.264"
        framerate_label = self.framerate_combo.get_active_text() or "Source"
        audio_codec_label = self.audio_codec_combo.get_active_text() or "AAC"
        self._on_export(
            DialogExportOptions(
                output_path=self._output_path,
                format_name=format_name,
                extension=extension,
                output_width=None,
                output_height=None,
                aspect_mode="original",
                video_crf=crf,
                video_encoder=self.encoder_options.get(encoder_label, "libx264"),
                video_preset=self.preset_combo.get_active_text() or "medium",
                target_video_bitrate_kbps=(
                    int(self.target_bitrate.get_value()) if self.target_bitrate_switch.get_active() else None
                ),
                framerate=self.FRAMERATES[framerate_label],
                audio_codec=self.AUDIO_CODECS[audio_codec_label],
                audio_bitrate_kbps=int(self.audio_bitrate.get_value()),
                audio_mode=self._default_audio_mode,
                loop_timeline_audio=self.loop_audio.get_active(),
            )
        )
        self.close()

def resolve_output_size(
    resolution: str,
    aspect: str,
    custom_width: int,
    custom_height: int,
) -> tuple[int | None, int | None]:
    if resolution == "Original" and aspect == "original":
        return None, None
    if resolution == "Custom" or aspect == "custom":
        return custom_width, custom_height
    if resolution == "1080 square" or aspect == "1:1":
        side = 1080 if resolution in ("Original", "1080 square") else _first_int(resolution, 1080)
        return side, side
    if aspect == "9:16":
        if resolution == "1080p":
            base_width = 1080
        elif resolution == "720p":
            base_width = 720
        elif resolution == "480p":
            base_width = 480
        else:
            base_width = _first_int(resolution, 0)
        if base_width <= 0:
            return None, None
        return base_width, _even(round(base_width * 16 / 9))
    if resolution == "1080p":
        base_width = 1920
    elif resolution == "720p":
        base_width = 1280
    elif resolution == "480p":
        base_width = 854
    else:
        base_width = _first_int(resolution, 0)
    if base_width <= 0:
        return None, None
    if aspect in ("16:9", "original"):
        return base_width, _even(round(base_width * 9 / 16)) if aspect == "16:9" else None
    return base_width, None


def _first_int(text: str, fallback: int) -> int:
    for part in text.split():
        if part.isdigit():
            return int(part)
    return fallback


def _even(value: int) -> int:
    return value if value % 2 == 0 else value + 1
