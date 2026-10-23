"""Source switcher: OBS replaces the radio on the outgoing stream whenever it streams.

    OBS   --> MediaMTX /laptop --+
                                 +-- relay --> /live --> output encoder --> Restream
    radio --> MediaMTX /radio  --+

MediaMTX keeps /live "always available": its only reader, the output
encoder, stays connected while the relay feeding it is swapped (and gets an
offline clip if nothing is relayed), so the connection to the destination
never drops. Starting or stopping the stream in OBS is all it takes.

    python3 -m livestream.switcher --config config.json
    python3 -m livestream.switcher --config config.json --prepare-offline-clip

The radio streams to rtmp://127.0.0.1:1935/radio (its `rtmp_url`), the
switcher sends to `output_url`. MediaMTX must be running with mediamtx/mediamtx.yml.
"""

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from .config import parse_args
from .constants import REPO_ROOT
from .ffmpeg import (
    build_live_reader_command,
    build_offline_clip_command,
    build_output_command,
    build_relay_publisher_command,
    build_relay_reader_command,
)
from .flv import from_keyframe, retime
from .runner import STARTUP_GRACE_SECONDS, ProgressWatchdog, ensure_dependencies, exit_hint, prepare_background

OFFLINE_CLIP = REPO_ROOT / "mediamtx" / "offline.mp4"  # alwaysAvailableFile in mediamtx.yml
API_URL = "http://127.0.0.1:9997/v3/paths/get/"
RTSP_URL = "rtsp://127.0.0.1:8554/"
RTMP_URL = "rtmp://127.0.0.1:1935/"
OBS_PATH = "laptop"
RADIO_PATH = "radio"
LIVE_PATH = "live"

POLL_SECONDS = 0.5
HANDOVER_SECONDS = 5.0  # the previous relay keeps publishing until the new one has run this long
RELAY_RETRY_SECONDS = 2.0
OBS_MAX_FAILURES = 3  # quick OBS relay failures before OBS is ignored until it reconnects


def announce(msg: str) -> None:
    print(f"[switcher] {msg}", flush=True)


def prepare_offline_clip(args) -> Path:
    """(Re)build the clip MediaMTX plays on /live when nothing is relayed: the radio background, silent."""
    background = prepare_background(args)
    stamp = OFFLINE_CLIP.with_name(OFFLINE_CLIP.name + ".source")
    if OFFLINE_CLIP.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == background.name:
        return OFFLINE_CLIP
    announce(f"Building offline clip {OFFLINE_CLIP}")
    partial = OFFLINE_CLIP.with_suffix(".partial.mp4")
    subprocess.run(build_offline_clip_command(background, partial, args.video_fps), check=True)
    partial.replace(OFFLINE_CLIP)
    stamp.write_text(background.name, encoding="utf-8")
    return OFFLINE_CLIP


def get_path(name: str) -> Optional[dict]:
    """MediaMTX state of one path, or None when it does not exist."""
    request = urllib.request.Request(API_URL + name, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def is_ready(info: Optional[dict]) -> bool:
    return bool(info and info.get("ready"))


class ObsMonitor:
    """Decides whether OBS should be on air: publishing, and data actually arriving."""

    def __init__(self, takeover_delay: float, release_timeout: float) -> None:
        self.takeover_delay = takeover_delay
        self.release_timeout = release_timeout
        self.since: Optional[float] = None  # when data started flowing
        self.last_data = 0.0
        self.last_bytes: Optional[int] = None
        self.failures = 0
        self.refused = False

    def update(self, info: Optional[dict], now: float) -> bool:
        if not is_ready(info):
            # Disconnected: forget everything, including a refusal.
            self.since, self.last_bytes, self.failures, self.refused = None, None, 0, False
            return False
        received = info.get("bytesReceived", info.get("inboundBytes", 0))
        if self.last_bytes is None or received > self.last_bytes:
            self.last_data = now
            if self.since is None:
                self.since = now
        self.last_bytes = received
        if now - self.last_data > self.release_timeout:
            self.since = None  # the takeover delay applies again once data flows
            return False
        return not self.refused and now - self.since >= self.takeover_delay

    def relay_failed(self, info: Optional[dict]) -> None:
        self.failures += 1
        if self.failures >= OBS_MAX_FAILURES and not self.refused:
            self.refused = True
            tracks = [track.get("codec") for track in (info or {}).get("tracks2", [])]
            announce(
                f"OBS stream keeps failing to relay (tracks: {', '.join(filter(None, tracks)) or 'unknown'}); "
                "staying on the radio until OBS reconnects. OBS must send H.264 video with AAC audio."
            )


class Relay:
    """Republishes a source path onto /live: reader --from_keyframe--> publisher.

    The publisher only connects to /live, replacing the previous relay, once
    a keyframe has come through, so the cut never shows a frozen picture.
    """

    def __init__(self, source: str, timeout: float) -> None:
        self.source = source
        self.started = time.monotonic()
        self.reader = subprocess.Popen(
            build_relay_reader_command(RTMP_URL + source, timeout, copy_audio=source == RADIO_PATH),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        self.process = subprocess.Popen(build_relay_publisher_command(RTSP_URL + LIVE_PATH), stdin=subprocess.PIPE)
        threading.Thread(target=self._pump, name=f"relay-{source}", daemon=True).start()

    def _pump(self) -> None:
        try:
            from_keyframe(self.reader.stdout, self.process.stdin)
        except (EOFError, BrokenPipeError, ValueError, OSError):
            pass
        finally:
            # Either side ending ends the relay.
            for stream in (self.process.stdin, self.reader.stdout):
                try:
                    stream.close()
                except (BrokenPipeError, ValueError, OSError):
                    pass
            for process in (self.process, self.reader):
                if process.poll() is None:
                    process.terminate()

    @property
    def alive(self) -> bool:
        return self.process.poll() is None and self.reader.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self.process.poll() if self.process.poll() is not None else self.reader.poll()

    def age(self, now: float) -> float:
        return now - self.started

    def stop(self) -> None:
        for process in (self.process, self.reader):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class OutputEncoder:
    """The single, permanent connection to the destination.

    /live --reader (FLV remux)--> flv_retime --> encoder --> destination
    """

    def __init__(self, args) -> None:
        self.args = args
        self.reader: Optional[subprocess.Popen] = None
        self.process: Optional[subprocess.Popen] = None
        self.watchdog: Optional[ProgressWatchdog] = None
        self.retry_at = 0.0

    def check(self, now: float) -> None:
        if self.process is not None:
            if self.process.poll() is not None or self.reader.poll() is not None:
                announce(f"Output encoder/reader exited ({self.process.poll()}/{self.reader.poll()})"
                         f"{exit_hint(self.process.poll(), self.watchdog)}; restarting in {self.args.restart_delay}s")
            else:
                limit = self.args.stall_timeout + (0 if self.watchdog.started else STARTUP_GRACE_SECONDS)
                if self.watchdog.seconds_since_progress() <= limit:
                    return
                announce(f"No output frame for {self.watchdog.seconds_since_progress():.0f}s; restarting the output encoder")
            self.stop()
            self.process = None
            self.retry_at = now + float(self.args.restart_delay)
        if now >= self.retry_at:
            url = self.args.output_url
            output_format = "mpegts" if url.split(":", 1)[0].lower() in {"udp", "tcp", "srt"} else "flv"
            announce("Starting output encoder")
            self.reader = subprocess.Popen(
                build_live_reader_command(RTMP_URL + LIVE_PATH, self.args.network_timeout),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            )
            self.process = subprocess.Popen(
                build_output_command(url, output_format, self.args), stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            self.watchdog = ProgressWatchdog(self.process)
            threading.Thread(
                target=self._pump, args=(self.reader, self.process), name="retime", daemon=True
            ).start()

    @staticmethod
    def _pump(reader: subprocess.Popen, encoder: subprocess.Popen) -> None:
        try:
            retime(reader.stdout, encoder.stdin, announce)
        except (EOFError, BrokenPipeError, ValueError, OSError):
            pass  # one side went away; check() restarts both
        finally:
            for stream in (encoder.stdin, reader.stdout):
                try:
                    stream.close()
                except (BrokenPipeError, ValueError, OSError):
                    pass

    def stop(self) -> None:
        # The encoder first, so it closes the RTMP connection cleanly.
        for process in (self.process, self.reader):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def run(args) -> None:
    obs = ObsMonitor(args.takeover_delay, args.release_timeout)
    output = OutputEncoder(args)
    relay: Optional[Relay] = None
    retiring: List[Relay] = []
    on_air: Optional[str] = None
    api_down = False
    announce("Started; priority is OBS > radio.")
    try:
        while True:
            now = time.monotonic()
            output.check(now)

            try:
                obs_info, radio_info = get_path(OBS_PATH), get_path(RADIO_PATH)
                if api_down:
                    announce("MediaMTX API reachable again")
                api_down = False
            except (OSError, ValueError) as exc:
                # Keep whatever is relayed now rather than guessing.
                if not api_down:
                    announce(f"MediaMTX API check failed: {exc}")
                api_down = True
                time.sleep(POLL_SECONDS)
                continue

            if relay is not None and not relay.alive and relay.age(now) >= RELAY_RETRY_SECONDS:
                announce(f"Relay from /{relay.source} exited with code {relay.returncode}")
                if relay.source == OBS_PATH and relay.age(now) < 10:
                    obs.relay_failed(obs_info)
                relay = None  # restarted below if its source is still wanted

            if obs.update(obs_info, now):
                wanted = OBS_PATH
            else:
                wanted = RADIO_PATH if is_ready(radio_info) else None

            if wanted != on_air or (wanted and relay is None):
                if wanted != on_air:
                    names = {OBS_PATH: "OBS", RADIO_PATH: "radio", None: "offline clip"}
                    announce(f"On air: {names[wanted]} (was {names[on_air]})")
                if relay is not None:
                    retiring.append(relay)  # the new relay replaces it on /live as soon as it publishes
                relay = Relay(wanted, args.network_timeout) if wanted else None
                on_air = wanted

            for old in list(retiring):
                if not old.alive or relay is None or (relay.alive and relay.age(now) > HANDOVER_SECONDS):
                    old.stop()
                    retiring.remove(old)

            time.sleep(POLL_SECONDS)
    finally:
        for process in [relay, *retiring]:
            if process is not None:
                process.stop()
        output.stop()


def handle_shutdown(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--prepare-offline-clip", action="store_true",
                       help="Only build mediamtx/offline.mp4 (needed before MediaMTX starts), then exit")
    options, rest = extra.parse_known_args()
    args = parse_args(rest)
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    try:
        ensure_dependencies()
        prepare_offline_clip(args)
        if options.prepare_offline_clip:
            return 0
        if not args.output_url:
            print("ERROR: set output_url (the final destination, e.g. Restream) in the config", file=sys.stderr)
            return 1
        run(args)
    except KeyboardInterrupt:
        announce("Shutting down cleanly.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
