from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager, heredoc_redirect, shell_quote


async def handle_environment_setup(arguments: dict) -> CallToolResult:
    env_type = arguments["environment_type"]
    requirements = arguments.get("requirements", [])
    workspace_path = arguments.get("workspace_path", "/tmp/ai_workspace")

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return CallToolResult(content=[TextContent(type="text", text="No available hardware endpoints")])

    is_local = getattr(endpoint, "private_key_path", "") == "__local__"
    is_windows = "win" in (endpoint.platform or "").lower()
    quoted_workspace = shell_quote(workspace_path)
    if is_local and is_windows:
        quoted_workspace = f'"{workspace_path}"'
        commands = [f"if not exist {quoted_workspace} mkdir {quoted_workspace}", f"cd /d {quoted_workspace}"]
    else:
        commands = [f"mkdir -p {quoted_workspace}", f"cd {quoted_workspace}"]

    if env_type == "python":
        if is_local and is_windows:
            commands.append("python -m venv venv")
        else:
            commands.extend(
                [
                    "python3 -m venv venv",
                    ". venv/bin/activate",
                ]
            )
        if requirements:
            for req in requirements:
                if is_local and is_windows:
                    commands.append(f'venv\\Scripts\\python.exe -m pip install "{req}"')
                else:
                    commands.append(f"pip install {shell_quote(req)}")

    elif env_type == "node":
        commands.extend(
            [
                "npm init -y",
            ]
        )
        if requirements:
            for req in requirements:
                commands.append(f"npm install {shell_quote(req)}")

    elif env_type == "docker":
        if requirements:
            dockerfile_content = f"FROM {requirements[0] if requirements else 'ubuntu:latest'}\n"
            if len(requirements) > 1:
                dockerfile_content += f"RUN {' && '.join(requirements[1:])}\n"
            if is_local and is_windows:
                escaped = dockerfile_content.replace('"', '\\"')
                commands.append(f'powershell -NoProfile -Command "Set-Content -Path Dockerfile -Value \\"{escaped}\\""')
            else:
                commands.append(heredoc_redirect(dockerfile_content, "Dockerfile"))

    elif env_type == "conda":
        commands.extend(
            [
                "wget -O miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh",
                "bash miniconda.sh -b -p ./miniconda",
                ". ./miniconda/bin/activate",
            ]
        )
        if requirements:
            for req in requirements:
                commands.append(f"conda install -y {shell_quote(req)}")

    full_command = " && ".join(commands)
    result = await get_tunnel_manager().execute_command(endpoint, full_command, timeout=300, allow_sudo=True)

    if result["success"]:
        result_text = f"Environment setup '{env_type}' completed successfully\n\n"
        result_text += f"Workspace: {workspace_path}\n"
        result_text += f"Requirements installed: {', '.join(requirements)}\n"
        result_text += f"\nSetup output:\n{result['stdout']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Environment setup '{env_type}' failed: {result.get('error', 'Unknown error')}",
                )
            ]
        )
