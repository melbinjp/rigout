import asyncio
import logging
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ContentBlock,
    TextContent,
    Tool,
    ToolAnnotations,
)

from ._version import __version__

# Import all decomposed tool handlers
from .tools import (
    handle_bulk_file_transfer,
    handle_close_terminal_session,
    handle_connect_hardware,
    handle_create_terminal_session,
    handle_docker_operations,
    handle_environment_setup,
    handle_execute_command,
    handle_execute_in_terminal,
    handle_file_operations,
    handle_get_hardware_info,
    handle_get_server_activity,
    handle_install_software,
    handle_list_terminal_sessions,
    handle_manage_tunnels,
    handle_system_monitoring,
)
from .tools._results import build_result, result_is_error, transport_safe_result

logger = logging.getLogger(__name__)

# The managed launcher captures stderr in its owner-only activity log. Keep the
# stdio fallback on stderr as well so importing this module never creates a log
# file in an arbitrary current working directory.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

server = Server("enhanced-hardware-server", version=__version__)

# --- constructing mcp types on either major ---------------------------------------------
#
# 2.x renamed these fields to snake_case and kept the camelCase spellings as construction
# aliases, so every call below runs unchanged on both. The type checker does not see an
# alias, only the field, and it only ever sees the major that happens to be installed - so
# without these the fifteen tool definitions are fifteen errors on 2.x and none on 1.x.
# Same reasoning as the handler registration at the bottom of this file: keep the fork in
# one named place, and keep everything around it checked.


def _rename_for_installed_mcp(
    kwargs: dict[str, Any], pairs: dict[str, str], fields: Mapping[str, Any]
) -> dict[str, Any]:
    """Respell camelCase keys as snake_case when the model no longer declares them."""
    for camel, snake in pairs.items():
        if camel in kwargs and camel not in fields:
            kwargs[snake] = kwargs.pop(camel)
    return kwargs


def _tool(**kwargs: Any) -> Tool:
    """Build a Tool. Callers spell the schema `inputSchema`, as both majors accept."""
    return Tool(**_rename_for_installed_mcp(kwargs, {"inputSchema": "input_schema"}, Tool.model_fields))


def _tool_annotations(**kwargs: Any) -> ToolAnnotations:
    """Build ToolAnnotations. Callers spell the hints in camelCase."""
    return ToolAnnotations(
        **_rename_for_installed_mcp(
            kwargs,
            {
                "readOnlyHint": "read_only_hint",
                "destructiveHint": "destructive_hint",
                "idempotentHint": "idempotent_hint",
                "openWorldHint": "open_world_hint",
            },
            ToolAnnotations.model_fields,
        )
    )


# What each tool does to the machine, in the terms MCP defines, so a client can tell a
# question apart from an action before it runs one. Rigout spans the whole range - a tool
# that reads a CPU count and a tool that runs arbitrary commands as root are both here -
# and until now it advertised them identically, leaving every client to guess.
#
# The fields mean what the specification says they mean, and the honest reading is the
# conservative one:
#   read_only     the tool does not change the machine at all
#   destructive   it may overwrite or remove something, not merely add
#   idempotent    calling it again with the same arguments changes nothing further
#   open_world    it reaches something outside this machine: a remote host, a registry
#
# Anything that can run a caller's command is destructive and not idempotent, because
# what it does is decided by the caller and cannot be known here.
TOOL_ANNOTATIONS: dict[str, tuple[str, bool, bool, bool, bool]] = {
    # name: (title, read_only, destructive, idempotent, open_world)
    "connect_hardware": ("Connect to hardware", False, False, True, True),
    "execute_command": ("Run a command", False, True, False, True),
    "create_terminal_session": ("Open a terminal session", False, False, False, True),
    "execute_in_terminal": ("Run a command in a session", False, True, False, True),
    "list_terminal_sessions": ("List terminal sessions", True, False, True, False),
    "close_terminal_session": ("Close a terminal session", False, True, True, True),
    "get_hardware_info": ("Read hardware information", True, False, True, True),
    "get_server_activity": ("Read Rigout's activity", True, False, True, False),
    "manage_tunnels": ("Manage SSH endpoints", False, True, False, True),
    "install_software": ("Install packages", False, True, False, True),
    "file_operations": ("Read and change files", False, True, False, True),
    "system_monitoring": ("Read system metrics", True, False, True, True),
    "docker_operations": ("Manage Docker", False, True, False, True),
    "bulk_file_transfer": ("Transfer files", False, True, False, True),
    "environment_setup": ("Prepare a development environment", False, True, False, True),
}


def annotate(tools: list[Tool]) -> list[Tool]:
    """Attach the declared annotations and title to each tool.

    Applied here rather than written into each definition so that the classification is
    one table somebody can read and argue with, instead of fifteen scattered arguments
    where a missing one is invisible. A tool with no entry keeps none, and the metadata
    test fails, which is what stops a new tool shipping unclassified.
    """
    for tool in tools:
        entry = TOOL_ANNOTATIONS.get(tool.name)
        if entry is None:
            continue
        title, read_only, destructive, idempotent, open_world = entry
        tool.title = title
        tool.annotations = _tool_annotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=open_world,
        )
    return tools


async def handle_list_tools() -> list[Tool]:
    """List available tools for AI agents"""
    return annotate(
        [
            _tool(
                name="connect_hardware",
                description="Connect to remote hardware with automatic failover",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "preferred_platform": {
                            "type": "string",
                            "description": "Preferred platform (windows, linux, docker)",
                            "enum": ["windows", "linux", "docker", "any"],
                        }
                    },
                },
            ),
            _tool(
                name="execute_command",
                description="Execute command on remote hardware with full system access",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command to execute (full sudo access available)"},
                        "timeout": {"type": "integer", "description": "Command timeout in seconds", "default": 30},
                        "use_sudo": {
                            "type": "boolean",
                            "description": "Whether to use sudo for elevated privileges",
                            "default": False,
                        },
                        "working_directory": {
                            "type": "string",
                            "description": "Working directory for command execution",
                            "default": "~",
                        },
                        "environment": {
                            "type": "object",
                            "description": "Environment variables for command",
                            "default": {},
                        },
                        "bypass_security": {
                            "type": "boolean",
                            "description": "Bypass security validation for advanced AI agent operations",
                            "default": False,
                        },
                    },
                    "required": ["command"],
                },
            ),
            _tool(
                name="create_terminal_session",
                description="Create a persistent interactive terminal session",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string", "description": "Optional name for the terminal session"}
                    },
                },
            ),
            _tool(
                name="execute_in_terminal",
                description="Execute command in existing terminal session (maintains state)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Terminal session ID"},
                        "command": {"type": "string", "description": "Command to execute in session"},
                        "timeout": {"type": "integer", "description": "Command timeout in seconds", "default": 30},
                        "use_sudo": {
                            "type": "boolean",
                            "description": "Whether to use sudo for elevated privileges",
                            "default": False,
                        },
                        "bypass_security": {
                            "type": "boolean",
                            "description": "Bypass security validation for advanced AI agent operations",
                            "default": False,
                        },
                    },
                    "required": ["session_id", "command"],
                },
            ),
            _tool(
                name="list_terminal_sessions",
                description="List all active terminal sessions",
                inputSchema={"type": "object", "properties": {}},
            ),
            _tool(
                name="close_terminal_session",
                description="Close a terminal session",
                inputSchema={
                    "type": "object",
                    "properties": {"session_id": {"type": "string", "description": "Terminal session ID to close"}},
                    "required": ["session_id"],
                },
            ),
            _tool(
                name="get_hardware_info",
                description="Get detailed hardware information from remote system",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "refresh": {
                            "type": "boolean",
                            "description": "Force refresh hardware information",
                            "default": False,
                        }
                    },
                },
            ),
            _tool(
                name="get_server_activity",
                description="Read bounded, sanitized Rigout lifecycle status and recent activity",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "lines": {
                            "type": "integer",
                            "description": "Number of recent activity lines to return",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 200,
                        }
                    },
                },
            ),
            _tool(
                name="manage_tunnels",
                description="Manage tunnel endpoints (add, remove, test, failover)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Action to perform",
                            "enum": ["add", "remove", "test", "list", "failover"],
                        },
                        "hostname": {"type": "string", "description": "Hostname for add/remove actions"},
                        "username": {"type": "string", "description": "Username for SSH connection"},
                        "private_key_path": {"type": "string", "description": "Path to SSH private key"},
                        "port": {
                            "type": "integer",
                            "description": "SSH port for the add action (default 22)",
                            "minimum": 1,
                            "maximum": 65535,
                            "default": 22,
                        },
                        "platform": {
                            "type": "string",
                            "description": "Platform type",
                            "enum": ["windows", "linux", "docker", "macos"],
                        },
                    },
                    "required": ["action"],
                },
            ),
            _tool(
                name="install_software",
                description="Install software packages on remote hardware",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "packages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of packages to install",
                        },
                        "package_manager": {
                            "type": "string",
                            "description": "Package manager to use",
                            "enum": ["apt", "yum", "dnf", "pacman", "brew", "choco", "pip", "npm", "auto"],
                            "default": "auto",
                        },
                    },
                    "required": ["packages"],
                },
            ),
            _tool(
                name="file_operations",
                description="Perform file operations on remote hardware",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "description": "File operation to perform",
                            "enum": ["read", "write", "append", "delete", "copy", "move", "chmod", "chown"],
                        },
                        "path": {"type": "string", "description": "File or directory path"},
                        "content": {"type": "string", "description": "Content for write/append operations"},
                        "destination": {"type": "string", "description": "Destination path for copy/move operations"},
                        "permissions": {
                            "type": "string",
                            "description": "Permissions for chmod operation (e.g., '755')",
                        },
                        "owner": {"type": "string", "description": "Owner for chown operation (e.g., 'user:group')"},
                        "recursive": {
                            "type": "boolean",
                            "description": "Required to delete a directory and everything inside it",
                            "default": False,
                        },
                    },
                    "required": ["operation", "path"],
                },
            ),
            _tool(
                name="system_monitoring",
                description="Monitor system resources and performance",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "metrics": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["cpu", "memory", "disk", "network", "gpu", "processes", "all"],
                            },
                            "description": "Metrics to monitor",
                            "default": ["all"],
                        },
                        "duration": {"type": "integer", "description": "Monitoring duration in seconds", "default": 10},
                    },
                },
            ),
            _tool(
                name="docker_operations",
                description="Manage Docker containers and images for AI agent workflows",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "description": "Docker operation to perform",
                            "enum": ["list", "run", "exec", "stop", "remove", "build", "pull", "logs", "inspect"],
                        },
                        "container_name": {"type": "string", "description": "Container name or ID"},
                        "image": {"type": "string", "description": "Docker image name"},
                        "command": {"type": "string", "description": "Command to run in container"},
                        "options": {"type": "object", "description": "Additional Docker options", "default": {}},
                    },
                    "required": ["operation"],
                },
            ),
            _tool(
                name="bulk_file_transfer",
                description="Transfer multiple files or directories for AI agent workflows",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "description": "Transfer operation",
                            "enum": ["upload", "download", "sync"],
                        },
                        "source": {"type": "string", "description": "Source path or content"},
                        "destination": {"type": "string", "description": "Destination path"},
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of files to transfer",
                        },
                        "compress": {
                            "type": "boolean",
                            "description": "Compress files during transfer",
                            "default": True,
                        },
                    },
                    "required": ["operation", "source", "destination"],
                },
            ),
            _tool(
                name="environment_setup",
                description="Set up development environments for AI agent projects",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "environment_type": {
                            "type": "string",
                            "description": "Type of environment to set up",
                            "enum": ["python", "node", "docker", "conda", "custom"],
                        },
                        "requirements": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of requirements or dependencies",
                        },
                        "workspace_path": {
                            "type": "string",
                            "description": "Path to set up the workspace",
                            "default": "/tmp/ai_workspace",
                        },
                        "configuration": {
                            "type": "object",
                            "description": "Additional configuration options",
                            "default": {},
                        },
                    },
                    "required": ["environment_type"],
                },
            ),
        ]
    )


async def _handle_call_tool_result(name: str, arguments: dict) -> CallToolResult:
    """Build a CallToolResult for direct tests and wrapper transports."""
    return transport_safe_result(await _dispatch_tool(name, arguments))


async def _dispatch_tool(name: str, arguments: dict) -> CallToolResult:
    """Route one tool call to its handler, converting any escape into a result."""
    try:
        if name == "connect_hardware":
            return await handle_connect_hardware(arguments)
        elif name == "execute_command":
            return await handle_execute_command(arguments)
        elif name == "create_terminal_session":
            return await handle_create_terminal_session(arguments)
        elif name == "execute_in_terminal":
            return await handle_execute_in_terminal(arguments)
        elif name == "list_terminal_sessions":
            return await handle_list_terminal_sessions(arguments)
        elif name == "close_terminal_session":
            return await handle_close_terminal_session(arguments)
        elif name == "get_hardware_info":
            return await handle_get_hardware_info(arguments)
        elif name == "get_server_activity":
            return await handle_get_server_activity(arguments)
        elif name == "manage_tunnels":
            return await handle_manage_tunnels(arguments)
        elif name == "install_software":
            return await handle_install_software(arguments)
        elif name == "file_operations":
            return await handle_file_operations(arguments)
        elif name == "system_monitoring":
            return await handle_system_monitoring(arguments)
        elif name == "docker_operations":
            return await handle_docker_operations(arguments)
        elif name == "bulk_file_transfer":
            return await handle_bulk_file_transfer(arguments)
        elif name == "environment_setup":
            return await handle_environment_setup(arguments)
        else:
            return build_result(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
    except Exception as e:
        return build_result(
            content=[TextContent(type="text", text=f"Error executing tool '{name}': {str(e)}")],
            isError=True,
        )


def _error_message(name: str, content: Sequence[ContentBlock]) -> str:
    """Flatten error content into the single string the MCP error channel allows.

    An error cannot travel out of here as a result: the SDK's call_tool handler
    hardcodes ``isError=False`` on its success path and only sets ``isError=True``
    by catching an exception, which it renders as one TextContent. Non-text blocks
    therefore cannot survive as blocks. Describe them instead of filtering them out,
    so an image or embedded resource on an error path is visible in the message
    rather than disappearing without a trace.
    """
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        kind = getattr(item, "type", None) or type(item).__name__
        uri = getattr(item, "uri", None) or getattr(getattr(item, "resource", None), "uri", None)
        parts.append(f"[{kind} content: {uri}]" if uri else f"[{kind} content]")
    return "\n".join(parts) or f"Tool '{name}' failed"


async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls from MCP clients."""
    result = await _handle_call_tool_result(name, arguments)
    if result_is_error(result):
        raise RuntimeError(_error_message(name, result.content))
    return result.content  # type: ignore


# mcp 1.x registers these with decorators; 2.x removed them for explicit registration.
# That is the entire incompatibility between the two majors - every tool definition above
# constructs unchanged, because 2.x renamed Tool's fields but kept the camelCase
# spellings as aliases, and stdio, streamable_http_manager, models and types all survive.
#
# Supporting both is deliberate rather than transitional. The fork is four lines wide and
# lets the dependency bound span both majors, so nobody is pushed onto a major the week it
# appears and nobody is stranded on the old one either. Which of the two is in use is
# decided by what is installed, not by a setting, so there is nothing to configure wrong.
def register_tool_handlers() -> None:
    """Register the tool handlers against whichever mcp major is installed."""
    if hasattr(server, "list_tools"):
        server.list_tools()(handle_list_tools)  # type: ignore[attr-defined]
        server.call_tool()(handle_call_tool)  # type: ignore[attr-defined]
        return

    from mcp.types import CallToolRequestParams, ListToolsResult, PaginatedRequestParams

    async def list_tools_request(_context: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=await handle_list_tools())

    async def call_tool_request(_context: Any, params: Any) -> CallToolResult:
        # Returned rather than raised. 1.x needs an error re-raised as RuntimeError for the
        # SDK to rebuild it; 2.x takes the result as it is, so isError survives directly.
        return await _handle_call_tool_result(params.name, params.arguments or {})

    # Guarded by the hasattr above: this branch only runs on the major that has these,
    # and the type checker sees whichever mcp is installed, so one of the two branches is
    # always unknown to it. Ignoring here rather than loosening the annotation keeps the
    # rest of the file checked.
    server.add_request_handler("tools/list", PaginatedRequestParams, list_tools_request)  # type: ignore[attr-defined]
    server.add_request_handler("tools/call", CallToolRequestParams, call_tool_request)  # type: ignore[attr-defined]


register_tool_handlers()


async def handle_call_tool_result(name: str, arguments: dict) -> CallToolResult:
    """Compatibility helper for tests that need the full CallToolResult object."""
    return await _handle_call_tool_result(name, arguments)


async def main():
    """Main entry point for the MCP server"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="enhanced-hardware-server",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def stdio_main() -> int:
    """Console-script entry point for the stdio MCP transport."""
    logger.info("Starting Rigout stdio MCP server...")
    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(stdio_main())
