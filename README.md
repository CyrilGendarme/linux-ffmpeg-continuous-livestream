# Headless FFmpeg Music Livestream

This project streams a continuous music radio feed from a VPS with an OBS-like visual output, fully headless.

## Visual output implemented

- Looping background video (full frame)
- Dynamic center title with typewriter-style appear/disappear on each new track
- Left-side now-playing panel with live metadata fields:
  - Artist
  - Title
  - Album
  - Label
  - Year
  - BPM

The style is aligned with the assets in:

- `submodules/obscustomsetup/typewriter.html`
- `submodules/obscustomsetup/now_playing.html`
- `submodules/videoeffectgenerator`

It does not use HTML rendering in the final stream. It reproduces a similar result directly through FFmpeg filters.

## Requirements

- Linux VPS
- FFmpeg and ffprobe installed
- Python 3.10+
- Audio files in a directory
- A background video file

Install FFmpeg:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

## Config-first usage

Edit `config.json`:

```json
{
  "music_dir": "/srv/music",
  "background_video": "/srv/video/background.mp4",
  "rtmp_url": "rtmp://live.restream.io/live/YOUR_RESTREAM_KEY",
  "title_file": "/tmp/music_livestream_overlay/center_title.txt",
  "playlist_file": "/tmp/music_playlist.txt",
  "overlay_dir": "/tmp/music_livestream_overlay",
  "title_font": "submodules/obscustomsetup/fonts/Staatliches-Regular.ttf",
  "info_font": "submodules/obscustomsetup/fonts/DMSans.ttf",
  "type_ms": 80,
  "erase_ms": 40,
  "title_hold": 1.8,
  "restart_delay": 2.0,
  "track_fade_seconds": 3.0
}
```

`track_fade_seconds` controls how long each track fades in at the start and fades out at the end. The default is 3 seconds.

Run:

```bash
python3 music_livestream.py --config config.json
```

## Optional CLI overrides

Every config field can still be overridden from CLI. Example:

```bash
python3 music_livestream.py \
  --config config.json \
  --rtmp-url rtmp://live.restream.io/live/ANOTHER_KEY \
  --type-ms 70
```

## Reliability

The script restarts stream cycles automatically and can be run under systemd for reboot persistence.

Example service:

```ini
[Unit]
Description=Headless music livestream
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/linux-ffmpeg-continuous-livestream
ExecStart=/usr/bin/python3 /home/ubuntu/linux-ffmpeg-continuous-livestream/music_livestream.py --config /home/ubuntu/linux-ffmpeg-continuous-livestream/config.json
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
