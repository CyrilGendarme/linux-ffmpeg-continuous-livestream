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

## Tracks in a bucket (Cloudflare R2 or S3)

Instead of `music_dir`, the tracks can live in a bucket. The radio lists it at startup (and saves the list, so it
can restart without network), downloads each next track in full while the current one plays, and keeps only the
previous, current and next tracks on disk. Install the dependency first: `pip install -r requirements.txt` (boto3).

1. Create the bucket and two keys: **read-only** for the server, **read & write** for uploading from your computer.
2. Fill in the bucket settings in `config.json` (empty `r2_endpoint` = Cloudflare R2):

   ```json
   {
     "r2_bucket": "radio",
     "r2_prefix": "music/",
     "r2_account_id": "your Cloudflare account ID (R2 only)",
     "r2_endpoint": "",
     "r2_access_key_id": "...",
     "r2_secret_access_key": "..."
   }
   ```

   For another S3 service, set `r2_endpoint` instead of the account ID, e.g. `https://s3.eu-west-3.amazonaws.com`
   (AWS; the region is read from it, or set `r2_region`), `https://s3.eu-central-003.backblazeb2.com` (Backblaze B2)
   or your MinIO server.
   An R2 bucket in the EU jurisdiction also needs `r2_endpoint`: `https://<account ID>.eu.r2.cloudflarestorage.com`
   (it is still treated as R2: region `auto`, free-tier guard on).
3. Upload the tracks of your music folder (subfolders are kept). It only uploads missing files and never overwrites
   or deletes anything:

   ```bash
   python3 r2_push.py --dry-run     # lists the bucket and what would be uploaded: also checks the keys
   python3 r2_push.py
   ```
4. Restart the radio: its log shows `Bucket: N track(s) in bucket …`. Once `r2_bucket` is set, `music_dir` is not
   used for playback.

Cost guard: every request is counted per month in the cache folder, and the radio stops calling the bucket at 90%
of R2's free tier (900k list and 9M download requests); one download per track, none when it is still on disk.
With R2, the push also stops before the bucket would exceed 9 GB (free storage: 10 GB). A failed download skips
that track (`WARNING bucket-download` then `track-skipped`).

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
[music-livestream] WARNING 2026-11-26 21:04:13 radio-restart: FFmpeg exited with code -9. 5.8s of audio still in the encoder is played again: nothing skipped, track1.mp3 resumes at 0:19. | cpu 97% steal 14% load 3.80 mem 412MB free swap 0MB used
```

| Kind | Meaning |
| --- | --- |
| `track-cut` | A track stopped before its end: decoder error, or a truncated/damaged file (decoded vs. expected length). |
| `track-skipped` | A track was not played at all: its download failed or no audio could be decoded. |
| `bucket-download` | Downloading a track from the bucket failed (network, keys, missing object): the track is skipped. |
| `bucket-storage` | The R2 bucket is close to the 10 GB free storage. |
| `slow-next-track` | Opening the next track took over 2 s (download or disk); the radio may have frozen meanwhile. |
| `radio-restart` | The radio encoder died or stalled and was restarted. The audio still inside it (a few seconds) is played again, so nothing is skipped; the line says where the track resumes, and how much was lost if it was more than the 15 s kept for this. |
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
