"""Bucket storage (Cloudflare R2 or S3): listing, preloading, playback, settings. No network: a fake S3 client.

    python -m unittest livestream.test_bucket
"""

import datetime
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from livestream import r2
from livestream.audio_feed import AudioFeeder
from livestream.config import validate_config
from livestream.media import display_stem, extract_track_fields, ffprobe_json


class FakeS3:
    """list_objects_v2 / get_object over a local folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.gets = []

    def list_objects_v2(self, **_params):
        files = sorted(path for path in self.root.rglob("*") if path.is_file())
        return {"IsTruncated": False, "Contents": [
            {"Key": path.relative_to(self.root).as_posix(), "Size": path.stat().st_size,
             "LastModified": datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc)}
            for path in files
        ]}

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        return {"Body": (self.root / Key).open("rb")}


def fake_library(bucket: Path, cache: Path, prefix: str) -> r2.R2Library:
    library = r2.R2Library.__new__(r2.R2Library)  # without boto3
    library.bucket, library.prefix, library.is_r2 = "test", r2.normalize_prefix(prefix), True
    library.client = FakeS3(bucket)
    library.budget = r2.OperationBudget(cache / "r2-operations.json", 1000, 1000)
    library.tracks_dir = cache / "r2-tracks"
    library.tracks_dir.mkdir(parents=True)
    library.listing_file = cache / "r2-listing.json"
    return library


def make_track(path: Path, seconds: float, **tags: str) -> None:
    metadata = [arg for key, value in tags.items() for arg in ("-metadata", f"{key}={value}")]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=d={seconds}",
                    "-ar", "48000", "-ac", "2", *metadata, str(path)], check=True)


class PlaybackTest(unittest.TestCase):
    def test_tracks_play_from_the_bucket_with_three_files_on_disk_at_most(self):
        with tempfile.TemporaryDirectory() as folder:
            bucket, cache = Path(folder) / "bucket", Path(folder) / "cache"
            for number in range(1, 5):
                make_track(bucket / "music" / f"Song {number}.mp3", 2, title=f"Song {number}")
            make_track(bucket / "music2" / "Other.mp3", 2)  # outside the prefix "music"
            library = fake_library(bucket, cache, "music")
            log, started, on_disk = [], [], []
            preloader = r2.R2Preloader(library, log.append)
            preloader.initial_listing()
            stop = threading.Event()

            def track_started(track: Path) -> None:
                started.append(track)
                on_disk.append(len([path for path in library.tracks_dir.iterdir() if not path.name.endswith(".partial")]))
                if len(started) >= 10:
                    stop.set()

            feeder = AudioFeeder(preloader.tracks_forever(), 0.5, log.append, prepare=preloader.wait_ready,
                                 on_upcoming=lambda track: preloader.when_ready(track, lambda: None))

            class Sink:
                def write(self, _data): pass
                def flush(self): pass

            feeder.feed(Sink(), track_started, stop)
            feeder.close()
            preloader.close()

        self.assertEqual(len(started), 10)
        self.assertEqual({display_stem(track) for track in started}, {f"Song {number}" for number in range(1, 5)})
        self.assertLessEqual(max(on_disk), 3)
        self.assertEqual([line for line in log if "WARNING" in line], [])
        self.assertLessEqual(len(library.client.gets), 10)  # a track still on disk is not downloaded again


class HelpersTest(unittest.TestCase):
    def test_region(self):
        self.assertEqual(r2.s3_region(None, None), "auto")
        self.assertEqual(r2.s3_region("https://s3.eu-west-3.amazonaws.com", None), "eu-west-3")
        self.assertEqual(r2.s3_region("https://s3.amazonaws.com", None), "us-east-1")
        self.assertEqual(r2.s3_region("https://minio.example.com", None), "us-east-1")
        self.assertEqual(r2.s3_region("https://minio.example.com", "eu-1"), "eu-1")

    def test_prefix(self):
        self.assertEqual(r2.normalize_prefix(None), "")
        self.assertEqual(r2.normalize_prefix("/music"), "music/")
        self.assertEqual(r2.normalize_prefix("music/"), "music/")

    def test_downloaded_untagged_track_shows_its_original_name(self):
        with tempfile.TemporaryDirectory() as folder:
            track = Path(folder) / "r2-tracks" / "4020d28920e4-My Song.mp3"
            make_track(track, 1)
            fields = extract_track_fields(track, ffprobe_json(track))
        self.assertEqual((fields["center"], fields["title"]), ("My Song", "My Song"))
        self.assertEqual(display_stem(Path("music/4020d28920e4-Local.mp3")), "4020d28920e4-Local")  # not a download

    def test_settings(self):
        base = {"background_video": __file__, "rtmp_url": "rtmp://x/y", "r2_bucket": "b",
                "r2_access_key_id": "k", "r2_secret_access_key": "s"}
        _clean, errors = validate_config(base)
        self.assertIn("r2_account_id", errors)  # R2 needs it
        _clean, errors = validate_config({**base, "r2_endpoint": "https://s3.eu-west-3.amazonaws.com"})
        self.assertNotIn("r2_account_id", errors)
        self.assertNotIn("music_dir", errors)


if __name__ == "__main__":
    unittest.main()
