"""User-scoped runtime state and process helpers for the Rigout CLI."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SETUP_TOKEN_PATTERN = re.compile(r"([?&]setup_token=)[^&\s\"'<>]+", re.IGNORECASE)
BEARER_TOKEN_PATTERN = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+\-/]+=*", re.IGNORECASE)
MAX_LOG_TAIL_LINES = 10_000


def redact_sensitive_text(value: str) -> str:
    """Remove connection credentials from persisted or relayed activity text."""
    value = SETUP_TOKEN_PATTERN.sub(r"\1<redacted>", value)
    return BEARER_TOKEN_PATTERN.sub(r"\1<redacted>", value)


def utc_now() -> str:
    """Return an ISO-8601 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


def default_state_dir(override: str | Path | None = None) -> Path:
    """Resolve the per-user Rigout state directory for the current platform."""
    configured = override or os.getenv("RIGOUT_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return (base / "rigout" / "state").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "rigout").resolve()

    base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (base / "rigout").resolve()


def secure_directory(path: Path) -> None:
    """Create a user-state directory with owner-only POSIX permissions."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(stat.S_IRWXU)


def secure_file(path: Path) -> None:
    """Apply owner-only POSIX permissions to a runtime file."""
    if os.name == "posix" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def secure_descriptor(descriptor: int) -> None:
    """Apply owner-only mode to an open POSIX file descriptor."""
    if os.name == "posix":
        fchmod = getattr(os, "fchmod", None)
        if fchmod:
            fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)


@dataclass(frozen=True)
class RuntimePaths:
    """Files used to manage one local Rigout launcher instance."""

    root: Path
    pid_file: Path
    runtime_file: Path
    log_file: Path
    connection_file: Path

    @classmethod
    def resolve(cls, state_dir: str | Path | None = None) -> RuntimePaths:
        """Resolve all lifecycle paths under a platform-appropriate state root."""
        root = default_state_dir(state_dir)
        return cls(
            root=root,
            pid_file=root / "rigout.pid",
            runtime_file=root / "runtime.json",
            log_file=root / "activity.log",
            connection_file=root / "connection.json",
        )

    def prepare(self) -> None:
        """Create the state directory with restrictive permissions."""
        secure_directory(self.root)


def write_text_secure(path: Path, value: str) -> None:
    """Atomically write a small owner-readable runtime file."""
    secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary_path = Path(temporary_name)
    try:
        secure_descriptor(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        secure_file(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def write_json_secure(path: Path, value: dict[str, Any]) -> None:
    """Atomically write owner-readable JSON runtime metadata."""
    write_text_secure(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, returning an empty object for missing or invalid state."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def write_pid(paths: RuntimePaths, pid: int) -> None:
    """Persist the launcher PID."""
    write_text_secure(paths.pid_file, f"{pid}\n")


def read_pid(paths: RuntimePaths) -> int | None:
    """Read the persisted launcher PID."""
    try:
        pid = int(paths.pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if pid > 0 else None


def remove_pid(paths: RuntimePaths, expected_pid: int | None = None) -> None:
    """Remove the PID file, optionally only when it still names the caller."""
    if expected_pid is not None and read_pid(paths) != expected_pid:
        return
    with contextlib.suppress(FileNotFoundError):
        paths.pid_file.unlink()


def process_is_running(pid: int | None) -> bool:
    """Return whether a process currently exists without changing it."""
    if not pid:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return bool(exit_code.value == still_active)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        stat_value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        if stat_value[stat_value.rfind(")") + 2 :].startswith("Z"):
            return False
    except (FileNotFoundError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_identity(pid: int | None) -> str | None:
    """Return a process creation fingerprint that changes when a PID is reused."""
    if not pid or not process_is_running(pid):
        return None
    if os.name == "nt":
        process_query_limited_information = 0x1000

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            creation = FileTime()
            exit_time = FileTime()
            kernel = FileTime()
            user = FileTime()
            succeeded = ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not succeeded:
                return None
            return f"windows-filetime:{(creation.high << 32) | creation.low}"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8")
        fields_after_command = value[value.rfind(")") + 2 :].split()
        return f"proc-start-ticks:{fields_after_command[19]}"
    except (FileNotFoundError, IndexError, OSError):
        pass

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = result.stdout.strip()
    return f"ps-lstart:{started}" if result.returncode == 0 and started else None


def process_matches_identity(pid: int | None, expected_identity: object) -> bool:
    """Return whether a live PID still represents the recorded launcher process."""
    return bool(expected_identity and process_identity(pid) == expected_identity)


def runtime_status(paths: RuntimePaths) -> dict[str, Any]:
    """Return normalized lifecycle state without exposing connection credentials."""
    runtime = read_json(paths.runtime_file)
    pid = read_pid(paths)
    process_exists = process_is_running(pid)
    identity_matches = process_matches_identity(pid, runtime.get("process_identity"))
    ownership_pending = bool(
        process_exists and runtime.get("status") == "starting" and not runtime.get("process_identity")
    )
    running = bool(process_exists and (identity_matches or ownership_pending))

    result: dict[str, Any] = {
        **runtime,
        "status": runtime.get("status", "stopped"),
        "pid": pid,
        "running": running,
        "state_dir": str(paths.root),
        "connection_file": str(runtime.get("connection_file", paths.connection_file)),
        "activity_log": str(paths.log_file),
    }
    if ownership_pending:
        result["ownership_pending"] = True
    elif process_exists and not identity_matches:
        result["ownership_mismatch"] = True
    if not running and result["status"] in {"starting", "running", "stopping"}:
        result["status"] = "stopped"
        result["stale_state"] = True
    return result


def open_activity_log(paths: RuntimePaths, *, truncate: bool = False) -> TextIO:
    """Open the activity log with owner-only permissions."""
    paths.prepare()
    flags = os.O_CREAT | os.O_WRONLY | (os.O_TRUNC if truncate else os.O_APPEND)
    descriptor = os.open(paths.log_file, flags, stat.S_IRUSR | stat.S_IWUSR)
    secure_descriptor(descriptor)
    return os.fdopen(descriptor, "a" if not truncate else "w", encoding="utf-8", buffering=1)


def append_activity(paths: RuntimePaths, text: str) -> None:
    """Append sanitized text to the owner-readable activity log."""
    with open_activity_log(paths) as output:
        output.write(text)


def launch_detached(
    command: list[str],
    paths: RuntimePaths,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    """Launch a managed child with output captured in the activity log."""
    log = open_activity_log(paths, truncate=True)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": env,
        "text": True,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        return subprocess.Popen(command, **kwargs)
    finally:
        log.close()


def terminate_process(pid: int, timeout: float = 10.0) -> bool:
    """Stop a managed launcher and wait for its process to disappear."""
    if not process_is_running(pid):
        return True

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_is_running(pid):
            return True
        time.sleep(0.1)

    if os.name != "nt":
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return True
        deadline = time.time() + 2
        while time.time() < deadline:
            if not process_is_running(pid):
                return True
            time.sleep(0.1)
    return not process_is_running(pid)


def read_tail(path: Path, line_count: int) -> list[str]:
    """Read the last requested text lines from a bounded runtime log."""
    if line_count <= 0:
        return []
    line_count = min(line_count, MAX_LOG_TAIL_LINES)
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            lines = deque(source, maxlen=line_count)
    except FileNotFoundError:
        return []
    return [line.rstrip("\r\n") for line in lines]
