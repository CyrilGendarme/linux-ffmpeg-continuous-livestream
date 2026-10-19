from pathlib import Path

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DEFAULT_TITLE_FONT = REPO_ROOT / "submodules" / "obscustomsetup" / "fonts" / "Staatliches-Regular.ttf"
DEFAULT_INFO_FONT = REPO_ROOT / "submodules" / "obscustomsetup" / "fonts" / "DMSans.ttf"
DEFAULT_OVERLAY_DIR = Path("/tmp/music_livestream_overlay")
