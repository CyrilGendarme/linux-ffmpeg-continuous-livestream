"""Incident reports: track cuts, freezes, slow stretches, live path jumps and gaps, audio lost on a restart.

    python -m unittest livestream.test_incidents
"""

import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from livestream import flv
from livestream.audio_feed import AudioFeeder
from livestream.health import clock, snapshot, warning
from livestream.runner import ProgressWatchdog, describe_audio_loss


def make_track(path: Path, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=d={seconds}",
                    "-ar", "48000", "-ac", "2", str(path)], check=True)


class TrackCutTest(unittest.TestCase):
    def test_truncated_track_is_reported_and_a_complete_one_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            # MP3: its header keeps the full length (ffprobe estimates a WAV's from the file size).
            cut, whole = Path(folder) / "cut.mp3", Path(folder) / "whole.mp3"
            make_track(cut, 10)
            make_track(whole, 4)
            with cut.open("r+b") as handle:  # its header still says 10 s
                handle.truncate(cut.stat().st_size // 2)
            log = []
            feeder = AudioFeeder(iter([cut, whole]), 0.5, log.append)
            sink = io.BytesIO()
            self.assertTrue(feeder.feed(sink, lambda _track: None, threading.Event()))

        cuts = [line for line in log if "track-cut" in line]
        self.assertEqual(len(cuts), 1, log)
        self.assertRegex(cuts[0], r"cut.mp3 ended early: decoded 0:0[45] of 0:10: the file is truncated")
        self.assertEqual(feeder.frames_written * 8, len(sink.getvalue()))
        self.assertEqual(feeder.previous_track.name, "cut.mp3")


class WatchdogTest(unittest.TestCase):
    def watchdog(self) -> ProgressWatchdog:
        return ProgressWatchdog(SimpleNamespace(stdout=[]), fps=30)

    def test_freeze_is_reported_with_its_length(self):
        watchdog, now = self.watchdog(), time.monotonic()
        watchdog.last_frame, watchdog.last_change = 100, now - 3
        self.assertEqual([kind for kind, _ in watchdog.incidents()], ["freeze"])
        self.assertEqual(watchdog.incidents(), [])  # once per freeze
        watchdog.last_change = now + 1.5  # frames again, 4.5 s after the last one
        (kind, message), = watchdog.incidents()
        self.assertEqual(kind, "freeze-end")
        self.assertIn("4.5s freeze", message)

    def test_slow_stretch_is_reported_then_its_end(self):
        watchdog, now = self.watchdog(), time.monotonic()
        watchdog.last_frame, watchdog.last_change = 1, now
        watchdog._first_frame_at = now - 10
        watchdog._history.extend((now - 10 + second, second * 20) for second in range(11))
        self.assertEqual(watchdog.incidents(), [])  # still warming up
        watchdog._history.clear()
        watchdog._first_frame_at = now - 60
        watchdog._history.extend((now - 10 + second, second * 20) for second in range(11))  # 20 of 30 fps
        (kind, message), = watchdog.incidents()
        self.assertEqual(kind, "slow")
        self.assertIn("20.0 fps", message)
        watchdog._history.clear()
        watchdog._history.extend((now - 10 + second, second * 30) for second in range(11))
        (kind, message), = watchdog.incidents()
        self.assertEqual(kind, "slow-end")
        self.assertIn("lowest 20.0 of 30 fps", message)


def flv_tag(kind: int, timestamp: int, data: bytes = b"\x17\x01xxxx") -> bytes:
    header = bytes([kind]) + len(data).to_bytes(3, "big") + (timestamp & 0xFFFFFF).to_bytes(3, "big")
    header += bytes([timestamp >> 24]) + b"\0\0\0"
    return header + data + (len(header) + len(data)).to_bytes(4, "big")


class SlowSource(io.BytesIO):
    """Pauses once, before the byte at `pause_at`."""

    def __init__(self, data: bytes, pause_at: int, seconds: float) -> None:
        super().__init__(data)
        self.pause_at, self.seconds = pause_at, seconds

    def read(self, size=-1):
        if self.tell() == self.pause_at:
            time.sleep(self.seconds)
        return super().read(size)


class RetimeTest(unittest.TestCase):
    def test_gap_and_skipped_content_are_reported(self):
        head = b"FLV\x01\x05\0\0\0\x09" + b"\0\0\0\0"
        before = b"".join(flv_tag(flv.VIDEO, ms) for ms in (0, 33, 66))
        after = b"".join(flv_tag(flv.VIDEO, ms) for ms in (5066, 5100))
        source = SlowSource(head + before + after, len(head + before), 0.2)
        log = []
        with mock.patch.object(flv, "INPUT_GAP_SECONDS", 0.1), self.assertRaises(EOFError):
            flv.retime(source, io.BytesIO(), lambda kind, message: log.append((kind, message)))
        self.assertEqual([kind for kind, _ in log], ["input-gap", "input-jump"])
        self.assertIn("jumped +5000 ms (source content missing, dropped upstream)", log[1][1])


class AudioLossTest(unittest.TestCase):
    def feeder(self, position: float) -> SimpleNamespace:
        return SimpleNamespace(current_track=Path("b.mp3"), previous_track=Path("a.mp3"),
                               track_frames_written=int(position * 48000))

    def test_loss_within_the_track(self):
        self.assertEqual(describe_audio_loss(self.feeder(65), 5.0),
                         "~5.0s of audio lost: b.mp3 jumps from 1:00 to 1:05.")

    def test_loss_reaching_into_the_previous_track(self):
        self.assertEqual(describe_audio_loss(self.feeder(2), 5.0),
                         "~5.0s of audio lost: the last 3.0s of a.mp3 and the first 0:02 of b.mp3 (track cut).")

    def test_nothing_lost(self):
        self.assertEqual(describe_audio_loss(self.feeder(30), 0.1), "No audio lost.")


class HealthTest(unittest.TestCase):
    def test_warning_line(self):
        line = warning("radio-freeze", "no frame")
        self.assertRegex(line, r"^WARNING \d{4}-\d\d-\d\d \d\d:\d\d:\d\d radio-freeze: no frame \| ")
        self.assertIn("mem ", snapshot())
        self.assertEqual(clock(125.9), "2:05")


if __name__ == "__main__":
    unittest.main()
