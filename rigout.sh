#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.rigout.pid"
CONNECTION_FILE="$SCRIPT_DIR/ai_agent_connection.json"
PORT="${RIGOUT_PORT:-8765}"
TUNNEL="${RIGOUT_TUNNEL:-cloudflare}"
BACKGROUND=false

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

usage() {
    cat <<'EOF'
Usage:
  ./rigout.sh [start] [--background] [--port 8765] [--tunnel cloudflare|none]
  ./rigout.sh stop
  ./rigout.sh status
EOF
}

get_saved_pids() {
    [ -f "$PID_FILE" ] && cat "$PID_FILE"
}

is_running() {
    local pids
    pids="$(get_saved_pids || true)"
    [ -z "$pids" ] && return 1
    for pid in $pids; do
        kill -0 "$pid" 2>/dev/null && return 0
    done
    return 1
}

wait_for_connection() {
    local timeout="${1:-45}"
    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if [ -f "$CONNECTION_FILE" ]; then
            "$PYTHON_BIN" - <<PY 2>/dev/null && return 0 || true
import json
with open(r"$CONNECTION_FILE", encoding="utf-8") as f:
    data = json.load(f)
print(data.get("mcp_server_url") or data.get("mcp", {}).get("url") or "")
PY
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

show_connection_info() {
    "$PYTHON_BIN" - <<PY
import json
from pathlib import Path
path = Path(r"$CONNECTION_FILE")
data = json.loads(path.read_text(encoding="utf-8"))
mcp = data.get("mcp", {})
print("")
print("Rigout is running")
print(f"MCP URL:   {mcp.get('url') or data.get('mcp_server_url')}")
print(f"Health:    {mcp.get('health_url')}")
print(f"Transport: {mcp.get('transport')}")
print(f"Config:    {path}")
print("")
PY
}

start_foreground() {
    if is_running; then
        echo "Rigout is already running in the background. Run './rigout.sh stop' first."
        return 1
    fi
    rm -f "$CONNECTION_FILE"
    echo "Starting Rigout on port $PORT with tunnel '$TUNNEL'. Press Ctrl+C to stop."
    "$PYTHON_BIN" -m rigout.mcp_url_launcher --tunnel "$TUNNEL" --port "$PORT"
}

start_background() {
    if is_running; then
        echo "Rigout is already running."
        return 1
    fi
    rm -f "$CONNECTION_FILE"
    echo "Starting Rigout in background on port $PORT with tunnel '$TUNNEL'."
    nohup "$PYTHON_BIN" -m rigout.mcp_url_launcher --tunnel "$TUNNEL" --port "$PORT" > "$SCRIPT_DIR/.rigout.log" 2>&1 &
    echo "$!" > "$PID_FILE"

    if wait_for_connection 45 >/dev/null; then
        show_connection_info
        echo "Stop with: ./rigout.sh stop"
    else
        echo "Rigout did not become ready within 45 seconds. Check .rigout.log."
        stop_server >/dev/null 2>&1 || true
        return 1
    fi
}

stop_server() {
    local pids
    pids="$(get_saved_pids || true)"
    if [ -z "$pids" ]; then
        echo "No background Rigout process found."
        return 0
    fi
    for pid in $pids; do
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        echo "Stopped process $pid"
    done
    rm -f "$PID_FILE"
}

show_status() {
    if is_running; then
        echo "Rigout background process: running ($(get_saved_pids | tr '\n' ' '))"
    else
        echo "Rigout background process: stopped"
    fi
    "$PYTHON_BIN" - <<PY 2>/dev/null || true
import json
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:$PORT/health", timeout=3) as r:
        data = json.load(r)
    print(f"Health: {data.get('status')} ({data.get('mcp_url')})")
except Exception as exc:
    print(f"Health: unavailable ({exc})")
PY
    [ -f "$CONNECTION_FILE" ] && show_connection_info
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

case "$ACTION" in
    start)
        if "$BACKGROUND"; then
            start_background
        else
            start_foreground
        fi
        ;;
    stop) stop_server ;;
    status) show_status ;;
esac
