import tempfile
from pathlib import Path, PureWindowsPath

from mcp.types import CallToolResult, TextContent

from ..ssh_manager import build_write_command, get_tunnel_manager, shell_quote
from ._platform import is_windows_platform
from ._results import error_result, failure_detail

DEFAULT_WORKSPACE_PATH = "/tmp/ai_workspace"

# The documented default is a POSIX path, so on Windows it has to be mapped to
# somewhere that exists rather than handed to cmd.exe as `mkdir "/tmp/..."`,
# which fails with "The syntax of the command is incorrect."
_POSIX_TEMP_PREFIXES = ("/tmp/", "/var/tmp/")

# cmd.exe has no shlex.quote. Values reach it inside double quotes, which is
# enough for pip specifiers such as "numpy>=1.2", but a value containing a
# double quote would close that quoting and a %NAME% would be expanded, so
# those characters are refused instead of being smuggled into the command.
_WINDOWS_UNSAFE_CHARACTERS = ('"', "%", "\r", "\n")


def reject_windows_unsafe(value: str, label: str) -> None:
    """Raise ValueError if a value cannot be safely quoted for cmd.exe."""
    for character in _WINDOWS_UNSAFE_CHARACTERS:
        if character in value:
            raise ValueError(f"{label} contains a character that cannot be quoted for cmd.exe: {character!r}")


def windows_workspace_path(workspace_path: str) -> str:
    """Translate a workspace path into one cmd.exe can actually use.

    A Windows path is passed through with separators normalised. `/tmp/...`
    maps onto the machine's temp directory, which is what the documented
    default means on a Windows host. Any other POSIX absolute path is rejected
    with an explanation, because guessing a drive for it would silently put a
    user's environment somewhere they did not ask for.
    """
    candidate = workspace_path.strip()
    if not candidate:
        raise ValueError("workspace_path must not be empty")
    reject_windows_unsafe(candidate, "workspace_path")

    normalized = candidate.replace("/", "\\")
    pure = PureWindowsPath(normalized)
    if pure.drive or normalized.startswith("\\\\"):
        return str(pure)

    lowered = candidate.rstrip("/").lower() + "/"
    for prefix in _POSIX_TEMP_PREFIXES:
        if lowered.startswith(prefix):
            remainder = candidate[len(prefix) :].strip("/").replace("/", "\\")
            return str(PureWindowsPath(tempfile.gettempdir()) / remainder) if remainder else tempfile.gettempdir()

    if candidate.startswith("/"):
        raise ValueError(
            f"workspace_path '{workspace_path}' is a POSIX absolute path and this endpoint is Windows. "
            "Pass a Windows path such as C:\\Users\\you\\ai_workspace, "
            f"or omit workspace_path to use {PureWindowsPath(tempfile.gettempdir()) / 'ai_workspace'}."
        )
    return str(pure)


def dockerfile_body(requirements: list) -> str:
    """Build Dockerfile content from the requirements list.

    The first requirement is the base image; any others become a single RUN.
    """
    base_image = str(requirements[0]) if requirements else "ubuntu:latest"
    content = f"FROM {base_image}\n"
    if len(requirements) > 1:
        content += f"RUN {' && '.join(str(requirement) for requirement in requirements[1:])}\n"
    return content


async def handle_environment_setup(arguments: dict) -> CallToolResult:
    env_type = arguments["environment_type"]
    requirements = arguments.get("requirements") or []
    workspace_path = arguments.get("workspace_path", DEFAULT_WORKSPACE_PATH)

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    if env_type not in {"python", "node", "docker", "conda", "custom"}:
        return error_result(f"Unsupported environment type: {env_type}")

    is_local = getattr(endpoint, "private_key_path", "") == "__local__"
    windows_shell = is_local and is_windows_platform(endpoint.platform)

    if windows_shell:
        try:
            workspace_path = windows_workspace_path(str(workspace_path))
            for requirement in requirements:
                reject_windows_unsafe(str(requirement), f"requirement {requirement!r}")
        except ValueError as exc:
            return error_result(f"Environment setup '{env_type}' failed: {exc}")

    # Creating the workspace from Python rather than from the shell is what
    # makes the Windows sequence honest. The old command was
    # `if not exist "WS" mkdir "WS" && cd /d "WS" && ...`, and cmd.exe binds the
    # whole `&&` chain to the body of the `if`: when the directory already
    # existed, cmd skipped every step and exited 0, so the handler reported a
    # successful setup that had done nothing at all.
    local_workspace: Path | None = None
    if is_local:
        # Resolve here so `~` reaches the shell already expanded. Quoting it
        # unexpanded would make `cd '~/work'` look for a directory literally
        # named "~", and reporting it unresolved would misstate where the
        # environment was built.
        local_workspace = Path(workspace_path).expanduser()
        try:
            local_workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return error_result(f"Environment setup '{env_type}' failed: cannot create workspace: {exc}")
        workspace_path = str(local_workspace)
        commands = [f'cd /d "{workspace_path}"'] if windows_shell else [f"cd {shell_quote(workspace_path)}"]
    else:
        commands = [f"mkdir -p {shell_quote(workspace_path)}", f"cd {shell_quote(workspace_path)}"]

    notes: list[str] = []

    if env_type == "python":
        if windows_shell:
            commands.append("python -m venv venv")
            for requirement in requirements:
                commands.append(f'venv\\Scripts\\python.exe -m pip install "{requirement}"')
        else:
            commands.extend(["python3 -m venv venv", ". venv/bin/activate"])
            for requirement in requirements:
                commands.append(f"pip install {shell_quote(requirement)}")

    elif env_type == "node":
        commands.append("npm init -y")
        for requirement in requirements:
            commands.append(f"npm install {shell_quote(requirement)}")

    elif env_type == "docker":
        content = dockerfile_body(requirements)
        if local_workspace is not None:
            # Writing the file from Python avoids quoting it into a shell. The
            # previous Windows path embedded the literal newlines of the
            # Dockerfile inside a `powershell -Command "..."` argument, and
            # PowerShell answered "The string is missing the terminator".
            dockerfile = local_workspace / "Dockerfile"
            try:
                dockerfile.write_text(content, encoding="utf-8")
            except OSError as exc:
                return error_result(f"Environment setup 'docker' failed: cannot write Dockerfile: {exc}")
            notes.append(f"Dockerfile written: {dockerfile}")
        else:
            commands.append(build_write_command(content, "Dockerfile"))
            notes.append("Dockerfile written: Dockerfile")

    elif env_type == "conda":
        commands.extend(
            [
                "wget -O miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh",
                "bash miniconda.sh -b -p ./miniconda",
                ". ./miniconda/bin/activate",
            ]
        )
        for requirement in requirements:
            commands.append(f"conda install -y {shell_quote(requirement)}")

    full_command = " && ".join(commands)
    result = await get_tunnel_manager().execute_command(endpoint, full_command, timeout=300, allow_sudo=True)

    if not result["success"]:
        return error_result(
            f"Environment setup '{env_type}' failed: {failure_detail(result, 'Environment command failed')}"
        )

    result_text = f"Environment setup '{env_type}' completed successfully\n\n"
    result_text += f"Workspace: {workspace_path}\n"
    if requirements and env_type in {"python", "node", "conda"}:
        result_text += f"Requirements installed: {', '.join(str(requirement) for requirement in requirements)}\n"
    elif requirements and env_type == "custom":
        result_text += "Requirements ignored: environment_type 'custom' installs nothing\n"
    for note in notes:
        result_text += f"{note}\n"
    result_text += f"\nSetup output:\n{result['stdout']}"
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
