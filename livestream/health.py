"""Incident logging: one greppable WARNING line per thing that went wrong, with system context.

    [music-livestream] WARNING 2026-11-26 21:04:13 radio-restart: ... | cpu 96% steal 14% load 3.8 mem 412MB free swap 0MB used

Every warning ends with a snapshot of the machine over the last few seconds
(CPU busy and steal time, load, available memory, swap in use), so a freeze can
be traced to CPU starvation, a noisy VPS neighbour (steal) or memory pressure.
The local time is in the line, so the radio's and the switcher's logs can be
lined up even where the log file has no timestamps.

    grep -h WARNING logs/radio.log logs/switcher.log | sort -k3,4
"""

import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

SAMPLE_SECONDS = 1.0
WINDOW_SECONDS = 10  # CPU figures are averaged over this many seconds

_samples: Deque[Tuple[int, int, int]] = deque(maxlen=WINDOW_SECONDS + 1)  # (total, idle, steal) jiffies
_started = False
_lock = threading.Lock()


def _read_cpu() -> Optional[Tuple[int, int, int]]:
    try:
        with open("/proc/stat", encoding="ascii") as stat:
            fields = [int(value) for value in stat.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    # user nice system idle iowait irq softirq steal ...
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    steal = fields[7] if len(fields) > 7 else 0
    return sum(fields[:8]), idle, steal


def _sample_forever() -> None:
    while True:
        sample = _read_cpu()
        if sample is not None:
            _samples.append(sample)
        time.sleep(SAMPLE_SECONDS)


def start() -> None:
    """Start the background sampler (once per process); snapshots work without it, minus CPU figures."""
    global _started
    with _lock:
        if not _started:
            _started = True
            threading.Thread(target=_sample_forever, name="health-sampler", daemon=True).start()


def _meminfo() -> dict:
    values = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                name, _, rest = line.partition(":")
                values[name] = int(rest.split()[0])  # kB
    except (OSError, ValueError, IndexError):
        pass
    return values


def snapshot() -> str:
    """CPU busy/steal over the last WINDOW_SECONDS, load average, available memory and swap in use."""
    parts = []
    if len(_samples) >= 2:
        (total0, idle0, steal0), (total1, idle1, steal1) = _samples[0], _samples[-1]
        elapsed = max(1, total1 - total0)
        parts.append(f"cpu {100 * (elapsed - (idle1 - idle0)) / elapsed:.0f}%")
        parts.append(f"steal {100 * (steal1 - steal0) / elapsed:.0f}%")
    try:
        with open("/proc/loadavg", encoding="ascii") as loadavg:
            parts.append(f"load {loadavg.read().split()[0]}")
    except (OSError, IndexError):
        pass
    memory = _meminfo()
    if "MemAvailable" in memory:
        parts.append(f"mem {memory['MemAvailable'] // 1024}MB free")
    if "SwapTotal" in memory and "SwapFree" in memory:
        parts.append(f"swap {(memory['SwapTotal'] - memory['SwapFree']) // 1024}MB used")
    return " ".join(parts)


def warning(kind: str, message: str) -> str:
    """The text of an incident line: `WARNING <local time> <kind>: <message> | <system snapshot>`."""
    return f"WARNING {time.strftime('%Y-%m-%d %H:%M:%S')} {kind}: {message} | {snapshot()}"


def clock(seconds: float) -> str:
    """M:SS for positions in a track."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
