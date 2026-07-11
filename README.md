<p align="center">
  <img src="cutie.png" alt="Cutie logo" width="160">
</p>

<h1 align="center">Cutie</h1>

<p align="center">
  A small Linux GTK4/libadwaita app for quick FFmpeg-powered video edits.
</p>

<p align="center">
  <a href="https://github.com/merchizm/cutie/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/merchizm/cutie/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/merchizm/cutie/releases"><img alt="Release" src="https://img.shields.io/github/v/release/merchizm/cutie?include_prereleases"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-Linux-2ea44f">
  <img alt="GTK" src="https://img.shields.io/badge/GTK-4-blue">
</p>

Cutie is built for lightweight video editing on Linux: crop a clip, trim a
range, split timeline segments, replace or mix audio, and export through
FFmpeg without opening a heavyweight editor.

## Highlights

- GTK4/libadwaita interface with video preview and timeline playhead seeking.
- Crop overlay with apply/decline flow and temporary working-video handling.
- Timeline video and audio clips backed by project state, not pixel-only UI.
- Split, move, trim, delete, mute, and reset operations for timeline clips.
- Original audio can be toggled independently from imported timeline audio.
- Audio/music import through the Add Media button or direct drag and drop.
- Export respects crop state, timeline edits, audio position, mute state,
  format, quality, resolution, encoder, and bitrate settings.
- Command-based undo/redo for core crop and timeline operations.

## Requirements

Cutie currently targets Linux desktops with GTK4 and libadwaita.

- Python 3.11+
- GTK4
- libadwaita
- PyGObject
- FFmpeg and ffprobe
- GStreamer playback plugins for `Gtk.Video`

Arch-based systems:

```bash
sudo pacman -S python-gobject gtk4 libadwaita ffmpeg gst-libav gst-plugins-base gst-plugins-good gst-plugins-bad
```

Debian/Ubuntu-based systems:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg gstreamer1.0-libav gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

## Run

From the repository root:

```bash
python3 main.py
```

After installing from a built package, the app also exposes a `cutie` command.

## Test

```bash
python3 -m compileall app tests
python3 -m tests.run_backend
```

## Release

For maintainers, GitHub Actions builds source and wheel distributions from
`pyproject.toml` and publishes them to GitHub Releases.

1. Update `version` in `pyproject.toml`.
2. Commit the change.
3. Create and push a matching tag:

```bash
git tag v0.1.0
git push origin main v0.1.0
```

The tag version must match `pyproject.toml` without the leading `v`. The release
workflow can also be started manually from GitHub Actions with the same version.

## Packaging

Linux desktop packaging files live in `packaging/linux/`.

- Desktop launcher: `packaging/linux/dev.mce.Cutie.desktop`
- AppStream metadata: `packaging/linux/dev.mce.Cutie.metainfo.xml`
- hicolor icons: `packaging/linux/icons/hicolor/`

Before publishing an installable app package, replace the remaining `TODO`
metadata and add production screenshots to the AppStream metadata.

## License

Cutie is released under the [MIT License](LICENSE).
