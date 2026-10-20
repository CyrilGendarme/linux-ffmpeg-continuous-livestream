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
  "restart_delay": 2.0,
  "track_fade_seconds": 3.0
}
```

`track_fade_seconds` controls how long each track fades in at the start and fades out at the end. The default is 3 seconds.

Run:

```bash
python3 music_livestream.py --config config.json
```

## VPS Self-Test (non-audible)

Use this command to validate the full pipeline on a headless VPS without relying on desktop audio output:

```bash
python3 music_self_test.py --config config.json
```

You can also run it as a module:

```bash
python3 -m livestream.self_test --config config.json
```

The self-test prints `PASS` / `FAIL` / `SKIP` for each step:

- Dependencies (`ffmpeg`, `ffprobe`)
- Track scan in `music_dir`
- Metadata probe via `ffprobe`
- Concat playlist decode to null output (non-audible)
- Overlay text file write/verify
- RTMP host/port TCP connectivity precheck

Exit code is non-zero if any check fails.

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

## Settings page (HTTPS)

Every `config.json` setting, profiles included, can be edited from a web page served over HTTPS by the VPS.
The admin secret is needed both to **view** the settings (they contain stream keys) and for **every save**;
5 wrong secrets within 10 minutes lock that client out for the rest of the window. Values are validated on the
server (types, ranges, paths that must exist), the file is replaced atomically, and the previous version is kept as
`config.json.bak`. Tick "Restart stream to apply" to restart the stream service after saving.

Install it on the VPS as a systemd user service (a strong secret is generated and printed once):

```bash
pip install -r requirements.txt
python3 -m livestream.service install-control \
  --tls-cert /etc/letsencrypt/live/<domain>/fullchain.pem \
  --tls-key  /etc/letsencrypt/live/<domain>/privkey.pem     # omit both for a self-signed certificate
sudo ufw allow 8443/tcp
```

Then open `https://<domain>:8443/config/`. The secret and settings live in `~/.config/music-livestream/control.env`
(mode 600). Manage it with `python3 -m livestream.service control-status | control-logs | uninstall-control`.

For local development, the "Backend: Control API" launch configuration runs it with `DJANGO_DEBUG=1`
on `https://127.0.0.1:8443/config/` only.
