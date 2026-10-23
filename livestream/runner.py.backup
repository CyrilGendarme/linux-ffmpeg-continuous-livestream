import hashlib
import os
import queue
import random
import shutil
import signal
import subprocess
import threading
import time
import sys
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple

from .audio_feed import AudioFeeder
from .config import parse_args
from .ffmpeg import build_background_command, build_ffmpeg_command
from .loudness import LoudnessNormalizer
from .media import extract_cover, extract_track_fields, ffprobe_json, find_audio_files, probe_duration_seconds
from .overlay import OverlayRenderer
from .prerender import Prerenderer


def announce(msg: str) -> None:
    print(f"[music-livestream] {msg}")


def ensure_dependencies() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing required tool: {tool}. Install FFmpeg first: sudo apt install ffmpeg")


def shuffled_forever(music_dir: Path, on_scan: Callable[[list], None] = lambda _tracks: None) -> Iterator[Path]:
    """Yield tracks in random order, rescanning and reshuffling after each pass."""
    while True:
        tracks = find_audio_files(music_dir)
        on_scan(tracks)
        random.shuffle(tracks)
        announce(f"Shuffled {len(tracks)} tracks")
        yield from tracks


def prepare_background(argv) -> Path:
    """Pre-scaled, audio-less background loop at the output frame rate (cached)."""
    cache_dir = Path(argv.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    source = Path(argv.background_video)
    if not source.exists():
        raise FileNotFoundError(f"Background video not found: {source}")
    stat = source.stat()
    key = hashlib.sha1(
        f"{source.resolve()}|{stat.st_size}|{stat.st_mtime}|{argv.video_fps}|{argv.background_bitrate}".encode()
    ).hexdigest()[:12]
    background = cache_dir / f"background-{key}.mp4"
    if not background.exists():
        announce(f"Preparing background loop once (cached in {cache_dir})...")
        partial = background.with_suffix(".partial.mp4")
        subprocess.run(
            build_background_command(source, partial, argv.video_fps, argv.background_bitrate), check=True
        )
        partial.replace(background)
    return background


class OverlayFeed:
    """Owns the overlay renderer (it survives encoder restarts) and streams its frames.

    It also prefetches the next track's metadata and cover, and has the next
    track change pre-rendered in the background (see livestream.prerender).
    """

    def __init__(self, fps: float, fade_seconds: float, log: Callable[[str], None]) -> None:
        self.fps = fps
        self.fade_seconds = fade_seconds
        self.log = log
        self.renderer = OverlayRenderer(fps)
        self.prerenderer = Prerenderer(fps, self.renderer.cache, log)
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._prefetched: Dict[Path, tuple] = {}
        self._seeds: Dict[Path, int] = {}
        self._next: Optional[Tuple[Path, int]] = None  # (track, events applied when it was announced)
        self._events_applied = 0
        self._current_end_frame: Optional[float] = None
        self._reported = True

    @staticmethod
    def _load(track: Path) -> tuple:
        try:
            info = ffprobe_json(track)
            fields = extract_track_fields(track, info)
            duration = probe_duration_seconds(info, track)
        except (subprocess.CalledProcessError, ValueError):
            fields, duration = {"center": track.stem, "title": track.stem}, 0.0
        return fields, extract_cover(track), duration

    def upcoming(self, track: Path) -> None:
        """The feeder knows the next track: prefetch it now, pre-render its transition later."""
        announced_at = self._events_applied

        def prefetch() -> None:
            loaded = self._load(track)
            with self._lock:
                self._prefetched[track] = loaded
                self._next = (track, announced_at)

        threading.Thread(target=prefetch, name="overlay-prefetch", daemon=True).start()

    def track_started(self, track: Path) -> Dict[str, str]:
        with self._lock:
            loaded = self._prefetched.pop(track, None)
            seed = self._seeds.pop(track, None)
        fields, cover, duration = loaded or self._load(track)
        self._events.put((track, fields, cover, duration, seed))
        return fields

    def _apply_events(self) -> None:
        while not self._events.empty():
            track, fields, cover, duration, seed = self._events.get_nowait()
            self.renderer.cache_hits = self.renderer.live_renders = 0
            self.renderer.on_track(fields, cover, str(track), seed)
            self._events_applied += 1
            self._reported = False
            self._current_end_frame = self.renderer.frame + max(0.0, duration - self.fade_seconds) * self.fps

    def _maybe_prerender(self) -> None:
        """Once the current transition has finished, pre-render the next one."""
        if not self._reported and self.renderer.settled():
            self._reported = True
            self.log(
                f"Overlay: transition used {self.renderer.cache_hits} pre-rendered frame(s), "
                f"{self.renderer.live_renders} rendered live"
            )
        with self._lock:
            pending = self._next
            if pending is None or self._events_applied <= pending[1] or not self.renderer.settled():
                return
            track = pending[0]
            fields, cover, _duration = self._prefetched[track]
            self._next = None
            seed = int.from_bytes(os.urandom(4), "big")
            self._seeds[track] = seed
        remaining = (self._current_end_frame - self.renderer.frame) / self.fps if self._current_end_frame else 0.0
        if remaining < 2.0:
            return  # too late to help; it will be rendered live
        event = {"fields": fields, "cover": cover, "cover_token": str(track), "seed": seed}
        self.prerenderer.submit(self.renderer.snapshot(), event, remaining)

    def stream(self, panel_fd: int, title_fd: int, stop: threading.Event) -> None:
        """Write one panel frame and one title frame per video frame until stopped or the pipes break."""
        with open(panel_fd, "wb", closefd=True) as panel, open(title_fd, "wb", closefd=True) as title:
            while not stop.is_set():
                self._apply_events()
                self._maybe_prerender()
                panel_bytes, title_bytes = self.renderer.next_frames()
                panel.write(panel_bytes)
                title.write(title_bytes)

    def close(self) -> None:
        self.prerenderer.close()


def start_encoder(
    background: Path, stream_url: str, output_format: str, settings, realtime: bool = True, duration: Optional[float] = None
):
    """Launch the encoder; returns (process, panel write fd, title write fd)."""
    panel_read, panel_write = os.pipe()
    title_read, title_write = os.pipe()
    cmd = build_ffmpeg_command(
        background, panel_read, title_read, stream_url, output_format, settings, realtime, duration
    )
    process = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, pass_fds=(panel_read, title_read)
    )
    os.close(panel_read)
    os.close(title_read)
    return process, panel_write, title_write


class ProgressWatchdog:
    """Reads FFmpeg's -progress output and reports how long since the last new frame."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.last_frame = -1
        self.last_change = time.monotonic()
        threading.Thread(target=self._read, args=(process,), name="progress", daemon=True).start()

    def _read(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            if line.startswith(b"frame="):
                try:
                    frame = int(line[6:])
                except ValueError:
                    continue
                if frame > self.last_frame:
                    self.last_frame = frame
                    self.last_change = time.monotonic()

    @property
    def started(self) -> bool:
        return self.last_frame > 0

    def seconds_since_progress(self) -> float:
        return time.monotonic() - self.last_change


STARTUP_GRACE_SECONDS = 30.0


def run_cycle(feeder: AudioFeeder, overlay: OverlayFeed, background: Path, argv) -> bool:
    """Run one encoder process. Returns True when every track has been played."""
    announce(f"Starting FFmpeg stream to {argv.rtmp_url}")
    process, panel_fd, title_fd = start_encoder(background, argv.rtmp_url, argv.output_format, argv)
    watchdog = ProgressWatchdog(process)

    started: "queue.Queue[Path]" = queue.Queue()
    stop = threading.Event()
    finished = [False]

    def feed_audio() -> None:
        try:
            finished[0] = feeder.feed(process.stdin, started.put, stop)
        except (BrokenPipeError, ValueError, OSError):
            pass  # encoder went away; the main loop reports it
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def feed_overlay() -> None:
        try:
            overlay.stream(panel_fd, title_fd, stop)
        except (BrokenPipeError, ValueError, OSError):
            pass

    threads = [
        threading.Thread(target=feed_audio, name="audio-feed", daemon=True),
        threading.Thread(target=feed_overlay, name="overlay-feed", daemon=True),
    ]
    for thread in threads:
        thread.start()
    audio_thread = threads[0]

    def check_health() -> None:
        limit = argv.stall_timeout if watchdog.started else argv.stall_timeout + STARTUP_GRACE_SECONDS
        if watchdog.seconds_since_progress() > limit:
            raise RuntimeError(f"No video frame output for {watchdog.seconds_since_progress():.0f}s (connection stalled?)")

    try:
        while process.poll() is None:
            check_health()
            try:
                track = started.get(timeout=0.5)
            except queue.Empty:
                if not audio_thread.is_alive() and finished[0]:
                    # Audio is over; the looping video would keep the encoder going.
                    time.sleep(1.0)
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=10)
                    return True
                continue
            fields = overlay.track_started(track)  # metadata/cover usually prefetched already
            announce(f"Now playing: {fields['center']}")
        raise RuntimeError(f"FFmpeg exited with code {process.returncode}")
    finally:
        stop.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=5)


def handle_shutdown(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    feeder: Optional[AudioFeeder] = None
    overlay: Optional[OverlayFeed] = None
    try:
        ensure_dependencies()
        if args.profile:
            announce(f"Using profile: {args.profile}")
        background = prepare_background(args)
        overlay = OverlayFeed(args.video_fps, float(args.track_fade_seconds), announce)

        normalizer = None
        if args.loudness_normalize:
            normalizer = LoudnessNormalizer(
                Path(args.cache_dir).expanduser() / "loudness.json",
                args.loudness_target,
                args.loudness_max_gain,
                announce,
            )
        feeder = AudioFeeder(
            shuffled_forever(Path(args.music_dir), normalizer.scan_library if normalizer else lambda _tracks: None),
            float(args.track_fade_seconds),
            announce,
            gain_for=normalizer.gain_db if normalizer else (lambda _track, _may_block: 0.0),
            on_upcoming=lambda track: (normalizer.prioritize(track) if normalizer else None, overlay.upcoming(track)),
        )

        while True:
            try:
                # The feeder keeps its place, so a restarted encoder resumes the same track.
                if run_cycle(feeder, overlay, background, args):
                    break
            except KeyboardInterrupt:
                announce("Shutting down cleanly.")
                break
            except Exception as exc:
                announce(f"Stream cycle failed: {exc}; restarting in {args.restart_delay}s")
                time.sleep(float(args.restart_delay))
    except KeyboardInterrupt:
        announce("Shutting down cleanly.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if feeder is not None:
            feeder.close()
        if overlay is not None:
            overlay.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
