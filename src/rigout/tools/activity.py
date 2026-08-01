"""Read-only MCP access to Rigout's managed lifecycle activity."""

import json
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from ..lifecycle import RuntimePaths, read_tail, redact_home_path, redact_sensitive_text, runtime_status
from ..security_validator import security_validator
from ._results import error_result

DEFAULT_ACTIVITY_LINES = 50
MAX_ACTIVITY_LINES = 200


def sanitized_path(path: Path) -> str:
    """Return a runtime path with the operator's account name and credentials removed.

    A local operator can still resolve a `~`-relative path, while a remote agent no longer
    learns the home directory, which carries the operating system user name.
    """
    sanitized = redact_sensitive_text(redact_home_path(str(path)))
    return security_validator.sanitize_command_output(sanitized)


async def handle_get_server_activity(arguments: dict) -> CallToolResult:
    """Return bounded, sanitized lifecycle state and recent activity as JSON."""
    line_count = arguments.get("lines", DEFAULT_ACTIVITY_LINES)
    if isinstance(line_count, bool) or not isinstance(line_count, int):
        return error_result("The lines argument must be an integer")
    if not 1 <= line_count <= MAX_ACTIVITY_LINES:
        return error_result(f"The lines argument must be between 1 and {MAX_ACTIVITY_LINES}")

    paths = RuntimePaths.resolve()
    status = runtime_status(paths)
    safe_lines = []
    for line in read_tail(paths.log_file, line_count):
        sanitized = redact_sensitive_text(line)
        sanitized = security_validator.sanitize_command_output(sanitized)
        safe_lines.append(sanitized)

    pid = status.get("pid")
    payload = {
        "status": str(status.get("status", "stopped")),
        "running": bool(status.get("running", False)),
        "pid": pid if isinstance(pid, int) else None,
        "state_dir": sanitized_path(paths.root),
        "activity_log": sanitized_path(paths.log_file),
        "lines": safe_lines,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
    )
