"""The rigout command."""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from ._version import __version__
from .clipboard import copy_to_clipboard

DEFAULT_PORT = 8765
COMMANDS = {"serve", "share", "url", "token", "stdio"}
QUICK_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CLOUDFLARED_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download"


# State: the token and the last URL, in the user's own state folder.


def state_dir() -> Path:
    configured = os.getenv("RIGOUT_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "rigout"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "rigout"
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "rigout"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        if sys.platform != "win32":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_token(reset: bool = False) -> str:
    """The token, created once and kept until reset."""
    path = state_dir() / "token"
    if not reset and path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    _write_private(path, token + "\n")
    return token


def save_url(url: str) -> None:
    _write_private(state_dir() / "last.json", json.dumps({"url": url}))


def last_url() -> str | None:
    try:
        url = json.loads((state_dir() / "last.json").read_text(encoding="utf-8")).get("url")
    except (OSError, ValueError):
        return None
    return url if isinstance(url, str) else None


def client_config(client: str, url: str, token: str) -> str:
    header = f"Bearer {token}"
    if client == "claude":
        return f'claude mcp add --transport http rigout {url} --header "Authorization: {header}"'
    if client == "cursor":
        return json.dumps({"mcpServers": {"rigout": {"url": url, "headers": {"Authorization": header}}}}, indent=2)
    if client == "vscode":
        return json.dumps(
            {"servers": {"rigout": {"type": "http", "url": url, "headers": {"Authorization": header}}}}, indent=2
        )
    return f"URL: {url}\nHeader: Authorization: {header}"


# cloudflared: used from PATH, or downloaded once into the state folder.


def cloudflared_binary() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None or system not in {"linux", "darwin", "windows"}:
        raise RuntimeError(f"Install cloudflared yourself; there is no automatic download for {system}/{machine}.")
    asset = {"linux": f"cloudflared-linux-{arch}", "darwin": f"cloudflared-darwin-{arch}.tgz"}.get(
        system, f"cloudflared-windows-{arch}.exe"
    )
    target = state_dir() / "bin" / ("cloudflared.exe" if system == "windows" else "cloudflared")
    if target.exists():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    download = target.parent / f"{asset}.download"
    print(f"Downloading cloudflared ({asset}) once...")
    try:
        with urllib.request.urlopen(f"{CLOUDFLARED_RELEASES}/{asset}", timeout=120) as response:  # noqa: S310
            download.write_bytes(response.read())
        if asset.endswith(".tgz"):
            with tarfile.open(download, "r:gz") as archive:
                member = next(m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "cloudflared")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError("The cloudflared download did not contain the program.")
                target.write_bytes(extracted.read())
        else:
            download.replace(target)
        if system != "windows":
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
    finally:
        if download.exists():
            download.unlink()
    return str(target)


def _relay(process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        lines.put(line)


def start_tunnel(port: int, name: str | None, hostname: str | None) -> tuple[subprocess.Popen[str], str]:
    local = f"http://127.0.0.1:{port}"
    argv = [cloudflared_binary(), "tunnel", "--no-autoupdate"]
    argv += ["run", "--url", local, name] if name else ["--url", local]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)  # noqa: S603
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_relay, args=(process, lines), daemon=True).start()
    seen: list[str] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(line.rstrip())
        if name and "Registered tunnel connection" in line:
            return process, f"https://{hostname}"
        match = QUICK_TUNNEL_URL.search(line)
        if not name and match:
            return process, match.group(0)
    process.terminate()
    tail = "\n".join(seen[-10:])
    raise RuntimeError(f"cloudflared did not open a tunnel.\n{tail}")


# Commands.


def serve(args: argparse.Namespace, public_base: str | None = None) -> int:
    import uvicorn

    from .http import MCP_PATH, create_app
    from .tools import Workspace

    workspace = Workspace(args.workspace)
    token = load_token()
    host = getattr(args, "host", "127.0.0.1")
    shown_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104
    url = f"{public_base or f'http://{shown_host}:{args.port}'}{MCP_PATH}"
    save_url(url)
    command = client_config("claude", url, token)
    # Only when sharing: that is when the command gets pasted somewhere else.
    copied = public_base is not None and copy_to_clipboard(command) is not None
    print(f"rigout {__version__}")
    print(f"Workspace  {workspace.root}")
    print(f"URL        {url}")
    print(f"Token      {token}")
    print(f"\nClaude Code{' (copied to clipboard)' if copied else ''}:\n  {command}")
    print("Other clients: rigout url --client cursor | vscode | raw")
    print("\nAnyone with this URL and token can run commands here as you. Ctrl+C stops rigout.")
    # Printed lines sit in a buffer when output goes to a file or a service log, and the server
    # never returns to flush them, so the URL would never appear.
    sys.stdout.flush()
    uvicorn.run(create_app(workspace, token), host=host, port=args.port, log_level="warning")
    return 0


def share(args: argparse.Namespace) -> int:
    if bool(args.tunnel) != bool(args.hostname):
        print("Use --tunnel and --hostname together, for a named Cloudflare tunnel.", file=sys.stderr)
        return 2
    try:
        tunnel, base = start_tunnel(args.port, args.tunnel, args.hostname)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not args.tunnel:
        print("This quick-tunnel URL changes each start; the token stays the same.")
    try:
        args.host = "127.0.0.1"
        return serve(args, public_base=base)
    finally:
        tunnel.terminate()


def stdio(args: argparse.Namespace) -> int:
    import asyncio

    from .server import serve_stdio
    from .tools import Workspace

    asyncio.run(serve_stdio(Workspace(args.workspace)))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="rigout", description="Give an AI agent a shell and files on this machine.")
    root.add_argument("--version", action="version", version=f"rigout {__version__}")
    commands = root.add_subparsers(dest="command")

    def served(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--workspace", help="Folder the agent starts in. Default: the current folder.")
        sub.add_argument("--port", type=int, default=DEFAULT_PORT)

    serve_cmd = commands.add_parser("serve", help="Serve on this machine (the default).")
    served(serve_cmd)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    share_cmd = commands.add_parser("share", help="Serve and share through a Cloudflare tunnel.")
    served(share_cmd)
    share_cmd.add_argument("--tunnel", help="Named Cloudflare tunnel, for a URL that does not change.")
    share_cmd.add_argument("--hostname", help="The hostname routed to that tunnel.")
    url_cmd = commands.add_parser("url", help="Print the settings a client needs.")
    url_cmd.add_argument("--client", choices=["claude", "cursor", "vscode", "raw"], default="claude")
    token_cmd = commands.add_parser("token", help="Print the token, or replace it.")
    token_cmd.add_argument("--reset", action="store_true", help="Replace the token. Clients need the new one.")
    stdio_cmd = commands.add_parser("stdio", help="Serve a local client over stdio, with no token.")
    stdio_cmd.add_argument("--workspace")
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (arguments[0] not in COMMANDS and arguments[0] not in {"-h", "--help", "--version"}):
        arguments.insert(0, "serve")
    args = parser().parse_args(arguments)
    handlers: dict[str, Any] = {"serve": serve, "share": share, "stdio": stdio}
    if args.command in handlers:
        result: int = handlers[args.command](args)
        return result
    if args.command == "token":
        print(load_token(reset=args.reset))
        return 0
    url = last_url()
    if url is None:
        print("Start rigout first; the URL is recorded when it starts.", file=sys.stderr)
        return 1
    print(client_config(args.client, url, load_token()))
    return 0


def stdio_main() -> int:
    return main(["stdio", *sys.argv[1:]])
