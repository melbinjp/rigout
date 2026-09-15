"""Start the rigout command and use it the way an MCP client does. Exits non-zero on any failure."""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client import streamable_http

EXPECTED_TOOLS = ["run", "process", "read", "write", "edit", "ls", "glob", "grep"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_health(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"rigout exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise SystemExit("rigout did not answer /health within 30 seconds")


def text(result) -> str:
    return result.content[0].text


def field(obj, *names):
    """Read a field under either spelling: mcp 2.x renamed them to snake_case."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(names[0])


def connect(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    if hasattr(streamable_http, "streamable_http_client"):  # mcp 2.x
        client = streamable_http.create_mcp_http_client(headers=headers)
        return streamable_http.streamable_http_client(url, http_client=client)
    return streamable_http.streamablehttp_client(url, headers=headers)


async def exercise(url: str, token: str, workspace: Path) -> None:
    async with connect(url, token) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_info = field(init, "server_info", "serverInfo")
            assert server_info.name == "rigout", server_info
            assert str(workspace) in (init.instructions or ""), init.instructions

            names = [tool.name for tool in (await session.list_tools()).tools]
            assert names == EXPECTED_TOOLS, names

            await session.call_tool("write", {"path": "hello.txt", "content": "hello\n"})
            edited = await session.call_tool("edit", {"path": "hello.txt", "old_string": "hello", "new_string": "hello rigout"})
            assert not field(edited, "is_error", "isError"), text(edited)
            assert (workspace / "hello.txt").read_text() == "hello rigout\n"

            ran = await session.call_tool("run", {"command": "echo rigout-ok"})
            assert not field(ran, "is_error", "isError") and "rigout-ok" in text(ran), text(ran)


def main() -> int:
    with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as folder:
        workspace = Path(folder).resolve()
        port = free_port()
        process = subprocess.Popen(
            [sys.executable, "-m", "rigout", "serve", "--workspace", str(workspace), "--port", str(port)],
            env={**os.environ, "RIGOUT_STATE_DIR": state},
            stdout=subprocess.DEVNULL,
        )
        try:
            wait_for_health(port, process)
            token = (Path(state) / "token").read_text().strip()
            asyncio.run(exercise(f"http://127.0.0.1:{port}/mcp", token, workspace))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    print("production validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
