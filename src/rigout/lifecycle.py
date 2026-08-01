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
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SETUP_TOKEN_PATTERN = re.compile(r"([?&]setup_token=)[^&\s\"'<>]+", re.IGNORECASE)
BEARER_TOKEN_PATTERN = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+\-/]+=*", re.IGNORECASE)
MAX_LOG_TAIL_LINES = 10_000
STATE_LOCK_NAME = "rigout.lock"
STATE_LOCK_TIMEOUT = 10.0
# `ps -o lstart=` costs a fork and exec, and the identity it reports has one-second
# granularity, so a cache no longer than that granularity cannot make the answer any
# less precise than the source already is. See process_identity.
PS_IDENTITY_CACHE_SECONDS = 1.0
PS_IDENTITY_CACHE_LIMIT = 128
# Keys runtime_status always reports, so `status --output json` has one shape whether or
# not Rigout has ever started. They are null until a run records them.
OPTIONAL_STATUS_KEYS = (
    "mcp_url",
    "health_url",
    "local_health_url",
    "host",
    "port",
    "path",
    "tunnel",
    "started_at",
    "stopped_at",
    "last_error",
)


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


def redact_home_path(value: str) -> str:
    """Replace the current user's home directory prefix with `~`.

    Runtime paths are reported to remote agents, and an absolute path under a home
    directory names the operating system account that runs Rigout.
    """
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return value
    if not home or len(value) < len(home):
        return value

    prefix = value[: len(home)]
    matches = prefix.lower() == home.lower() if os.name == "nt" else prefix == home
    remainder = value[len(home) :]
    if not matches or (remainder and remainder[0] not in {"/", "\\"}):
        return value
    return f"~{remainder}"


_SHARED_DIRECTORY_WARNINGS: set[str] = set()


def warn_about_shared_directory(path: Path) -> None:
    """Warn once when runtime state lives in a directory other users can reach."""
    if os.name != "posix" or str(path) in _SHARED_DIRECTORY_WARNINGS:
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if not mode & (stat.S_IRWXG | stat.S_IRWXO):
        return
    _SHARED_DIRECTORY_WARNINGS.add(str(path))
    print(
        f"rigout: warning: runtime directory {redact_home_path(str(path))} is reachable by other users "
        f"(mode {stat.filemode(mode)}). Rigout will not change permissions on a directory it did not "
        "create; runtime files are still written owner-only.",
        file=sys.stderr,
    )


def secure_directory(path: Path) -> None:
    """Create a user-state directory with owner-only POSIX permissions.

    Permissions are only tightened on a directory Rigout created. A caller that points
    `--state-dir` or `--connection-file` at an existing shared directory such as /tmp must
    not have that directory's mode rewritten for everyone else on the machine.
    """
    try:
        path.mkdir(parents=True, exist_ok=False, mode=stat.S_IRWXU)
    except FileExistsError:
        if not path.is_dir():
            raise
        warn_about_shared_directory(path)
        return
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
    lock_file: Path

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
            lock_file=root / STATE_LOCK_NAME,
        )

    def prepare(self) -> None:
        """Create the state directory with restrictive permissions."""
        secure_directory(self.root)


def try_lock_descriptor(descriptor: int) -> bool:
    """Take an exclusive OS-level lock on an open descriptor without blocking.

    Both back ends are released by the kernel when the holding process exits, however it
    exits. That is the property being bought here: a lock file with a hand-written stale
    check has to decide whether a holder is dead, and a waiter that decides wrong deletes
    a live holder's lock.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def state_lock(paths: RuntimePaths, timeout: float = STATE_LOCK_TIMEOUT) -> Iterator[bool]:
    """Serialize the read-modify-write sections that own one state directory.

    runtime.json and the PID file are two files that have to agree, and every lifecycle
    command reads them, decides, and writes them back. Without this, two `rigout start`
    moments apart both read "not running", both launch, and the loser's shutdown path
    overwrites the winner's record.

    Yields whether the lock was actually taken. A filesystem that cannot lock still gets a
    working CLI: every critical section is additionally guarded by the recorded instance
    id, so an unlocked section degrades to the previous behaviour rather than failing.
    """
    paths.prepare()
    descriptor: int | None = None
    held = False
    try:
        descriptor = os.open(paths.lock_file, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
        secure_descriptor(descriptor)
        deadline = time.monotonic() + timeout
        while True:
            held = try_lock_descriptor(descriptor)
            if held or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not held:
            print(
                f"rigout: warning: could not lock {redact_home_path(str(paths.root))} within {timeout:g}s; "
                "continuing without it. Concurrent rigout commands on this state directory may disagree.",
                file=sys.stderr,
            )
    except OSError:
        held = False
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            descriptor = None

    try:
        yield held
    finally:
        # Closing releases both back ends; there is no unlock step that can be skipped.
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)


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


def _windows_kernel32() -> Any | None:
    """Return the Windows kernel API without exposing platform-only attributes to mypy."""
    loader = getattr(ctypes, "windll", None)
    return getattr(loader, "kernel32", None) if loader is not None else None


def process_is_running(pid: int | None) -> bool:
    """Return whether a process currently exists without changing it."""
    if not pid:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = _windows_kernel32()
        if kernel32 is None:
            return False
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return bool(exit_code.value == still_active)
        finally:
            kernel32.CloseHandle(handle)
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
        kernel32 = _windows_kernel32()
        if kernel32 is None:
            return None

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            creation = FileTime()
            exit_time = FileTime()
            kernel = FileTime()
            user = FileTime()
            succeeded = kernel32.GetProcessTimes(
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
            kernel32.CloseHandle(handle)

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8")
        fields_after_command = value[value.rfind(")") + 2 :].split()
        return f"proc-start-ticks:{fields_after_command[19]}"
    except (FileNotFoundError, IndexError, OSError):
        pass

    return ps_identity(pid)


_PS_IDENTITY_CACHE: dict[int, tuple[float, str | None]] = {}


def ps_identity(pid: int) -> str | None:
    """Return a `ps`-derived creation fingerprint for a live PID, cached briefly.

    This is the fallback for systems without /proc, which means every macOS run. Callers
    poll process state several times a second, and a fork plus exec of `ps` at that rate
    is a real cost on the platform that can least afford it.

    The cache cannot weaken the PID-reuse guard: `ps -o lstart=` reports whole seconds, so
    it already cannot tell apart two processes that held one PID within the same second,
    and the cache is not held longer than that. Callers reach this only after
    process_is_running has confirmed the PID is still live.
    """
    cached = _PS_IDENTITY_CACHE.get(pid)
    now = time.monotonic()
    if cached is not None and now - cached[0] < PS_IDENTITY_CACHE_SECONDS:
        return cached[1]

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
    identity = f"ps-lstart:{started}" if result.returncode == 0 and started else None

    if len(_PS_IDENTITY_CACHE) >= PS_IDENTITY_CACHE_LIMIT:
        _PS_IDENTITY_CACHE.clear()
    _PS_IDENTITY_CACHE[pid] = (now, identity)
    return identity


def process_matches_identity(pid: int | None, expected_identity: object) -> bool:
    """Return whether a live PID still represents the recorded launcher process."""
    return bool(expected_identity and process_identity(pid) == expected_identity)


def runtime_status(paths: RuntimePaths) -> dict[str, Any]:
    """Return normalized lifecycle state without exposing connection credentials."""
    runtime = read_json(paths.runtime_file)
    pid = read_pid(paths)
    process_exists = process_is_running(pid)
    # A live PID counts as Rigout only when the recorded creation fingerprint still names
    # that exact process. There is deliberately no "trust any live process at this PID"
    # path: an unrelated process that inherits a recorded PID would then make status,
    # stop and start all believe Rigout is running, with no supported way back out.
    identity_matches = process_matches_identity(pid, runtime.get("process_identity"))
    running = bool(process_exists and identity_matches)

    result: dict[str, Any] = {
        # Seeded first so a caller can read status["mcp_url"] before the first start and
        # get null instead of a KeyError. A JSON contract whose key set changes with the
        # state is not a contract; the recorded values below still win.
        **dict.fromkeys(OPTIONAL_STATUS_KEYS),
        **runtime,
        "status": runtime.get("status", "stopped"),
        "pid": pid,
        "running": running,
        "state_dir": str(paths.root),
        "connection_file": str(runtime.get("connection_file", paths.connection_file)),
        "activity_log": str(paths.log_file),
    }
    if process_exists and not identity_matches:
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
        create_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = create_process_group | create_no_window
    else:
        kwargs["start_new_session"] = True

    try:
        return subprocess.Popen(command, **kwargs)
    finally:
        log.close()


def posix_kill_tree(pid: int, signal_number: int) -> None:
    """Signal a detached launcher's whole process group, falling back to the process itself.

    `launch_detached` starts a new session, so a detached launcher leads its own group and
    its children are reachable in one call. A launcher that does not lead a group shares the
    caller's group, which must never be signalled.
    """
    get_process_group = getattr(os, "getpgid", None)
    kill_process_group = getattr(os, "killpg", None)
    if get_process_group is not None and kill_process_group is not None:
        try:
            if get_process_group(pid) == pid:
                kill_process_group(pid, signal_number)
                return
        except OSError:
            # A dead or foreign process falls through to the single-PID path below,
            # which reports the same errors the caller already handles.
            pass
    os.kill(pid, signal_number)


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
            posix_kill_tree(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
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
