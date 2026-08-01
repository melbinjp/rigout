#!/usr/bin/env bash
#
# Rigout convenience wrapper for running from a source checkout.
#
# DEPRECATED as of 0.3.0. Every action here now delegates to the packaged
# lifecycle CLI, which is the supported entry point:
#
#   rigout start [--detach]   rigout status   rigout logs [--follow]   rigout stop
#
# Not a like-for-like alias: this wrapper's historical default is --tunnel
# cloudflare (a PUBLIC quick-tunnel URL), where the packaged CLI defaults to
# --tunnel none (loopback only). The default is kept because changing it would
# break existing scripts, so the difference is stated instead - see usage().
#
# The wrapper remains so that a checkout with no `pip install` still has a
# one-command start, and so existing scripts that call it keep working. It
# deliberately implements no lifecycle logic of its own: the previous version
# polled for a connection file the launcher writes elsewhere, timed out after
# 45 seconds, and then killed the healthy server it had just started.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Pre-0.3.0 wrappers supervised the server themselves and left these behind.
# They are read for cleanup only; nothing writes them any more.
LEGACY_PID_FILE="$SCRIPT_DIR/.rigout.pid"
LEGACY_CONNECTION_FILE="$SCRIPT_DIR/ai_agent_connection.json"
PORT="${RIGOUT_PORT:-8765}"
TUNNEL="${RIGOUT_TUNNEL:-cloudflare}"
BACKGROUND=false

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi
# Let `python -m rigout...` resolve from the checkout when the package is not installed.
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

usage() {
    cat <<'EOF'
Usage:
  ./rigout.sh [start] [--background] [--port 8765] [--tunnel cloudflare|none]
  ./rigout.sh stop
  ./rigout.sh status

Deprecated. These map onto the packaged CLI, which supports many more options:
  ./rigout.sh              ->  rigout start --tunnel cloudflare
  ./rigout.sh --background ->  rigout start --detach --tunnel cloudflare
  ./rigout.sh stop         ->  rigout stop
  ./rigout.sh status       ->  rigout status

Note the --tunnel above. This wrapper defaults to a Cloudflare quick tunnel,
which puts this machine on a PUBLIC URL. The installed `rigout` CLI defaults to
--tunnel none, serving on loopback only. `./rigout.sh` and `rigout start` are
NOT equivalent. For a local-only server here, pass --tunnel none explicitly.
EOF
}

deprecation_notice() {
    echo "rigout.sh is deprecated; it now forwards to the packaged CLI (rigout start/status/stop)." >&2
}

# Fail with an instruction rather than a ModuleNotFoundError traceback.
require_rigout() {
    if ! "$PYTHON_BIN" -c "import rigout" >/dev/null 2>&1; then
        echo "Could not import rigout using '$PYTHON_BIN'." >&2
        echo "Install it first:  $PYTHON_BIN -m pip install -e \"$SCRIPT_DIR\"" >&2
        return 1
    fi
}

launcher() {
    "$PYTHON_BIN" -m rigout.mcp_url_launcher "$@"
}

# A server started by a pre-0.3.0 wrapper is not in the managed state directory,
# so `rigout stop` cannot see it. Clean it up here or it becomes unstoppable.
stop_legacy_processes() {
    [ -f "$LEGACY_PID_FILE" ] || return 0
    local pids
    pids="$(cat "$LEGACY_PID_FILE" 2>/dev/null || true)"
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
            pkill -P "$pid" 2>/dev/null || true
            kill "$pid" 2>/dev/null || true
            echo "Stopped legacy background process $pid (started by an older rigout.sh)."
        fi
    done
    rm -f "$LEGACY_PID_FILE"
}

# Never start over the top of a live legacy server: refuse and let the user
# decide, rather than killing something healthy on their behalf.
refuse_if_legacy_running() {
    [ -f "$LEGACY_PID_FILE" ] || return 0
    local pids
    pids="$(cat "$LEGACY_PID_FILE" 2>/dev/null || true)"
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "A Rigout server started by an older rigout.sh is still running (PID $pid)." >&2
            echo "Stop it first:  ./rigout.sh stop" >&2
            return 1
        fi
    done
    # Only the file is left; no live process. Safe to clear.
    rm -f "$LEGACY_PID_FILE"
}

note_legacy_leftovers() {
    if [ -f "$LEGACY_PID_FILE" ]; then
        echo "Note: $LEGACY_PID_FILE is left over from an older rigout.sh. Run './rigout.sh stop' to clear it." >&2
    fi
    if [ -f "$LEGACY_CONNECTION_FILE" ]; then
        echo "Note: $LEGACY_CONNECTION_FILE is stale and no longer used; the live one is shown by 'rigout status'." >&2
    fi
}

ACTION="start"
while [ "$#" -gt 0 ]; do
    case "$1" in
        start|stop|status)
            ACTION="$1"
            shift
            ;;
        --background|-b)
            BACKGROUND=true
            shift
            ;;
        --port|-p)
            PORT="$2"
            shift 2
            ;;
        --tunnel|-t)
            TUNNEL="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

deprecation_notice
require_rigout

# --port and --tunnel are always passed explicitly so this wrapper keeps its own
# historical defaults (tunnel=cloudflare) rather than inheriting the CLI's.
case "$ACTION" in
    start)
        refuse_if_legacy_running
        if [ "$BACKGROUND" = true ]; then
            launcher start --detach --tunnel "$TUNNEL" --port "$PORT"
        else
            launcher start --tunnel "$TUNNEL" --port "$PORT"
        fi
        ;;
    stop)
        stop_legacy_processes
        launcher stop
        ;;
    status)
        note_legacy_leftovers
        launcher status
        ;;
esac
