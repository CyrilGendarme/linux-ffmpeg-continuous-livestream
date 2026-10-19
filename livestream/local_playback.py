import argparse
import json
import random
import shutil
import subprocess
import sys
import time
import os
from pathlib import Path

from .media import find_audio_files, write_playlist


def announce(message: str) -> None:
    print(f"[music-local-playback] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Shuffle and play music locally with ffplay.")
    parser.add_argument("--config", help="Optional JSON config file.")
    parser.add_argument("--music-dir", help="Directory containing audio tracks.")
    parser.add_argument("--playlist-file", default="/tmp/music_local_playlist.txt", help="Generated concat playlist path.")
    parser.add_argument("--restart-delay", type=float, default=2.0, help="Seconds to wait before reshuffling.")
    args = parser.parse_args()

    config = {}
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
    if args.music_dir in (None, ""):
        parser.error("Missing required value: music-dir")

    return args


def ensure_dependencies() -> None:
    for tool in ("ffplay",):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing required tool: {tool}. Install FFmpeg first: sudo apt install ffmpeg")


def ensure_display() -> None:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError(
            "No graphical display detected. Local playback uses ffplay, which needs a desktop session."
        )


def main() -> int:
    args = parse_args()
    ensure_dependencies()
    ensure_display()

    playlist_path = Path(args.playlist_file)
    music_dir = Path(args.music_dir)

    while True:
        tracks = find_audio_files(music_dir)
        random.shuffle(tracks)
        write_playlist(tracks, playlist_path)
        announce(f"Playing {len(tracks)} shuffled tracks locally from {playlist_path}")
        announce("Launching ffplay in audio-only mode; this tier is for local playback validation, not a video preview")

        process = subprocess.Popen(
            [
                "ffplay",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-autoexit",
                "-nodisp",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(playlist_path),
            ],
        )
        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise

        time.sleep(max(0.0, float(args.restart_delay)))


if __name__ == "__main__":
    raise SystemExit(main())
