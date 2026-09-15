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
from mcp.client.streamable_http import streamablehttp_client

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


async def exercise(url: str, token: str, workspace: Path) -> None:
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "rigout", init.serverInfo
            assert str(workspace) in (init.instructions or ""), init.instructions

            names = [tool.name for tool in (await session.list_tools()).tools]
            assert names == EXPECTED_TOOLS, names

            await session.call_tool("write", {"path": "hello.txt", "content": "hello\n"})
            edited = await session.call_tool("edit", {"path": "hello.txt", "old_string": "hello", "new_string": "hello rigout"})
            assert not edited.isError, text(edited)
            assert (workspace / "hello.txt").read_text() == "hello rigout\n"

            ran = await session.call_tool("run", {"command": "echo rigout-ok"})
            assert not ran.isError and "rigout-ok" in text(ran), text(ran)


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
