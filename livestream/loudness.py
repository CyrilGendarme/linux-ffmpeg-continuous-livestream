"""Per-track loudness normalization with a persistent cache.

Each track is measured once (EBU R128 integrated loudness + true peak) by a
single low-priority FFmpeg process, and the result is cached on disk keyed by
path, size and mtime. The track about to play is measured first; the rest of
the library is measured in the background, one file at a time.
"""

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

PEAK_CEILING_DB = -1.0  # never push a track's true peak above -1 dBTP

_INTEGRATED = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
_TRUE_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


def _cache_key(track: Path) -> str:
    stat = track.stat()
    return f"{track}|{stat.st_size}|{int(stat.st_mtime)}"


def measure(track: Path) -> Optional[Dict[str, float]]:
    """Return {"integrated": LUFS, "true_peak": dBTP}, or None if the track can't be measured."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-threads", "1",
        "-i", str(track), "-map", "0:a:0", "-vn",
        "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-",
    ]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "19", *cmd]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
    summary = proc.stderr[proc.stderr.rfind("Summary:"):]
    integrated = _INTEGRATED.search(summary)
    peak = _TRUE_PEAK.search(summary)
    if proc.returncode != 0 or not integrated or "inf" in integrated.group(1):
        return None
    true_peak = float(peak.group(1)) if peak and "inf" not in peak.group(1) else 0.0
    return {"integrated": float(integrated.group(1)), "true_peak": true_peak}


class LoudnessNormalizer:
    def __init__(
        self,
        cache_file: Path,
        target_lufs: float,
        max_gain_db: float,
        log: Callable[[str], None],
    ) -> None:
        self.cache_file = cache_file
        self.target_lufs = target_lufs
        self.max_gain_db = max_gain_db
        self.log = log
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._urgent: List[Path] = []
        self._background: List[Path] = []
        self._cache: Dict[str, Dict[str, float]] = self._load()
        self._dirty = 0
        threading.Thread(target=self._worker, name="loudness", daemon=True).start()

    def _load(self) -> Dict[str, Dict[str, float]]:
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._cache, indent=0), encoding="utf-8")
        os.replace(temporary, self.cache_file)

    def _cached(self, track: Path) -> Optional[Dict[str, float]]:
        try:
            return self._cache.get(_cache_key(track))
        except OSError:
            return None

    def prioritize(self, track: Path) -> None:
        """Measure this track next (it is about to play)."""
        with self._lock:
            if self._cached(track) is None and track not in self._urgent:
                self._urgent.append(track)
                self._wakeup.notify()

    def scan_library(self, tracks: Iterable[Path]) -> None:
        """Queue every not-yet-measured track for slow background measurement."""
        with self._lock:
            self._background = [track for track in tracks if self._cached(track) is None]
            if self._background:
                self.log(f"Loudness: {len(self._background)} track(s) to measure in the background")
                self._wakeup.notify()

    def gain_db(self, track: Path, may_block: bool = False) -> float:
        """Gain to apply to this track; 0 dB if it hasn't been measured yet.

        With may_block (only safe before audio is flowing), an unmeasured track
        is measured right away instead.
        """
        with self._lock:
            entry = self._cached(track)
        if entry is None and may_block:
            self.log(f"Loudness: measuring {track.name} before playback...")
            result = measure(track)
            with self._lock:
                entry = result or {"integrated": self.target_lufs, "true_peak": PEAK_CEILING_DB}
                self._cache[_cache_key(track)] = entry
                self._save()
        if entry is None:
            return 0.0
        gain = self.target_lufs - entry["integrated"]
        gain = min(gain, self.max_gain_db, PEAK_CEILING_DB - entry["true_peak"])
        return round(gain, 2)

    def _worker(self) -> None:
        while True:
            with self._lock:
                while not self._urgent and not self._background:
                    self._wakeup.wait()
                track = (self._urgent or self._background).pop(0)
                if self._cached(track) is not None:
                    continue
            result = measure(track)
            with self._lock:
                try:
                    key = _cache_key(track)
                except OSError:
                    continue
                # Unmeasurable tracks are cached as 0 dB so they aren't retried forever.
                self._cache[key] = result or {"integrated": self.target_lufs, "true_peak": PEAK_CEILING_DB}
                self._dirty += 1
                if self._urgent == [] or self._dirty >= 10:
                    self._save()
                    self._dirty = 0
                remaining = len(self._background)
            if remaining and remaining % 25 == 0:
                self.log(f"Loudness: {remaining} track(s) left to measure")
