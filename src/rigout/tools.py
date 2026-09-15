"""The eight tools, and the jobs `run` hands off when a command outlives one call."""

from __future__ import annotations

import asyncio
import codecs
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from mcp.types import CallToolResult, Tool, ToolAnnotations

from .results import text_result

# Common MCP clients abandon a call after about 60 seconds. A command still running at 50 is
# handed off as a job instead of being lost; nothing is ever stopped for taking time.
RUN_WAIT_SECONDS = 50
# Per stream, per response. The newest output is kept, because the end of a log is what matters.
OUTPUT_CHARS = 100_000
READ_LINES = 2000
LINE_CHARS = 2000
LIST_LIMIT = 1000
GREP_LIMIT = 500
GREP_MAX_FILE_BYTES = 5_000_000
# edit holds the whole file to replace text exactly. Past this size it points the agent at run instead.
EDIT_MAX_BYTES = 50_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}

TOOL_NAMES = ("run", "process", "read", "write", "edit", "ls", "glob", "grep")


class Workspace:
    """Where relative paths and commands start. Absolute paths go wherever they point."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getcwd()).expanduser().resolve()

    def path(self, value: str | None) -> Path:
        candidate = Path(value or ".").expanduser()
        return candidate if candidate.is_absolute() else self.root / candidate

    def show(self, path: Path) -> str:
        try:
            shown = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
        return shown or "."


def shell_argv(command: str) -> list[str]:
    """The platform shell, given one command string."""
    if sys.platform == "win32":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            return [powershell, "-NoProfile", "-NonInteractive", "-Command", command]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return [shutil.which("bash") or "/bin/sh", "-c", command]


def shell_name() -> str:
    return Path(shell_argv("")[0]).stem


class Stream:
    """One output stream of a job, read incrementally."""

    KEEP = 2_000_000

    def __init__(self) -> None:
        self._text = ""
        self._dropped = 0
        self._read = 0
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            self._text += text
            overflow = len(self._text) - self.KEEP
            if overflow > 0:
                self._text = self._text[overflow:]
                self._dropped += overflow

    def take(self) -> tuple[str, int]:
        """Text added since the last take, and how many characters were skipped."""
        with self._lock:
            start = max(self._read, self._dropped)
            skipped = start - self._read
            text = self._text[start - self._dropped :]
            self._read = self._dropped + len(self._text)
        if len(text) > OUTPUT_CHARS:
            skipped += len(text) - OUTPUT_CHARS
            text = text[-OUTPUT_CHARS:]
        return text, skipped


class Job:
    """A command running in the platform shell, with its output kept in memory."""

    def __init__(self, command: str, cwd: Path, env: dict[str, Any] | None = None):
        self.id = uuid.uuid4().hex[:6]
        self.command = command
        self.started = time.monotonic()
        self.stdout = Stream()
        self.stderr = Stream()
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        self.process = subprocess.Popen(  # noqa: S603 - running the agent's command is the point
            shell_argv(command),
            cwd=cwd,
            env={**os.environ, **{key: str(value) for key, value in (env or {}).items()}},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
        self._pumps = [
            threading.Thread(target=_pump, args=(self.process.stdout, self.stdout), daemon=True),
            threading.Thread(target=_pump, args=(self.process.stderr, self.stderr), daemon=True),
        ]
        for pump in self._pumps:
            pump.start()

    @property
    def exit_code(self) -> int | None:
        code = self.process.poll()
        if code is not None:
            for pump in self._pumps:
                pump.join(timeout=5)
        return code

    @property
    def seconds(self) -> int:
        return int(time.monotonic() - self.started)

    async def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while self.process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def stop(self) -> None:
        """End the command and everything it started."""
        if self.process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],  # noqa: S607
                    capture_output=True,
                    check=False,
                )
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
        except OSError:
            self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def _pump(pipe: IO[bytes] | None, stream: Stream) -> None:
    if pipe is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    read = getattr(pipe, "read1", pipe.read)
    for chunk in iter(lambda: read(65536), b""):
        stream.add(decoder.decode(chunk))
    stream.add(decoder.decode(b"", final=True))
    pipe.close()


class Jobs:
    KEEP_FINISHED = 50

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def start(self, command: str, cwd: Path, env: dict[str, Any] | None = None) -> Job:
        job = Job(command, cwd, env)
        self._jobs[job.id] = job
        finished = [j for j in self._jobs.values() if j.process.poll() is not None]
        for old in finished[: -self.KEEP_FINISHED]:
            self._jobs.pop(old.id, None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def stop_all(self) -> None:
        for job in self._jobs.values():
            job.stop()


def job_report(job: Job) -> CallToolResult:
    out, out_skipped = job.stdout.take()
    err, err_skipped = job.stderr.take()
    code = job.exit_code
    lines = []
    if code is None:
        lines.append(
            f'still running after {job.seconds}s as job "{job.id}". '
            f'Call process with job "{job.id}" to read more output, wait for it, or stop it.'
        )
    else:
        lines.append(f"exit code {code}")
    if out_skipped:
        lines.append(f"[{out_skipped} earlier characters not shown]")
    if out:
        lines.append(out.rstrip("\n"))
    if err or err_skipped:
        lines.append("[stderr]")
        if err_skipped:
            lines.append(f"[{err_skipped} earlier characters not shown]")
        if err:
            lines.append(err.rstrip("\n"))
    return text_result("\n".join(lines), error=code not in (None, 0))


def _error(text: str) -> CallToolResult:
    return text_result(text, error=True)


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _clip(line: str) -> str:
    return line if len(line) <= LINE_CHARS else f"{line[:LINE_CHARS]} [+{len(line) - LINE_CHARS} characters]"


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A glob pattern as a regex over forward-slash relative paths, with ** spanning folders."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            # fnmatch rules: "!" negates, a leading "^" is literal, and a "]" straight after "[" or
            # "[!" is a member rather than the end of the class.
            start = i + 1
            if start < len(pattern) and pattern[start] == "!":
                start += 1
            if start < len(pattern) and pattern[start] == "]":
                start += 1
            close = pattern.find("]", start)
            if close == -1:
                out.append(re.escape(pattern[i]))
                i += 1
            else:
                body = pattern[i + 1 : close]
                negate = body.startswith("!")
                if negate:
                    body = body[1:]
                body = body.replace("\\", "\\\\")
                if body.startswith("^"):
                    body = "\\" + body
                out.append(f"[{'^' if negate else ''}{body}]")
                i = close + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _glob_files(base: Path, pattern: str) -> list[Path]:
    """Files under base matching pattern, never descending into SKIP_DIRS."""
    pattern = pattern.replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    regex = _glob_regex(pattern)
    top_level_only = "/" not in pattern
    matches = []
    for folder, dirs, names in os.walk(base):
        if top_level_only:
            dirs[:] = []
        else:
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        relative_folder = Path(folder).relative_to(base).as_posix()
        prefix = "" if relative_folder == "." else f"{relative_folder}/"
        for name in names:
            if regex.match(prefix + name):
                matches.append(Path(folder) / name)
    return matches


class Tools:
    """The tool implementations for one workspace."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.jobs = Jobs()

    async def call(self, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        if name not in TOOL_NAMES:
            return _error(f"Unknown tool {name!r}. The tools are: {', '.join(TOOL_NAMES)}.")
        handler = getattr(self, f"_{name}")
        try:
            result: CallToolResult = await handler(arguments or {})
            return result
        except KeyError as missing:
            return _error(f"{name}: missing required argument {missing}.")
        except Exception as exc:  # a tool call must come back as a result, never as a crash
            return _error(f"{name} failed: {type(exc).__name__}: {exc}")

    async def _run(self, args: dict[str, Any]) -> CallToolResult:
        cwd = self.workspace.path(args.get("cwd"))
        if not cwd.is_dir():
            return _error(
                f"run: cwd {self.workspace.show(cwd)} is not a folder. "
                "Call ls to find the folder, or leave cwd out to run in the workspace."
            )
        job = self.jobs.start(args["command"], cwd, args.get("env"))
        await job.wait(float(args.get("wait_seconds", RUN_WAIT_SECONDS)))
        return job_report(job)

    async def _process(self, args: dict[str, Any]) -> CallToolResult:
        action = args.get("action", "read")
        if action == "list":
            jobs = self.jobs.all()
            if not jobs:
                return text_result("No jobs.")
            rows = []
            for listed in jobs:
                code = listed.exit_code
                state = "running" if code is None else f"exit {code}"
                rows.append(f"{listed.id}  {state}  {listed.seconds}s  {listed.command[:100]}")
            return text_result("\n".join(rows))
        job = self.jobs.get(str(args.get("job", "")))
        if job is None:
            return _error(f'process: no job "{args.get("job", "")}". Call process with action "list" to see jobs.')
        if action == "stop":
            job.stop()
            return job_report(job)
        if action == "read":
            await job.wait(float(args.get("wait_seconds", 0)))
            return job_report(job)
        return _error(f'process: action must be "read", "stop" or "list", not {action!r}.')

    async def _read(self, args: dict[str, Any]) -> CallToolResult:
        path = self.workspace.path(args["path"])
        shown = self.workspace.show(path)
        if not path.exists():
            return _error(f"read: {shown} does not exist. Call glob or ls to find the file.")
        if path.is_dir():
            return _error(f"read: {shown} is a folder. Call ls on it.")
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, int(args.get("limit", READ_LINES)))
        result: CallToolResult = await asyncio.to_thread(self._read_lines, path, shown, offset, limit)
        return result

    def _read_lines(self, path: Path, shown: str, offset: int, limit: int) -> CallToolResult:
        # Line by line, so a multi-gigabyte log costs only the lines returned, not the whole file.
        with open(path, "rb") as probe:
            head = probe.read(8192)
        if b"\x00" in head:
            return _error(f"read: {shown} is a binary file ({_size(path.stat().st_size)}), so it is not shown as text.")
        kept: list[str] = []
        total = 0
        last = offset + limit - 1
        with open(path, encoding="utf-8", errors="replace", newline=None) as handle:
            for total, line in enumerate(handle, 1):
                if offset <= total <= last:
                    kept.append(line.rstrip("\n"))
        if total == 0:
            return text_result(f"{shown} is empty.")
        if not kept:
            return _error(f"read: {shown} has {total} lines, so offset {offset} is past the end.")
        end = offset + len(kept) - 1
        width = len(str(end))
        body = "\n".join(f"{number:>{width}}\t{_clip(line)}" for number, line in enumerate(kept, offset))
        if end < total:
            body += f"\n[lines {offset}-{end} of {total}. Call read with offset {end + 1} for more.]"
        return text_result(body)

    async def _write(self, args: dict[str, Any]) -> CallToolResult:
        path = self.workspace.path(args["path"])
        content = args["content"]
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        verb = "Replaced" if existed else "Created"
        return text_result(f"{verb} {self.workspace.show(path)} ({_size(len(content.encode('utf-8')))}).")

    async def _edit(self, args: dict[str, Any]) -> CallToolResult:
        path = self.workspace.path(args["path"])
        shown = self.workspace.show(path)
        old, new = args["old_string"], args["new_string"]
        replace_all = bool(args.get("replace_all", False))
        if not old:
            return _error("edit: old_string is empty. Use write to replace a whole file.")
        if old == new:
            return _error("edit: old_string and new_string are the same, so there is nothing to change.")
        if not path.is_file():
            return _error(f"edit: {shown} does not exist. Use write to create it.")
        size = path.stat().st_size
        if size > EDIT_MAX_BYTES:
            return _error(
                f"edit: {shown} is {_size(size)}, too large to edit in memory. "
                "Use run with a streaming tool such as sed instead."
            )
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            return _error(
                f"edit: old_string was not found in {shown}. Call read on it and copy the text exactly, "
                "including indentation."
            )
        if count > 1 and not replace_all:
            return _error(
                f"edit: old_string appears {count} times in {shown}. Include more surrounding lines so it is "
                "unique, or pass replace_all: true."
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        replaced = count if replace_all else 1
        return text_result(f"Edited {shown}: {replaced} replacement{'s' if replaced != 1 else ''}.")

    async def _ls(self, args: dict[str, Any]) -> CallToolResult:
        path = self.workspace.path(args.get("path"))
        shown = self.workspace.show(path)
        if not path.exists():
            return _error(f"ls: {shown} does not exist. Call ls on its parent folder, or glob to find it.")
        if path.is_file():
            return text_result(f"{shown}  {_size(path.stat().st_size)}")
        entries = sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        rows = []
        for entry in entries[:LIST_LIMIT]:
            if entry.is_dir():
                rows.append(f"{entry.name}/")
                continue
            try:
                rows.append(f"{entry.name}  {_size(entry.stat().st_size)}")
            except OSError:
                rows.append(entry.name)
        header = f"{shown}/ ({len(entries)} entries)"
        if len(entries) > LIST_LIMIT:
            rows.append(f"[first {LIST_LIMIT} shown]")
        return text_result("\n".join([header, *rows]) if rows else f"{header} is empty")

    async def _glob(self, args: dict[str, Any]) -> CallToolResult:
        base = self.workspace.path(args.get("path"))
        pattern = args["pattern"]
        if not base.is_dir():
            return _error(
                f"glob: {self.workspace.show(base)} is not a folder. "
                "Call ls to find the folder, or leave path out to search the workspace."
            )
        matches = await asyncio.to_thread(_glob_files, base, pattern)
        if not matches:
            return text_result(f"No files match {pattern} under {self.workspace.show(base)}.")
        matches.sort(key=lambda match: match.stat().st_mtime, reverse=True)
        rows = [self.workspace.show(match) for match in matches[:LIST_LIMIT]]
        if len(matches) > LIST_LIMIT:
            rows.append(f"[{len(matches)} matches, newest {LIST_LIMIT} shown. Narrow the pattern for the rest.]")
        return text_result("\n".join(rows))

    async def _grep(self, args: dict[str, Any]) -> CallToolResult:
        base = self.workspace.path(args.get("path"))
        pattern = args["pattern"]
        include = args.get("glob")
        ignore_case = bool(args.get("ignore_case", False))
        literal = bool(args.get("literal", False))
        if not base.exists():
            return _error(
                f"grep: {self.workspace.show(base)} does not exist. "
                "Call ls or glob to find it, or leave path out to search the workspace."
            )
        ripgrep = shutil.which("rg")
        if ripgrep:
            rows = await asyncio.to_thread(self._ripgrep, ripgrep, pattern, base, include, ignore_case, literal)
        else:
            try:
                regex = re.compile(re.escape(pattern) if literal else pattern, re.IGNORECASE if ignore_case else 0)
            except re.error as exc:
                return _error(f"grep: invalid pattern ({exc}). Escape characters such as ( [ . with a backslash.")
            rows = await asyncio.to_thread(self._python_grep, regex, base, include)
        if isinstance(rows, CallToolResult):
            return rows
        if not rows:
            return text_result(f"No matches for {pattern!r} under {self.workspace.show(base)}.")
        if len(rows) > GREP_LIMIT:
            rows = [*rows[:GREP_LIMIT], f"[first {GREP_LIMIT} matches shown. Narrow the pattern, path or glob.]"]
        return text_result("\n".join(rows))

    def _ripgrep(
        self, ripgrep: str, pattern: str, base: Path, include: str | None, ignore_case: bool, literal: bool = False
    ) -> list[str] | CallToolResult:
        argv = [ripgrep, "--line-number", "--no-heading", "--color", "never", "--hidden", "--path-separator", "/"]
        # --null ends each path with a NUL byte, so a file name containing a colon stays whole, and
        # --with-filename names the file even when the search is a single file.
        argv += ["--null", "--with-filename", "--max-columns", "500", "--max-columns-preview"]
        for skipped in sorted(SKIP_DIRS):
            argv += ["--glob", f"!{skipped}"]
        if ignore_case:
            argv.append("--ignore-case")
        if literal:
            argv.append("--fixed-strings")
        if include:
            argv += ["--glob", include]
        folder = base.parent if base.is_file() else base
        argv += ["--", pattern] + ([base.name] if base.is_file() else [])
        # Streamed and stopped at the limit, so a pattern matching millions of lines never sits in
        # memory. stderr goes to a file, so a noisy search cannot fill a pipe and stall.
        rows: list[str] = []
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(  # noqa: S603
                argv,
                cwd=folder,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                relative, _, rest = line.rstrip("\n").partition("\0")
                rows.append(f"{self.workspace.show(folder / relative)}:{rest}")
                if len(rows) > GREP_LIMIT:
                    process.kill()
                    break
            process.stdout.close()
            process.wait()
            errors.seek(0)
            message = errors.read().decode("utf-8", errors="replace").strip()
        if not rows and process.returncode == 2:
            return _error(f"grep: {message}. Check the pattern, or pass literal: true to search for plain text.")
        return rows

    @staticmethod
    def _walk_files(base: Path) -> Iterator[Path]:
        """Files under base, skipping dependency folders, yielded as found rather than listed first."""
        if base.is_file():
            yield base
            return
        for folder, dirs, names in os.walk(base):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            for name in names:
                yield Path(folder) / name

    def _python_grep(self, regex: re.Pattern[str], base: Path, include: str | None) -> list[str]:
        rows: list[str] = []
        for file in self._walk_files(base):
            if include and not (fnmatch.fnmatch(file.name, include) or file.match(include)):
                continue
            try:
                if file.stat().st_size > GREP_MAX_FILE_BYTES:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    rows.append(f"{self.workspace.show(file)}:{number}:{line[:500]}")
                    if len(rows) > GREP_LIMIT:
                        return rows
        return rows


def instructions(workspace: Workspace) -> str:
    return (
        "Rigout gives you a shell and the files on this machine.\n"
        f"Workspace: {workspace.root}. Relative paths and commands start there.\n"
        f"run uses {shell_name()}.\n"
        "For files use read, write, edit, ls, glob and grep rather than shell commands, "
        "and edit rather than write for a file that already exists.\n"
        "Use run for builds, tests, git, installs and servers. A command still running after about "
        f"{RUN_WAIT_SECONDS} seconds keeps going as a job; follow it with process, and stop jobs you "
        "started when you are done."
    )


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _tool(**fields: Any) -> Tool:
    """Built from the wire names, which both mcp majors accept. 2.x renamed the fields to snake_case."""
    return Tool.model_validate(fields)


def _annotations(**fields: Any) -> ToolAnnotations:
    return ToolAnnotations.model_validate(fields)


def tool_definitions() -> list[Tool]:
    text = {"type": "string"}
    path = {"type": "string", "description": "Relative to the workspace, or absolute."}
    folder = {"type": "string", "description": "Relative to the workspace, or absolute. Default: the workspace."}
    return [
        _tool(
            name="run",
            title="Run a command",
            description=(
                f"Run a shell command ({shell_name()}) and get its exit code and output. Use for builds, tests, "
                "git, package managers and servers. For files prefer read, write, edit, ls, glob and grep. "
                f"A command still running after wait_seconds (default {RUN_WAIT_SECONDS}) keeps running as a "
                "job, and the first line of the result gives its id; follow it with process."
            ),
            inputSchema=_schema(
                {
                    "command": text,
                    "cwd": {"type": "string", "description": "Folder to run in. Default: the workspace."},
                    "env": {"type": "object", "additionalProperties": {"type": "string"}},
                    "wait_seconds": {"type": "number", "description": f"Default {RUN_WAIT_SECONDS}."},
                },
                ["command"],
            ),
            annotations=_annotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        ),
        _tool(
            name="process",
            title="Follow a job",
            description=(
                'Follow a command that run handed off as a job. "read" returns new output, waiting up to '
                'wait_seconds for it to finish; "stop" ends it and everything it started; "list" shows jobs.'
            ),
            inputSchema=_schema(
                {
                    "action": {"type": "string", "enum": ["read", "stop", "list"], "description": "Default read."},
                    "job": {"type": "string", "description": "Job id from run. Required for read and stop."},
                    "wait_seconds": {"type": "number", "description": "For read. Default 0."},
                },
                [],
            ),
            annotations=_annotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
        ),
        _tool(
            name="read",
            title="Read a file",
            description=(
                f"Read a text file with line numbers, {READ_LINES} lines at a time. Use offset and limit for "
                "long files. Use this rather than run with cat."
            ),
            inputSchema=_schema(
                {
                    "path": path,
                    "offset": {"type": "integer", "minimum": 1, "description": "First line number to read. Default 1."},
                    "limit": {"type": "integer", "minimum": 1, "description": "Number of lines."},
                },
                ["path"],
            ),
            annotations=_annotations(readOnlyHint=True, openWorldHint=False),
        ),
        _tool(
            name="write",
            title="Write a file",
            description=(
                "Create a file or replace a whole file, creating parent folders. To change part of an existing "
                "file use edit instead."
            ),
            inputSchema=_schema({"path": path, "content": text}, ["path", "content"]),
            annotations=_annotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
        ),
        _tool(
            name="edit",
            title="Edit a file",
            description=(
                "Change part of an existing file by replacing old_string with new_string exactly. Use this "
                "instead of write or run with sed. Fails if old_string is missing or appears more than once; "
                "add surrounding lines or set replace_all. Call read first and copy the text exactly, including indentation."
            ),
            inputSchema=_schema(
                {"path": path, "old_string": text, "new_string": text, "replace_all": {"type": "boolean"}},
                ["path", "old_string", "new_string"],
            ),
            annotations=_annotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
        ),
        _tool(
            name="ls",
            title="List a folder",
            description="List a folder. Folders end with /, files show their size.",
            inputSchema=_schema({"path": folder}, []),
            annotations=_annotations(readOnlyHint=True, openWorldHint=False),
        ),
        _tool(
            name="glob",
            title="Find files",
            description="Find files by name pattern, such as **/*.py or src/**/test_*.ts, newest first.",
            inputSchema=_schema({"pattern": text, "path": folder}, ["pattern"]),
            annotations=_annotations(readOnlyHint=True, openWorldHint=False),
        ),
        _tool(
            name="grep",
            title="Search file contents",
            description=(
                "Search file contents with a regular expression and get file:line:text matches. Skips .git and "
                "node_modules. Narrow with path and glob, such as *.py."
            ),
            inputSchema=_schema(
                {
                    "pattern": text,
                    "path": folder,
                    "glob": text,
                    "ignore_case": {"type": "boolean"},
                    "literal": {
                        "type": "boolean",
                        "description": "Match the pattern as plain text, not a regular expression.",
                    },
                },
                ["pattern"],
            ),
            annotations=_annotations(readOnlyHint=True, openWorldHint=False),
        ),
    ]
