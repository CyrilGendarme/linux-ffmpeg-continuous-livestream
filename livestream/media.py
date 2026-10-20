import json
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List

from .constants import SUPPORTED_AUDIO_EXTENSIONS


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
        "artist": artist,
        "title": title or track.stem,
        "album": album,
        "label": label,
        "year": year,
        "bpm": bpm,
    }


def extract_cover(track: Path):
    """Embedded cover art of a track as a PIL image, or None if it has none."""
    from io import BytesIO

    from PIL import Image

    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(track), "-map", "0:v:0", "-frames:v", "1",
         "-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        image = Image.open(BytesIO(proc.stdout))
        image.load()
        return image
    except OSError:
        return None
