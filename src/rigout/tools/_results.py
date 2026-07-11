from collections.abc import Mapping
from typing import Any

from mcp.types import CallToolResult, TextContent


def error_result(message: str) -> CallToolResult:
    """Return a tool result that MCP clients can reliably identify as failed."""
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


def failure_detail(result: Mapping[str, Any], fallback: str = "Operation failed") -> str:
    """Build a non-empty diagnostic from a failed command result."""
    error = str(result.get("error") or "").strip()
    stderr = str(result.get("stderr") or "").strip()

    if error and stderr and stderr != error:
        return f"{error}\nStderr: {stderr}"
    if error:
        return error
    if stderr:
        return stderr

    exit_code = result.get("exit_code")
    if exit_code is not None:
        return f"Command exited with status {exit_code}"
    return fallback
