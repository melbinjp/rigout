import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def run_audit():
    port = get_free_port()
    # Use a unique connection file to avoid conflicts
    connection_file = f"audit_connection_{port}.json"

    print(f"--- Starting Agent Audit on port {port} ---")

    # Start the launcher
    # Ensure PYTHONPATH is set so rigout is findable
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src_path if "PYTHONPATH" not in env else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"

    # We use -u for unbuffered output to ensure we can read the log lines immediately
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "rigout.mcp_url_launcher",
        "--port",
        str(port),
        "--connection-file",
        connection_file,
        "--tunnel",
        "none",
        "--public-url",
        f"http://localhost:{port}/mcp",
    ]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env, preexec_fn=os.setsid
    )

    setup_url = None
    start_time = time.time()
    timeout = 15

    print("Waiting for Agent Setup URL...")
    try:
        # Read output line by line
        while time.time() - start_time < timeout:
            line = process.stdout.readline()
            if not line:
                break
            # Print log line for CI visibility
            sys.stdout.write(f"SERVER: {line}")
            sys.stdout.flush()

            if "Agent setup URL:" in line:
                setup_url = line.split("Agent setup URL:")[1].strip()
                break
    except Exception as e:
        print(f"Error reading output: {e}")

    success = False
    if setup_url:
        print(f"\nSUCCESS: Captured Setup URL: {setup_url}")

        # Verify we can fetch the connection file using the setup URL
        print("Verifying setup URL access...")
        try:
            # Add a small delay to ensure server is ready
            time.sleep(1)
            with urllib.request.urlopen(setup_url) as response:
                data = json.loads(response.read().decode())
                print("SUCCESS: Retrieved connection data via setup URL")

                # Extract MCP details
                mcp_url = data["mcp"]["url"]
                auth_token = data["mcp"]["headers"].get("Authorization")

                print(f"MCP URL: {mcp_url}")
                print(f"Auth Token present: {bool(auth_token)}")

                if auth_token and "Bearer" in auth_token:
                    success = True
                    print("SUCCESS: Connection JSON is valid and contains credentials")
                else:
                    print("FAIL: Connection data missing Bearer token")
        except Exception as e:
            print(f"FAIL: Could not fetch connection data: {e}")
    else:
        print("\nFAIL: Could not find Agent Setup URL in output within timeout")

    print(f"--- Audit Complete: {'SUCCESS' if success else 'FAIL'} ---")

    # Cleanup: Kill the process group
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    # Cleanup connection file
    if Path(connection_file).exists():
        Path(connection_file).unlink()

    return success


if __name__ == "__main__":
    if run_audit():
        sys.exit(0)
    else:
        sys.exit(1)
