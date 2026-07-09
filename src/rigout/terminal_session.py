import contextlib
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import paramiko

if TYPE_CHECKING:
    from .ssh_manager import TunnelEndpoint


@dataclass
class TerminalSession:
    """Represents an active SSH-backed terminal session"""

    session_id: str
    endpoint: "TunnelEndpoint"
    ssh_client: paramiko.SSHClient
    channel: paramiko.Channel
    created: datetime
    last_activity: datetime
    is_interactive: bool = False
    working_directory: str = "~"
    command_history: list[str] = field(default_factory=list)
    max_idle_time: int = 3600  # 1 hour

    def is_expired(self) -> bool:
        """Check if session has expired due to inactivity"""
        return (datetime.now() - self.last_activity).total_seconds() > self.max_idle_time

    def add_command(self, command: str):
        """Add command to history with size limit"""
        self.command_history.append(command)
        if len(self.command_history) > 100:  # Keep last 100 commands
            self.command_history = self.command_history[-100:]
        self.last_activity = datetime.now()

    def close(self) -> None:
        """Close the SSH channel and client"""
        with contextlib.suppress(Exception):
            self.channel.close()
        with contextlib.suppress(Exception):
            self.ssh_client.close()


class LocalTerminalSession:
    """Persistent shell session on the machine running Rigout (no SSH).

    Commands are executed by writing them to a long-lived shell process,
    followed by a sentinel echo. Output is read until the sentinel appears,
    which also carries the command's exit code.
    """

    def __init__(self, session_id: str, endpoint: "TunnelEndpoint", max_idle_time: int = 3600):
        self.session_id = session_id
        self.endpoint = endpoint
        self.created = datetime.now()
        self.last_activity = datetime.now()
        self.is_interactive = True
        self.working_directory = "~"
        self.command_history: list[str] = []
        self.max_idle_time = max_idle_time

        self._is_windows = platform.system().lower() == "windows"
        if self._is_windows:
            shell = ["cmd.exe", "/q"]  # /q suppresses command echo
        else:
            shell = [shutil.which("bash") or os.environ.get("SHELL") or "/bin/sh"]

        self._process = subprocess.Popen(
            shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._output: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump_output, daemon=True).start()

    def _pump_output(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._output.put(line)
        self._output.put(None)  # signals shell exit

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def is_expired(self) -> bool:
        return (datetime.now() - self.last_activity).total_seconds() > self.max_idle_time

    def add_command(self, command: str) -> None:
        self.command_history.append(command)
        if len(self.command_history) > 100:
            self.command_history = self.command_history[-100:]
        self.last_activity = datetime.now()

    def execute(self, command: str, timeout: int = 30) -> dict[str, Any]:
        """Execute a command in the persistent shell and wait for completion."""
        base_result = {
            "session_id": self.session_id,
            "command": command,
            "timestamp": datetime.now().isoformat(),
        }
        if not self.is_alive():
            return {"success": False, "error": "Terminal session shell has exited", **base_result}

        with self._lock:
            # Drop output left over from a previous command that timed out
            with contextlib.suppress(queue.Empty):
                while True:
                    self._output.get_nowait()

            marker = f"__RIGOUT_DONE_{uuid.uuid4().hex}__"
            sentinel = f"echo {marker} %ERRORLEVEL%" if self._is_windows else f"echo {marker} $?"

            assert self._process.stdin is not None
            try:
                self._process.stdin.write(f"{command}\n{sentinel}\n")
                self._process.stdin.flush()
            except OSError as exc:
                return {"success": False, "error": f"Failed to send command to shell: {exc}", **base_result}
            self.add_command(command)

            output_lines: list[str] = []
            exit_code: int | None = None
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "success": False,
                        "error": f"Command timed out after {timeout}s (it may still be running)",
                        "output": "".join(output_lines),
                        **base_result,
                    }
                try:
                    line = self._output.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    return {
                        "success": False,
                        "error": "Terminal session shell exited while running the command",
                        "output": "".join(output_lines),
                        **base_result,
                    }
                stripped = line.strip()
                if marker in stripped:
                    # On Windows the interactive cmd.exe prompt (e.g. "C:\path>")
                    # is printed even with /q, prefixing the sentinel's own
                    # output, so the marker isn't necessarily at the start.
                    tail = stripped.split(marker, 1)[1].strip()
                    if tail.lstrip("-").isdigit():
                        exit_code = int(tail)
                    break
                if "__RIGOUT_DONE_" in line:
                    # Echoed sentinel command, or a stale sentinel from a
                    # previously timed-out command that finished late.
                    continue
                output_lines.append(line)

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "output": "".join(output_lines),
            **base_result,
        }

    def close(self) -> None:
        """Terminate the shell process"""
        if self._process.poll() is None:
            with contextlib.suppress(Exception):
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
