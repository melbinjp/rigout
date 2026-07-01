import asyncio
import logging
import sys

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

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
    handle_install_software,
    handle_list_terminal_sessions,
    handle_manage_tunnels,
    handle_system_monitoring,
)

logger = logging.getLogger(__name__)

# Configure logging fallback if not done by parent process
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler("mcp-hardware-server.log"), logging.StreamHandler(sys.stdout)],
    )

server = Server("enhanced-hardware-server")


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """List available tools for AI agents"""
    return [
        Tool(
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
        Tool(
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
        Tool(
            name="create_terminal_session",
            description="Create a persistent interactive terminal session",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_name": {"type": "string", "description": "Optional name for the terminal session"}
                },
            },
        ),
        Tool(
            name="execute_in_terminal",
            description="Execute command in existing terminal session (maintains state)",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Terminal session ID"},
                    "command": {"type": "string", "description": "Command to execute in session"},
                    "timeout": {"type": "integer", "description": "Command timeout in seconds", "default": 30},
                },
                "required": ["session_id", "command"],
            },
        ),
        Tool(
            name="list_terminal_sessions",
            description="List all active terminal sessions",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="close_terminal_session",
            description="Close a terminal session",
            inputSchema={
                "type": "object",
                "properties": {"session_id": {"type": "string", "description": "Terminal session ID to close"}},
                "required": ["session_id"],
            },
        ),
        Tool(
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
        Tool(
            name="manage_tunnels",
            description="Manage tunnel endpoints (add, remove, test, failover)",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to perform",
                        "enum": ["add", "remove", "test", "list", "failover", "rotate"],
                    },
                    "hostname": {"type": "string", "description": "Hostname for add/remove actions"},
                    "username": {"type": "string", "description": "Username for SSH connection"},
                    "private_key_path": {"type": "string", "description": "Path to SSH private key"},
                    "platform": {
                        "type": "string",
                        "description": "Platform type",
                        "enum": ["windows", "linux", "docker", "macos"],
                    },
                },
                "required": ["action"],
            },
        ),
        Tool(
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
        Tool(
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
                    "permissions": {"type": "string", "description": "Permissions for chmod operation (e.g., '755')"},
                    "owner": {"type": "string", "description": "Owner for chown operation (e.g., 'user:group')"},
                },
                "required": ["operation", "path"],
            },
        ),
        Tool(
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
        Tool(
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
        Tool(
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
                    "files": {"type": "array", "items": {"type": "string"}, "description": "List of files to transfer"},
                    "compress": {"type": "boolean", "description": "Compress files during transfer", "default": True},
                },
                "required": ["operation", "source", "destination"],
            },
        ),
        Tool(
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


async def _handle_call_tool_result(name: str, arguments: dict) -> CallToolResult:
    """Build a CallToolResult for direct tests and wrapper transports."""
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
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")])
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=f"Error executing tool '{name}': {str(e)}")])


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls from MCP clients."""
    result = await _handle_call_tool_result(name, arguments)
    return result.content  # type: ignore


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
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=None,
                    experimental_capabilities=None,
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
