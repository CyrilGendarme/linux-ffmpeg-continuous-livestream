import argparse
import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from .constants import DEFAULT_OVERLAY_DIR
from .media import extract_track_fields, ffprobe_json, find_audio_files, probe_duration_seconds, write_playlist, write_title


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    duration_seconds: float


def announce(message: str) -> None:
    print(f"[music-self-test] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a non-audible self-test for the FFmpeg music livestream stack."
    )
    parser.add_argument("--config", help="Optional JSON config file.")
    parser.add_argument("--music-dir", help="Directory containing audio files.")
    parser.add_argument(
        "--playlist-file",
        default=None,
        help="Playlist file used for concat decode checks.",
    )
    parser.add_argument(
        "--overlay-dir",
        default=None,
        help="Directory where overlay text files are written.",
    )
    parser.add_argument("--rtmp-url", default=None, help="Optional RTMP/RTMPS URL for connectivity precheck.")
    parser.add_argument(
        "--sample-tracks",
        type=int,
        default=3,
        help="How many tracks to sample for metadata/playlist checks.",
    )
    parser.add_argument(
        "--decode-seconds",
        type=float,
        default=20.0,
        help="How many seconds to decode from the test playlist.",
    )
    args = parser.parse_args()

    config: Dict[str, object] = {}
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            parser.error(f"Config file not found: {config_path}")
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
        except json.JSONDecodeError as exc:
            parser.error(f"Config file is not valid JSON: {config_path}: {exc}")
        if not isinstance(config, dict):
            parser.error(f"Config file must contain a JSON object: {config_path}")

    if args.music_dir is None:
        args.music_dir = config.get("music_dir")
    if args.playlist_file is None:
        args.playlist_file = config.get("playlist_file", "/tmp/music_self_test_playlist.txt")
    if args.overlay_dir is None:
        args.overlay_dir = config.get("overlay_dir", str(DEFAULT_OVERLAY_DIR))
    if args.rtmp_url is None:
        args.rtmp_url = config.get("rtmp_url")

    if args.music_dir in (None, ""):
        parser.error("Missing required value: music-dir")
    if args.sample_tracks < 1:
        parser.error("sample-tracks must be >= 1")
    if args.decode_seconds <= 0:
        parser.error("decode-seconds must be > 0")

    return args


def run_check(name: str, fn: Callable[[], str]) -> CheckResult:
    started = time.monotonic()
    try:
        detail = fn()
        status = "PASS"
    except SkipCheck as exc:
        detail = str(exc)
        status = "SKIP"
    except Exception as exc:
        detail = str(exc)
        status = "FAIL"
    return CheckResult(
        name=name,
        status=status,
        detail=detail,
        duration_seconds=time.monotonic() - started,
    )


class SkipCheck(RuntimeError):
    pass


def check_dependencies() -> str:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError("Missing required tools: " + ", ".join(missing))
    return "ffmpeg and ffprobe are available"


def check_track_scan(music_dir: Path) -> List[Path]:
    tracks = find_audio_files(music_dir)
    if not tracks:
        raise RuntimeError("No supported audio files found")
    return tracks


def check_metadata(tracks: List[Path], sample_tracks: int) -> str:
    sample = tracks[:sample_tracks]
    parsed = 0
    for track in sample:
        info = ffprobe_json(track)
        duration = probe_duration_seconds(info, track)
        if duration <= 0:
            raise RuntimeError(f"Non-positive duration for {track}")
        fields = extract_track_fields(track, info)
        if not fields.get("center"):
            raise RuntimeError(f"Missing display title for {track}")
        parsed += 1
    return f"Parsed metadata for {parsed} track(s)"


def check_playlist_decode(playlist_path: Path, tracks: List[Path], sample_tracks: int, decode_seconds: float) -> str:
    sample = tracks[:sample_tracks]
    write_playlist(sample, playlist_path)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(playlist_path),
        "-t",
        f"{decode_seconds:.3f}",
        "-vn",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "Unknown ffmpeg decode error"
        raise RuntimeError(message)
    return f"Decoded {len(sample)} track(s) from concat playlist for {decode_seconds:.1f}s"


def check_overlay_write(overlay_dir: Path) -> str:
    overlay_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "center_title.txt": "Self test center title",
        "artist.txt": "Self Test Artist",
        "title.txt": "Self Test Track",
        "album.txt": "Self Test Album",
        "label.txt": "Self Test Label",
        "year.txt": "2026",
        "bpm.txt": "128",
    }
    for name, expected in files.items():
        path = overlay_dir / name
        write_title(path, expected)
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            raise RuntimeError(f"Overlay file verification failed: {path}")
    return f"Wrote and verified {len(files)} overlay text files"


def check_rtmp_connectivity(rtmp_url: Optional[str]) -> str:
    if not rtmp_url:
        raise SkipCheck("No rtmp-url provided; skipping network precheck")

    parsed = urlparse(rtmp_url)
    if parsed.scheme not in {"rtmp", "rtmps"}:
        raise RuntimeError(f"Unsupported RTMP scheme: {parsed.scheme or '<missing>'}")
    if not parsed.hostname:
        raise RuntimeError("RTMP URL is missing a host")

    default_port = 1935 if parsed.scheme == "rtmp" else 443
    port = parsed.port or default_port

    with socket.create_connection((parsed.hostname, port), timeout=5):
        pass
    return f"Connected to {parsed.hostname}:{port} ({parsed.scheme})"


def print_result(result: CheckResult) -> None:
    announce(
        f"[{result.status}] {result.name} ({result.duration_seconds:.2f}s) - {result.detail}"
    )


def main() -> int:
    args = parse_args()
    music_dir = Path(args.music_dir)
    playlist_path = Path(args.playlist_file)
    overlay_dir = Path(args.overlay_dir)

    results: List[CheckResult] = []

    dependencies_result = run_check("Dependencies", check_dependencies)
    results.append(dependencies_result)
    print_result(dependencies_result)

    tracks: List[Path] = []

    def track_scan_wrapper() -> str:
        nonlocal tracks
        tracks = check_track_scan(music_dir)
        return f"Found {len(tracks)} supported track(s) in {music_dir}"

    track_scan_result = run_check("Track Scan", track_scan_wrapper)
    results.append(track_scan_result)
    print_result(track_scan_result)

    def metadata_wrapper() -> str:
        if not tracks:
            raise SkipCheck("Track scan failed; skipping metadata probe")
        return check_metadata(tracks, int(args.sample_tracks))

    metadata_result = run_check("Metadata Probe", metadata_wrapper)
    results.append(metadata_result)
    print_result(metadata_result)

    def playlist_wrapper() -> str:
        if not tracks:
            raise SkipCheck("Track scan failed; skipping playlist decode")
        return check_playlist_decode(playlist_path, tracks, int(args.sample_tracks), float(args.decode_seconds))

    playlist_result = run_check("Playlist Decode", playlist_wrapper)
    results.append(playlist_result)
    print_result(playlist_result)

    overlay_result = run_check("Overlay Write", lambda: check_overlay_write(overlay_dir))
    results.append(overlay_result)
    print_result(overlay_result)

    rtmp_result = run_check("RTMP Connectivity", lambda: check_rtmp_connectivity(args.rtmp_url))
    results.append(rtmp_result)
    print_result(rtmp_result)

    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")

    announce(f"Summary: PASS={passed} FAIL={failed} SKIP={skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
