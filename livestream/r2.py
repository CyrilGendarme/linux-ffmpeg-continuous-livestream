"""Tracks stored in a Cloudflare R2 bucket (or any S3 service), downloaded one track ahead.

- Initial listing: the bucket is listed once at startup (and saved, so a
  restart without network still knows the library). The listing is refreshed
  in the background once per shuffle pass, never at a track boundary.
- Full preloading: as soon as the feeder knows the next track (that is, when
  the current one starts), the whole file is downloaded to the local cache.
  The decoder only ever reads complete local files.
- Only the previous, current and next tracks are kept on disk.

Free-tier guard (Cloudflare R2: 10 GB stored, 1M class A, 10M class B ops per
month). Every request this process makes is counted in a per-month file in
the cache, and R2 is no longer called once a safety cap is reached:
- class A (ListObjectsV2): one request per 1000 objects, once per pass;
- class B (GetObject): exactly one request per downloaded track (no multipart,
  no HEAD, no automatic retries), none when the file is already cached.
The radio never writes to the bucket, so it adds nothing to stored size; it
only warns when the bucket gets close to 10 GB.

Other S3 services (AWS S3, Backblaze B2, MinIO...) work through `r2_endpoint`.
The same operation caps then still limit what a runaway process could cost,
but the 10 GB storage limit is R2's only and is not applied.
"""

import hashlib
import json
import os
import queue
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from .constants import BUCKET_TRACKS_DIR, SUPPORTED_AUDIO_EXTENSIONS
from .health import warning

# Free tier, per calendar month (UTC). Caps leave a 10% margin for anything
# else using the same account (uploads from your laptop are class A too).
FREE_CLASS_A = 1_000_000
FREE_CLASS_B = 10_000_000
FREE_STORAGE_BYTES = 10 * 1000**3
CAP_RATIO = 0.9
STORAGE_WARNING_BYTES = int(FREE_STORAGE_BYTES * CAP_RATIO)

KEEP_ON_DISK = 3  # previous, current and next track
DOWNLOAD_CHUNK = 1024 * 1024


class BudgetExceeded(RuntimeError):
    pass


class OperationBudget:
    """Counts R2 operations per month on disk and refuses them past the caps."""

    def __init__(self, path: Path, cap_a: int, cap_b: int) -> None:
        self.path = path
        self.caps = {"class_a": cap_a, "class_b": cap_b}
        self._lock = threading.Lock()
        self._state = self._load()

    @staticmethod
    def _month() -> str:
        return time.strftime("%Y-%m", time.gmtime())

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("month") != self._month():
            state = {"month": self._month(), "class_a": 0, "class_b": 0}
        return state

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state), encoding="utf-8")
        os.replace(temporary, self.path)

    def _roll(self) -> None:
        if self._state["month"] != self._month():
            self._state = {"month": self._month(), "class_a": 0, "class_b": 0}

    def can_spend(self, kind: str, count: int = 1) -> bool:
        with self._lock:
            self._state = self._load()  # another process may have spent some
            return self._state[kind] + count <= self.caps[kind]

    def spend(self, kind: str) -> None:
        """Record one request before sending it; raises when the cap is reached.

        The file is re-read under an exclusive lock, so the radio and the push
        script can share one counter.
        """
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.path.with_suffix(".lock"), "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            self._state = self._load()
            if self._state[kind] >= self.caps[kind]:
                raise BudgetExceeded(f"Bucket {kind.replace('_', ' ')} cap reached for {self._state['month']} "
                                     f"({self.caps[kind]} operations)")
            self._state[kind] += 1
            self._save()

    def summary(self) -> str:
        with self._lock:
            self._roll()
            return (f"{self._state['month']}: class A {self._state['class_a']}/{self.caps['class_a']}, "
                    f"class B {self._state['class_b']}/{self.caps['class_b']}")


@dataclass(frozen=True)
class RemoteTrack:
    key: str
    size: int
    modified: float  # unix time


def is_r2_endpoint(endpoint: Optional[str]) -> bool:
    """No endpoint (built from the account ID) or an explicit R2 one, e.g. an EU-jurisdiction bucket's."""
    return not endpoint or bool(re.match(r"https?://[^/]+\.r2\.cloudflarestorage\.com(/|$)", endpoint))


def s3_region(endpoint: Optional[str], region: Optional[str]) -> str:
    """The signing region: as configured, else read from an AWS endpoint, else auto (R2) or us-east-1."""
    if region:
        return region
    if is_r2_endpoint(endpoint):
        return "auto"
    match = re.match(r"https?://s3[.-]([a-z0-9-]+)\.amazonaws\.com", endpoint)
    return match.group(1) if match and match.group(1) != "external-1" else "us-east-1"


class R2Library:
    """Lists and downloads audio files of one bucket (optionally under a prefix).

    Cloudflare R2 by default (endpoint from the account ID), or any S3 service given its `endpoint`.
    """

    def __init__(self, account_id: Optional[str], bucket: str, prefix: str, access_key_id: str,
                 secret_access_key: str, cache_dir: Path, budget: OperationBudget,
                 endpoint: Optional[str] = None, region: Optional[str] = None) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("Bucket storage needs boto3: pip install -r requirements.txt") from exc
        self.bucket = bucket
        self.prefix = normalize_prefix(prefix)
        self.is_r2 = is_r2_endpoint(endpoint)
        self.budget = budget
        self.tracks_dir = cache_dir / BUCKET_TRACKS_DIR
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        self.listing_file = cache_dir / "r2-listing.json"
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=s3_region(endpoint, region),
            # One counted request per call: botocore must not retry behind our back.
            config=Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=30),
        )

    # -- listing (class A) ----------------------------------------------------

    def list_tracks(self) -> List[RemoteTrack]:
        """Audio objects under the prefix. Also sets `stored_bytes` (every object listed)."""
        tracks: List[RemoteTrack] = []
        token: Optional[str] = None
        self.stored_bytes = 0
        while True:
            self.budget.spend("class_a")
            # Whole bucket (filtered below), so stored_bytes covers every object.
            params = {"Bucket": self.bucket, "MaxKeys": 1000}
            if token:
                params["ContinuationToken"] = token
            page = self.client.list_objects_v2(**params)
            for item in page.get("Contents", []):
                key = item["Key"]
                self.stored_bytes += int(item["Size"])
                if key.startswith(self.prefix) and Path(key).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS and item["Size"] > 0:
                    tracks.append(RemoteTrack(key, int(item["Size"]), item["LastModified"].timestamp()))
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
        self._save_listing(tracks)
        return tracks

    def _save_listing(self, tracks: List[RemoteTrack]) -> None:
        temporary = self.listing_file.with_suffix(".tmp")
        temporary.write_text(json.dumps([track.__dict__ for track in tracks]), encoding="utf-8")
        os.replace(temporary, self.listing_file)

    def saved_listing(self) -> List[RemoteTrack]:
        try:
            return [RemoteTrack(**item) for item in json.loads(self.listing_file.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            return []

    # -- files (class B) ------------------------------------------------------

    def local_path(self, track: RemoteTrack) -> Path:
        digest = hashlib.sha1(track.key.encode()).hexdigest()[:12]
        name = re.sub(r"[^\w.\- ()&',]", "_", Path(track.key).name)[-120:]
        return self.tracks_dir / f"{digest}-{name}"

    def is_cached(self, track: RemoteTrack) -> bool:
        try:
            return self.local_path(track).stat().st_size == track.size
        except OSError:
            return False

    def download(self, track: RemoteTrack) -> Path:
        """Download the whole file (one GetObject), unless it is already cached."""
        target = self.local_path(track)
        if self.is_cached(track):
            return target
        self.budget.spend("class_b")
        partial = target.with_name(target.name + ".partial")
        response = self.client.get_object(Bucket=self.bucket, Key=track.key)
        with partial.open("wb") as handle:
            body = response["Body"]
            try:
                for chunk in iter(lambda: body.read(DOWNLOAD_CHUNK), b""):
                    handle.write(chunk)
            finally:
                body.close()
        if partial.stat().st_size != track.size:
            partial.unlink(missing_ok=True)
            raise OSError(f"incomplete download of {track.key}")
        # Same mtime on every download, so the loudness cache key stays valid.
        os.utime(partial, (track.modified, track.modified))
        os.replace(partial, target)
        return target

    # -- uploads (class A) ----------------------------------------------------

    def upload(self, local: Path, key: str) -> None:
        """Upload one file with a single PutObject (no multipart: 1 class A operation)."""
        import mimetypes

        self.budget.spend("class_a")
        content_type = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        with local.open("rb") as body:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                   ContentLength=local.stat().st_size, ContentType=content_type)


class R2Preloader:
    """Shuffled R2 tracks as local paths; each file is fully downloaded before it plays."""

    def __init__(self, library: R2Library, log: Callable[[str], None]) -> None:
        self.library = library
        self.log = log
        self._listing: List[RemoteTrack] = []
        self._refreshed: Optional[List[RemoteTrack]] = None
        self._remote: Dict[Path, RemoteTrack] = {}
        self._ready: Dict[Path, threading.Event] = {}
        self._ok: Dict[Path, bool] = {}
        self._callbacks: Dict[Path, List[Callable[[], None]]] = {}
        self._recent: "deque[Path]" = deque(maxlen=KEEP_ON_DISK)
        self._lock = threading.Lock()
        self._downloads: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._offline_logged = False
        threading.Thread(target=self._worker, name="r2-download", daemon=True).start()

    # -- listing --------------------------------------------------------------

    def initial_listing(self) -> None:
        """List the bucket once before streaming (falls back to the last saved listing)."""
        try:
            self._listing = self.library.list_tracks()
            self.log(f"Bucket: {len(self._listing)} track(s) in bucket {self.library.bucket} "
                     f"({self.library.budget.summary()})")
        except Exception as exc:  # network, credentials, budget
            self._listing = self.library.saved_listing()
            self.log(f"Bucket: listing failed ({exc}); using the saved listing of {len(self._listing)} track(s)")
        if not self._listing:
            raise FileNotFoundError(f"No audio tracks found in bucket {self.library.bucket}/{self.library.prefix}")
        self._check_storage(self._listing)

    def _check_storage(self, tracks: List[RemoteTrack]) -> None:
        if not self.library.is_r2:
            return  # the free storage limit is R2's
        total = getattr(self.library, "stored_bytes", 0) or sum(track.size for track in tracks)
        if total > STORAGE_WARNING_BYTES:
            self.log(warning("bucket-storage", f"the bucket holds {total / 1000**3:.2f} GB, "
                             f"close to the {FREE_STORAGE_BYTES / 1000**3:.0f} GB R2 free storage"))

    def _refresh_in_background(self) -> None:
        def refresh() -> None:
            try:
                tracks = self.library.list_tracks()
            except Exception as exc:
                self.log(f"Bucket: listing refresh failed ({exc}); keeping the current listing")
                return
            if tracks:
                self._check_storage(tracks)
                self._refreshed = tracks

        threading.Thread(target=refresh, name="r2-list", daemon=True).start()

    # -- track order ----------------------------------------------------------

    def tracks_forever(self) -> Iterator[Path]:
        """Yield local paths in shuffled order; the download of each starts when it is yielded."""
        if not self._listing:
            self.initial_listing()
        while True:
            if self._refreshed is not None:
                self._listing, self._refreshed = self._refreshed, None
            order = list(self._listing)
            random.shuffle(order)
            self.log(f"Bucket: shuffled {len(order)} track(s)")
            self._refresh_in_background()  # ready for the next pass, off the audio path
            for track in order:
                yield self._schedule(track)

    def _schedule(self, track: RemoteTrack) -> Path:
        library = self.library
        if not library.is_cached(track) and not library.budget.can_spend("class_b"):
            fallback = self._cached_fallback()
            if not self._offline_logged:
                self._offline_logged = True
                self.log("Bucket: monthly class B cap reached; replaying tracks already on disk")
            if fallback is not None:
                track = fallback
            else:
                time.sleep(5.0)  # nothing playable: don't spin through the listing
        path = library.local_path(track)
        with self._lock:
            self._remote[path] = track
            if path not in self._ready or (self._ready[path].is_set() and not self._ok.get(path)):
                self._ready[path] = threading.Event()
                self._ok.pop(path, None)
                self._downloads.put(path)
            if path in self._recent:
                self._recent.remove(path)
            self._recent.append(path)
        self._evict()
        return path

    def _cached_fallback(self) -> Optional[RemoteTrack]:
        cached = [track for track in self._listing if self.library.is_cached(track)]
        return random.choice(cached) if cached else None

    # -- downloads ------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            path = self._downloads.get()
            if path is None:
                return
            with self._lock:
                track = self._remote[path]
            started = time.monotonic()
            try:
                cached = self.library.is_cached(track)
                self.library.download(track)
                ok = True
                if not cached:
                    self.log(f"Bucket: preloaded {Path(track.key).name} "
                             f"({track.size / 1e6:.1f} MB in {time.monotonic() - started:.1f}s)")
            except Exception as exc:
                ok = False
                self.log(warning("bucket-download", f"download of {track.key} failed after "
                                 f"{time.monotonic() - started:.1f}s: {exc}"))
            with self._lock:
                self._ok[path] = ok
                callbacks = self._callbacks.pop(path, []) if ok else []
                self._ready[path].set()
                if not ok:
                    self._callbacks.pop(path, None)
            for callback in callbacks:
                try:
                    callback()
                except Exception as exc:
                    self.log(f"Bucket: preload callback failed: {exc}")

    def when_ready(self, path: Path, callback: Callable[[], None]) -> None:
        """Run `callback` once `path` is fully on disk (right away if it already is)."""
        with self._lock:
            event = self._ready.get(path)
            if event is not None and not event.is_set():
                self._callbacks.setdefault(path, []).append(callback)
                return
            ok = self._ok.get(path, path.exists())
        if ok:
            callback()

    def wait_ready(self, path: Path) -> bool:
        """Block until `path` is fully downloaded. False if the download failed."""
        with self._lock:
            event = self._ready.get(path)
        if event is None:
            return path.exists()
        if not event.is_set():
            self.log(f"Bucket: waiting for {path.name} to finish downloading...")
            event.wait()
        with self._lock:
            return bool(self._ok.get(path))

    def _evict(self) -> None:
        """Delete every cached track except the previous, current and next ones."""
        with self._lock:
            keep = {path.name for path in self._recent}
            keep |= {path.name + ".partial" for path in self._recent}
        for file in self.library.tracks_dir.iterdir():
            if file.name not in keep:
                try:
                    file.unlink()
                except OSError:
                    pass
                with self._lock:
                    self._ready.pop(file, None)
                    self._ok.pop(file, None)
                    self._remote.pop(file, None)

    def close(self) -> None:
        self._downloads.put(None)


def normalize_prefix(prefix: Optional[str]) -> str:
    """"music" and "/music/" both mean the folder music/ (so music2/ is not included)."""
    prefix = (prefix or "").strip("/")
    return prefix + "/" if prefix else ""


def library_from_args(argv) -> R2Library:
    endpoint = getattr(argv, "r2_endpoint", None)
    required = ("r2_bucket", "r2_access_key_id", "r2_secret_access_key") + (() if endpoint else ("r2_account_id",))
    missing = [name for name in required if not getattr(argv, name)]
    if missing:
        raise ValueError(f"Bucket settings missing: {', '.join(missing)}")
    cache_dir = Path(argv.cache_dir).expanduser()
    budget = OperationBudget(
        cache_dir / "r2-operations.json",
        int(FREE_CLASS_A * CAP_RATIO),
        int(FREE_CLASS_B * CAP_RATIO),
    )
    return R2Library(argv.r2_account_id, argv.r2_bucket, argv.r2_prefix or "", argv.r2_access_key_id,
                     argv.r2_secret_access_key, cache_dir, budget, endpoint, getattr(argv, "r2_region", None))


def preloader_from_args(argv, log: Callable[[str], None]) -> R2Preloader:
    return R2Preloader(library_from_args(argv), log)
