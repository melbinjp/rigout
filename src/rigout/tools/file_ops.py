import posixpath
import shutil
import uuid
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager, heredoc_redirect, shell_join, shell_quote
from ._results import error_result, failure_detail


async def handle_file_operations(arguments: dict) -> CallToolResult:
    operation = arguments["operation"]
    path = arguments["path"]

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    if getattr(endpoint, "private_key_path", "") == "__local__":
        return await _handle_local_file_operation(operation, arguments)

    if operation == "read":
        command = f"cat {shell_quote(path)}"
    elif operation == "write":
        content = arguments.get("content", "")
        command = heredoc_redirect(content, path)
    elif operation == "append":
        content = arguments.get("content", "")
        delimiter = f"EOF_{uuid.uuid4().hex}"
        command = f"cat >> {shell_quote(path)} <<'{delimiter}'\n{content}\n{delimiter}"
    elif operation == "delete":
        command = f"rm -f {shell_quote(path)}"
    elif operation == "copy":
        destination = arguments.get("destination", "")
        command = f"cp {shell_quote(path)} {shell_quote(destination)}"
    elif operation == "move":
        destination = arguments.get("destination", "")
        command = f"mv {shell_quote(path)} {shell_quote(destination)}"
    elif operation == "chmod":
        permissions = arguments.get("permissions", "644")
        command = f"chmod {shell_quote(permissions)} {shell_quote(path)}"
    elif operation == "chown":
        owner = arguments.get("owner", "")
        command = f"sudo chown {shell_quote(owner)} {shell_quote(path)}"
    else:
        return error_result(f"Unsupported file operation: {operation}")

    result = await get_tunnel_manager().execute_command(endpoint, command)

    if result["success"]:
        result_text = f"File operation '{operation}' completed successfully\n\n"
        result_text += f"Path: {path}\n"
        result_text += f"Endpoint: {result['endpoint']}\n"
        if result["stdout"]:
            result_text += f"\nOutput:\n{result['stdout']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        result_text = f"File operation '{operation}' failed\n\n"
        result_text += f"Path: {path}\n"
        result_text += f"Error: {failure_detail(result, 'File operation command failed')}"
        return error_result(result_text)


async def handle_bulk_file_transfer(arguments: dict) -> CallToolResult:
    operation = arguments["operation"]
    source = arguments["source"]
    destination = arguments["destination"]

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    if getattr(endpoint, "private_key_path", "") == "__local__":
        return await _handle_local_bulk_file_transfer(operation, arguments)

    if operation == "upload":
        parent_dir = posixpath.dirname(destination) or "."
        command = f"mkdir -p {shell_quote(parent_dir)} && {heredoc_redirect(source, destination)}"
    elif operation == "download":
        command = f"cat {shell_quote(source)}"
    elif operation == "sync":
        files = arguments.get("files", [])
        if files:
            file_list = shell_join(files)
            command = f"cp -r {file_list} {shell_quote(destination)}"
        else:
            command = f"cp -r {shell_quote(source)}/. {shell_quote(destination)}/"
    else:
        return error_result(f"Unsupported file transfer operation: {operation}")

    result = await get_tunnel_manager().execute_command(endpoint, command, timeout=120, allow_sudo=True)

    if result["success"]:
        result_text = f"File transfer '{operation}' completed successfully\n\n"
        result_text += f"Source: {source}\n"
        result_text += f"Destination: {destination}\n"
        if result["stdout"]:
            result_text += f"\nOutput:\n{result['stdout']}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return error_result(
            f"File transfer '{operation}' failed: {failure_detail(result, 'File transfer command failed')}"
        )


async def _handle_local_file_operation(operation: str, arguments: dict) -> CallToolResult:
    path = Path(arguments["path"]).expanduser()
    try:
        if operation == "read":
            output = path.read_text(encoding="utf-8", errors="replace")
        elif operation == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments.get("content", ""), encoding="utf-8")
            output = ""
        elif operation == "append":
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(arguments.get("content", ""))
            output = ""
        elif operation == "delete":
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            output = ""
        elif operation == "copy":
            destination = Path(arguments.get("destination", "")).expanduser()
            if path.is_dir():
                shutil.copytree(path, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            output = ""
        elif operation == "move":
            destination = Path(arguments.get("destination", "")).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            output = ""
        elif operation == "chmod":
            path.chmod(int(str(arguments.get("permissions", "644")), 8))
            output = ""
        elif operation == "chown":
            return error_result("Local chown is not supported on this platform")
        else:
            return error_result(f"Unsupported file operation: {operation}")

        result_text = f"Local file operation '{operation}' completed successfully\n\nPath: {path}"
        if output:
            result_text += f"\n\nOutput:\n{output}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    except Exception as exc:
        return error_result(f"Local file operation '{operation}' failed: {exc}")


async def _handle_local_bulk_file_transfer(operation: str, arguments: dict) -> CallToolResult:
    source = arguments["source"]
    destination = Path(arguments["destination"]).expanduser()
    try:
        if operation == "upload":
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8")
        elif operation == "download":
            content = Path(source).expanduser().read_text(encoding="utf-8", errors="replace")
            return CallToolResult(content=[TextContent(type="text", text=content)])
        elif operation == "sync":
            files = arguments.get("files", [])
            destination.mkdir(parents=True, exist_ok=True)
            if files:
                for item in files:
                    item_path = Path(item).expanduser()
                    target = destination / item_path.name
                    if item_path.is_dir():
                        shutil.copytree(item_path, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item_path, target)
            else:
                source_path = Path(source).expanduser()
                if source_path.is_dir():
                    shutil.copytree(source_path, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(source_path, destination)
        else:
            return error_result(f"Unsupported file transfer operation: {operation}")

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Local file transfer '{operation}' completed successfully\n\nDestination: {destination}",
                )
            ]
        )
    except Exception as exc:
        return error_result(f"Local file transfer '{operation}' failed: {exc}")
