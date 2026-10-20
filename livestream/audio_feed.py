"""Stream tracks one after another into the encoder as raw PCM.

Only the current track (and, during a crossfade, the next one) has a decoder
running. The mixer keeps just the last `fade` seconds of the current track in
memory so it can crossfade it with the head of the next track.

The feeder outlives the encoder: if the encoder is restarted (network drop,
stall), feeding resumes from the exact position in the current track.
"""

import subprocess
import threading
from array import array
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional, Union

from .ffmpeg import PCM_CHANNELS, PCM_SAMPLE_RATE, build_decoder_command

SAMPLE_BYTES = 4  # f32le
CHUNK_FRAMES = 4800  # 100 ms


class TrackDecoder:
    def __init__(self, track: Path, gain_db: float) -> None:
        self.track = track
        self.process = subprocess.Popen(
            build_decoder_command(track, gain_db),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )

    def read(self, frames: int) -> array:
        """Read up to `frames` stereo frames; returns fewer only at end of track."""
        wanted = frames * PCM_CHANNELS * SAMPLE_BYTES
        data = bytearray()
        while len(data) < wanted:
            block = self.process.stdout.read(wanted - len(data))
            if not block:
                break
            data.extend(block)
        usable = len(data) - len(data) % (PCM_CHANNELS * SAMPLE_BYTES)
        samples = array("f")
        samples.frombytes(bytes(data[:usable]))
        return samples

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.stdout.close()
        self.process.wait()


def _apply_gain_ramp(samples: array, start: float, end: float) -> None:
    frames = len(samples) // PCM_CHANNELS
    if frames == 0:
        return
    step = (end - start) / frames
    for frame in range(frames):
        gain = start + step * frame
        base = frame * PCM_CHANNELS
        for channel in range(PCM_CHANNELS):
            samples[base + channel] *= gain


def _crossfade(tail: array, head: array) -> array:
    frames = min(len(tail), len(head)) // PCM_CHANNELS
    count = frames * PCM_CHANNELS
    mixed = array("f", bytes(count * SAMPLE_BYTES))
    for frame in range(frames):
        fade_in = frame / frames
        base = frame * PCM_CHANNELS
        for channel in range(PCM_CHANNELS):
            index = base + channel
            mixed[index] = tail[index] * (1.0 - fade_in) + head[index] * fade_in
    return mixed


class AudioFeeder:
    def __init__(
        self,
        tracks: Iterator[Path],
        fade_seconds: float,
        log: Callable[[str], None],
        gain_for: Callable[[Path, bool], float] = lambda _track, _may_block: 0.0,
        on_upcoming: Callable[[Path], None] = lambda _track: None,
    ) -> None:
        self._tracks = tracks
        self._lookahead: Optional[Path] = None
        self._fade_frames = max(0, int(fade_seconds * PCM_SAMPLE_RATE))
        self._log = log
        self._gain_for = gain_for
        self._on_upcoming = on_upcoming
        self._chunks = self._generate()
        self._pending: Optional[bytes] = None
        self.current_track: Optional[Path] = None

    def _next_track(self) -> Optional[Path]:
        track = self._lookahead if self._lookahead is not None else next(self._tracks, None)
        self._lookahead = next(self._tracks, None) if track is not None else None
        if self._lookahead is not None:
            self._on_upcoming(self._lookahead)  # e.g. measure loudness while `track` plays
        return track

    def _open_next(self) -> Optional[tuple[TrackDecoder, array]]:
        """Open the next playable track and return it with its first fade_frames of audio."""
        while True:
            track = self._next_track()
            if track is None:
                return None
            # Blocking is only acceptable before any audio has been sent.
            gain = self._gain_for(track, self.current_track is None)
            if gain:
                self._log(f"Loudness gain {gain:+.1f} dB for {track.name}")
            decoder = TrackDecoder(track, gain)
            head = decoder.read(max(self._fade_frames, CHUNK_FRAMES))
            if head:
                return decoder, head
            decoder.close()
            self._log(f"Skipping unreadable track: {track}")

    def _generate(self) -> Iterator[Union[bytes, Path]]:
        """Yield PCM byte chunks, and a Path at the moment each track becomes audible."""
        fade_samples = self._fade_frames * PCM_CHANNELS
        opened = self._open_next()
        if opened is None:
            return
        current, held = opened
        _apply_gain_ramp(held, 0.0, 1.0)  # fade in the very first track
        yield current.track
        try:
            while True:
                chunk = current.read(CHUNK_FRAMES)
                if chunk:
                    held.extend(chunk)
                    # Keep only the tail needed for the crossfade; the rest can play.
                    if len(held) > fade_samples:
                        cut = len(held) - fade_samples
                        yield held[:cut].tobytes()
                        del held[:cut]
                    continue

                # Current track finished: `held` is its last fade_seconds.
                current.close()
                opened = self._open_next()
                if opened is None:
                    _apply_gain_ramp(held, 1.0, 0.0)
                    yield held.tobytes()
                    return
                current, head = opened
                overlap = min(len(held), len(head))
                yield held[: len(held) - overlap].tobytes()
                yield current.track
                yield _crossfade(held[len(held) - overlap :], head[:overlap]).tobytes()
                held = head[overlap:]
        finally:
            current.close()

    def feed(
        self,
        sink: BinaryIO,
        on_track_start: Callable[[Path], None],
        stop: threading.Event,
    ) -> bool:
        """Write audio to `sink` until `stop` is set or the sink breaks.

        Returns True when all tracks have been played. A chunk that could not be
        written is kept and re-sent first on the next call.
        """
        if self.current_track is not None:
            on_track_start(self.current_track)  # resumed: show the track again
        while not stop.is_set():
            if self._pending is None:
                item = next(self._chunks, None)
                if item is None:
                    sink.flush()
                    return True
                if isinstance(item, Path):
                    self.current_track = item
                    on_track_start(item)
                    continue
                self._pending = item
            sink.write(self._pending)
            self._pending = None
        return False

    def close(self) -> None:
        self._chunks.close()
