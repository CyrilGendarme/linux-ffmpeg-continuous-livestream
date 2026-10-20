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
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "music-livestream"
