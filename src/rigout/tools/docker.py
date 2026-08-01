from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager, shell_quote
from ._results import error_result, failure_detail

# `docker logs` with no bound reads a container's entire history into memory and
# then into the caller's context. Both the line count asked of Docker and the
# size of what comes back are capped, and truncation is always stated.
DEFAULT_LOG_TAIL_LINES = 200
MAX_LOG_TAIL_LINES = 5000
MAX_OUTPUT_CHARS = 20000


def bounded_output(text: str) -> str:
    """Return command output capped to a stated size."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    kept = text[:MAX_OUTPUT_CHARS]
    dropped = len(text) - MAX_OUTPUT_CHARS
    return f"{kept}\n[output truncated: {dropped} more characters]"


def log_tail_lines(options: dict) -> int | None:
    """Resolve the `tail` option for `docker logs`.

    Returns the number of lines to request, or None if the caller explicitly
    asked for everything with `{"tail": "all"}`.
    """
    requested = options.get("tail", DEFAULT_LOG_TAIL_LINES)
    if isinstance(requested, str) and requested.strip().lower() == "all":
        return None
    if isinstance(requested, bool) or not isinstance(requested, int):
        return DEFAULT_LOG_TAIL_LINES
    if requested <= 0:
        return DEFAULT_LOG_TAIL_LINES
    return min(int(requested), MAX_LOG_TAIL_LINES)


async def handle_docker_operations(arguments: dict) -> CallToolResult:
    operation = arguments["operation"]

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    command = ""
    notes: list[str] = []
    if operation == "list":
        command = "docker ps -a"
    elif operation == "run":
        image = arguments.get("image", "")
        cmd = arguments.get("command", "")
        options = arguments.get("options", {})

        docker_cmd = "docker run"
        if options.get("detach", False):
            docker_cmd += " -d"
        if options.get("interactive", False):
            # No TTY is available for command execution, so use -i without -t
            docker_cmd += " -i"
        # Containers are removed on exit by default. That default predates this
        # release and flipping it would start leaving stopped containers on
        # every user's machine, so it stays - but it is now visible in the
        # result instead of being a silent surprise when the container is gone.
        if options.get("remove", True):
            docker_cmd += " --rm"
            notes.append(
                "Auto-remove: --rm applied, the container is deleted on exit (options.remove=false to keep it)"
            )
        else:
            notes.append("Auto-remove: disabled, the container persists after exit")

        docker_cmd += f" {shell_quote(image)}"
        if cmd:
            docker_cmd += f" {cmd}"
        command = docker_cmd

    elif operation == "exec":
        container = arguments.get("container_name", "")
        cmd = arguments.get("command", "")
        # -t requires a TTY, which command execution does not have
        command = f"docker exec {shell_quote(container)} {cmd}"

    elif operation == "stop":
        container = arguments.get("container_name", "")
        command = f"docker stop {shell_quote(container)}"

    elif operation == "remove":
        container = arguments.get("container_name", "")
        command = f"docker rm {shell_quote(container)}"

    elif operation == "build":
        image = arguments.get("image", "")
        path = arguments.get("options", {}).get("path", ".")
        command = f"docker build -t {shell_quote(image)} {shell_quote(path)}"

    elif operation == "pull":
        image = arguments.get("image", "")
        command = f"docker pull {shell_quote(image)}"

    elif operation == "logs":
        container = arguments.get("container_name", "")
        tail = log_tail_lines(arguments.get("options", {}) or {})
        if tail is None:
            command = f"docker logs {shell_quote(container)}"
            notes.append("Log scope: the entire log, as requested with options.tail='all'")
        else:
            command = f"docker logs --tail {tail} {shell_quote(container)}"
            notes.append(f"Log scope: last {tail} lines (options.tail sets this, 'all' for the whole log)")

    elif operation == "inspect":
        container = arguments.get("container_name", "")
        command = f"docker inspect {shell_quote(container)}"
    else:
        return error_result(f"Unsupported Docker operation: {operation}")

    result = await get_tunnel_manager().execute_command(endpoint, command, timeout=60, allow_sudo=True)

    if result["success"]:
        result_text = f"Docker operation '{operation}' completed successfully\n\n"
        result_text += f"Command: {result['command']}\n"
        for note in notes:
            result_text += f"{note}\n"
        result_text += f"Output:\n{bounded_output(str(result['stdout']))}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return error_result(f"Docker operation '{operation}' failed: {failure_detail(result, 'Docker command failed')}")
