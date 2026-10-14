#!/usr/bin/env python3

import argparse
import json
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List

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
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TITLE_FONT = SCRIPT_DIR / "submodules" / "obscustomsetup" / "fonts" / "Staatliches-Regular.ttf"
DEFAULT_INFO_FONT = SCRIPT_DIR / "submodules" / "obscustomsetup" / "fonts" / "DMSans.ttf"
DEFAULT_OVERLAY_DIR = Path("/tmp/music_livestream_overlay")


def announce(msg: str) -> None:
    print(f"[music-livestream] {msg}")


def resolve_default_font(preferred: Path, fallback: str) -> str:
    if preferred.exists():
        return str(preferred)
    return fallback


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
    parser.add_argument("--font", default=None, help="Legacy alias for --title-font")
    parser.add_argument("--title-font", default=None, help="TTF font used by the center typewriter text")
    parser.add_argument("--info-font", default=None, help="TTF font used by the left now-playing panel")
    parser.add_argument("--overlay-dir", default=None, help="Directory where FFmpeg text overlay files are written")
    parser.add_argument("--type-ms", type=float, default=None, help="Typewriter speed in milliseconds per character")
    parser.add_argument("--erase-ms", type=float, default=None, help="Erase speed in milliseconds per character")
    parser.add_argument("--title-hold", type=float, default=None, help="Seconds to hold center title before erasing")
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
        "title_font": "title_font",
        "title-font": "title_font",
        "info_font": "info_font",
        "info-font": "info_font",
        "overlay_dir": "overlay_dir",
        "overlay-dir": "overlay_dir",
        "type_ms": "type_ms",
        "type-ms": "type_ms",
        "erase_ms": "erase_ms",
        "erase-ms": "erase_ms",
        "title_hold": "title_hold",
        "title-hold": "title_hold",
        "restart_delay": "restart_delay",
        "restart-delay": "restart_delay",
    }
    config_values = {}
    for key, value in config.items():
        mapped_key = config_key_map.get(key)
        if mapped_key is not None:
            config_values[mapped_key] = value

    if args.title_file is None:
        args.title_file = config_values.get("title_file", str(DEFAULT_OVERLAY_DIR / "center_title.txt"))
    if args.playlist_file is None:
        args.playlist_file = config_values.get("playlist_file", "/tmp/music_playlist.txt")
    if args.overlay_dir is None:
        args.overlay_dir = config_values.get("overlay_dir", str(DEFAULT_OVERLAY_DIR))
    legacy_font = args.font or config_values.get("font")
    if args.title_font is None:
        args.title_font = config_values.get(
            "title_font",
            legacy_font or resolve_default_font(DEFAULT_TITLE_FONT, DEFAULT_FONT),
        )
    if args.info_font is None:
        args.info_font = config_values.get(
            "info_font",
            resolve_default_font(DEFAULT_INFO_FONT, DEFAULT_FONT),
        )
    if args.type_ms is None:
        args.type_ms = float(config_values.get("type_ms", 80.0))
    if args.erase_ms is None:
        args.erase_ms = float(config_values.get("erase_ms", 40.0))
    if args.title_hold is None:
        args.title_hold = float(config_values.get("title_hold", 1.8))
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


def ffmpeg_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def ffprobe_json(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def format_track_name(track: Path, info: dict) -> str:
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


def probe_duration_seconds(info: dict, path: Path) -> float:
    duration_raw = info.get("format", {}).get("duration")
    if duration_raw is None:
        raise ValueError(f"ffprobe did not return a duration for {path}")
    return float(duration_raw)


def extract_track_fields(track: Path, info: dict) -> Dict[str, str]:
    tags = info.get("format", {}).get("tags", {})

    def first(*keys: str) -> str:
        for key in keys:
            value = tags.get(key)
            if value is not None and str(value).strip() != "":
                return str(value).strip()
        return ""

    artist = first("artist", "ARTIST", "album_artist", "ALBUM_ARTIST")
    title = first("title", "TITLE")
    album = first("album", "ALBUM")
    label = first("label", "LABEL", "publisher", "PUBLISHER")
    year_raw = first("date", "DATE", "year", "YEAR")
    bpm = first("bpm", "BPM", "TBPM")
    year = year_raw[:4] if len(year_raw) >= 4 else year_raw

    display_title = format_track_name(track, info)
    return {
        "center": display_title,
        "artist": artist or "-",
        "title": title or track.stem,
        "album": album or "-",
        "label": label or "-",
        "year": year or "-",
        "bpm": bpm or "-",
    }


def build_ffmpeg_command(
    background_video: Path,
    playlist_file: Path,
    overlay_files: Dict[str, Path],
    stream_url: str,
    title_font_path: str,
    info_font_path: str,
) -> List[str]:
    title_font = ffmpeg_escape(title_font_path)
    info_font = ffmpeg_escape(info_font_path)
    center_title = ffmpeg_escape(str(overlay_files["center"]))
    artist = ffmpeg_escape(str(overlay_files["artist"]))
    track_title = ffmpeg_escape(str(overlay_files["title"]))
    album = ffmpeg_escape(str(overlay_files["album"]))
    label = ffmpeg_escape(str(overlay_files["label"]))
    year = ffmpeg_escape(str(overlay_files["year"]))
    bpm = ffmpeg_escape(str(overlay_files["bpm"]))

    overlay = (
        "[0:v]scale=1920:1080,format=yuv420p,"
        "drawbox=x=0:y=0:w=300:h=ih:color=black@0.54:t=fill,"
        "drawbox=x=16:y=22:w=268:h=66:color=#111111@0.95:t=fill,"
        "drawbox=x=16:y=102:w=268:h=710:color=#111111@0.92:t=fill,"
        "drawbox=x=16:y=826:w=268:h=230:color=#111111@0.95:t=fill,"
        f"drawtext=fontfile='{info_font}':text='Now Playing':x=28:y=38:fontsize=34:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Artist\\:':x=28:y=140:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{artist}':reload=1:x=28:y=172:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Title\\:':x=28:y=224:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{track_title}':reload=1:x=28:y=256:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Album\\:':x=28:y=308:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{album}':reload=1:x=28:y=340:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Label\\:':x=28:y=392:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{label}':reload=1:x=28:y=424:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Year\\:':x=28:y=476:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{year}':reload=1:x=28:y=508:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='BPM\\:':x=28:y=560:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{bpm}':reload=1:x=28:y=592:fontsize=23:fontcolor=white,"
        "drawbox=x=320:y=h/2-120:w=w-420:h=240:color=black@0.44:t=fill,"
        f"drawtext=fontfile='{title_font}':textfile='{center_title}':reload=1:"
        "x=(w-text_w)/2:y=(h-text_h)/2:fontsize=80:fontcolor=white:shadowcolor=black:shadowx=2:shadowy=2[video]"
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


def run_cycle(tracks: List[Path], argv: argparse.Namespace) -> None:
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
        playlist_file,
        overlay_files,
        argv.rtmp_url,
        argv.title_font,
        argv.info_font,
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


if __name__ == "__main__":
    raise SystemExit(main())
