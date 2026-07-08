#!/usr/bin/env python3
"""
Streamable HTTP MCP server for AI Agent Hardware Access.

This keeps the existing low-level MCP tool implementation intact and exposes it
at a URL, normally http://127.0.0.1:8765/mcp or a public tunnel URL.
"""

import argparse
import hmac
import json
import os
import platform
import secrets
import socket
import stat
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .server import server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_CONNECTION_FILE = "ai_agent_connection.json"


def tokens_match(provided: str | bytes | None, expected: str | bytes) -> bool:
    """Compare secrets in constant time to avoid timing side channels."""
    if provided is None:
        return False
    provided_bytes = provided.encode() if isinstance(provided, str) else provided
    expected_bytes = expected.encode() if isinstance(expected, str) else expected
    return hmac.compare_digest(provided_bytes, expected_bytes)


class BearerAuthASGIApp:
    """Protect an ASGI app with a static bearer token."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        headers = dict(scope.get("headers") or [])
        if not tokens_match(headers.get(b"authorization"), self.expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class StreamableHTTPASGIApp:
    """Small ASGI adapter for the MCP streamable HTTP session manager."""

    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def normalize_path(path: str) -> str:
    if not path:
        return DEFAULT_PATH
    return path if path.startswith("/") else f"/{path}"


def local_url(host: str, port: int, path: str) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{display_host}:{port}{normalize_path(path)}"


def health_url_from_mcp_url(mcp_url: str, path: str) -> str:
    normalized = normalize_path(path)
    if mcp_url.endswith(normalized):
        return mcp_url[: -len(normalized)] + "/health"
    return mcp_url.rstrip("/") + "/health"


_hardware_summary_cache: dict[str, Any] | None = None


def get_hardware_summary() -> dict[str, Any]:
    global _hardware_summary_cache
    if _hardware_summary_cache is None:
        gpu_info: list[str] = []
        _hardware_summary_cache = {
            "cpu_count": os.cpu_count() or 0,
            "gpu_info": gpu_info,
            "platform": platform.system(),
            "architecture": platform.machine(),
            "hostname": socket.gethostname(),
        }
    return _hardware_summary_cache


def build_connection_data(
    mcp_url: str,
    host: str,
    port: int,
    path: str,
    auth_token: str | None = None,
    agent_setup_url: str | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    connection_data = {
        "mcp_server_type": "hardware_access",
        "connection_method": "mcp_streamable_http",
        "mcp_server_url": mcp_url,
        "mcp": {
            "transport": "streamable-http",
            "url": mcp_url,
            "health_url": health_url_from_mcp_url(mcp_url, path),
            "headers": headers,
        },
        "capabilities": [
            "command_execution",
            "file_operations",
            "docker_management",
            "environment_setup",
            "system_monitoring",
            "package_installation",
            "hardware_access",
            "terminal_sessions",
        ],
        "security": {
            "ai_agent_mode": True,
            "bypass_security_available": True,
            "audit_log": "mcp-hardware-server.log",
            "bind_host": host,
            "port": port,
            "auth": "bearer" if auth_token else "none",
        },
        "hardware_info": get_hardware_summary(),
        "agent_instructions": [
            "Configure the agent MCP client with mcp.url using streamable-http transport.",
            "Call manage_tunnels/add if remote SSH endpoints should be registered.",
            "Call manage_tunnels/list or system_monitoring to verify connectivity before heavy work.",
        ],
        "setup_date": datetime.now().isoformat(),
    }
    if agent_setup_url:
        connection_data["agent_setup_url"] = agent_setup_url
        connection_data["agent_setup_security"] = "credential_url"
    return connection_data


def connection_setup_url(mcp_url: str, mcp_path: str, setup_token: str) -> str:
    base_url = health_url_from_mcp_url(mcp_url, mcp_path).removesuffix("/health")
    return f"{base_url}/connection.json?{urlencode({'setup_token': setup_token})}"


def write_connection_file(
    path: str | Path,
    mcp_url: str,
    host: str,
    port: int,
    mcp_path: str,
    auth_token: str | None = None,
    agent_setup_url: str | None = None,
) -> None:
    connection_path = Path(path)
    connection_path.write_text(
        json.dumps(build_connection_data(mcp_url, host, port, mcp_path, auth_token, agent_setup_url), indent=2),
        encoding="utf-8",
    )
    if os.name == "posix":
        # The file can contain a bearer token; keep it owner-readable only
        connection_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def create_app(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    public_url: str | None = None,
    connection_file: str | Path | None = None,
    setup_token: str | None = None,
    auth_token: str | None = None,
    json_response: bool = False,
    stateless: bool = False,
) -> Starlette:
    path = normalize_path(path)
    mcp_url = public_url or local_url(host, port, path)

    if connection_file:
        write_connection_file(connection_file, mcp_url, host, port, path, auth_token)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=json_response,
        stateless=stateless,
    )
    mcp_app = StreamableHTTPASGIApp(session_manager)
    protected_mcp_app = BearerAuthASGIApp(mcp_app, auth_token) if auth_token else mcp_app

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "server": "enhanced-hardware-server",
                "transport": "streamable-http",
                "mcp_url": mcp_url,
                "mcp_path": path,
            }
        )

    async def connection(request: Request) -> JSONResponse:
        bearer_authorized = bool(
            auth_token and tokens_match(request.headers.get("authorization"), f"Bearer {auth_token}")
        )
        # Setup tokens should be passed in headers to avoid URL leakage where possible
        setup_token_header = request.headers.get("x-setup-token")
        setup_token_query = request.query_params.get("setup_token")
        setup_authorized = bool(
            setup_token
            and (tokens_match(setup_token_header, setup_token) or tokens_match(setup_token_query, setup_token))
        )

        if auth_token and not (bearer_authorized or setup_authorized):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(build_connection_data(mcp_url, host, port, path, auth_token))

    async def root(_: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "AI Agent Hardware Access MCP server\n"
            f"MCP endpoint: {mcp_url}\n"
            f"Health: {health_url_from_mcp_url(mcp_url, path)}\n"
        )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/", endpoint=root, methods=["GET"]),
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/connection.json", endpoint=connection, methods=["GET"]),
            Route(path, endpoint=protected_mcp_app, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the hardware MCP server over Streamable HTTP")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host, default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port, default: 8765")
    parser.add_argument("--path", default=DEFAULT_PATH, help="MCP path, default: /mcp")
    parser.add_argument("--public-url", help="Public MCP URL to write into connection files")
    parser.add_argument("--connection-file", default=DEFAULT_CONNECTION_FILE)
    parser.add_argument(
        "--setup-token",
        default=os.environ.get("RIGOUT_SETUP_TOKEN"),
        help="Allow this setup token to fetch /connection.json (or set RIGOUT_SETUP_TOKEN)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("RIGOUT_AUTH_TOKEN"),
        help="Bearer token required for MCP requests (or set RIGOUT_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--generate-token", action="store_true", help="Generate a bearer token and write it to the connection file"
    )
    parser.add_argument("--no-write-connection", action="store_true")
    parser.add_argument("--json-response", action="store_true", help="Use JSON responses instead of SSE streams")
    parser.add_argument("--stateless", action="store_true", help="Use stateless HTTP sessions")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mcp_path = normalize_path(args.path)
    mcp_url = args.public_url or local_url(args.host, args.port, mcp_path)
    connection_file = None if args.no_write_connection else args.connection_file
    auth_token = args.auth_token or (secrets.token_urlsafe(32) if args.generate_token else None)

    app = create_app(
        host=args.host,
        port=args.port,
        path=mcp_path,
        public_url=mcp_url,
        connection_file=connection_file,
        setup_token=args.setup_token,
        auth_token=auth_token,
        json_response=args.json_response,
        stateless=args.stateless,
    )

    print("AI Agent Hardware Access MCP server")
    print(f"MCP URL: {mcp_url}")
    print(f"Health: {health_url_from_mcp_url(mcp_url, mcp_path)}")
    if connection_file:
        print(f"Connection file: {Path(connection_file).resolve()}")
    if auth_token:
        print("Auth: bearer token written to connection file")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
