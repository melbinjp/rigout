from mcp.types import CallToolResult, TextContent

from ..ssh_manager import (
    get_tunnel_manager,
    shell_join,
)
from ._results import error_result, failure_detail


async def handle_execute_command(arguments: dict) -> CallToolResult:
    command = arguments["command"]
    timeout = arguments.get("timeout", 30)
    use_sudo = arguments.get("use_sudo", False)
    working_directory = arguments.get("working_directory", "~")
    environment = arguments.get("environment", {})
    bypass_security = arguments.get("bypass_security", False)

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    if use_sudo and not command.startswith("sudo"):
        command = f"sudo {command}"

    result = await get_tunnel_manager().execute_command(
        endpoint,
        command,
        timeout,
        allow_sudo=use_sudo,
        bypass_security=bypass_security,
        working_directory=working_directory,
        environment=environment,
    )

    if result["success"]:
        result_text = f"Command executed successfully on {result['endpoint']}\n\n"
        result_text += f"Command: {result['command']}\n"
        result_text += f"Exit Code: {result['exit_code']}\n\n"
        result_text += f"Output:\n{result['stdout']}"
        if result["stderr"]:
            result_text += f"\n\nErrors:\n{result['stderr']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        result_text = f"Command failed on {result['endpoint']}\n\n"
        result_text += f"Command: {result['command']}\n"
        result_text += f"Exit Code: {result.get('exit_code', 'N/A')}\n"
        result_text += f"Error: {failure_detail(result, 'Command execution failed')}"
        return error_result(result_text)


async def handle_create_terminal_session(arguments: dict) -> CallToolResult:
    session_name = arguments.get("session_name")

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    session = await get_tunnel_manager().create_terminal_session(endpoint, session_name)

    if session:
        result_text = "Terminal session created successfully\n\n"
        result_text += f"Session ID: {session.session_id}\n"
        result_text += f"Endpoint: {session.endpoint.hostname}\n"
        result_text += f"Created: {session.created.isoformat()}\n\n"
        result_text += "You can now execute commands in this persistent session using execute_in_terminal."
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return error_result("Failed to create terminal session")


async def handle_execute_in_terminal(arguments: dict) -> CallToolResult:
    session_id = arguments["session_id"]
    command = arguments["command"]
    timeout = arguments.get("timeout", 30)

    result = await get_tunnel_manager().execute_in_session(session_id, command, timeout)

    if result["success"]:
        result_text = f"Command executed in session {session_id}\n\n"
        result_text += f"Command: {result['command']}\n\n"
        result_text += f"Output:\n{result['output']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return error_result(
            f"Command failed in session {session_id}: {failure_detail(result, 'Terminal command failed')}"
        )


async def handle_list_terminal_sessions(arguments: dict) -> CallToolResult:
    if not get_tunnel_manager().terminal_sessions:
        return CallToolResult(content=[TextContent(type="text", text="No active terminal sessions")])

    result_text = "Active Terminal Sessions:\n\n"
    for session_id, session in get_tunnel_manager().terminal_sessions.items():
        result_text += f"Session ID: {session_id}\n"
        result_text += f"Endpoint: {session.endpoint.hostname}\n"
        result_text += f"Created: {session.created.isoformat()}\n"
        result_text += f"Last Activity: {session.last_activity.isoformat()}\n"
        result_text += f"Interactive: {session.is_interactive}\n\n"
    return CallToolResult(content=[TextContent(type="text", text=result_text)])


async def handle_close_terminal_session(arguments: dict) -> CallToolResult:
    session_id = arguments["session_id"]

    if get_tunnel_manager().close_terminal_session(session_id):
        return CallToolResult(
            content=[TextContent(type="text", text=f"Terminal session {session_id} closed successfully")]
        )
    else:
        return error_result(f"Failed to close terminal session {session_id} (session not found)")


async def handle_install_software(arguments: dict) -> CallToolResult:
    packages = arguments["packages"]
    package_manager = arguments.get("package_manager", "auto")

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    if package_manager == "auto":
        endpoint_platform = endpoint.platform.lower()
        if "win" in endpoint_platform:
            package_manager = "choco"
        elif "darwin" in endpoint_platform or "mac" in endpoint_platform:
            package_manager = "brew"
        elif "ubuntu" in endpoint_platform or "debian" in endpoint_platform or "linux" in endpoint_platform:
            package_manager = "apt"
        elif "centos" in endpoint_platform or "rhel" in endpoint_platform:
            package_manager = "yum"
        else:
            package_manager = "apt"

    quoted_packages = shell_join(packages)
    if package_manager == "apt":
        command = f"sudo apt update && sudo apt install -y {quoted_packages}"
    elif package_manager == "yum":
        command = f"sudo yum install -y {quoted_packages}"
    elif package_manager == "dnf":
        command = f"sudo dnf install -y {quoted_packages}"
    elif package_manager == "pip":
        command = f"pip install {quoted_packages}"
    elif package_manager == "npm":
        command = f"npm install -g {quoted_packages}"
    elif package_manager == "brew":
        command = f"brew install {quoted_packages}"
    elif package_manager == "choco":
        command = f"choco install -y {quoted_packages}"
    else:
        return error_result(f"Unsupported package manager: {package_manager}")

    result = await get_tunnel_manager().execute_command(endpoint, command, timeout=300, allow_sudo=True)

    if result["success"]:
        result_text = "Software installation completed successfully\n\n"
        result_text += f"Packages: {', '.join(packages)}\n"
        result_text += f"Package Manager: {package_manager}\n"
        result_text += f"Endpoint: {result['endpoint']}\n\n"
        result_text += f"Output:\n{result['stdout']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        result_text = "Software installation failed\n\n"
        result_text += f"Packages: {', '.join(packages)}\n"
        result_text += f"Error: {failure_detail(result, 'Software installation failed')}"
        return error_result(result_text)
