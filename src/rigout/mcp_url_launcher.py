#!/usr/bin/env python3
"""
One-command launcher for the URL-based hardware MCP server.

Examples:
  rigout
  rigout --tunnel cloudflare
  python -m rigout.mcp_url_launcher --tunnel cloudflare
"""

import argparse
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .mcp_http_server import (
    DEFAULT_CONNECTION_FILE,
    DEFAULT_HOST,
    DEFAULT_PATH,
    DEFAULT_PORT,
    health_url_from_mcp_url,
    local_url,
    normalize_path,
    write_connection_file,
)

CLOUDFLARE_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def install_dependencies() -> None:
    requirements = Path("requirements.txt")
    if not requirements.exists():
        return
    print("Installing Python dependencies...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        check=True,
    )


def wait_for_health(url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return bool(response.status == 200)
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    return False


def start_server(args: argparse.Namespace) -> subprocess.Popen:
    command = [
        sys.executable,
        "-m",
        "rigout.mcp_http_server",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--path",
        args.path,
        "--no-write-connection",
    ]
    if args.json_response:
        command.append("--json-response")
    if args.stateless:
        command.append("--stateless")
    if args.auth_token:
        command.extend(["--auth-token", args.auth_token])

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    src_path = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    return subprocess.Popen(command, env=env)


def stream_process_output(process: subprocess.Popen, output_queue: queue.Queue[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        output_queue.put(line)


def start_cloudflare_tunnel(port: int, timeout: int = 45) -> tuple[subprocess.Popen, str]:
    if not shutil.which("cloudflared"):
        raise RuntimeError(
            "cloudflared is not installed. Install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        )

    command = [
        "cloudflared",
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port}",
        "--no-autoupdate",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    output_queue: queue.Queue[str] = queue.Queue()
    threading.Thread(target=stream_process_output, args=(process, output_queue), daemon=True).start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("cloudflared exited before publishing a tunnel URL")
        try:
            line = output_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        match = CLOUDFLARE_URL_PATTERN.search(line)
        if match:
            return process, match.group(0)

    process.terminate()
    raise RuntimeError("Timed out waiting for cloudflared to publish a tunnel URL")


def stop_process(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up and run a URL-based hardware MCP server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Local bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local bind port")
    parser.add_argument("--path", default=DEFAULT_PATH, help="MCP path")
    parser.add_argument(
        "--tunnel",
        choices=["none", "cloudflare"],
        default="none",
        help="Expose a public URL with a quick tunnel",
    )
    parser.add_argument("--public-url", help="Use an already-created public base URL or full MCP URL")
    parser.add_argument("--connection-file", default=DEFAULT_CONNECTION_FILE)
    parser.add_argument("--auth-token", help="Bearer token required for MCP requests")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Do not generate a bearer token for public/tunnel URLs. Unsafe for internet-exposed servers.",
    )
    parser.add_argument("--skip-install", action="store_true", help="Do not install Python requirements")
    parser.add_argument("--json-response", action="store_true")
    parser.add_argument("--stateless", action="store_true")
    return parser.parse_args(argv)


def resolve_public_mcp_url(args: argparse.Namespace, tunnel_base_url: str | None) -> str:
    path = normalize_path(args.path)
    if args.public_url:
        public_url = str(args.public_url).rstrip("/")
        return str(public_url if public_url.endswith(path) else public_url + path)
    if tunnel_base_url:
        return tunnel_base_url.rstrip("/") + path
    return local_url(args.host, args.port, path)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.path = normalize_path(args.path)
    if not args.auth_token and not args.no_auth and (args.tunnel != "none" or args.public_url):
        args.auth_token = secrets.token_urlsafe(32)

    server_process: subprocess.Popen | None = None
    tunnel_process: subprocess.Popen | None = None

    def shutdown(*_: object) -> None:
        print("\nStopping MCP server...")
        stop_process(tunnel_process)
        stop_process(server_process)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        if not args.skip_install:
            install_dependencies()

        server_process = start_server(args)
        health_url = health_url_from_mcp_url(local_url(args.host, args.port, args.path), args.path)
        if not wait_for_health(health_url):
            raise RuntimeError(f"MCP server did not become healthy at {health_url}")

        tunnel_base_url = None
        if args.tunnel == "cloudflare":
            print("Starting Cloudflare quick tunnel...")
            tunnel_process, tunnel_base_url = start_cloudflare_tunnel(args.port)

        mcp_url = resolve_public_mcp_url(args, tunnel_base_url)
        write_connection_file(args.connection_file, mcp_url, args.host, args.port, args.path, args.auth_token)

        print()
        print("Hardware MCP server is running.")
        print(f"MCP URL: {mcp_url}")
        print(f"Health: {health_url_from_mcp_url(mcp_url, args.path)}")
        print(f"Connection file: {Path(args.connection_file).resolve()}")
        if args.auth_token:
            print("Auth: bearer token written to connection file")
        print("Press Ctrl+C to stop.")

        while server_process.poll() is None:
            if tunnel_process and tunnel_process.poll() is not None:
                raise RuntimeError("Cloudflare tunnel stopped unexpectedly")
            time.sleep(1)

        return server_process.returncode or 0
    except Exception as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        stop_process(tunnel_process)
        stop_process(server_process)
        return 1


if __name__ == "__main__":
    sys.exit(main())
