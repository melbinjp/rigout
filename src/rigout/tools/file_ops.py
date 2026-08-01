import posixpath
import shutil
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from ..security_validator import security_validator
from ..ssh_manager import build_write_command, get_tunnel_manager, shell_join, shell_quote
from ._results import error_result, failure_detail

# A file read lands in an agent's context, so it has to be bounded. This is generous
# for source and configuration files while making it impossible to exhaust memory on
# a multi-gigabyte file or bury the model in one call.
MAX_READ_BYTES = 1_048_576


def scrub(text: str) -> str:
    """Redact credentials from text a local operation is about to return.

    The SSH path inherits this from `execute_command`, which sanitizes stdout and
    stderr. The local path has no such chokepoint, so every local branch that
    returns content has to call this itself. Without it the same secret is refused
    through `execute_command` and handed over through `file_operations`.
    """
    return security_validator.sanitize_command_output(text)


# How much of a file is examined to decide whether it is text. A NUL byte in the first
# few KB is the classic test and is what `grep`, `git` and `file` all effectively use;
# no real text file contains one.
BINARY_SNIFF_BYTES = 8192


def looks_binary(sample: bytes) -> bool:
    """Return whether `sample` is bytes rather than text."""
    return b"\x00" in sample[:BINARY_SNIFF_BYTES]


def binary_refusal(path: str, size: int | None) -> str:
    """Explain that a file is not text, and name the tools that do handle bytes.

    Returning the bytes instead is worse than useless. Decoded with replacement
    characters they tell the caller nothing, they cost whatever an agent pays for the
    context they fill, and the control characters among them corrupt the event stream
    carrying the response, which hung a live session until the client gave up.
    """
    measured = f" ({size} bytes)" if size is not None else ""
    return (
        f"'{path}'{measured} is a binary file, and file_operations read returns text.\n\n"
        "Reading it as text would return nothing usable. To work with it instead:\n"
        "- bulk_file_transfer moves the file without decoding it\n"
        "- execute_command with `file`, `xxd | head`, or `sha256sum` inspects it in place\n"
        "- execute_command with `base64` returns it as text if it must travel in a response"
    )


def _read_bounded(path: Path) -> str:
    """Read at most MAX_READ_BYTES, stating plainly when the file was truncated."""
    with path.open("rb") as handle:
        raw = handle.read(MAX_READ_BYTES + 1)
    if looks_binary(raw):
        size = path.stat().st_size if path.exists() else None
        return binary_refusal(str(path), size)
    text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(raw) > MAX_READ_BYTES:
        text += f"\n\n[truncated: only the first {MAX_READ_BYTES} bytes are shown]"
    return text


def _truncate_remote(text: str, path: str | None = None) -> str:
    """Apply the same statement of truncation to output of a bounded remote read.

    A remote read arrives already decoded, so the binary test is for a NUL character
    rather than a NUL byte; it is the same test and it has to happen here too, because
    a remote binary file breaks the response exactly as a local one does.
    """
    if path is not None and "\x00" in text[:BINARY_SNIFF_BYTES]:
        return binary_refusal(path, None)
    if len(text) > MAX_READ_BYTES:
        return text[:MAX_READ_BYTES] + f"\n\n[truncated: only the first {MAX_READ_BYTES} bytes are shown]"
    return text


async def handle_file_operations(arguments: dict) -> CallToolResult:
    operation = arguments["operation"]
    path = arguments["path"]

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    if getattr(endpoint, "private_key_path", "") == "__local__":
        return await _handle_local_file_operation(operation, arguments)

    if operation == "read":
        command = f"head -c {MAX_READ_BYTES + 1} {shell_quote(path)}"
    elif operation == "write":
        content = arguments.get("content", "")
        command = build_write_command(content, path)
    elif operation == "append":
        content = arguments.get("content", "")
        command = build_write_command(content, path, append=True)
    elif operation == "delete":
        # Recursive deletion is opt-in, so that a directory passed by mistake is
        # refused rather than removed. The local branch enforces the same rule.
        flag = "-rf" if arguments.get("recursive", False) else "-f"
        command = f"rm {flag} {shell_quote(path)}"
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
            payload = _truncate_remote(result["stdout"], path if operation == "read" else None)
            result_text += f"\nOutput:\n{payload}"
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
        command = f"mkdir -p {shell_quote(parent_dir)} && {build_write_command(source, destination)}"
    elif operation == "download":
        command = f"head -c {MAX_READ_BYTES + 1} {shell_quote(source)}"
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
            result_text += f"\nOutput:\n{_truncate_remote(result['stdout'])}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return error_result(
            f"File transfer '{operation}' failed: {failure_detail(result, 'File transfer command failed')}"
        )


async def _handle_local_file_operation(operation: str, arguments: dict) -> CallToolResult:
    path = Path(arguments["path"]).expanduser()
    try:
        if operation == "read":
            output = _read_bounded(path)
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
                if not arguments.get("recursive", False):
                    return error_result(
                        f"'{path}' is a directory. Pass recursive=true to delete a directory and its contents."
                    )
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
            result_text += f"\n\nOutput:\n{scrub(output)}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    except Exception as exc:
        return error_result(scrub(f"Local file operation '{operation}' failed: {exc}"))


async def _handle_local_bulk_file_transfer(operation: str, arguments: dict) -> CallToolResult:
    source = arguments["source"]
    destination = Path(arguments["destination"]).expanduser()
    try:
        if operation == "upload":
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8")
        elif operation == "download":
            content = _read_bounded(Path(source).expanduser())
            return CallToolResult(content=[TextContent(type="text", text=scrub(content))])
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
        return error_result(scrub(f"Local file transfer '{operation}' failed: {exc}"))
