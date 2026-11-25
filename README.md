# Linux FFmpeg Continuous Livestream Radio

A Python-based music streaming radio with FFmpeg integration and optional background videos.

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure
Create or edit `config.json`:
```json
{
  "music_dir": "/path/to/music",
  "background_videos": "video1.mp4, video2.mp4",
  "rtmp_url": "rtmp://your-server/live/stream",
  "ffmpeg_binary": "ffmpeg"
}
```

### 3. Launch
```bash
python music_livestream.py --config config.json
```

## Configuration Options

- **music_dir**: Path to music directory (recursive scan enabled)
- **background_videos**: Comma-separated video file paths (optional, fades to black between videos)
- **rtmp_url**: RTMP stream URL
- **ffmpeg_binary**: Path to FFmpeg executable
- **port**: Control API port (default: 5000)

## Notes

- Music files are scanned recursively from all subdirectories
- Cover art is automatically detected and loaded with tracks
- Videos preload at startup and cycle on each track change
- Use `radiofull.sh` or `radiofull-windows.sh` to start the full stack

## Stop

Press `Ctrl+C` or use the control API endpoint.
