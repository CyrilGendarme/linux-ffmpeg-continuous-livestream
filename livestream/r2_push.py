"""Upload the local tracks that are not in the R2 bucket yet.

Reads the R2 settings and music_dir from the config file (CLI flags override).
A track is "already pushed" when an object with the same key exists; the key is
r2_prefix + the path relative to music_dir. Existing objects are never
overwritten, and nothing is ever deleted from the bucket.

Free tier: each file is one PutObject (class A, no multipart) and the listing
is one class A request per 1000 objects, both counted in the same monthly
counter as the radio. The push stops before the bucket would exceed 9 GB
(90% of the 10 GB free storage) or the class A cap.
"""

import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from .config import OPTIONS_BY_NAME, load_config_file, option_value
from .media import find_audio_files
from .r2 import CAP_RATIO, FREE_STORAGE_BYTES, BudgetExceeded, library_from_args

SETTINGS = ("music_dir", "cache_dir", "r2_bucket", "r2_prefix", "r2_account_id", "r2_access_key_id",
            "r2_secret_access_key")


def parse_args(argv=None) -> SimpleNamespace:
    parser = argparse.ArgumentParser(description="Upload local tracks that are not in the R2 bucket yet.")
    parser.add_argument("--config", default="config.json", help="Config file with the R2 settings (default: config.json)")
    parser.add_argument("--music-dir", help="Local folder to push (default: music_dir from the config)")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be uploaded")
    args = parser.parse_args(argv)

    config: dict = {}
    if Path(args.config).exists():
        try:
            config, _profile = load_config_file(Path(args.config))
        except ValueError as exc:
            parser.error(str(exc))
    elif args.config != "config.json":
        parser.error(f"Config file not found: {args.config}")
    settings = SimpleNamespace(**{name: option_value(OPTIONS_BY_NAME[name], config) for name in SETTINGS})
    settings.music_dir = args.music_dir or settings.music_dir
    settings.dry_run = args.dry_run
    if not settings.music_dir:
        parser.error("No music folder: set music_dir in the config or pass --music-dir")
    return settings


def human(size: float) -> str:
    return f"{size / 1000**3:.2f} GB" if size >= 1000**3 else f"{size / 1e6:.1f} MB"


def main(argv=None) -> int:
    settings = parse_args(argv)
    try:
        library = library_from_args(settings)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    music_dir = Path(settings.music_dir).expanduser()
    local = find_audio_files(music_dir)
    prefix = library.prefix
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    try:
        remote = {track.key for track in library.list_tracks()}
    except BudgetExceeded as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    stored = library.stored_bytes
    pending = [(path, prefix + path.relative_to(music_dir).as_posix()) for path in local]
    pending = [(path, key) for path, key in pending if key not in remote]
    total = sum(path.stat().st_size for path, _key in pending)
    limit = int(FREE_STORAGE_BYTES * CAP_RATIO)

    print(f"Bucket {library.bucket}: {len(remote)} track(s), {human(stored)} stored (limit used here: {human(limit)})")
    print(f"Local {music_dir}: {len(local)} track(s), {len(pending)} not pushed yet ({human(total)})")
    print(f"Operations this month: {library.budget.summary()}")
    if not pending:
        print("Nothing to push.")
        return 0
    if settings.dry_run:
        for path, key in pending:
            print(f"  would upload {key} ({human(path.stat().st_size)})")
        return 0

    uploaded = skipped = failed = 0
    for index, (path, key) in enumerate(pending, 1):
        size = path.stat().st_size
        if stored + size > limit:
            print(f"  skip {key}: would bring the bucket to {human(stored + size)}, over {human(limit)}")
            skipped += 1
            continue
        if not library.budget.can_spend("class_a"):
            print("Stopping: monthly class A cap reached.")
            break
        started = time.monotonic()
        try:
            library.upload(path, key)
        except BudgetExceeded as exc:
            print(f"Stopping: {exc}")
            break
        except Exception as exc:  # network, permissions: try the next file
            print(f"  FAILED {key}: {exc}")
            failed += 1
            continue
        stored += size
        uploaded += 1
        print(f"  [{index}/{len(pending)}] {key} ({human(size)} in {time.monotonic() - started:.1f}s)")

    print(f"Done: {uploaded} uploaded, {skipped} skipped (storage limit), {failed} failed. "
          f"Bucket now about {human(stored)}. {library.budget.summary()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
