#!/bin/bash
# Stops the components launched by radiofull-windows.sh (terminal-window version)
# and closes their terminal windows.
# Pass --wg to also bring down the WireGuard tunnel.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# Closing the terminals' children first; the windows then show "press Enter".
for pat in \
    'livestream\.switcher --config config\.json$' \
    'livestream\.runner --config config\.json --rtmp-url' \
    "$DIR/mediamtx/mediamtx|\./mediamtx mediamtx\.yml"; do
    pkill -TERM -u "$USER" -f "$pat" && echo "Stopped: $pat"
done

sleep 2

# Close the terminal windows (their titles are set by radiofull-windows.sh).
for title in MediaMTX Radio Switcher; do
    pkill -TERM -u "$USER" -f "xfce4-terminal --title=$title" 2>/dev/null
done

if [[ "${1:-}" == "--wg" ]]; then
    sudo systemctl disable --now wg-quick@wg0
fi
echo "Done."
