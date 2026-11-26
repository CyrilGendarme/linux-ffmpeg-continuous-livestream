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

## Incidents

Anything that disturbs the stream is logged as one `WARNING` line with its time, its kind, the reason, what
listeners missed, and the machine's state (CPU busy and steal over the last 10 s, load, free memory, swap):

```bash
grep -h WARNING logs/radio.log logs/switcher.log | sort -k3,4     # both logs, in time order
```

```
[music-livestream] WARNING 2026-11-26 21:04:13 radio-restart: FFmpeg exited with code -9. ~5.8s of audio lost: track1.mp3 jumps from 0:19 to 0:25. | cpu 97% steal 14% load 3.80 mem 412MB free swap 0MB used
```

| Kind | Meaning |
| --- | --- |
| `track-cut` | A track stopped before its end: decoder error, or a truncated/damaged file (decoded vs. expected length). |
| `track-skipped` | A track was not played at all: download failed (R2) or no audio could be decoded. |
| `slow-next-track` | Opening the next track took over 2 s (download or disk); the radio may have frozen meanwhile. |
| `radio-restart` | The radio encoder died or stalled and was restarted. Says how much queued audio was lost, and which track (and its position) it was cut from. |
| `radio-freeze` / `radio-freeze-end` | The radio produced no frame for 2 s; the second line gives the freeze's total length. |
| `radio-slow` / `radio-slow-end` | The radio ran below 95% of real time over 10 s (CPU too slow): the stream falls behind and will stutter. |
| `radio-lost` | The switcher went to the offline clip because the radio stopped publishing: see the radio log at that time. |
| `output-freeze` / `-freeze-end`, `output-slow` / `-slow-end` | The same for the switcher's encoder towards Restream. |
| `output-restart` | The switcher's encoder was restarted: viewers skip ahead by the time this took. |
| `input-gap` | No data reached the switcher's encoder for over 2 s: the relayed source stalled. |
| `input-jump` | Part of the source never reached the switcher's encoder (dropped upstream): viewers skip ahead by that much. |
| `relay-exit` | The relay of the radio or OBS to the live path exited and is restarted. |

How to read them: a high `steal` means the VPS host is short of CPU (a plan with dedicated vCPUs fixes it); high
`cpu` with low steal means the settings are too heavy for the machine (lower `video_fps`, `video_size` or
`background_size`); `swap` in use means more RAM would help. A `radio-…` incident points at the radio; `output-…`
or `input-…` incidents without one point at the switcher or the upload to Restream. MediaMTX logs its own packet
drops in `logs/mediamtx.log`.
