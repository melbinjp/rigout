import json
import re
import sys

import pytest

from rigout.results import result_is_error
from rigout.tools import TOOL_NAMES, Tools, Workspace, tool_definitions

pytestmark = pytest.mark.unit


@pytest.fixture
def tools(tmp_path):
    instance = Tools(Workspace(tmp_path))
    yield instance
    instance.jobs.stop_all()


def text(result):
    return result.content[0].text


def python(code: str) -> str:
    """A shell command running `code`, spelled for the platform shell."""
    prefix = "& " if sys.platform == "win32" else ""
    return f'{prefix}"{sys.executable}" -c "{code}"'


def test_exactly_the_eight_tools_and_no_escape_hatch():
    definitions = tool_definitions()
    assert [tool.name for tool in definitions] == list(TOOL_NAMES)
    assert len(definitions) == 8
    assert "bypass" not in json.dumps([tool.inputSchema for tool in definitions])


async def test_write_then_read_with_line_numbers(tools, tmp_path):
    written = await tools.call("write", {"path": "src/a.txt", "content": "one\ntwo\nthree\n"})
    assert not result_is_error(written)
    assert (tmp_path / "src" / "a.txt").read_text() == "one\ntwo\nthree\n"

    result = await tools.call("read", {"path": "src/a.txt", "offset": 2, "limit": 1})
    assert text(result).startswith("2\ttwo")
    assert "offset 3" in text(result)


async def test_read_explains_what_to_do_when_missing(tools):
    result = await tools.call("read", {"path": "nope.txt"})
    assert result_is_error(result)
    assert "glob or ls" in text(result)


async def test_edit_requires_a_unique_match(tools, tmp_path):
    (tmp_path / "f.py").write_text("x = 1\nx = 1\n")
    result = await tools.call("edit", {"path": "f.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert result_is_error(result)
    assert "2 times" in text(result)

    result = await tools.call(
        "edit", {"path": "f.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}
    )
    assert not result_is_error(result)
    assert (tmp_path / "f.py").read_text() == "x = 2\nx = 2\n"


async def test_edit_says_to_read_when_text_is_not_found(tools, tmp_path):
    (tmp_path / "f.py").write_text("a = 1\n")
    result = await tools.call("edit", {"path": "f.py", "old_string": "b = 1", "new_string": "b = 2"})
    assert result_is_error(result)
    assert "Call read" in text(result)


async def test_edit_keeps_windows_line_endings(tools, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"alpha\r\nbeta\r\n")
    result = await tools.call("edit", {"path": "w.txt", "old_string": "alpha\nbeta", "new_string": "alpha\ngamma"})
    assert not result_is_error(result)
    assert (tmp_path / "w.txt").read_bytes() == b"alpha\r\ngamma\r\n"


async def test_ls_glob_and_grep_skip_dependency_folders(tools, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 1\n")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "mod.py").write_text("def hello(): pass\n")

    assert "pkg/" in text(await tools.call("ls", {}))

    found = text(await tools.call("glob", {"pattern": "**/*.py"}))
    assert "pkg/mod.py" in found
    assert "node_modules" not in found

    matches = text(await tools.call("grep", {"pattern": "def hello"}))
    assert "pkg/mod.py:1:" in matches
    assert "node_modules" not in matches


async def test_run_starts_in_the_workspace_and_reports_exit_code(tools, tmp_path):
    result = await tools.call("run", {"command": python("import os; print(os.getcwd())")})
    assert not result_is_error(result)
    assert text(result).startswith("exit code 0")
    assert str(tmp_path.resolve()).lower() in text(result).lower()


async def test_run_marks_a_failing_command_as_an_error(tools):
    result = await tools.call("run", {"command": "exit 3"})
    assert result_is_error(result)
    assert text(result).startswith("exit code 3")


async def test_a_long_command_becomes_a_job_instead_of_being_killed(tools):
    started = await tools.call(
        "run", {"command": python("import time; time.sleep(2); print('done')"), "wait_seconds": 0.2}
    )
    assert "still running" in text(started)
    job = re.search(r'job "(\w+)"', text(started)).group(1)

    finished = await tools.call("process", {"job": job, "wait_seconds": 20})
    assert text(finished).startswith("exit code 0")
    assert "done" in text(finished)


async def test_process_stops_a_job(tools):
    started = await tools.call("run", {"command": python("import time; time.sleep(60)"), "wait_seconds": 0.2})
    job = re.search(r'job "(\w+)"', text(started)).group(1)
    stopped = await tools.call("process", {"action": "stop", "job": job})
    assert "exit code" in text(stopped)
    assert "running" in text(await tools.call("process", {"action": "list"})) or "exit" in text(
        await tools.call("process", {"action": "list"})
    )


async def test_unknown_tool_names_the_real_ones(tools):
    result = await tools.call("execute_command", {"command": "ls"})
    assert result_is_error(result)
    assert "run" in text(result)


async def test_grep_literal_treats_regex_characters_as_text(tools, tmp_path):
    (tmp_path / "a.txt").write_text("call foo(bar)\n")
    result = await tools.call("grep", {"pattern": "foo(bar", "literal": True})
    assert not result_is_error(result)
    assert "a.txt:1:" in text(result)
