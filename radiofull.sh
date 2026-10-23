#!/bin/bash
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR" || exit 1

sudo systemctl enable --now wg-quick@wg0 || exit 1

# Opens a new terminal window running the given command; window stays open on exit.
open_win() {
  local title="$1" cmd="$2"
  xfce4-terminal --title="$title" --working-directory="$DIR" \
    -x bash -c "$cmd; echo; echo '[$title exited] press Enter to close'; read"
}

# 1. MediaMTX (builds the offline clip first if needed)
python3 -m livestream.switcher --config config.json --prepare-offline-clip || exit 1
open_win "MediaMTX" "cd mediamtx && ./mediamtx mediamtx.yml" &
sleep 3

# 2. Radio -> MediaMTX /radio
open_win "Radio" "python3 -m livestream.runner --config config.json --rtmp-url rtmp://127.0.0.1:1935/radio" &
sleep 2

# 3. Switcher: OBS or radio -> output_url (Restream)
open_win "Switcher" "python3 -m livestream.switcher --config config.json" &
wait
