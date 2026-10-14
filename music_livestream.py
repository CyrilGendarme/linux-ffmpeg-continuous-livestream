#!/usr/bin/env python3

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List

SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def announce(msg: str) -> None:
    print(f"[music-livestream] {msg}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously stream a looping background video with music files and dynamic track text."
    )
    parser.add_argument("--config", help="Optional JSON config file. Values can be overridden by CLI arguments.")
    parser.add_argument("--music-dir", help="Directory containing audio tracks.")
    parser.add_argument("--background-video", help="Looping MP4/WEBM background video.")
    parser.add_argument("--rtmp-url", help="RTMP destination, e.g. rtmp://a.rtmp.youtube.com/live2/KEY")
    parser.add_argument("--title-file", default=None, help="Text file read by FFmpeg drawtext")
    parser.add_argument("--playlist-file", default=None, help="FFmpeg concat playlist file")
    parser.add_argument("--font", default=None, help="TTF font used by drawtext")
    parser.add_argument("--restart-delay", type=float, default=None, help="Seconds to wait before restarting FFmpeg after a cycle")
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

    config_key_map = {
        "music_dir": "music_dir",
        "music-dir": "music_dir",
        "background_video": "background_video",
        "background-video": "background_video",
        "rtmp_url": "rtmp_url",
        "rtmp-url": "rtmp_url",
        "title_file": "title_file",
        "title-file": "title_file",
        "playlist_file": "playlist_file",
        "playlist-file": "playlist_file",
        "font": "font",
        "restart_delay": "restart_delay",
        "restart-delay": "restart_delay",
    }
    config_values = {}
    for key, value in config.items():
        mapped_key = config_key_map.get(key)
        if mapped_key is not None:
            config_values[mapped_key] = value

    if args.title_file is None:
        args.title_file = config_values.get("title_file", "/tmp/now_playing.txt")
    if args.playlist_file is None:
        args.playlist_file = config_values.get("playlist_file", "/tmp/music_playlist.txt")
    if args.font is None:
        args.font = config_values.get("font", DEFAULT_FONT)
    if args.restart_delay is None:
        args.restart_delay = config_values.get("restart_delay", 2.0)
    if args.music_dir is None:
        args.music_dir = config_values.get("music_dir")
    if args.background_video is None:
        args.background_video = config_values.get("background_video")
    if args.rtmp_url is None:
        args.rtmp_url = config_values.get("rtmp_url")

    missing = [
        name
        for name, value in {
            "music-dir": args.music_dir,
            "background-video": args.background_video,
            "rtmp-url": args.rtmp_url,
        }.items()
        if value in (None, "")
    ]
    if missing:
        parser.error(
            "Missing required values. Provide them via CLI or config JSON: " + ", ".join(missing)
        )

    return args


def ensure_dependencies() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing required tool: {tool}. Install FFmpeg first: sudo apt install ffmpeg")


def find_audio_files(music_dir: Path) -> List[Path]:
    if not music_dir.exists():
        raise FileNotFoundError(f"Music directory does not exist: {music_dir}")

    tracks = [
        path
        for path in sorted(music_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
    ]
    if not tracks:
        raise FileNotFoundError(
            f"No supported audio files found in {music_dir}. Supported extensions: {', '.join(sorted(SUPPORTED_AUDIO_EXTENSIONS))}"
        )
    return tracks


def escape_concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def write_playlist(tracks: Iterable[Path], playlist_path: Path) -> None:
    playlist_path.parent.mkdir(parents=True, exist_ok=True)
    with playlist_path.open("w", encoding="utf-8") as handle:
        for track in tracks:
            handle.write(f"file '{escape_concat_path(track)}'\n")


def write_title(title_file: Path, text: str) -> None:
    title_file.parent.mkdir(parents=True, exist_ok=True)
    title_file.write_text(text, encoding="utf-8")


def ffprobe_json(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def format_track_name(track: Path) -> str:
    try:
        info = ffprobe_json(track)
    except Exception:
        return track.stem

    tags = info.get("format", {}).get("tags", {})
    artist = tags.get("artist") or tags.get("ARTIST") or tags.get("album_artist") or tags.get("ALBUM_ARTIST")
    title = tags.get("title") or tags.get("TITLE")
    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    if artist:
        return artist
    return track.stem


def probe_duration_seconds(path: Path) -> float:
    info = ffprobe_json(path)
    duration_raw = info.get("format", {}).get("duration")
    if duration_raw is None:
        raise ValueError(f"ffprobe did not return a duration for {path}")
    return float(duration_raw)


def build_ffmpeg_command(background_video: Path, playlist_file: Path, title_file: Path, stream_url: str, font_path: str) -> List[str]:
    overlay = (
        f"[0:v]scale=1920:1080,format=yuv420p,"
        f"drawtext=fontfile={font_path}:textfile={title_file}:reload=1:"
        "x=(w-text_w)/2:y=h*0.12:fontsize=52:fontcolor=white:box=1:boxcolor=black@0.55:"
        "boxborderw=10:shadowcolor=black:shadowx=2:shadowy=2:line_spacing=8[video]"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stream_loop",
        "-1",
        "-i",
        str(background_video),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(playlist_file),
        "-filter_complex",
        overlay,
        "-map",
        "[video]",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        "-f",
        "flv",
        stream_url,
    ]


def run_cycle(tracks: List[Path], argv: argparse.Namespace) -> None:
    playlist_file = Path(argv.playlist_file)
    title_file = Path(argv.title_file)
    background_video = Path(argv.background_video)

    if not background_video.exists():
        raise FileNotFoundError(f"Background video not found: {background_video}")

    write_playlist(tracks, playlist_file)
    write_title(title_file, "")

    total_duration = 0.0
    sequence: List[tuple[Path, float]] = []
    for track in tracks:
        duration = probe_duration_seconds(track)
        sequence.append((track, duration))
        total_duration += duration

    announce(f"Loaded {len(tracks)} tracks, total runtime ~{total_duration:.0f}s")
    announce(f"Starting FFmpeg stream to {argv.rtmp_url}")

    ffmpeg_cmd = build_ffmpeg_command(background_video, playlist_file, title_file, argv.rtmp_url, argv.font)
    process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        for track, duration in sequence:
            text = format_track_name(track)
            write_title(title_file, text)
            announce(f"Now playing: {text}")
            if duration <= 0:
                duration = 1.0
            end_time = time.monotonic() + duration
            while time.monotonic() < end_time:
                if process.poll() is not None:
                    return
                time.sleep(0.25)
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


# The process can be restarted continuously; this prevents the terminal from leaving
# a stale stream alive if the user interrupts the script.


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


if __name__ == "__main__":
    raise SystemExit(main())
