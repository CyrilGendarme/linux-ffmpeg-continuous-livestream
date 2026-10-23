#!/usr/bin/env bash
set -euo pipefail

cd /home/rdpuser/Desktop/linux-ffmpeg-continuous-livestream

python3 -m livestream.runner \
  --config ./config.json \
  --rtmp-url "udp://127.0.0.1:1234?pkt_size=1316" \
  --output-format mpegts &
RUNNER_PID=$!

python3 -m livestream.local_viewer \
  --url "udp://@:1234?fifo_size=1000000&overrun_nonfatal=1"

kill $RUNNER_PID
