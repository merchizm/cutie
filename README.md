# Cutie

Cutie is a small Linux-only GTK4/libadwaita app for lightweight FFmpeg video
editing, cropping, timeline edits, audio replacement, and format conversion.

## Features

- Video import with GTK preview and timeline playhead seeking.
- Crop overlay with Apply and Decline. Apply generates a cropped temporary
  working video, reloads the preview, and exports from that working source.
- Timeline video and audio clips backed by project state, not pixel-only UI.
- Split at playhead for video clips and linked original audio.
- Movable and trimmable timeline clips with delete actions.
- Original audio can be toggled independently from timeline clips.
- Audio/music import from the Add Audio button or direct drag/drop onto the
  timeline.
- Export respects the working source, split video clips, timeline audio
  position, mute state, output format, quality, and resolution settings.
- Command-based undo/redo for core crop and timeline operations.

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

## Test

```bash
python3 -m compileall app tests
python3 -m tests.run_backend
```
