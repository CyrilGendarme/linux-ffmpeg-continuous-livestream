"""Background playlist: config parsing and the fade-to-black switch between videos.

    python -m unittest livestream.test_background
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from livestream.config import background_sources, load_config_file, option_value, OPTIONS_BY_NAME, validate_config
from livestream.runner import BackgroundFeed

SIZE = "4x2"  # 8 luma + 4 chroma bytes per yuv420p frame
FRAME = 12
FPS = 4  # 1 s fade = 4 frames


class FakeDecoder:
    """Endless frames of one flat colour: luma `value`, chroma 128."""

    def __init__(self, value: int) -> None:
        self.frame = bytes([value] * 8 + [128] * 4)
        self.killed = False
        self.stdout = self

    def readinto(self, buffer) -> int:
        buffer[: len(self.frame)] = self.frame[: len(buffer)]
        return min(len(buffer), len(self.frame))

    def poll(self):
        return 0 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> None:
        pass


class FakeFeed(BackgroundFeed):
    """Fake decoders; `hooks` maps a frame number to a callback run by the feed thread just after it reads that frame."""

    VALUES = {"a": 200, "b": 100, "c": 50}

    def __init__(self, names, hooks=None) -> None:
        super().__init__([Path(name) for name in names], SIZE, FPS)
        self.started = []
        self.hooks = hooks or {}
        self.reads = 0

    def _start(self, background: Path):
        self.started.append(background.name)
        return FakeDecoder(self.VALUES[background.name])

    def _read_frame(self, process, frame) -> bool:
        ok = super()._read_frame(process, frame)
        hook = self.hooks.get(self.reads)
        self.reads += 1
        if hook:
            hook()
        return ok


def luma_frames(feed: BackgroundFeed, count: int):
    """Run feed.stream and return the first luma byte of each of the first `count` frames."""
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    def run() -> None:
        try:
            feed.stream(write_fd, stop)
        except OSError:
            pass  # the reader closed the pipe

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    with open(read_fd, "rb") as reader:
        lumas = [reader.read(FRAME)[0] for _ in range(count)]
    stop.set()
    thread.join(timeout=5)
    return lumas


def changes(feed: BackgroundFeed, *frames):
    """Hooks that announce a new track after the given frame numbers."""
    return {frame: (lambda n=n: feed.track_changed(Path(f"t{n}.mp3"))) for n, frame in enumerate(frames, start=2)}


class BackgroundFeedTest(unittest.TestCase):
    def feed(self, names, *change_frames):
        feed = FakeFeed(names)
        feed.track_changed(Path("t1.mp3"))  # first track: no switch
        feed.hooks = changes(feed, *change_frames)
        return feed

    def test_track_change_fades_to_black_then_plays_next_video(self):
        lumas = luma_frames(self.feed(["a", "b"], 1), 14)
        self.assertEqual(lumas[:2], [200, 200])
        fade_out, fade_in = lumas[2:6], lumas[6:10]
        self.assertEqual(fade_out[-1], 16)  # black
        self.assertEqual(fade_out, sorted(fade_out, reverse=True))
        self.assertEqual(fade_in, sorted(fade_in))
        self.assertEqual(fade_in[-1], 100)  # video b, full brightness
        self.assertEqual(set(lumas[10:]), {100})

    def test_next_decoder_is_started_ahead_and_playlist_wraps(self):
        feed = self.feed(["a", "b"], 1, 12)
        lumas = luma_frames(feed, 30)
        self.assertEqual(lumas[10], 100)
        self.assertEqual(lumas[-1], 200)  # back on a after b
        self.assertEqual(feed.started[:2], ["a", "b"])  # b was already warm before the first switch

    def test_single_video_and_resumed_track_never_switch(self):
        self.assertEqual(set(luma_frames(self.feed(["a"], 1, 3, 5), 10)), {200})
        feed = FakeFeed(["a", "b"])
        feed.track_changed(Path("t1.mp3"))
        feed.hooks = {1: lambda: feed.track_changed(Path("t1.mp3"))}
        self.assertEqual(set(luma_frames(feed, 10)), {200})

    def test_new_playlist_fades_to_its_first_video(self):
        feed = FakeFeed(["a", "b"], {1: lambda: feed.set_playlist([Path("c")])})
        lumas = luma_frames(feed, 12)
        self.assertEqual(lumas[-1], 50)
        self.assertIn(16, lumas)


class ConfigTest(unittest.TestCase):
    def test_sources_from_list_string_or_single_video(self):
        self.assertEqual(background_sources(SimpleNamespace(background_videos="a.mp4, b c.mp4 ,", background_video="z.mp4")),
                         ["a.mp4", "b c.mp4"])
        self.assertEqual(background_sources(SimpleNamespace(background_videos=None, background_video="z.mp4")), ["z.mp4"])
        self.assertEqual(background_sources(SimpleNamespace(background_videos="", background_video=None)), [])

    def test_json_list_and_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b'\xef\xbb\xbf{"background_videos": ["a.mp4", "b.mp4"]}')
            config, _ = load_config_file(path)
        self.assertEqual(option_value(OPTIONS_BY_NAME["background_videos"], config), "a.mp4, b.mp4")

    def test_validation_needs_a_video_and_checks_each_file(self):
        base = {"music_dir": ".", "rtmp_url": "rtmp://x/y"}
        _, errors = validate_config(base, check_paths=False)
        self.assertIn("background_videos", errors)
        _, errors = validate_config({**base, "background_video": "a.mp4"}, check_paths=False)
        self.assertNotIn("background_videos", errors)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.mp4").write_bytes(b"x")
            _, errors = validate_config({**base, "background_videos": "a.mp4, gone.mp4"}, base_dir=Path(tmp))
            self.assertIn("gone.mp4", errors["background_videos"])
            _, errors = validate_config({**base, "background_videos": ["a.mp4"]}, base_dir=Path(tmp))
            self.assertNotIn("background_videos", errors)


if __name__ == "__main__":
    unittest.main()
