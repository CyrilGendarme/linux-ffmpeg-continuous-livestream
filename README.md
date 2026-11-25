# Radio livestream

Streams the tracks of a music folder (all subfolders are scanned) over looping background videos, with cover art and track titles.

## Requirements

- Linux, `python3`, `ffmpeg` + `ffprobe`
- `pip install -r requirements.txt`
- The MediaMTX binary at `mediamtx/mediamtx`

## Configure

Copy `config.example.json` to `config.json` and set at least:

```json
{
  "music_dir": "/home/user/radio_music/",
  "background_videos": "videos/loop 1.mp4, videos/loop 2.mp4",
  "rtmp_url": "rtmp://127.0.0.1:1935/radio",
  "output_url": "rtmp://live.restream.io/live/YOUR_KEY"
}
```

`background_videos` is a comma-separated list (or a JSON list). The videos are prepared once at startup; on each track change the picture fades to black and the next video fades in. A single `background_video` also works.

## Run

```bash
./radiofull-headless.sh          # start (add: stop | restart | status | logs radio)
```

Logs are in `logs/`.
