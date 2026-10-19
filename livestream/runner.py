import random
import shutil
import signal
import subprocess
import time
import sys
from pathlib import Path
from typing import Dict, List

from .config import parse_args
from .ffmpeg import build_ffmpeg_command
from .media import extract_track_fields, ffprobe_json, find_audio_files, probe_duration_seconds, write_playlist, write_title


def announce(msg: str) -> None:
    print(f"[music-livestream] {msg}")


def ensure_dependencies() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing required tool: {tool}. Install FFmpeg first: sudo apt install ffmpeg")


def write_overlay_files(overlay_files: Dict[str, Path], fields: Dict[str, str]) -> None:
    for key, path in overlay_files.items():
        value = fields.get(key, "")
        write_title(path, value)


def wait_with_process(process: subprocess.Popen, seconds: float, interval: float = 0.05) -> bool:
    end_time = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end_time:
        if process.poll() is not None:
            return False
        time.sleep(interval)
    return process.poll() is None


def animate_center_text(
    process: subprocess.Popen,
    center_file: Path,
    text: str,
    duration: float,
    type_ms: float,
    erase_ms: float,
    hold_seconds: float,
) -> bool:
    started = time.monotonic()
    safe_text = text or ""

    type_delay = max(0.02, type_ms / 1000.0)
    erase_delay = max(0.02, erase_ms / 1000.0)

    for index in range(1, len(safe_text) + 1):
        write_title(center_file, safe_text[:index])
        if not wait_with_process(process, type_delay):
            return False

    if not wait_with_process(process, hold_seconds):
        return False

    for index in range(len(safe_text), -1, -1):
        write_title(center_file, safe_text[:index])
        if not wait_with_process(process, erase_delay):
            return False

    elapsed = time.monotonic() - started
    remaining = duration - elapsed
    if remaining > 0:
        return wait_with_process(process, remaining)
    return process.poll() is None


def run_cycle(tracks: List[Path], argv) -> None:
    playlist_file = Path(argv.playlist_file)
    overlay_dir = Path(argv.overlay_dir)
    overlay_files = {
        "center": Path(argv.title_file),
        "artist": overlay_dir / "artist.txt",
        "title": overlay_dir / "title.txt",
        "album": overlay_dir / "album.txt",
        "label": overlay_dir / "label.txt",
        "year": overlay_dir / "year.txt",
        "bpm": overlay_dir / "bpm.txt",
    }
    background_video = Path(argv.background_video)

    if not background_video.exists():
        raise FileNotFoundError(f"Background video not found: {background_video}")

    if not Path(argv.title_font).exists():
        raise FileNotFoundError(f"Title font not found: {argv.title_font}")
    if not Path(argv.info_font).exists():
        raise FileNotFoundError(f"Info font not found: {argv.info_font}")

    write_playlist(tracks, playlist_file)
    write_overlay_files(
        overlay_files,
        {
            "center": "",
            "artist": "-",
            "title": "-",
            "album": "-",
            "label": "-",
            "year": "-",
            "bpm": "-",
        },
    )

    total_duration = 0.0
    sequence: List[tuple[Path, float, Dict[str, str]]] = []
    for track in tracks:
        info = ffprobe_json(track)
        duration = probe_duration_seconds(info, track)
        fields = extract_track_fields(track, info)
        sequence.append((track, duration, fields))
        total_duration += duration

    announce(f"Loaded {len(tracks)} tracks, total runtime ~{total_duration:.0f}s")
    announce(f"Starting FFmpeg stream to {argv.rtmp_url}")

    ffmpeg_cmd = build_ffmpeg_command(
        background_video,
        [track for track, _duration, _fields in sequence],
        [duration for _track, duration, _fields in sequence],
        overlay_files,
        argv.rtmp_url,
        argv.output_format,
        argv.title_font,
        argv.info_font,
        float(argv.track_fade_seconds),
    )
    process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        for _track, duration, fields in sequence:
            fields_for_panel = dict(fields)
            fields_for_panel["center"] = ""
            write_overlay_files(overlay_files, fields_for_panel)
            announce(f"Now playing: {fields['center']}")
            if duration <= 0:
                duration = 1.0
            if not animate_center_text(
                process,
                overlay_files["center"],
                fields["center"],
                duration,
                argv.type_ms,
                argv.erase_ms,
                argv.title_hold,
            ):
                return
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

        if process.stdout is not None:
            try:
                output = process.stdout.read()
                if output:
                    print(output)
            except Exception:
                pass


def handle_shutdown(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        ensure_dependencies()
        while True:
            tracks = find_audio_files(Path(args.music_dir))
            random.shuffle(tracks)
            try:
                run_cycle(tracks, args)
            except KeyboardInterrupt:
                announce("Shutting down cleanly.")
                break
            except Exception as exc:
                announce(f"Stream cycle failed: {exc}")
                time.sleep(args.restart_delay)
                continue
            time.sleep(args.restart_delay)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0
