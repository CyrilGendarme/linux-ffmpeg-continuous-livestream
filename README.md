# Headless FFmpeg Music Livestream

A 24/7 music radio, rendered headless on a Linux server (VPS), with an OBS-style picture: a looping background
video, a typewriter title on each new track and a "now playing" panel. Whenever you go live from **OBS on your
laptop**, your show automatically replaces the radio on the outgoing stream; when you stop streaming in OBS,
the radio comes back. There is nothing to click on the server: starting or stopping the stream in OBS *is* the
switch.

```
                         SERVER (VPS)
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │ radio (livestream.runner) ──rtmp──► MediaMTX /radio  ─┐                         │
 │                                                       ├─► relay ─► /live ─► output encoder ──rtmp──► Restream
 │                    ┌─────────────► MediaMTX /laptop ──┘   (livestream.switcher)                    (YouTube,
 └────────────────────│──────────────────────────────────────────────────────────┘                     Twitch…)
                      │ rtmp :1935
 LAPTOP   OBS ────────┘
```

- **One connection to Restream, never dropped.** The output encoder connects once; MediaMTX keeps its `/live`
  path open while the source behind it changes.
- **OBS wins.** OBS takes over once it has streamed steadily for 1 s, and hands back as soon as it disconnects
  (or after 3 s without data, e.g. the laptop lost its connection).
- **Clean cuts.** A new source only goes on air on a keyframe, so a cut shows no frozen picture, and audio and
  video stay in sync. Everything is re-encoded once to the same size, frame rate and bitrate, so OBS may send
  another resolution or frame rate than the radio.

## Contents

1. [Requirements](#1-requirements)
2. [Server setup](#2-server-setup-vps)
3. [Laptop setup (OBS)](#3-laptop-setup-obs)
4. [Day-to-day operation](#4-day-to-day-operation)
5. [Settings page (HTTPS)](#5-settings-page-https)
6. [Running by hand / testing on one machine](#6-running-by-hand--testing-on-one-machine)
7. [Configuration reference](#7-configuration-reference)
8. [Troubleshooting](#8-troubleshooting)
9. [Project layout](#9-project-layout)

## 1. Requirements

**Server (VPS)**

- Linux with systemd (tested on Ubuntu 24.04), x86-64. The bundled `mediamtx/mediamtx` binary is x86-64;
  on an ARM server, download the matching MediaMTX v1.21+ build from
  <https://github.com/bluenviron/mediamtx/releases> and replace it (keep `mediamtx/mediamtx.yml`).
- FFmpeg 6+ (`ffmpeg`, `ffprobe`), Python 3.10+.
- CPU: two 1080p x264 encoders run at once (the radio and the output). Count about 2 to 4 vCPUs at the
  `veryfast` preset; use a faster `video_preset` or a lower `video_size` on a small server.
- Upload bandwidth: `video_bitrate` + `audio_bitrate` to Restream (about 5 Mbit/s by default).
- Your music files and a background video (MP4/WEBM), on the server.
- A Restream (or other RTMP) destination URL with its stream key.

**Laptop**

- OBS Studio 28+ (any OS), with an H.264 encoder (x264, NVENC, QuickSync, Apple VT… all fine).
- Upload bandwidth for your OBS bitrate (e.g. 6 Mbit/s for 4500 kbit/s video), to the server.

## 2. Server setup (VPS)

### 2.1 Get the code

The overlay fonts and styles live in Git submodules, so clone with them:

```bash
git clone --recurse-submodules <this-repository-url> ~/linux-ffmpeg-continuous-livestream
cd ~/linux-ffmpeg-continuous-livestream
# already cloned without them?  git submodule update --init --recursive
```

### 2.2 Install system packages and Python dependencies

```bash
sudo apt update
sudo apt install -y ffmpeg python3-venv libraqm0     # libraqm0: proper text shaping in the overlay
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Use `.venv/bin/python` for every command below: the services are installed with the Python that runs the
install command.

### 2.3 Configure the stream (`config.json`)

```bash
cp config.example.json config.json
chmod 600 config.json          # it holds your stream key
nano config.json
```

The settings that matter for a first start:

```json
{
  "music_dir": "/home/radio/music",
  "background_video": "/home/radio/video/background.mp4",
  "rtmp_url": "rtmp://127.0.0.1:1935/radio",
  "output_url": "rtmp://live.restream.io/live/YOUR_RESTREAM_KEY",
  "video_size": "1920x1080",
  "video_fps": 30,
  "video_bitrate": "4500k",
  "keyframe_seconds": 2
}
```

- `rtmp_url` is where the **radio** sends its stream: keep it on the local MediaMTX path
  `rtmp://127.0.0.1:1935/radio`.
- `output_url` is the **final destination**: your Restream ingest URL including the stream key.
- `video_size`, `video_fps` and the bitrates apply to the stream Restream receives, whatever the source.
- `keyframe_seconds: 1` makes the hand-back from OBS to the radio about half a second faster.
- Profiles (`"profiles"` / `"profile"`) override settings, e.g. a profile for another destination: with the
  switcher, put that destination in the profile's `output_url` and keep its `rtmp_url` on the local radio path.

All settings are listed in [section 7](#7-configuration-reference).

### 2.4 Set the MediaMTX passwords

`mediamtx/mediamtx.yml` defines who may publish to the server. **Replace the example passwords** before opening
the server to the internet:

```yaml
authInternalUsers:
  ...
- user: laptop_streamer
  pass: CHOOSE_A_LONG_RANDOM_PASSWORD        # OBS uses this one (section 3)
  permissions:
  - action: publish
    path: laptop
- user: radio_publisher
  pass: CHOOSE_ANOTHER_ONE
  ...
```

Generate one with `openssl rand -base64 24 | tr -d '/+='`. Only `laptop_streamer` can publish from the
internet, and only to `/laptop`. The radio and the switcher work from the server itself (127.0.0.1), which needs
no password. Reading streams is allowed from the server only. Leave the `live` path settings at the bottom of the
file as they are.

### 2.5 Check the setup

```bash
.venv/bin/python music_self_test.py --config config.json
```

This prints `PASS` / `FAIL` / `SKIP` for each step: FFmpeg present, tracks found in `music_dir`, metadata
readable, a short silent decode, encoder speed (can this server encode in real time with your settings?), RTMP
connectivity. The RTMP check targets `rtmp_url`, so it only passes once MediaMTX runs (2.6). The exit code is
non-zero if a check fails.

### 2.6 Install and start the services

Three systemd *user* services (no root needed to run them):

| Service | Role |
| --- | --- |
| `music-livestream-mediamtx` | MediaMTX: receives the radio and OBS. Before it starts, it builds `mediamtx/offline.mp4`. |
| `music-livestream-switcher` | Chooses OBS or the radio and sends it to `output_url` (Restream). |
| `music-livestream` | The radio itself. |

```bash
.venv/bin/python -m livestream.service install            # the radio (enabled, not started)
.venv/bin/python -m livestream.service install-switcher   # MediaMTX + switcher (enabled, not started)
sudo loginctl enable-linger "$USER"                       # keep user services running after logout and at boot

systemctl --user start music-livestream-mediamtx music-livestream-switcher music-livestream
```

The first start takes a little longer: the background video is pre-scaled once and the offline clip is built
(both cached).

### 2.7 Open the firewall

```bash
sudo ufw allow 1935/tcp      # OBS -> MediaMTX (RTMP)
sudo ufw allow 22/tcp        # keep SSH open if you enable ufw now
sudo ufw enable
```

Only port 1935 is needed for streaming. MediaMTX also listens on other ports (RTSP 8554, HLS 8888,
WebRTC 8889, SRT 8890, API 9997 on 127.0.0.1 only). Don't open them: everything that reads streams runs on the
server.

### 2.8 Check that it is on air

```bash
.venv/bin/python -m livestream.service switcher-logs
```

You should see:

```
[switcher] Started; priority is OBS > radio.
[switcher] Starting output encoder
[switcher] On air: radio (was offline clip)
```

Your Restream dashboard should show the stream as live. To see exactly what is being sent, without going
through Restream, open an SSH tunnel from your laptop and watch the `/live` path over HLS:

```bash
ssh -N -L 8888:127.0.0.1:8888 -L 1936:127.0.0.1:1935 <user>@<server>
# then open http://localhost:8888/live/ in a browser, or: ffplay rtmp://localhost:1936/live
# (not ffplay on the HLS URL: it reads low-latency HLS in bursts and looks like it keeps freezing)
```

## 3. Laptop setup (OBS)

### 3.1 Stream settings

OBS → **Settings → Stream**:

| Field | Value |
| --- | --- |
| Service | `Custom...` |
| Server | `rtmp://<server-ip-or-domain>:1935` |
| Stream Key | `laptop?user=laptop_streamer&pass=<the laptop_streamer password from 2.4>` |
| Use authentication | off (the credentials are in the stream key) |

The stream key must start with `laptop`: that is the path the switcher watches.

### 3.2 Output, video and audio settings

OBS → **Settings → Output** (Output Mode: *Advanced*, tab *Streaming*):

| Setting | Value |
| --- | --- |
| Video Encoder | any **H.264** encoder (x264, NVIDIA NVENC H.264, QuickSync H.264, Apple VT H.264). Not HEVC/AV1. |
| Rate Control | CBR |
| Bitrate | 4500–6000 kbps (it is re-encoded on the server to `video_bitrate`) |
| Keyframe Interval | **1 s** or 2 s. 1 s makes the takeover faster. Don't leave it at 0 (auto). |
| Profile | high (or main) |

**Settings → Video**: Output (Scaled) Resolution `1920x1080`, Common FPS Values `30`, ideally the same as
`video_size` / `video_fps` on the server. Other values work: they are scaled and converted on the server.

**Settings → Audio**: Sample Rate `48 kHz`, Channels `Stereo` (others are converted on the server).

### 3.3 Going live

1. In OBS, click **Start Streaming**. About 1 to 2 seconds later your show replaces the radio on Restream. The
   switcher log says `On air: OBS (was radio)`.
2. Click **Stop Streaming** when done. The radio comes back about 2 seconds later (a moment of the silent radio
   background covers the gap). The log says `On air: radio (was OBS)`.

If the laptop loses its connection, the radio takes over after 3 seconds (`release_timeout`). When OBS
reconnects (it retries on its own), your show comes back.

Check the connection from the laptop: `nc -vz <server> 1935` (or `Test-NetConnection <server> -Port 1935` on
Windows) must succeed. The OBS status bar shows green, with dropped frames near 0 %.

> RTMP is not encrypted, and the password travels with the stream key. Use a password specific to this setup
> (not one you use elsewhere).

## 4. Day-to-day operation

```bash
# status / logs
.venv/bin/python -m livestream.service status            # radio
.venv/bin/python -m livestream.service switcher-status   # MediaMTX + switcher
.venv/bin/python -m livestream.service logs              # radio log (tracks played, restarts)
.venv/bin/python -m livestream.service switcher-logs     # which source is on air, and why
journalctl --user -fu music-livestream-switcher          # follow live

# stop everything / start everything
systemctl --user stop  music-livestream music-livestream-switcher music-livestream-mediamtx
systemctl --user start music-livestream-mediamtx music-livestream-switcher music-livestream

# after editing config.json
systemctl --user restart music-livestream                # radio settings (music, overlay, radio encoder)
systemctl --user restart music-livestream-switcher       # output_url, output encoding, takeover timings
# after editing mediamtx/mediamtx.yml
systemctl --user restart music-livestream-mediamtx music-livestream-switcher
```

Restarting the switcher reconnects to Restream (a short interruption for viewers). Restarting the radio while OBS
is on air is invisible.

Behaviour summary:

| Situation | What goes to Restream |
| --- | --- |
| Radio running, OBS not streaming | the radio |
| OBS starts streaming | OBS, after `takeover_delay` (1 s) plus the next OBS keyframe |
| OBS stops streaming | the radio, about 2 s later (silent radio background in between) |
| OBS connected but sending nothing (network trouble) | the radio after `release_timeout` (3 s) |
| OBS sends an unusable stream (e.g. HEVC) | the radio stays on air; the log explains why |
| Radio down (e.g. restarting) and no OBS | the silent radio background (`mediamtx/offline.mp4`) |
| Output encoder or Restream connection fails | restarted automatically after `restart_delay` |

Removing the services: `.venv/bin/python -m livestream.service uninstall` (radio) and
`.venv/bin/python -m livestream.service uninstall-switcher`.

## 5. Settings page (HTTPS)

Every `config.json` setting, profiles included, can be edited from a web page served over HTTPS by the server.
The admin secret is needed both to **view** the settings (they contain stream keys) and for **every save**.
5 wrong secrets within 10 minutes lock that client out for the rest of the window. Values are validated on the
server (types, ranges, paths that must exist). The file is replaced atomically, and the previous version is kept
as `config.json.bak`. "Restart stream to apply" restarts the radio service. After changing switcher settings
(`output_url`, `takeover_delay`, `release_timeout`, output encoding), also run
`systemctl --user restart music-livestream-switcher`.

Install it as a systemd user service (a strong secret is generated and printed once):

```bash
.venv/bin/python -m livestream.service install-control \
  --tls-cert /etc/letsencrypt/live/<domain>/fullchain.pem \
  --tls-key  /etc/letsencrypt/live/<domain>/privkey.pem     # omit both for a self-signed certificate
sudo ufw allow 8443/tcp
```

Then open `https://<domain>:8443/config/`. The secret and settings live in
`~/.config/music-livestream/control.env` (mode 600). Manage it with
`.venv/bin/python -m livestream.service control-status | control-logs | uninstall-control`.

The `/show/` page also includes Restream channel controls. Add a Restream app's client ID and secret to
`config.json`, configure its callback URL as shown on the page, and grant the `channels.read` and `channels.write`
scopes. Once connected, you can edit each channel's title and description, set one title across all channels, and
enable or disable destinations without disconnecting them.

For local development, the "Backend: Control API" launch configuration runs it with `DJANGO_DEBUG=1` on
`https://127.0.0.1:8443/config/` only.

## 6. Running by hand / testing on one machine

Without systemd, in four terminals from the project folder (works on a laptop too, to try the whole chain
before deploying):

```bash
# 1. offline clip (once), then MediaMTX
.venv/bin/python -m livestream.switcher --config config.json --prepare-offline-clip
cd mediamtx && ./mediamtx mediamtx.yml

# 2. switcher (sends to output_url)
.venv/bin/python -m livestream.switcher --config config.json

# 3. radio (sends to rtmp_url = rtmp://127.0.0.1:1935/radio)
.venv/bin/python music_livestream.py --config config.json

# 4. watch the result locally instead of on Restream
ffplay rtmp://localhost:1935/live
```

Then point OBS at `rtmp://127.0.0.1:1935` with the stream key from 3.1, and start and stop streaming to watch it
switch. To test without sending anything to Restream, set `output_url` to a local preview such as
`udp://127.0.0.1:1234?pkt_size=1316` and view it with `ffplay "udp://@:1234"`.

**The radio alone, without the switcher** (the original mode): set `rtmp_url` to the destination (e.g. your
Restream URL) and run only `music_livestream.py`, or use the `local_preview` profile:

```bash
.venv/bin/python music_livestream.py --config config.json --profile local_preview   # sends to udp://127.0.0.1:1234
.venv/bin/python -m livestream.local_viewer                                          # ffplay window
```

Every setting can be overridden on the command line, e.g. `--video-bitrate 3000k --video-preset superfast`
(`--help` lists them all).

## 7. Configuration reference

All settings live in `config.json` (also editable on the settings page). Any of them can be overridden per
profile or on the command line (`--snake-case` → `--kebab-case`).

| Setting | Default | Meaning |
| --- | --- | --- |
| `music_dir` | required | Folder scanned (recursively) for audio tracks. |
| `background_video` | required | Looping MP4/WEBM shown behind the overlay (and, silent, while nothing is on air). |
| `rtmp_url` | required | Where the **radio** streams. With the switcher: `rtmp://127.0.0.1:1935/radio`. |
| `output_format` | flv | Container of the radio stream: flv for RTMP, mpegts for UDP/local preview. |
| `track_fade_seconds` | 3.0 | Crossfade length between tracks. |
| `restart_delay` | 2.0 | Wait before restarting an encoder after a failure. |
| `video_bitrate` | 4500k | Constant video bitrate. |
| `video_bufsize` | empty | Rate-control buffer; empty = same as the bitrate (1 second). |
| `video_fps` | 30 | Output frames per second. |
| `video_size` | 1920x1080 | Output WxH; the overlay is drawn at 1920x1080 then scaled. |
| `video_preset` | veryfast | x264 preset; faster presets use less CPU (lower quality per bit). |
| `encoder_threads` | 0 | Encoder threads, 0 = automatic. |
| `rc_lookahead` | empty | Lower uses less memory; empty = x264 default. |
| `keyframe_seconds` | 2.0 | Keyframe interval; most platforms want 2 s (1 s also speeds up the hand-back to the radio). |
| `background_bitrate` | 4M | Bitrate of the pre-scaled background loop. |
| `audio_bitrate` | 160k | AAC bitrate (Mixcloud: 320k). |
| `loudness_normalize` | true | Measure each track once and even out volume. |
| `loudness_target` | -14.0 | Target loudness in LUFS; streaming platforms use about -14. |
| `loudness_max_gain` | 9.0 | Never boost a quiet track more than this (dB). |
| `output_url` | empty | **Final destination** of the switcher (Restream URL with stream key). |
| `takeover_delay` | 1.0 | OBS must stream steadily this long (s) before it replaces the radio. |
| `release_timeout` | 3.0 | Go back to the radio when OBS sends nothing for this long (s). |
| `stall_timeout` | 15.0 | Restart an encoder if no frame goes out for this long (s). |
| `network_timeout` | 10.0 | RTMP read/write timeout (s). |
| `cache_dir` | ~/.cache/music-livestream | Loudness cache and pre-rendered assets. |

The video and audio settings apply both to the radio's own encoding and to the switcher's output encoding.

MediaMTX settings (ports, passwords) are in `mediamtx/mediamtx.yml`. The switcher expects the defaults: RTMP
1935, RTSP 8554, API 127.0.0.1:9997, and the paths `radio`, `laptop` and `live`.

## 8. Troubleshooting

| Symptom | Check |
| --- | --- |
| OBS: "Failed to connect to server" | Port 1935 open (`sudo ufw status`)? MediaMTX running (`switcher-status`)? Server address correct? |
| OBS connects, then disconnects at once | Wrong stream key / password: `journalctl --user -u music-livestream-mediamtx` shows `authentication failed`. The key must start with `laptop?user=laptop_streamer&pass=`. |
| OBS streams but Restream still shows the radio | Switcher log: OBS is probably HEVC/AV1 (`OBS stream keeps failing to relay`): choose an H.264 encoder. |
| Takeover takes several seconds | OBS keyframe interval set to 0 (auto): set 1 s. |
| `On air: offline clip` and silence, no OBS | The radio is not publishing: check `logs` and that `rtmp_url` is `rtmp://127.0.0.1:1935/radio`. |
| MediaMTX does not start: `invalid 'alwaysAvailableFile'` | The offline clip is missing. Run `.venv/bin/python -m livestream.switcher --config config.json --prepare-offline-clip` (the service does it on start; check `background_video`). |
| Restream shows nothing | Switcher log: `output_url` set? `Output encoder/reader exited` repeating means the destination refuses the connection (wrong key, or another device streaming with the same key). |
| Stuttering, `No output frame for ...` | CPU too slow for two encoders: `video_preset` `superfast`/`ultrafast`, or a lower `video_size`/`video_fps`. Check with `top`. |
| Services stop when you log out | `sudo loginctl enable-linger $USER`. |

Messages that are normal in the logs: `Relay from /laptop exited with code 255` when OBS stops,
`Closed a +1500 ms timestamp jump` at a change of source.

## 9. Project layout

| Path | Role |
| --- | --- |
| `music_livestream.py`, `livestream/runner.py` | The radio: playlist, crossfades, overlay, encoder, watchdog. |
| `livestream/overlay.py`, `prerender.py`, `audio_feed.py`, `loudness.py`, `media.py` | Overlay rendering, audio mixing, loudness, metadata. |
| `livestream/switcher.py` | Source switcher: OBS/radio choice, relays, output encoder. |
| `livestream/flv.py` | Keyframe gate for relays and timestamp correction for the output. |
| `livestream/ffmpeg.py` | All FFmpeg command lines. |
| `livestream/config.py` | Settings schema, validation and CLI (shared with the settings page). |
| `livestream/service.py` | systemd user services (install / start / logs). |
| `mediamtx/` | MediaMTX binary and `mediamtx.yml`; `offline.mp4` is generated. |
| `control_api/`, `streamcontrol/`, `start_control_api.py` | HTTPS settings page (Django). |
| `music_self_test.py`, `livestream/self_test.py` | Non-audible pipeline self-test. |
| `submodules/obscustomsetup` | Fonts, styles and the original OBS HTML overlays the stream reproduces. |
