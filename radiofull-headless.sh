#!/usr/bin/env bash

set -u

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR" || exit 1

RUNDIR="$DIR/.radiofull"
LOGDIR="$DIR/logs"

mkdir -p "$RUNDIR" "$LOGDIR"

# Run one component continuously, restarting it if it exits.
worker() {
    local name="$1"
    shift

    while true; do
        echo "[$(date -Is)] Starting $name"

        "$@"
        rc=$?

        echo "[$(date -Is)] $name exited with code $rc; restarting in 3 seconds"
        sleep 3
    done
}

start_one() {
    local name="$1"
    shift

    local pidfile="$RUNDIR/$name.pid"

    if [[ -f "$pidfile" ]]; then
        local oldpid
        oldpid="$(cat "$pidfile")"

        if kill -0 "$oldpid" 2>/dev/null; then
            echo "$name is already running (PID $oldpid)"
            return 0
        fi

        rm -f "$pidfile"
    fi

    echo "Starting $name..."

    nohup setsid bash -c '
        name="$1"
        shift
        while true; do
            echo "[$(date -Is)] Starting $name"
            "$@"
            rc=$?
            echo "[$(date -Is)] $name exited with code $rc; restarting in 3 seconds"
            sleep 3
        done
    ' _ "$name" "$@" >> "$LOGDIR/$name.log" 2>&1 \
        < /dev/null &

    local pid=$!
    echo "$pid" > "$pidfile"

    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo "$name started (PID $pid)"
    else
        echo "ERROR: $name failed to start. Check $LOGDIR/$name.log"
        rm -f "$pidfile"
        return 1
    fi
}

stop_one() {
    local name="$1"
    local pidfile="$RUNDIR/$name.pid"

    if [[ ! -f "$pidfile" ]]; then
        echo "$name: no PID file"
        return 0
    fi

    local pid
    pid="$(cat "$pidfile")"

    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        echo "Stopping $name (PID $pid)..."

        # Each component has its own process group.
        kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true

        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done

        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
        fi
    fi

    rm -f "$pidfile"
    echo "$name stopped"
}

case "${1:-start}" in
    start)
        echo "Enabling WireGuard..."
        sudo systemctl enable --now wg-quick@wg0 || exit 1

        echo "Preparing offline clip..."
        python3 -m livestream.switcher \
            --config "$DIR/config.json" \
            --prepare-offline-clip || exit 1

        # Avoid accidentally launching a second copy.
        for name in mediamtx radio switcher; do
            pidfile="$RUNDIR/$name.pid"

            if [[ -f "$pidfile" ]]; then
                pid="$(cat "$pidfile")"
                if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
                    echo "$name is already running (PID $pid)."
                    echo "Use '$0 status' or '$0 stop' first."
                    exit 1
                fi
                rm -f "$pidfile"
            fi
        done

        # Don't compete with an older runner from the RDP session.
        if pgrep -u "$USER" -f \
            '^/usr/bin/python3 -u -m livestream.runner --config .*/config.json --profile restream$' \
            >/dev/null; then
            echo "An older radio runner using --profile restream is still running."
            echo "Inspect it with: pgrep -af livestream.runner"
            echo "Stop that old process before starting this pipeline."
            exit 1
        fi

        start_one mediamtx \
            "$DIR/mediamtx/mediamtx" \
            "$DIR/mediamtx/mediamtx.yml" || exit 1

        sleep 3

        start_one radio \
            /usr/bin/python3 -u -m livestream.runner \
            --config "$DIR/config.json" \
            --rtmp-url rtmp://127.0.0.1:1935/radio || exit 1

        sleep 2

        start_one switcher \
            /usr/bin/python3 -u -m livestream.switcher \
            --config "$DIR/config.json" || exit 1

        echo
        echo "All components launched in the background."
        echo "They will restart automatically if they exit."
        echo "Disconnecting from SSH or RDP will not stop them."
        echo "Logs: $LOGDIR/"
        ;;

    stop)
        stop_one switcher
        stop_one radio
        stop_one mediamtx
        echo "Headless pipeline stopped."
        ;;

    restart)
        "$0" stop
        sleep 2
        "$0" start
        ;;

    status)
        for name in mediamtx radio switcher; do
            pidfile="$RUNDIR/$name.pid"

            if [[ -f "$pidfile" ]]; then
                pid="$(cat "$pidfile")"
                if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
                    echo "$name: RUNNING (PID $pid)"
                else
                    echo "$name: NOT RUNNING (stale PID file)"
                fi
            else
                echo "$name: NOT RUNNING"
            fi
        done

        echo
        echo "RTMP port:"
        ss -lnt 2>/dev/null | grep ':1935' || true

        echo
        echo "Other matching processes:"
        pgrep -af 'mediamtx|livestream.runner|livestream.switcher' || true
        ;;

    logs)
        name="${2:-}"
        if [[ "$name" != "mediamtx" && "$name" != "radio" && "$name" != "switcher" ]]; then
            echo "Usage: $0 logs mediamtx|radio|switcher"
            exit 1
        fi
        tail -n 100 -f "$LOGDIR/$name.log"
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
