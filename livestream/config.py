import argparse
import json
from pathlib import Path

from .constants import DEFAULT_FONT, DEFAULT_INFO_FONT, DEFAULT_OVERLAY_DIR, DEFAULT_TITLE_FONT


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
    parser.add_argument(
        "--track-fade-seconds",
        type=float,
        default=None,
        help="Seconds used to fade each track in and out",
    )
    parser.add_argument(
        "--output-format",
        default=None,
        help="FFmpeg container/format used for the final output stream",
    )
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
        "track_fade_seconds": "track_fade_seconds",
        "track-fade-seconds": "track_fade_seconds",
        "output_format": "output_format",
        "output-format": "output_format",
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
    if args.track_fade_seconds is None:
        args.track_fade_seconds = float(config_values.get("track_fade_seconds", 3.0))
    if args.output_format is None:
        args.output_format = config_values.get("output_format", "flv")
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
