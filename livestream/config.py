"""Stream configuration: one schema shared by the runner (CLI + JSON) and the web config editor.

Every option can be set in the config JSON (snake_case), overridden in a
profile under "profiles", or overridden on the CLI (--kebab-case).
"""

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import DEFAULT_CACHE_DIR

X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
_BITRATE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")
_SIZE = re.compile(r"^(\d+)x(\d+)$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
DEFAULT_MENU_TEXT = "45 to 33 rpm\npopuri music"
MENU_MAX_LINES = 4
MENU_MAX_LENGTH = 200


def menu_lines(text: str) -> List[str]:
    """The non-empty lines of a menu text."""
    return [line.strip() for line in str(text).splitlines() if line.strip()]


@dataclass(frozen=True)
class Option:
    name: str
    kind: type  # str, int, float or bool
    default: Any
    group: str
    label: str
    help: str
    required: bool = False
    nullable: bool = False  # empty means "use the automatic value"
    choices: Sequence[str] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    pattern: Optional[str] = None  # "bitrate" | "size"
    secret: bool = False  # contains credentials (e.g. a stream key)
    path: str = ""  # "dir" | "file": must exist on the server
    profile_override: bool = True  # may be overridden per profile
    multiline: bool = False  # free text over several lines


OPTIONS: List[Option] = [
    # -- Stream ---------------------------------------------------------------
    Option("music_dir", str, None, "Stream", "Music folder", "Folder scanned (recursively) for audio tracks.",
           required=True, path="dir", profile_override=False),
    Option("background_video", str, None, "Stream", "Background video", "Looping MP4/WEBM shown behind the overlay.",
           required=True, path="file"),
    Option("rtmp_url", str, None, "Stream", "Destination URL", "RTMP(S) ingest URL including the stream key, "
           "e.g. rtmp://live.restream.io/live/KEY (or udp://... for local preview).", required=True, secret=True),
    Option("output_format", str, "flv", "Stream", "Container format", "flv for RTMP, mpegts for UDP/local preview.",
           choices=("flv", "mpegts")),
    Option("track_fade_seconds", float, 3.0, "Stream", "Crossfade (s)", "Crossfade length between tracks.",
           minimum=0, maximum=30),
    Option("restart_delay", float, 2.0, "Stream", "Restart delay (s)", "Wait before restarting the encoder after a failure.",
           minimum=0, maximum=300),
    # -- Video ----------------------------------------------------------------
    Option("video_bitrate", str, "4500k", "Video", "Video bitrate", "Constant video bitrate, e.g. 4500k.", pattern="bitrate"),
    Option("video_bufsize", str, None, "Video", "Rate buffer", "Rate-control buffer; empty = same as the bitrate (1 second).",
           nullable=True, pattern="bitrate"),
    Option("video_fps", float, 30.0, "Video", "Frame rate", "Output frames per second.", minimum=1, maximum=60),
    Option("video_size", str, "1920x1080", "Video", "Resolution", "Output WxH; the layout is drawn at 1920x1080 then scaled.",
           pattern="size"),
    Option("video_preset", str, "veryfast", "Video", "x264 preset", "Faster presets use less CPU (lower quality per bit).",
           choices=X264_PRESETS),
    Option("encoder_threads", int, 0, "Video", "Encoder threads", "0 = automatic.", minimum=0, maximum=64),
    Option("rc_lookahead", int, None, "Video", "Lookahead frames", "Lower uses less memory; empty = x264 default.",
           nullable=True, minimum=0, maximum=250),
    Option("keyframe_seconds", float, 2.0, "Video", "Keyframe interval (s)", "Most platforms want 2 seconds.",
           minimum=0.5, maximum=10),
    Option("background_bitrate", str, "4M", "Video", "Background cache bitrate", "Bitrate of the pre-scaled background loop.",
           pattern="bitrate"),
    # -- Audio ----------------------------------------------------------------
    Option("audio_bitrate", str, "160k", "Audio", "Audio bitrate", "AAC bitrate, e.g. 160k (Mixcloud: 320k).", pattern="bitrate"),
    Option("loudness_normalize", bool, True, "Audio", "Normalize loudness", "Measure each track once and even out volume."),
    Option("loudness_target", float, -14.0, "Audio", "Target loudness (LUFS)", "Streaming platforms use about -14.",
           minimum=-40, maximum=-5),
    Option("loudness_max_gain", float, 9.0, "Audio", "Max boost (dB)", "Never boost a quiet track more than this.",
           minimum=0, maximum=30),
    # -- Switcher -------------------------------------------------------------
    Option("output_url", str, None, "Switcher", "Final destination URL", "Where the source switcher sends the selected "
           "source (OBS when it streams, otherwise the radio), e.g. rtmp://live.restream.io/live/KEY. With the switcher, "
           "the radio's destination URL is the local MediaMTX radio path: rtmp://127.0.0.1:1935/radio.",
           nullable=True, secret=True),
    Option("takeover_delay", float, 1.0, "Switcher", "OBS takeover delay (s)",
           "OBS must stream steadily this long before it replaces the radio.", minimum=0, maximum=30),
    Option("release_timeout", float, 3.0, "Switcher", "OBS loss timeout (s)",
           "Go back to the radio when OBS sends nothing for this long.", minimum=1, maximum=60),
    # -- Stability ------------------------------------------------------------
    Option("stall_timeout", float, 15.0, "Stability", "Stall timeout (s)", "Restart the encoder if no frame goes out for this long.",
           minimum=3, maximum=300),
    Option("network_timeout", float, 10.0, "Stability", "Network timeout (s)", "RTMP read/write timeout.", minimum=1, maximum=300),
    Option("cache_dir", str, str(DEFAULT_CACHE_DIR), "Stability", "Cache folder", "Loudness cache and pre-rendered assets.",
           profile_override=False),
    # -- Restream (stream title; used by the web app only) --------------------
    Option("restream_client_id", str, None, "Restream", "Restream app client ID",
           "From your app at developers.restream.io/apps, to change the stream title from the show page.",
           nullable=True, profile_override=False),
    Option("restream_client_secret", str, None, "Restream", "Restream app client secret",
           "Client secret of the same app. Its redirect URI must be https://<this server>/restream/callback/.",
           nullable=True, secret=True, profile_override=False),
    # -- Overlay (applied by the running stream without a restart) -------------
    Option("menu_text", str, DEFAULT_MENU_TEXT, "Overlay", "Today's menu",
           f"Lines under \"Today's menu\" (at most {MENU_MAX_LINES} lines).", profile_override=False, multiline=True),
    Option("countdown_enabled", bool, False, "Overlay", "Show countdown",
           "\"Live DJ Set / Starting Soon\" countdown in the bottom-left corner.", profile_override=False),
    Option("countdown_hour", int, 19, "Overlay", "Countdown hour", "Start time of the show (server time), 0-23.",
           minimum=0, maximum=23, profile_override=False),
    Option("countdown_minute", int, 45, "Overlay", "Countdown minute", "Start time of the show, 0-59.",
           minimum=0, maximum=59, profile_override=False),
]
OPTIONS_BY_NAME: Dict[str, Option] = {option.name: option for option in OPTIONS}
# Options the runner picks up from the config file while streaming.
LIVE_OPTIONS = ("background_video", "menu_text", "countdown_enabled", "countdown_hour", "countdown_minute")


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------ validation


def _validate_value(option: Option, value: Any, check_paths: bool) -> Tuple[Any, Optional[str]]:
    """Return (normalized value, error message or None)."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if option.required:
            return None, "is required"
        return None, None  # unset: the default applies
    try:
        if option.kind is bool:
            if not isinstance(value, bool) and str(value).lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                return None, "must be true or false"
            value = _to_bool(value)
        elif option.kind is int:
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                return None, "must be a whole number"
            value = int(value)
        elif option.kind is float:
            if isinstance(value, bool):
                return None, "must be a number"
            value = float(value)
        else:
            value = str(value).strip()
    except (TypeError, ValueError):
        return None, "must be a number" if option.kind in (int, float) else "is invalid"

    if option.choices and value not in option.choices:
        return None, f"must be one of: {', '.join(option.choices)}"
    if option.minimum is not None and value < option.minimum:
        return None, f"must be at least {option.minimum:g}"
    if option.maximum is not None and value > option.maximum:
        return None, f"must be at most {option.maximum:g}"
    if option.pattern == "bitrate" and not _BITRATE.match(value):
        return None, "must look like 4500k, 4M or 320000"
    if option.pattern == "size":
        match = _SIZE.match(value)
        if not match or int(match.group(1)) % 2 or int(match.group(2)) % 2 or not 16 <= int(match.group(1)) <= 7680:
            return None, "must look like 1920x1080 (even numbers)"
    if option.name == "menu_text":
        lines = menu_lines(value)
        if len(lines) > MENU_MAX_LINES:
            return None, f"must have at most {MENU_MAX_LINES} lines"
        value = "\n".join(lines)
        if len(value) > MENU_MAX_LENGTH:
            return None, f"must be at most {MENU_MAX_LENGTH} characters"
    if option.name in ("rtmp_url", "output_url") and not re.match(r"^(rtmps?|udp|tcp|srt)://\S+$", value):
        return None, "must start with rtmp://, rtmps://, udp://, tcp:// or srt://"
    if check_paths and option.path:
        path = Path(value).expanduser()
        if option.path == "dir" and not path.is_dir():
            return None, "folder does not exist on the server"
        if option.path == "file" and not path.is_file():
            return None, "file does not exist on the server"
    return value, None


def validate_config(data: Any, check_paths: bool = True, base_dir: Optional[Path] = None) -> Tuple[dict, Dict[str, str]]:
    """Validate a whole config document. Returns (clean config, {field path: error}).

    Relative paths are checked against `base_dir` (the runner's working directory).
    """
    errors: Dict[str, str] = {}
    if not isinstance(data, dict):
        return {}, {"": "config must be a JSON object"}

    def check(option: Option, value: Any) -> Tuple[Any, Optional[str]]:
        value, error = _validate_value(option, value, False)
        if error is None and check_paths and option.path and value:
            resolved = Path(value).expanduser()
            if base_dir is not None and not resolved.is_absolute():
                resolved = base_dir / resolved
            _ignored, error = _validate_value(option, str(resolved), True)
        return value, error

    clean: Dict[str, Any] = {}
    for option in OPTIONS:
        value, error = check(option, data.get(option.name))
        if error:
            errors[option.name] = error
        elif value is not None:
            clean[option.name] = value

    for key in sorted(set(data) - set(OPTIONS_BY_NAME) - {"profile", "profiles"}):
        errors[key] = "unknown setting"

    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        errors["profiles"] = "must be an object of profile name -> overrides"
        profiles = {}
    clean_profiles: Dict[str, dict] = {}
    for name, overrides in profiles.items():
        prefix = f"profiles.{name}"
        if not _PROFILE_NAME.match(str(name)):
            errors[prefix] = "profile names may only use letters, digits, - and _"
            continue
        if not isinstance(overrides, dict):
            errors[prefix] = "must be an object of overrides"
            continue
        clean_overrides = {}
        for key, value in overrides.items():
            option = OPTIONS_BY_NAME.get(key)
            if option is None or not option.profile_override:
                errors[f"{prefix}.{key}"] = "cannot be overridden in a profile"
                continue
            value, error = check(Option(**{**option.__dict__, "required": False}), value)
            if error:
                errors[f"{prefix}.{key}"] = error
            elif value is not None:
                clean_overrides[key] = value
        clean_profiles[name] = clean_overrides
    if clean_profiles:
        clean["profiles"] = clean_profiles

    active = data.get("profile")
    if active not in (None, ""):
        if active not in clean_profiles:
            errors["profile"] = "must be the name of an existing profile"
        else:
            clean["profile"] = active
    return clean, errors


def schema() -> List[dict]:
    """JSON-friendly description of every option, for the web editor."""
    return [
        {
            "name": option.name,
            "type": option.kind.__name__,
            "default": option.default,
            "group": option.group,
            "label": option.label,
            "help": option.help,
            "required": option.required,
            "nullable": option.nullable,
            "choices": list(option.choices),
            "minimum": option.minimum,
            "maximum": option.maximum,
            "secret": option.secret,
            "path": option.path,
            "profile_override": option.profile_override,
            "multiline": option.multiline,
        }
        for option in OPTIONS
    ]


# ------------------------------------------------------------------ runner CLI


def load_config_file(path: Path, profile: Optional[str] = None) -> Tuple[dict, Optional[str]]:
    """Read a config file and apply a profile (`profile` or the file's own).

    Returns (settings with the profile applied, profile name). Raises ValueError.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except OSError as exc:
        raise ValueError(f"Config file not readable: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Config file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    profile = profile or config.get("profile")
    if profile:
        profiles = config.get("profiles") or {}
        if profile not in profiles:
            raise ValueError(f"Unknown profile {profile!r}. Available: {', '.join(profiles) or 'none'}")
        config = {**config, **profiles[profile]}
    return config, profile


def option_value(option: Option, config: dict) -> Any:
    """The value of `option` in a (profile-applied) config, or its default."""
    value = config.get(option.name)
    if value is None or value == "":
        return option.default
    if option.kind is bool:
        return _to_bool(value)
    return option.kind(value)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously stream a looping background video with music files and dynamic track text."
    )
    parser.add_argument("--config", help="Optional JSON config file. Values can be overridden by CLI arguments.")
    parser.add_argument("--profile", default=None, help="Name of a profile in the config's \"profiles\" section")
    for option in OPTIONS:
        flag = "--" + option.name.replace("_", "-")
        if option.kind is bool:
            parser.add_argument(flag, default=None, type=_to_bool, metavar="{true,false}", help=option.help)
        else:
            parser.add_argument(flag, default=None, type=option.kind, help=option.help)
    args = parser.parse_args(argv)

    config: Dict[str, Any] = {}
    profile_name = args.profile
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            parser.error(f"Config file not found: {config_path}")
        try:
            config, profile_name = load_config_file(config_path, args.profile)
        except ValueError as exc:
            parser.error(str(exc))
    args.profile = profile_name

    # Set on the command line: the config file never overrides these, not even while streaming.
    args.cli_options = {option.name for option in OPTIONS if getattr(args, option.name) is not None}
    for option in OPTIONS:
        if option.name not in args.cli_options:
            setattr(args, option.name, option_value(option, config))

    missing = [
        "--" + option.name.replace("_", "-")
        for option in OPTIONS
        if option.required and getattr(args, option.name) in (None, "")
    ]
    if missing:
        parser.error("Missing required values. Provide them via CLI or config JSON: " + ", ".join(missing))

    return args
