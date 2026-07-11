from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager, shell_quote
from ._results import error_result, failure_detail


async def handle_docker_operations(arguments: dict) -> CallToolResult:
    operation = arguments["operation"]

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    command = ""
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
        if options.get("remove", True):
            docker_cmd += " --rm"

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
        command = f"docker logs {shell_quote(container)}"

    elif operation == "inspect":
        container = arguments.get("container_name", "")
        command = f"docker inspect {shell_quote(container)}"
    else:
        return error_result(f"Unsupported Docker operation: {operation}")

    result = await get_tunnel_manager().execute_command(endpoint, command, timeout=60, allow_sudo=True)

    if result["success"]:
        result_text = f"Docker operation '{operation}' completed successfully\n\n"
        result_text += f"Command: {result['command']}\n"
        result_text += f"Output:\n{result['stdout']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return error_result(f"Docker operation '{operation}' failed: {failure_detail(result, 'Docker command failed')}")
