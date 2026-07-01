#!/usr/bin/env bash
set -euo pipefail

PORT="${RIGOUT_PORT:-8765}"

echo "Rigout diagnostics"
echo "Port: $PORT"
echo

python - <<PY
import json
import urllib.request

url = "http://127.0.0.1:$PORT/health"
try:
    with urllib.request.urlopen(url, timeout=3) as response:
        data = json.load(response)
    print("Health: ok")
    print(f"MCP URL: {data.get('mcp_url')}")
    print(f"Transport: {data.get('transport')}")
except Exception as exc:
    print(f"Health: unavailable ({exc})")
PY
