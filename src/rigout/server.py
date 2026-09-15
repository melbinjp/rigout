"""The rigout MCP server: the eight tools, with instructions for the agent."""

from __future__ import annotations

from typing import Any

from mcp.server import Server
from mcp.types import CallToolResult, Tool

from ._version import __version__
from .tools import Tools, Workspace, instructions, tool_definitions


def build_server(workspace: Workspace | None = None) -> tuple[Server, Tools]:
    workspace = workspace or Workspace()
    tools = Tools(workspace)
    server: Server = Server("rigout", version=__version__, instructions=instructions(workspace))
    definitions = tool_definitions()

    async def list_tools() -> list[Tool]:
        return definitions

    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        return await tools.call(name, arguments)

    if hasattr(server, "list_tools"):
        # mcp 1.x registers handlers with decorators. Input is checked by the tools themselves,
        # so a mistake comes back with a next step rather than a schema error.
        server.list_tools()(list_tools)  # type: ignore[attr-defined]
        try:
            decorator = server.call_tool(validate_input=False)  # type: ignore[attr-defined]
        except TypeError:
            decorator = server.call_tool()  # type: ignore[attr-defined]
        decorator(call_tool)
        return server, tools

    # mcp 2.x removed the decorators for explicit registration.
    from mcp.types import CallToolRequestParams, ListToolsResult, PaginatedRequestParams

    async def list_request(_context: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=definitions)

    async def call_request(_context: Any, params: Any) -> CallToolResult:
        return await tools.call(params.name, params.arguments)

    server.add_request_handler("tools/list", PaginatedRequestParams, list_request)  # type: ignore[attr-defined]
    server.add_request_handler("tools/call", CallToolRequestParams, call_request)  # type: ignore[attr-defined]
    return server, tools


async def serve_stdio(workspace: Workspace) -> None:
    from mcp.server.stdio import stdio_server

    server, tools = build_server(workspace)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        tools.jobs.stop_all()
