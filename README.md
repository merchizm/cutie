# Cutie

Cutie is a small Linux-only GTK4/libadwaita app for basic FFmpeg video editing,
audio replacement, and format conversion.

## Requirements

- Python 3
- GTK4
- libadwaita
- PyGObject
- FFmpeg and ffprobe
- GStreamer playback plugins for `Gtk.Video`

On Arch-based systems:

```bash
sudo pacman -S python-gobject gtk4 libadwaita ffmpeg gst-libav gst-plugins-base gst-plugins-good gst-plugins-bad
```

On Debian/Ubuntu-based systems:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg gstreamer1.0-libav gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

## Run

```bash
python3 main.py
```