#!/bin/bash
# Launches MediaMTX, radio and switcher detached from the RDP/SSH session:
# they survive logout and auto-restart if they exit.
# Usage: ./radiofull.sh [start|stop|restart|status|logs <name>]
# (Old terminal-window version: radiofull-windows.sh)
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$DIR/radiofull-headless.sh" "$@"
