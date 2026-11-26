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
from collections import deque
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .audio_feed import AudioFeeder
from . import health
from .config import LIVE_OPTIONS, OPTIONS_BY_NAME, background_sources, load_config_file, menu_lines, option_value, parse_args
from .ffmpeg import PCM_SAMPLE_RATE, build_background_command, build_background_decoder_command, build_ffmpeg_command
from .health import clock, warning
from .loudness import LoudnessNormalizer
from .media import extract_cover, extract_track_fields, ffprobe_json, find_audio_files, probe_duration_seconds
from .overlay import OverlayRenderer
from .prerender import Prerenderer
from .r2 import preloader_from_args

BACKGROUND_FADE_SECONDS = 1.0  # fade to black (and the same again back up) on each video switch


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


def prepare_background(argv, source: Optional[str] = None) -> Path:
    """Pre-scaled, audio-less background loop at the output frame rate (cached)."""
    cache_dir = Path(argv.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    source = source or (background_sources(argv) or [None])[0]
    if not source:
        raise ValueError("No background video configured (background_videos)")
    source = Path(source).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Background video not found: {source}")
    stat = source.stat()
    key = hashlib.sha1(
        f"{source.resolve()}|{stat.st_size}|{stat.st_mtime}|{argv.video_fps}|{argv.background_bitrate}|{argv.background_size}".encode()
    ).hexdigest()[:12]
    background = cache_dir / f"background-{key}.mp4"
    if not background.exists():
        announce(f"Preparing background loop once (cached in {cache_dir})...")
        partial = background.with_suffix(".partial.mp4")
        subprocess.run(
            build_background_command(source, partial, argv.video_fps, argv.background_bitrate, argv.background_size),
            check=True,
        )
        partial.replace(background)
    return background


def prepare_backgrounds(argv, sources: Optional[Sequence[str]] = None) -> List[Path]:
    """Prepare every background video up front (cached), so a switch never waits for a conversion."""
    return [prepare_background(argv, source) for source in (sources or background_sources(argv))]

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
        self._show: Optional[tuple] = None  # pending (menu lines, countdown enabled, hour, minute)

    def set_show(self, menu: Tuple[str, ...], countdown_enabled: bool, hour: int, minute: int) -> None:
        """New menu text and countdown settings, applied on the next frame."""
        with self._lock:
            self._show = (tuple(menu), countdown_enabled, hour, minute)

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
        with self._lock:
            show, self._show = self._show, None
        if show is not None:
            menu, countdown_enabled, hour, minute = show
            self.renderer.panel.menu = menu
            countdown = self.renderer.countdown
            countdown.enabled, countdown.hour, countdown.minute = countdown_enabled, hour, minute
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

    def stream(self, panel_fd: int, title_fd: int, countdown_fd: int, stop: threading.Event) -> None:
        """Write one frame of each overlay per video frame until stopped or the pipes break."""
        with open(panel_fd, "wb", closefd=True) as panel, open(title_fd, "wb", closefd=True) as title, \
                open(countdown_fd, "wb", closefd=True) as countdown:
            while not stop.is_set():
                self._apply_events()
                self._maybe_prerender()
                panel_bytes, title_bytes, countdown_bytes = self.renderer.next_frames()
                panel.write(panel_bytes)
                title.write(title_bytes)
                countdown.write(countdown_bytes)

    def close(self) -> None:
        self.prerenderer.close()


class BackgroundFeed:
    """Streams the looping backgrounds as raw frames, one video at a time.

    Every track change fades the current video to black and then the next
    video of the playlist in (BACKGROUND_FADE_SECONDS each way). The prepared
    loops are cached files, and a decoder for the next video is kept running
    ahead of time, so the cut happens on a ready frame; the encoder and the
    music never stop. The fade works on the raw frames (no re-encode).
    """

    def __init__(self, playlist: Sequence[Path], size: str, fps: float, fade_seconds: float = BACKGROUND_FADE_SECONDS) -> None:
        width, height = (int(value) for value in size.split("x"))
        self.luma_size = width * height
        self.frame_size = self.luma_size * 3 // 2  # yuv420p
        self.fade_frames = max(1, round(fade_seconds * fps))
        # Fade tables indexed by brightness step 0..fade_frames: luma fades to 16 (black), chroma to 128 (neutral).
        steps = range(self.fade_frames + 1)
        self._luma = [bytes(self._level(i, 16, k / self.fade_frames) for i in range(256)) for k in steps]
        self._chroma = [bytes(self._level(i, 128, k / self.fade_frames) for i in range(256)) for k in steps]
        self._playlist = list(playlist)
        self._index = 0
        self._requested = False
        self._new_playlist: Optional[List[Path]] = None
        self._last_track: Optional[Path] = None
        self._lock = threading.Lock()

    @staticmethod
    def _level(value: int, black: int, level: float) -> int:
        return max(0, min(255, round(black + (value - black) * level)))

    @property
    def background(self) -> Path:
        return self._playlist[self._index]

    def track_changed(self, track: Path) -> None:
        """A new track started: move on to the next video (not for the first track, nor a resumed one)."""
        with self._lock:
            previous, self._last_track = self._last_track, track
            if previous is not None and previous != track and len(self._playlist) > 1:
                self._requested = True

    def set_playlist(self, playlist: Sequence[Path]) -> None:
        """Replace the playlist: fades to black, then plays its first video."""
        with self._lock:
            self._new_playlist = list(playlist)

    def _start(self, background: Path) -> subprocess.Popen:
        return subprocess.Popen(
            build_background_decoder_command(background), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, bufsize=0
        )

    def _read_frame(self, process: subprocess.Popen, frame: memoryview) -> bool:
        filled = 0
        while filled < self.frame_size:
            count = process.stdout.readinto(frame[filled:])
            if not count:
                return False
            filled += count
        return True

    @staticmethod
    def _stop(process: Optional[subprocess.Popen]) -> None:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()

    def _dim(self, frame: memoryview, step: int) -> None:
        """Scale the frame's brightness to step / fade_frames."""
        frame[: self.luma_size] = bytes(frame[: self.luma_size]).translate(self._luma[step])
        frame[self.luma_size :] = bytes(frame[self.luma_size :]).translate(self._chroma[step])

    def stream(self, fd: int, stop: threading.Event) -> None:
        """Write background frames until stopped or the pipe breaks."""
        frame = memoryview(bytearray(self.frame_size))
        current = self._start(self.background)
        warm: Optional[Tuple[int, subprocess.Popen]] = None  # decoder of the next video, ready before the switch
        phase: Optional[str] = None  # "out" (fading to black), "in" (fading up) or None
        step = 0
        target = 0
        replacement: Optional[List[Path]] = None  # new playlist, installed once the old video is black
        try:
            with open(fd, "wb", closefd=True) as output:
                while not stop.is_set():
                    if phase is None:
                        with self._lock:
                            new, self._new_playlist = self._new_playlist, None
                            requested, self._requested = self._requested, False
                        if new is not None:
                            replacement, target, phase, step = new, 0, "out", 0
                        elif requested and len(self._playlist) > 1:
                            target, phase, step = (self._index + 1) % len(self._playlist), "out", 0
                    following = (self._index + 1) % len(self._playlist)
                    if warm is None and len(self._playlist) > 1:
                        warm = (following, self._start(self._playlist[following]))

                    if not self._read_frame(current, frame):
                        raise OSError(f"Background decoder stopped ({self.background})")
                    if phase == "out":
                        self._dim(frame, self.fade_frames - 1 - step)
                    elif phase == "in":
                        self._dim(frame, step + 1)
                    output.write(frame)

                    if phase is not None:
                        step += 1
                        if step < self.fade_frames:
                            continue
                        if phase == "out":
                            self._stop(current)
                            if replacement is not None:
                                self._playlist, replacement = replacement, None
                                self._stop(warm[1] if warm else None)  # it was for the old playlist
                                warm = None
                            if warm is not None and warm[0] == target:
                                current, warm = warm[1], None
                            else:
                                self._stop(warm[1] if warm else None)
                                current, warm = self._start(self._playlist[target]), None
                            self._index = target
                            announce(f"Background switched: {self.background.name}")
                            phase = "in"
                        else:
                            phase = None
                        step = 0
        finally:
            self._stop(current)
            self._stop(warm[1] if warm else None)


def start_encoder(stream_url: str, output_format: str, settings, realtime: bool = True, duration: Optional[float] = None):
    """Launch the encoder; returns (process, background write fd, (panel, title, countdown) write fds)."""
    pipes = [os.pipe() for _ in range(4)]
    read_fds = tuple(read for read, _write in pipes)
    cmd = build_ffmpeg_command(*read_fds, stream_url, output_format, settings, realtime, duration)
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, pass_fds=read_fds)
    for fd in read_fds:
        os.close(fd)
    background_fd, *overlay_fds = (write for _read, write in pipes)
    return process, background_fd, tuple(overlay_fds)


class LiveConfig:
    """Applies edits of the config file while streaming (see config.LIVE_OPTIONS).

    The menu and countdown go to the overlay on the next frame. A new
    background video is prepared (once, cached) in a thread, then swapped in
    between two frames: the stream and the music never stop.
    Options set on the command line are never overridden.
    """

    POLL_SECONDS = 1.0

    def __init__(self, argv, overlay: OverlayFeed) -> None:
        self.argv = argv
        self.overlay = overlay
        self.path = Path(argv.config) if argv.config else None
        self.background = BackgroundFeed(prepare_backgrounds(argv), argv.background_size, argv.video_fps)
        self._sources = tuple(background_sources(argv))
        self._preparing: Optional[tuple] = None
        self._stamp = self._file_stamp()
        self._next_poll = 0.0
        self._apply_overlay()

    def _file_stamp(self) -> Optional[tuple]:
        try:
            stat = self.path.stat()
        except (AttributeError, OSError):
            return None
        return stat.st_mtime_ns, stat.st_size

    def _apply_overlay(self) -> None:
        argv = self.argv
        self.overlay.set_show(
            tuple(menu_lines(argv.menu_text)), argv.countdown_enabled, argv.countdown_hour, argv.countdown_minute
        )

    def poll(self) -> None:
        """Reload the config file if it changed (checked at most once per POLL_SECONDS)."""
        if self.path is None or time.monotonic() < self._next_poll:
            return
        self._next_poll = time.monotonic() + self.POLL_SECONDS
        stamp = self._file_stamp()
        if stamp is None or stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            config, _profile = load_config_file(self.path, self.argv.profile)
            values = {
                name: option_value(OPTIONS_BY_NAME[name], config)
                for name in LIVE_OPTIONS
                if name not in self.argv.cli_options
            }
        except (ValueError, TypeError) as exc:
            announce(f"Config changed but could not be applied: {exc}")
            return
        changed = [name for name, value in values.items() if getattr(self.argv, name) != value]
        if not changed:
            return
        for name in changed:
            setattr(self.argv, name, values[name])
        announce(f"Config changed: applying {', '.join(changed)}")
        self._apply_overlay()
        if "background_videos" in changed or "background_video" in changed:
            self._switch_background()

    def _switch_background(self) -> None:
        sources = tuple(background_sources(self.argv))
        if not sources or sources == self._sources or sources == self._preparing:
            return
        self._preparing = sources

        def prepare() -> None:
            try:
                playlist = prepare_backgrounds(self.argv, sources)
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                announce(f"Background videos {', '.join(sources)} not usable: {exc}")
                return
            finally:
                if self._preparing == sources:
                    self._preparing = None
            if tuple(background_sources(self.argv)) != sources:
                return  # changed again meanwhile
            self._sources = sources
            announce(f"Background videos ready: {', '.join(sources)}")
            self.background.set_playlist(playlist)

        threading.Thread(target=prepare, name="background-prepare", daemon=True).start()


FREEZE_WARN_SECONDS = 2.0  # no new frame for this long is reported as a freeze
SLOW_WINDOW_SECONDS = 10.0
SLOW_RATIO = 0.95  # below this share of the frame rate over SLOW_WINDOW_SECONDS, the stream falls behind
SLOW_GRACE_SECONDS = 30.0  # an encoder warms up below real time for a few seconds after it starts


class ProgressWatchdog:
    """Reads FFmpeg's -progress output: time since the last new frame, output time, and incidents.

    incidents() reports freezes (no new frame for FREEZE_WARN_SECONDS, then
    their total length once frames flow again) and, given the expected `fps`,
    stretches where the encoder runs slower than real time.
    """

    def __init__(self, process: subprocess.Popen, fps: Optional[float] = None) -> None:
        self.last_frame = -1
        self.last_change = time.monotonic()
        self.out_seconds = 0.0  # media time written so far
        self.fps = fps
        self._history: "deque[Tuple[float, int]]" = deque()  # (monotonic, frame) per progress report
        self._first_frame_at: Optional[float] = None
        self._frozen_since: Optional[float] = None
        self._slow_since: Optional[float] = None
        self._slowest = 0.0
        threading.Thread(target=self._read, args=(process,), name="progress", daemon=True).start()

    def _read(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            if line.startswith(b"frame="):
                try:
                    frame = int(line[6:])
                except ValueError:
                    continue
                now = time.monotonic()
                if frame > self.last_frame:
                    self.last_frame = frame
                    self.last_change = now
                    if self._first_frame_at is None and frame > 0:
                        self._first_frame_at = now
                self._history.append((now, frame))
                while self._history and self._history[0][0] < now - SLOW_WINDOW_SECONDS:
                    self._history.popleft()
            elif line.startswith(b"out_time_us="):
                try:
                    self.out_seconds = int(line[12:]) / 1_000_000
                except ValueError:
                    pass  # N/A before the first packet

    @property
    def started(self) -> bool:
        return self.last_frame > 0

    def seconds_since_progress(self) -> float:
        return time.monotonic() - self.last_change

    def incidents(self) -> List[Tuple[str, str]]:
        """(kind, message) for each freeze or slow stretch that began or ended since the last call."""
        found = []
        if not self.started:
            return found
        stalled = self.seconds_since_progress()
        if self._frozen_since is None and stalled > FREEZE_WARN_SECONDS:
            self._frozen_since = self.last_change
            found.append(("freeze", f"no new video frame for {stalled:.1f}s (frame {self.last_frame})"))
        elif self._frozen_since is not None and stalled <= FREEZE_WARN_SECONDS:
            found.append(("freeze-end", f"frames flow again after a {self.last_change - self._frozen_since:.1f}s freeze"))
            self._frozen_since = None
            self._history.clear()  # the freeze must not also count as a slow stretch
        warmed_up = self._first_frame_at is not None and time.monotonic() - self._first_frame_at > SLOW_GRACE_SECONDS
        if self.fps and warmed_up and self._frozen_since is None and len(self._history) >= 2:
            (first_time, first_frame), (last_time, last_frame) = self._history[0], self._history[-1]
            if last_time - first_time >= SLOW_WINDOW_SECONDS * 0.8:
                rate = (last_frame - first_frame) / (last_time - first_time)
                if rate < self.fps * SLOW_RATIO:
                    if self._slow_since is None:
                        self._slow_since, self._slowest = first_time, rate
                        found.append(("slow", f"{rate:.1f} fps over the last {last_time - first_time:.0f}s "
                                      f"({100 * rate / self.fps:.0f}% of real time): falling behind"))
                    self._slowest = min(self._slowest, rate)
                elif self._slow_since is not None:
                    found.append(("slow-end", f"back to real time after {last_time - self._slow_since:.0f}s "
                                  f"(lowest {self._slowest:.1f} of {self.fps:g} fps)"))
                    self._slow_since = None
        return found


STARTUP_GRACE_SECONDS = 30.0


def exit_hint(returncode: Optional[int], watchdog: ProgressWatchdog) -> str:
    """Likely cause of an encoder exit, for the log ('' when there is nothing useful to add)."""
    if returncode == -signal.SIGSEGV and not watchdog.started:
        # FFmpeg 6.1 segfaults when an RTMP server closes the connection after
        # the publish command, which is how Restream & co refuse a stream key.
        return (" (crashed before the first frame: the destination most likely refused the connection; "
                "check the stream key and that nothing else is publishing with it)")
    return ""


def run_cycle(feeder: AudioFeeder, overlay: OverlayFeed, live: LiveConfig, argv) -> bool:
    """Run one encoder process. Returns True when every track has been played."""
    announce("Starting FFmpeg stream")
    process, background_fd, overlay_fds = start_encoder(argv.rtmp_url, argv.output_format, argv)
    watchdog = ProgressWatchdog(process, argv.video_fps)
    written_at_start = feeder.frames_written
    failure: Optional[BaseException] = None

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
            overlay.stream(*overlay_fds, stop)
        except (BrokenPipeError, ValueError, OSError):
            pass

    def feed_background() -> None:
        try:
            live.background.stream(background_fd, stop)
        except (BrokenPipeError, ValueError, OSError) as exc:
            if not stop.is_set() and not isinstance(exc, BrokenPipeError):
                announce(str(exc))

    threads = [
        threading.Thread(target=feed_audio, name="audio-feed", daemon=True),
        threading.Thread(target=feed_overlay, name="overlay-feed", daemon=True),
        threading.Thread(target=feed_background, name="background-feed", daemon=True),
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
            for kind, message in watchdog.incidents():
                announce(warning(f"radio-{kind}", message))
            check_health()
            live.poll()
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
            live.background.track_changed(track)
            announce(f"Now playing: {fields['center']}")
        raise RuntimeError(f"FFmpeg exited with code {process.returncode}{exit_hint(process.returncode, watchdog)}")
    except Exception as exc:
        failure = exc
        raise
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
        if failure is not None:
            sent = (feeder.frames_written - written_at_start) / PCM_SAMPLE_RATE
            announce(warning("radio-restart", f"{failure}. {describe_audio_loss(feeder, sent - watchdog.out_seconds)}"))
            failure.reported = True


def describe_audio_loss(feeder: AudioFeeder, lost: float) -> str:
    """What listeners miss when an encoder dies with `lost` seconds of audio still queued inside it.

    The feeder resumes right after the last chunk it wrote, so that queued audio is skipped.
    """
    track = feeder.current_track
    if track is None or lost < 0.2:
        return "No audio lost."
    position = feeder.track_frames_written / PCM_SAMPLE_RATE  # where the track resumes
    if lost <= position:
        return f"~{lost:.1f}s of audio lost: {track.name} jumps from {clock(position - lost)} to {clock(position)}."
    previous = f"the last {lost - position:.1f}s of {feeder.previous_track.name} and " if feeder.previous_track else ""
    return f"~{lost:.1f}s of audio lost: {previous}the first {clock(position)} of {track.name} (track cut)."


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
        health.start()
        if args.profile:
            announce(f"Using profile: {args.profile}")
        overlay = OverlayFeed(args.video_fps, float(args.track_fade_seconds), announce)
        live = LiveConfig(args, overlay)

        normalizer = None
        if args.loudness_normalize:
            normalizer = LoudnessNormalizer(
                Path(args.cache_dir).expanduser() / "loudness.json",
                args.loudness_target,
                args.loudness_max_gain,
                announce,
            )
        def prepare_upcoming(track: Path) -> None:
            if normalizer:
                normalizer.prioritize(track)
            overlay.upcoming(track)

        if args.r2_bucket:
            # Remote library: only the previous/current/next files are on disk, so
            # there is no whole-library loudness scan; each track is measured
            # (and its metadata read) once its download has completed.
            preloader = preloader_from_args(args, announce)
            preloader.initial_listing()
            tracks = preloader.tracks_forever()
            prepare = preloader.wait_ready

            def on_upcoming(track: Path) -> None:
                preloader.when_ready(track, lambda: prepare_upcoming(track))
        else:
            tracks = shuffled_forever(Path(args.music_dir), normalizer.scan_library if normalizer else lambda _tracks: None)
            prepare = lambda _track: True  # noqa: E731
            on_upcoming = prepare_upcoming

        feeder = AudioFeeder(
            tracks,
            float(args.track_fade_seconds),
            announce,
            gain_for=normalizer.gain_db if normalizer else (lambda _track, _may_block: 0.0),
            on_upcoming=on_upcoming,
            prepare=prepare,
        )

        while True:
            try:
                # The feeder keeps its place, so a restarted encoder resumes the same track.
                if run_cycle(feeder, overlay, live, args):
                    break
            except KeyboardInterrupt:
                announce("Shutting down cleanly.")
                break
            except Exception as exc:
                if not getattr(exc, "reported", False):
                    announce(warning("radio-restart", str(exc)))
                announce(f"Restarting the radio encoder in {args.restart_delay}s")
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
