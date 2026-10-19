import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open ffplay against a local stream URL.")
    parser.add_argument("--url", default="udp://@:1234?fifo_size=1000000&overrun_nonfatal=1", help="Stream URL to view locally.")
    return parser.parse_args()


def ensure_dependencies() -> None:
    if shutil.which("ffplay") is None:
        raise RuntimeError("Missing required tool: ffplay. Install FFmpeg first: sudo apt install ffmpeg")


def ensure_display() -> None:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError(
            "No graphical display detected. The viewer tier opens ffplay as a window and needs a desktop session."
        )


def ensure_xauthority() -> None:
    if os.environ.get("XAUTHORITY"):
        return

    default_xauthority = Path.home() / ".Xauthority"
    if default_xauthority.exists():
        os.environ["XAUTHORITY"] = str(default_xauthority)


def main() -> int:
    args = parse_args()
    ensure_dependencies()
    ensure_display()
    ensure_xauthority()

    print(f"[music-local-viewer] Opening stream viewer for {args.url}")

    process = subprocess.Popen(
        [
            "ffplay",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            args.url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return_code = process.wait()
        if return_code != 0:
            stdout_text, stderr_text = process.communicate()
            if stdout_text:
                print(stdout_text, end="")
            if stderr_text:
                print(stderr_text, end="", file=sys.stderr)
        return return_code
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
