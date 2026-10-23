#!/usr/bin/env python3
"""Local-only prototype: select laptop over radio using FFmpeg ZMQ."""

import json
import subprocess
import signal
import time
import urllib.request

import zmq

API = "http://127.0.0.1:9997/v3/paths/list"
OUTPUT = "/tmp/source-switch-test.mkv"
DURATION = 30


def paths():
    with urllib.request.urlopen(API, timeout=2) as response:
        data = json.load(response)
    return {item["name"]: item for item in data.get("items", [])}


def usable(items, name):
    item = items.get(name, {})
    return (
        item.get("ready") is True
        and item.get("online") is True
        and {"H264", "MPEG-4 Audio"}.issubset(set(item.get("tracks", [])))
    )


def preferred(items):
    if usable(items, "laptop"):
        return 1
    if usable(items, "radio"):
        return 0
    return None


def main():
    # Separate named selector filters let the controller change audio and
    # video together. This is a local recording test, not the live publisher.
    graph = (
        "[0:v]setpts=PTS-STARTPTS[v0];"
        "[1:v]setpts=PTS-STARTPTS[v1];"
        "[v0][v1]streamselect@sv=inputs=2:map=0,"
        "zmq=bind_address='tcp\\://127.0.0.1\\:5555'[vout];"
        "[0:a]asetpts=PTS-STARTPTS[a0];"
        "[1:a]asetpts=PTS-STARTPTS[a1];"
        "[a0][a1]astreamselect@sa=inputs=2:map=0,"
        "azmq=bind_address='tcp\\://127.0.0.1\\:5556'[aout]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts", "-i", "rtmp://127.0.0.1:1935/radio",
        "-fflags", "+genpts", "-i", "rtmp://127.0.0.1:1935/laptop",
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-t", str(DURATION),
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-f", "matroska", OUTPUT,
    ]

    initial = preferred(paths())
    if initial is None:
        raise SystemExit("Neither source is ready; leaving existing streams untouched.")

    # ZMQ sockets connect to FFmpeg's filter command endpoints.
    context = zmq.Context()
    video_socket = context.socket(zmq.REQ)
    audio_socket = context.socket(zmq.REQ)
    video_socket.setsockopt(zmq.RCVTIMEO, 1000)
    audio_socket.setsockopt(zmq.RCVTIMEO, 1000)
    video_socket.setsockopt(zmq.SNDTIMEO, 1000)
    audio_socket.setsockopt(zmq.SNDTIMEO, 1000)

    process = subprocess.Popen(cmd)
    try:
        video_socket.connect("tcp://127.0.0.1:5555")
        audio_socket.connect("tcp://127.0.0.1:5556")
        time.sleep(2)

        last = None
        deadline = time.monotonic() + DURATION
        while process.poll() is None and time.monotonic() < deadline:
            try:
                choice = preferred(paths())
                if choice is not None and choice != last:
                    for sock, target in (
                        (video_socket, "sv"),
                        (audio_socket, "sa"),
                    ):
                        sock.send_string(f"{target} map {choice}")
                        print("FFmpeg selector:", sock.recv_string(), flush=True)
                    last = choice
                    print(
                        "Preferred source:",
                        "laptop" if choice == 1 else "radio",
                        flush=True,
                    )
            except Exception as exc:
                print(f"Controller warning: {exc}", flush=True)
            time.sleep(1)

        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    finally:
        video_socket.close()
        audio_socket.close()
        context.term()

    print(f"Local test output: {OUTPUT}")
    print("This prototype does not yet guarantee recovery from an RTMP input disconnect.")


if __name__ == "__main__":
    main()
