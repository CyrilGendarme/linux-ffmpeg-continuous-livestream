#!/usr/bin/env python3
"""Track source priority for the MediaMTX laptop/radio feeds."""

import json
import time
import urllib.request
from pathlib import Path

API_URL = "http://127.0.0.1:9997/v3/paths/list"
POLL_SECONDS = 1.0
STATE_FILE = Path.home() / ".cache" / "livestream-source-state.json"


def get_paths():
    request = urllib.request.Request(
        API_URL,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.load(response)
    return {
        item["name"]: item
        for item in payload.get("items", [])
    }


def is_ready(paths, name):
    path = paths.get(name, {})
    tracks = set(path.get("tracks", []))
    return (
        path.get("ready") is True
        and path.get("online") is True
        and {"H264", "MPEG-4 Audio"}.issubset(tracks)
    )


def choose_source(paths):
    # Laptop always wins when it is publishing a usable video/audio feed.
    if is_ready(paths, "laptop"):
        return "laptop"
    if is_ready(paths, "radio"):
        return "radio"
    return None


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    previous = object()

    print("Source controller started; priority is laptop > radio.", flush=True)

    while True:
        try:
            source = choose_source(get_paths())
            state = {"source": source, "updated_at": time.time()}

            if source != previous:
                temporary = STATE_FILE.with_suffix(".tmp")
                temporary.write_text(json.dumps(state) + "\n")
                temporary.replace(STATE_FILE)
                print(
                    f"Preferred source changed to: {source or 'NONE'}",
                    flush=True,
                )
                previous = source

        except Exception as exc:
            # An API hiccup must not erase the last known state.
            print(f"MediaMTX API check failed: {exc}", flush=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
