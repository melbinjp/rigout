"""Prove a freshly started server hands an agent a usable connection.

WHAT THIS USED TO DO, AND WHY IT COULD NEVER PASS
-------------------------------------------------
It launched the server with `--tunnel none` and then scraped stdout for a line reading
`Agent setup URL:`. A local-only server has no setup URL *by design* - `mcp_url_launcher`
says so itself when you ask for one:

    "A local-only server has no setup URL by design"

So the line never arrived. And the wait for it was not bounded either, because

    while time.time() - start_time < timeout:
        line = process.stdout.readline()

only re-checks the clock BETWEEN reads. `readline()` blocks, the server stayed alive and
quiet after its banner, and the pipe never closed. The `timeout = 15` was decorative.

The result: every pull request started a job that sat for **six hours** and was then killed
at GitHub's ceiling. Run 32043881040 ran 15:57:11 to 21:57:27 and its last output was a
health check three hundredths of a second in. It has been failing this way, slowly and
expensively, on every PR.

There was a third defect underneath: even if the line HAD arrived, it reads
`Agent setup URL: stored in the owner-readable connection file`, so
`line.split("Agent setup URL:")[1]` yields that sentence and the script then called
`urlopen()` on it.

WHAT IT DOES NOW
----------------
Reads the connection file, which is where a local-only server puts exactly what an agent
needs, and which the audit's own later code already expected the shape of. Nothing is
scraped from stdout, so nothing can block on it.

Every wait has a deadline, and the workflow carries `timeout-minutes` as well, because a
script-level bound is a thing that can have a hole in it and a job-level one cannot.

Unix only (`os.setsid`), which is what CI runs.
"""

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

SETUP_TOKEN_PATTERN = re.compile(r"(setup_token=)[^&\s]+")

# The server writes its connection file within a second in practice. Ten is slack for a
# cold CI runner; it is a DEADLINE rather than a hope, and it is enforced by a clock the
# reader cannot block.
CONNECTION_TIMEOUT = 30
HEALTH_TIMEOUT = 10


def redact_setup_tokens(text: str) -> str:
    """Remove credential query values before writing diagnostic output."""
    return SETUP_TOKEN_PATTERN.sub(r"\1***", text)


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def drain(stream) -> None:
    """Echo server output for CI visibility, on a thread that nothing waits on.

    It runs as a daemon precisely so that a quiet, still-running server cannot hold the
    audit open. Draining also stops the pipe buffer filling and blocking the server itself,
    which is the other way this shape deadlocks.
    """
    try:
        for line in iter(stream.readline, ""):
            sys.stdout.write(f"SERVER: {redact_setup_tokens(line)}")
            sys.stdout.flush()
    except (ValueError, OSError):
        pass


def wait_for_connection(path: Path, deadline: float) -> dict | None:
    """The connection file, once it is present AND parses. None if the deadline passes.

    Both conditions matter: the file appears before it is fully written, so a bare
    `exists()` races and reads half a JSON document.
    """
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.2)
    return None


def run_audit() -> bool:
    port = get_free_port()
    connection_file = Path(f"audit_connection_{port}.json")

    print(f"--- Starting Agent Audit on port {port} ---")

    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src_path if "PYTHONPATH" not in env else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "rigout.mcp_url_launcher",
        "--port",
        str(port),
        "--connection-file",
        str(connection_file),
        "--tunnel",
        "none",
        "--public-url",
        f"http://localhost:{port}/mcp",
    ]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env, preexec_fn=os.setsid
    )
    threading.Thread(target=drain, args=(process.stdout,), daemon=True).start()

    success = False
    try:
        print(f"Waiting up to {CONNECTION_TIMEOUT}s for the connection file...")
        data = wait_for_connection(connection_file, time.time() + CONNECTION_TIMEOUT)

        if data is None:
            print(f"FAIL: no readable connection file after {CONNECTION_TIMEOUT}s")
        else:
            mcp = data.get("mcp") or {}
            mcp_url = mcp.get("url")
            health_url = mcp.get("health_url")
            auth_token = (mcp.get("headers") or {}).get("Authorization")

            print(f"MCP URL: {mcp_url}")
            print(f"Auth token present: {bool(auth_token)}")

            if not mcp_url:
                print("FAIL: connection file has no mcp.url")
            elif not (auth_token and "Bearer" in auth_token):
                print("FAIL: connection file has no Bearer token")
            elif not health_url:
                print("FAIL: connection file has no mcp.health_url")
            else:
                # A file on disk only proves the launcher wrote something. Answering on the
                # advertised health URL is what proves an agent could actually reach it.
                try:
                    with urllib.request.urlopen(health_url, timeout=HEALTH_TIMEOUT) as response:
                        code = response.status
                    print(f"Health check: HTTP {code}")
                    success = code == 200
                    if not success:
                        print(f"FAIL: health URL answered {code}")
                except Exception as exc:  # noqa: BLE001 - any failure to reach it is a failure
                    print(f"FAIL: could not reach {health_url}: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        if connection_file.exists():
            with contextlib.suppress(OSError):
                connection_file.unlink()

    print(f"--- Audit Complete: {'SUCCESS' if success else 'FAIL'} ---")
    return success


if __name__ == "__main__":
    sys.exit(0 if run_audit() else 1)
