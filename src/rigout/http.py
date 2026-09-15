"""Streamable HTTP transport, protected by a bearer token."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ._version import __version__
from .server import build_server
from .tools import Workspace

MCP_PATH = "/mcp"


class BearerAuth:
    """Refuse any request that does not carry the token."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        provided = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(provided, self.expected):
            response = JSONResponse(
                {
                    "error": "Missing or wrong token. Send the header Authorization: Bearer <token>. `rigout url` prints it."
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(workspace: Workspace, token: str, *, stateless: bool = False, json_response: bool = False) -> Starlette:
    server, tools = build_server(workspace)
    manager = StreamableHTTPSessionManager(app=server, stateless=stateless, json_response=json_response)

    async def mcp_endpoint(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "name": "rigout", "version": __version__})

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            try:
                yield
            finally:
                tools.jobs.stop_all()

    return Starlette(
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route(MCP_PATH, endpoint=BearerAuth(mcp_endpoint, token), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
