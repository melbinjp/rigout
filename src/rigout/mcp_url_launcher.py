#!/usr/bin/env python3
"""
One-command launcher for the URL-based hardware MCP server.

Examples:
  rigout
  rigout --tunnel cloudflare
  python -m rigout.mcp_url_launcher --tunnel cloudflare
"""

import argparse
import hashlib
import json
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
from typing import Any

from .lifecycle import (
    MAX_LOG_TAIL_LINES,
    RuntimePaths,
    append_activity,
    launch_detached,
    open_activity_log,
    process_identity,
    process_is_running,
    process_matches_identity,
    read_json,
    read_pid,
    read_tail,
    redact_sensitive_text,
    remove_pid,
    runtime_status,
    secure_file,
    terminate_process,
    utc_now,
    write_json_secure,
    write_pid,
)
from .mcp_http_server import (
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


def verify_checksum(path: Path, expected_sha256: str) -> bool:
    sha256_hash = hashlib.sha256()
    with path.open("rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest() == expected_sha256


def download_file(url: str, destination: Path, expected_sha256: str | None = None) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)

    if expected_sha256 and not verify_checksum(destination, expected_sha256):
        destination.unlink()
        raise RuntimeError(f"Checksum verification failed for {url}")


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
        # Optional pinning: verify against a user-supplied checksum. The
        # download URL tracks Cloudflare's latest release, so a fixed
        # checksum cannot be baked in here.
        expected_sha256 = os.getenv("RIGOUT_CLOUDFLARED_SHA256")
        download_file(download_url, temporary_download, expected_sha256)
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
    capture_output: bool = False,
    runtime_cwd: str | Path | None = None,
) -> subprocess.Popen[str]:
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

    env = os.environ.copy()
    # Tokens travel via the environment so they never show up in the
    # process list (argv is world-readable on most platforms).
    if args.auth_token:
        env["RIGOUT_AUTH_TOKEN"] = args.auth_token
    if setup_token:
        env["RIGOUT_SETUP_TOKEN"] = setup_token
    if runtime_cwd:
        env["RIGOUT_STATE_DIR"] = str(runtime_cwd)
    env["PYTHONUNBUFFERED"] = "1"
    kwargs: dict[str, Any] = {"env": env, "text": True}
    if runtime_cwd:
        kwargs["cwd"] = str(runtime_cwd)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    return subprocess.Popen(command, **kwargs)


def stream_process_output(
    process: subprocess.Popen[str],
    output_queue: queue.Queue[str] | None = None,
    activity_paths: RuntimePaths | None = None,
) -> None:
    """Relay subprocess output after redacting credentials."""
    assert process.stdout is not None
    for line in process.stdout:
        safe_line = redact_sensitive_text(line)
        print(safe_line, end="")
        if activity_paths:
            append_activity(activity_paths, safe_line)
        if output_queue:
            output_queue.put(line)


def start_cloudflare_tunnel(
    port: int,
    timeout: int = 45,
    cloudflared_path: str | None = None,
    allow_download: bool = True,
    activity_paths: RuntimePaths | None = None,
) -> tuple[subprocess.Popen[str], str]:
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
    threading.Thread(
        target=stream_process_output,
        args=(process, output_queue, activity_paths),
        daemon=True,
    ).start()

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


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def add_start_arguments(parser: argparse.ArgumentParser) -> None:
    """Add foreground and managed-start options to a parser."""
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
    parser.add_argument("--connection-file", help="Connection file (defaults to the managed state directory)")
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
    parser.add_argument("--json-response", action="store_true")
    parser.add_argument("--stateless", action="store_true")
    parser.add_argument("--detach", action="store_true", help="Run in the background and return after startup")
    parser.add_argument("--state-dir", help="Runtime state directory (or set RIGOUT_STATE_DIR)")
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="CLI result format; JSON startup output requires --detach",
    )
    parser.add_argument("--managed-child", action="store_true", help=argparse.SUPPRESS)


def add_management_arguments(parser: argparse.ArgumentParser) -> None:
    """Add options shared by status, logs, and stop."""
    parser.add_argument("--state-dir", help="Runtime state directory (or set RIGOUT_STATE_DIR)")
    parser.add_argument("--output", choices=["text", "json"], default="text")


def bounded_tail_count(value: str) -> int:
    """Parse a bounded activity-log tail count."""
    count = int(value)
    if not 0 <= count <= MAX_LOG_TAIL_LINES:
        raise argparse.ArgumentTypeError(f"tail must be between 0 and {MAX_LOG_TAIL_LINES}")
    return count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse lifecycle commands while preserving the legacy flag-only start form."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    command = raw_args[0] if raw_args and raw_args[0] in {"start", "status", "logs", "stop"} else "start"
    explicit_lifecycle = bool(raw_args and raw_args[0] in {"start", "status", "logs", "stop"})
    command_args = raw_args[1:] if explicit_lifecycle else raw_args

    if command == "start":
        parser = argparse.ArgumentParser(
            prog="rigout start" if explicit_lifecycle else "rigout",
            description="Set up and run a URL-based hardware MCP server",
            epilog=("Lifecycle commands: rigout start [--detach], rigout status, rigout logs [--follow], rigout stop"),
        )
        add_start_arguments(parser)
    elif command == "status":
        parser = argparse.ArgumentParser(prog="rigout status", description="Show managed Rigout status")
        add_management_arguments(parser)
    elif command == "logs":
        parser = argparse.ArgumentParser(prog="rigout logs", description="Read managed Rigout activity")
        add_management_arguments(parser)
        parser.add_argument("--tail", type=bounded_tail_count, default=100, help="Number of existing lines to show")
        parser.add_argument("--follow", action="store_true", help="Continue streaming until Rigout stops")
    else:
        parser = argparse.ArgumentParser(prog="rigout stop", description="Stop managed Rigout")
        add_management_arguments(parser)
        parser.add_argument("--timeout", type=float, default=10.0, help="Seconds to wait for shutdown")

    parsed = parser.parse_args(command_args)
    parsed.command = command
    parsed.explicit_lifecycle = explicit_lifecycle
    return parsed


def resolve_public_mcp_url(args: argparse.Namespace, tunnel_base_url: str | None) -> str:
    path = normalize_path(args.path)
    if args.public_url:
        public_url = str(args.public_url).rstrip("/")
        return str(public_url if public_url.endswith(path) else public_url + path)
    if tunnel_base_url:
        return tunnel_base_url.rstrip("/") + path
    return local_url(args.host, args.port, path)


def prepare_start_args(args: argparse.Namespace, paths: RuntimePaths) -> bool:
    """Normalize start options and return whether lifecycle state is enabled."""
    args.path = normalize_path(args.path)
    args.auth_token = args.auth_token or os.getenv("RIGOUT_AUTH_TOKEN")
    args.setup_token = args.setup_token or os.getenv("RIGOUT_SETUP_TOKEN")

    managed = True
    if not args.connection_file:
        args.connection_file = str(paths.connection_file)
    return managed


def runtime_metadata(
    args: argparse.Namespace,
    paths: RuntimePaths,
    *,
    status: str,
    pid: int,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Build credential-free runtime metadata for status and automation."""
    metadata = {
        "status": status,
        "pid": pid,
        "managed": True,
        "process_identity": process_identity(pid),
        "started_at": utc_now(),
        "host": args.host,
        "port": args.port,
        "path": args.path,
        "tunnel": args.tunnel,
        "connection_file": str(Path(args.connection_file).expanduser().resolve()),
        "activity_log": str(paths.log_file),
    }
    resolved_instance_id = instance_id or os.getenv("RIGOUT_INSTANCE_ID")
    if resolved_instance_id:
        metadata["instance_id"] = resolved_instance_id
    return metadata


def build_managed_child_command(args: argparse.Namespace, paths: RuntimePaths) -> list[str]:
    """Build a token-free command line for the detached launcher child."""
    command = [
        sys.executable,
        "-m",
        "rigout.mcp_url_launcher",
        "start",
        "--managed-child",
        "--state-dir",
        str(paths.root),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--path",
        args.path,
        "--tunnel",
        args.tunnel,
        "--connection-file",
        str(Path(args.connection_file).expanduser().resolve()),
    ]
    value_options = {
        "--public-url": args.public_url,
        "--cloudflared-path": args.cloudflared_path,
    }
    for option, value in value_options.items():
        if value:
            command.extend([option, str(value)])
    flag_options = {
        "--no-agent-setup-url": args.no_agent_setup_url,
        "--no-cloudflared-download": args.no_cloudflared_download,
        "--no-auth": args.no_auth,
        "--json-response": args.json_response,
        "--stateless": args.stateless,
    }
    for option, enabled in flag_options.items():
        if enabled:
            command.append(option)
    return command


def connection_summary(path: str | Path) -> tuple[dict[str, Any], str | None]:
    """Read safe connection fields plus the human-only setup URL."""
    connection = read_json(Path(path))
    mcp_value = connection.get("mcp")
    mcp: dict[str, Any] = mcp_value if isinstance(mcp_value, dict) else {}
    summary = {
        "transport": mcp.get("transport", "streamable-http"),
        "mcp_url": mcp.get("url"),
        "health_url": mcp.get("health_url"),
    }
    setup_url = connection.get("agent_setup_url")
    return summary, str(setup_url) if setup_url else None


def print_json(value: dict[str, Any]) -> None:
    """Print one machine-readable CLI result."""
    print(json.dumps(value, indent=2, sort_keys=True))


def print_start_result(result: dict[str, Any], setup_url: str | None, output: str) -> None:
    """Print detached startup state for an agent or human."""
    if output == "json":
        print_json(result)
        return

    print("Hardware MCP server is running in the background.")
    if result.get("mcp_url"):
        print(f"MCP URL: {result['mcp_url']}")
    if result.get("health_url"):
        print(f"Health: {result['health_url']}")
    print(f"PID: {result['pid']}")
    print(f"Connection file: {result['connection_file']}")
    print(f"Activity log: {result['activity_log']}")
    if setup_url:
        print(f"Agent setup URL: {setup_url}")
        print("Treat the agent setup URL like a password; it can fetch the bearer token.")
    print("Use `rigout status`, `rigout logs --follow`, and `rigout stop` to manage it.")


def start_detached(args: argparse.Namespace, paths: RuntimePaths) -> int:
    """Launch a background Rigout process and wait for a definitive startup result."""
    existing = runtime_status(paths)
    if existing["running"]:
        message = f"Rigout is already running with PID {existing['pid']}"
        if args.output == "json":
            print_json({**existing, "error": message})
        else:
            print(message, file=sys.stderr)
        return 1

    paths.prepare()
    instance_id = secrets.token_urlsafe(16)
    initial = runtime_metadata(args, paths, status="starting", pid=0, instance_id=instance_id)
    write_json_secure(paths.runtime_file, initial)
    remove_pid(paths)

    env = os.environ.copy()
    env.pop("RIGOUT_AUTH_TOKEN", None)
    env.pop("RIGOUT_SETUP_TOKEN", None)
    if args.auth_token:
        env["RIGOUT_AUTH_TOKEN"] = args.auth_token
    if args.setup_token:
        env["RIGOUT_SETUP_TOKEN"] = args.setup_token
    env["RIGOUT_STATE_DIR"] = str(paths.root)
    env["RIGOUT_DETACHED_CHILD"] = "1"
    env["RIGOUT_INSTANCE_ID"] = instance_id
    env["PYTHONUNBUFFERED"] = "1"

    command = build_managed_child_command(args, paths)
    process = launch_detached(command, paths, env)
    write_pid(paths, process.pid)

    deadline = time.time() + 90
    while time.time() < deadline:
        runtime = runtime_status(paths)
        owns_runtime = runtime.get("instance_id") == instance_id
        if owns_runtime and runtime.get("status") == "running" and runtime.get("running"):
            safe_connection, setup_url = connection_summary(runtime["connection_file"])
            result = {**runtime, **safe_connection}
            print_start_result(result, setup_url, args.output)
            return 0
        if owns_runtime and runtime.get("status") == "failed":
            message = str(runtime.get("last_error", "Rigout startup failed"))
            if args.output == "json":
                print_json({**runtime, "error": message})
            else:
                print(f"Setup failed: {message}", file=sys.stderr)
                print(f"Activity log: {paths.log_file}", file=sys.stderr)
            return 1
        if process.poll() is not None:
            runtime = runtime_status(paths)
            message = str(runtime.get("last_error", f"Rigout exited with code {process.returncode}"))
            if args.output == "json":
                print_json({**runtime, "error": message})
            else:
                print(f"Setup failed: {message}", file=sys.stderr)
                print(f"Activity log: {paths.log_file}", file=sys.stderr)
            return 1
        time.sleep(0.2)

    runtime = runtime_status(paths)
    runtime_pid = runtime.get("pid") if runtime.get("instance_id") == instance_id else None
    pid_to_stop = runtime_pid if isinstance(runtime_pid, int) else process.pid
    terminate_process(pid_to_stop)
    remove_pid(paths, pid_to_stop)
    failed = {**initial, "status": "failed", "last_error": "Timed out waiting for managed startup"}
    write_json_secure(paths.runtime_file, failed)
    if args.output == "json":
        print_json({**runtime_status(paths), "error": failed["last_error"]})
    else:
        print(f"Setup failed: {failed['last_error']}", file=sys.stderr)
        print(f"Activity log: {paths.log_file}", file=sys.stderr)
    return 1


def run_foreground(args: argparse.Namespace, paths: RuntimePaths, managed: bool) -> int:
    """Run the launcher in the foreground, optionally with lifecycle state."""
    detached_child = bool(args.managed_child or os.getenv("RIGOUT_DETACHED_CHILD"))
    if managed and not detached_child:
        existing = runtime_status(paths)
        existing_pid = existing.get("pid")
        if existing.get("running") and existing_pid != os.getpid():
            print(f"Setup failed: Rigout is already running with PID {existing_pid}", file=sys.stderr)
            return 1

    is_public = bool(args.tunnel != "none" or args.public_url)
    if not args.auth_token and not args.no_auth and is_public:
        args.auth_token = secrets.token_urlsafe(32)
    setup_token = None
    if args.auth_token and is_public and not args.no_agent_setup_url:
        setup_token = args.setup_token or secrets.token_urlsafe(32)

    server_process: subprocess.Popen[str] | None = None
    tunnel_process: subprocess.Popen[str] | None = None
    activity_paths = paths if managed and not detached_child else None
    last_error: str | None = None

    if managed:
        paths.prepare()
        if not detached_child:
            with open_activity_log(paths, truncate=True):
                pass
        write_pid(paths, os.getpid())
        write_json_secure(paths.runtime_file, runtime_metadata(args, paths, status="starting", pid=os.getpid()))

    def report(message: str = "", *, error: bool = False) -> None:
        print(message, file=sys.stderr if error else sys.stdout)
        if activity_paths:
            append_activity(paths, redact_sensitive_text(message) + "\n")

    def shutdown(*_: object) -> None:
        report("\nStopping MCP server...")
        stop_process(tunnel_process)
        stop_process(server_process)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        tunnel_base_url = None
        if args.tunnel == "cloudflare":
            report("Starting Cloudflare quick tunnel...")
            tunnel_process, tunnel_base_url = start_cloudflare_tunnel(
                args.port,
                cloudflared_path=args.cloudflared_path,
                allow_download=not args.no_cloudflared_download,
                activity_paths=activity_paths,
            )

        mcp_url = resolve_public_mcp_url(args, tunnel_base_url)
        server_process = start_server(
            args,
            public_url=mcp_url,
            setup_token=setup_token,
            capture_output=managed,
            runtime_cwd=paths.root if managed else None,
        )
        if managed:
            threading.Thread(
                target=stream_process_output,
                args=(server_process, None, activity_paths),
                daemon=True,
            ).start()
        local_health_url = health_url_from_mcp_url(local_url(args.host, args.port, args.path), args.path)
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
        secure_file(Path(args.connection_file))
        secure_file(paths.root / "mcp-hardware-server.log")

        public_health_url = health_url_from_mcp_url(mcp_url, args.path)
        if managed:
            running = runtime_metadata(args, paths, status="running", pid=os.getpid())
            running.update(
                {
                    "started_at": read_json(paths.runtime_file).get("started_at", utc_now()),
                    "mcp_url": mcp_url,
                    "health_url": public_health_url,
                    "local_health_url": local_health_url,
                }
            )
            write_json_secure(paths.runtime_file, running)

        report()
        report("Hardware MCP server is running.")
        report(f"MCP URL: {mcp_url}")
        report(f"Health: {public_health_url}")
        report(f"Connection file: {Path(args.connection_file).resolve()}")
        if args.auth_token:
            report("Auth: bearer token written to connection file")
        if setup_url and not detached_child:
            report(f"Agent setup URL: {setup_url}")
            report("Paste this URL to your AI agent so it can configure itself.")
            report("Treat the agent setup URL like a password; it can fetch the bearer token.")
        elif setup_url:
            report("Agent setup URL: stored in the owner-readable connection file")
        report("Press Ctrl+C to stop.")

        while server_process.poll() is None:
            if tunnel_process and tunnel_process.poll() is not None:
                raise RuntimeError("Cloudflare tunnel stopped unexpectedly")
            time.sleep(1)

        return_code = server_process.returncode or 0
        if return_code in (-signal.SIGTERM, -signal.SIGINT):
            return 0  # normal shutdown via Ctrl+C or terminate
        return return_code
    except Exception as exc:
        last_error = str(exc)
        report(f"Setup failed: {exc}", error=True)
        return 1
    finally:
        stop_process(tunnel_process)
        stop_process(server_process)
        if managed:
            current = read_json(paths.runtime_file)
            current.update(
                {
                    "status": "failed" if last_error else "stopped",
                    "stopped_at": utc_now(),
                }
            )
            if last_error:
                current["last_error"] = last_error
            write_json_secure(paths.runtime_file, current)
            remove_pid(paths, os.getpid())


def handle_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    """Print current managed lifecycle status."""
    status = runtime_status(paths)
    if args.output == "json":
        print_json(status)
    else:
        print(f"Rigout status: {status['status']}")
        if status.get("pid"):
            print(f"PID: {status['pid']}")
        if status.get("mcp_url"):
            print(f"MCP URL: {status['mcp_url']}")
        print(f"Connection file: {status['connection_file']}")
        print(f"Activity log: {status['activity_log']}")
    return 0 if status["running"] else 1


def handle_logs(args: argparse.Namespace, paths: RuntimePaths) -> int:
    """Print or follow the managed activity log."""
    if args.follow and args.output == "json":
        print("--output json cannot be combined with --follow; follow output is a text stream", file=sys.stderr)
        return 2

    lines = read_tail(paths.log_file, max(0, args.tail))
    if args.output == "json":
        print_json(
            {
                "status": runtime_status(paths)["status"],
                "activity_log": str(paths.log_file),
                "lines": lines,
            }
        )
        return 0 if paths.log_file.exists() else 1

    for line in lines:
        print(line)
    if not args.follow:
        return 0 if paths.log_file.exists() else 1

    try:
        with paths.log_file.open(encoding="utf-8", errors="replace") as source:
            source.seek(0, os.SEEK_END)
            idle_after_stop = 0
            while True:
                line = source.readline()
                if line:
                    print(line, end="")
                    idle_after_stop = 0
                    continue
                if runtime_status(paths)["running"]:
                    time.sleep(0.25)
                    continue
                idle_after_stop += 1
                if idle_after_stop >= 2:
                    break
                time.sleep(0.25)
    except FileNotFoundError:
        print(f"No activity log exists at {paths.log_file}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


def handle_stop(args: argparse.Namespace, paths: RuntimePaths) -> int:
    """Stop the managed launcher and its child processes."""
    status = runtime_status(paths)
    pid = read_pid(paths)
    if not pid or not process_is_running(pid):
        remove_pid(paths)
        result = {**status, "status": "stopped", "running": False, "pid": None}
        if args.output == "json":
            print_json(result)
        else:
            print("Rigout is not running.")
        return 0

    runtime = read_json(paths.runtime_file)
    if (
        not runtime.get("managed")
        or runtime.get("pid") != pid
        or not process_matches_identity(pid, runtime.get("process_identity"))
    ):
        message = "Refusing to stop a PID that is not recorded as a managed Rigout launcher"
        if args.output == "json":
            print_json({**status, "error": message})
        else:
            print(message, file=sys.stderr)
        return 1

    runtime["status"] = "stopping"
    write_json_secure(paths.runtime_file, runtime)
    stopped = terminate_process(pid, timeout=args.timeout)
    if not stopped:
        message = f"Rigout process {pid} did not stop"
        if args.output == "json":
            print_json({**runtime_status(paths), "error": message})
        else:
            print(message, file=sys.stderr)
        return 1

    remove_pid(paths, pid)
    runtime = read_json(paths.runtime_file) or runtime
    runtime.update({"status": "stopped", "stopped_at": utc_now()})
    write_json_secure(paths.runtime_file, runtime)
    result = runtime_status(paths)
    if args.output == "json":
        print_json(result)
    else:
        print("Rigout stopped.")
        print(f"Activity log: {paths.log_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = RuntimePaths.resolve(args.state_dir)

    if args.command == "status":
        return handle_status(args, paths)
    if args.command == "logs":
        return handle_logs(args, paths)
    if args.command == "stop":
        return handle_stop(args, paths)

    managed = prepare_start_args(args, paths)
    if args.output == "json" and not args.detach and not args.managed_child:
        print("--output json requires --detach for a finite, machine-readable startup result", file=sys.stderr)
        return 2
    if args.detach and not args.managed_child:
        is_public = bool(args.tunnel != "none" or args.public_url)
        if not args.auth_token and not args.no_auth and is_public:
            args.auth_token = secrets.token_urlsafe(32)
        if args.auth_token and is_public and not args.no_agent_setup_url and not args.setup_token:
            args.setup_token = secrets.token_urlsafe(32)
        return start_detached(args, paths)
    return run_foreground(args, paths, managed)


if __name__ == "__main__":
    sys.exit(main())
