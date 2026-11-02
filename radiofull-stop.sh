#!/bin/bash
# Stops the pipeline started by radiofull.sh / radiofull-headless.sh.
# Pass --wg to also bring down the WireGuard tunnel.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

"$DIR/radiofull-headless.sh" stop

if [[ "${1:-}" == "--wg" ]]; then
    sudo systemctl disable --now wg-quick@wg0
fi
