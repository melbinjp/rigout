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
import platform
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
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
    connection_setup_url,
    health_url_from_mcp_url,
    local_url,
    normalize_path,
    write_connection_file,
)

CLOUDFLARE_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
CLOUDFLARED_DOWNLOAD_BASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download"


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


def normalize_cloudflared_arch(machine: str | None = None) -> str:
    machine_name = (machine or platform.machine()).lower()
    if machine_name in {"x86_64", "amd64"}:
        return "amd64"
    if machine_name in {"i386", "i686", "x86"}:
        return "386"
    if machine_name in {"aarch64", "arm64"}:
        return "arm64"
    if machine_name.startswith("armv7") or machine_name.startswith("armv6") or machine_name == "arm":
        return "arm"
    raise RuntimeError(f"Unsupported architecture for cloudflared auto-install: {machine_name}")


def cloudflared_asset(system: str | None = None, machine: str | None = None) -> tuple[str, bool]:
    system_name = (system or platform.system()).lower()
    arch = normalize_cloudflared_arch(machine)

    if system_name == "linux" and arch in {"amd64", "386", "arm", "arm64"}:
        return f"cloudflared-linux-{arch}", False
    if system_name == "darwin" and arch in {"amd64", "arm64"}:
        return f"cloudflared-darwin-{arch}.tgz", True
    if system_name == "windows" and arch in {"amd64", "386"}:
        return f"cloudflared-windows-{arch}.exe", False

    raise RuntimeError(f"Unsupported platform for cloudflared auto-install: {system_name}/{arch}")


def cloudflared_cache_dir() -> Path:
    override = os.getenv("RIGOUT_CACHE_DIR")
    if override:
        return Path(override).expanduser() / "cloudflared"

    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))

    return base / "rigout" / "cloudflared"


def cloudflared_cache_path(system: str | None = None, machine: str | None = None) -> Path:
    asset_name, _ = cloudflared_asset(system, machine)
    system_name = (system or platform.system()).lower()
    executable_name = "cloudflared.exe" if system_name == "windows" else "cloudflared"
    platform_dir = asset_name.removesuffix(".tgz").removesuffix(".exe")
    return cloudflared_cache_dir() / platform_dir / executable_name


def make_executable(path: Path) -> None:
    if sys.platform == "win32":
        return
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def download_file(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def extract_cloudflared_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and Path(member.name).name == "cloudflared":
                source = archive.extractfile(member)
                if source is None:
                    break
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                return

    raise RuntimeError("Downloaded cloudflared archive did not contain a cloudflared executable")


def install_cloudflared() -> Path:
    target = cloudflared_cache_path()
    if target.exists():
        make_executable(target)
        return target

    asset_name, is_archive = cloudflared_asset()
    download_url = f"{CLOUDFLARED_DOWNLOAD_BASE_URL}/{asset_name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_download = target.parent / f"{asset_name}.download"

    print(f"cloudflared is not installed. Downloading {asset_name}...")
    try:
        download_file(download_url, temporary_download)
        if is_archive:
            extract_cloudflared_archive(temporary_download, target)
        else:
            temporary_download.replace(target)
        make_executable(target)
    finally:
        if temporary_download.exists():
            temporary_download.unlink()

    print(f"Installed cloudflared at {target}")
    return target


def resolve_cloudflared_binary(cloudflared_path: str | None = None, allow_download: bool = True) -> str:
    if cloudflared_path:
        path = Path(cloudflared_path).expanduser()
        if not path.exists():
            raise RuntimeError(f"cloudflared binary not found: {path}")
        return str(path)

    existing_binary = shutil.which("cloudflared")
    if existing_binary:
        return existing_binary

    if allow_download:
        return str(install_cloudflared())

    raise RuntimeError(
        "cloudflared is not installed. Re-run without --no-cloudflared-download or install it from "
        "https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/"
    )


def start_server(
    args: argparse.Namespace,
    *,
    public_url: str | None = None,
    setup_token: str | None = None,
) -> subprocess.Popen:
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
        "--connection-file",
        args.connection_file,
        "--no-write-connection",
    ]
    if public_url:
        command.extend(["--public-url", public_url])
    if args.json_response:
        command.append("--json-response")
    if args.stateless:
        command.append("--stateless")
    if args.auth_token:
        command.extend(["--auth-token", args.auth_token])
    if setup_token:
        command.extend(["--setup-token", setup_token])

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


def start_cloudflare_tunnel(
    port: int,
    timeout: int = 45,
    cloudflared_path: str | None = None,
    allow_download: bool = True,
) -> tuple[subprocess.Popen, str]:
    cloudflared_binary = resolve_cloudflared_binary(cloudflared_path, allow_download)

    command = [
        cloudflared_binary,
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
    parser.add_argument("--setup-token", help="Use this token for the generated agent setup URL")
    parser.add_argument(
        "--no-agent-setup-url",
        action="store_true",
        help="Do not generate a credential-bearing setup URL for public/tunnel mode",
    )
    parser.add_argument("--cloudflared-path", help="Use this cloudflared binary instead of PATH/cache lookup")
    parser.add_argument(
        "--no-cloudflared-download",
        action="store_true",
        help="Fail instead of downloading cloudflared when --tunnel cloudflare is used and cloudflared is missing",
    )
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
    is_public = bool(args.tunnel != "none" or args.public_url)
    if not args.auth_token and not args.no_auth and is_public:
        args.auth_token = secrets.token_urlsafe(32)
    setup_token = None
    if args.auth_token and is_public and not args.no_agent_setup_url:
        setup_token = args.setup_token or secrets.token_urlsafe(32)

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

        server_public_url = resolve_public_mcp_url(args, None) if args.public_url else None
        initial_mcp_url = server_public_url or local_url(args.host, args.port, args.path)
        server_process = start_server(
            args,
            public_url=server_public_url,
            setup_token=setup_token if server_public_url else None,
        )
        local_health_url = health_url_from_mcp_url(local_url(args.host, args.port, args.path), args.path)
        if not wait_for_health(local_health_url):
            raise RuntimeError(f"MCP server did not become healthy at {local_health_url}")

        tunnel_base_url = None
        if args.tunnel == "cloudflare":
            print("Starting Cloudflare quick tunnel...")
            tunnel_process, tunnel_base_url = start_cloudflare_tunnel(
                args.port,
                cloudflared_path=args.cloudflared_path,
                allow_download=not args.no_cloudflared_download,
            )

        mcp_url = resolve_public_mcp_url(args, tunnel_base_url)
        if mcp_url != initial_mcp_url:
            stop_process(server_process)
            server_process = start_server(args, public_url=mcp_url, setup_token=setup_token)
            if not wait_for_health(local_health_url):
                raise RuntimeError(f"MCP server did not become healthy at {local_health_url}")

        setup_url = connection_setup_url(mcp_url, args.path, setup_token) if setup_token else None
        write_connection_file(
            args.connection_file,
            mcp_url,
            args.host,
            args.port,
            args.path,
            args.auth_token,
            agent_setup_url=setup_url,
        )

        print()
        print("Hardware MCP server is running.")
        print(f"MCP URL: {mcp_url}")
        print(f"Health: {health_url_from_mcp_url(mcp_url, args.path)}")
        print(f"Connection file: {Path(args.connection_file).resolve()}")
        if args.auth_token:
            print("Auth: bearer token written to connection file")
        if setup_url:
            print(f"Agent setup URL: {setup_url}")
            print("Paste this URL to your AI agent so it can configure itself.")
            print("Treat the agent setup URL like a password; it can fetch the bearer token.")
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
