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
    assert "bypass" not in json.dumps([tool.model_dump(by_alias=True)["inputSchema"] for tool in definitions])


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


async def test_read_streams_a_large_file_and_numbers_lines_correctly(tools, tmp_path):
    (tmp_path / "big.log").write_text("".join(f"line {n}\n" for n in range(1, 50_001)))
    result = await tools.call("read", {"path": "big.log", "offset": 49_999, "limit": 5})
    assert not result_is_error(result)
    assert text(result).startswith("49999\tline 49999")
    assert "50000\tline 50000" in text(result)

    middle = await tools.call("read", {"path": "big.log", "offset": 10, "limit": 2})
    assert "of 50000" in text(middle)


async def test_edit_refuses_a_file_too_large_to_hold_in_memory(tools, tmp_path, monkeypatch):
    monkeypatch.setattr("rigout.tools.EDIT_MAX_BYTES", 10)
    (tmp_path / "big.txt").write_text("x = 1\n" * 10)
    result = await tools.call(
        "edit", {"path": "big.txt", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}
    )
    assert result_is_error(result)
    assert "run" in text(result)
    assert (tmp_path / "big.txt").read_text() == "x = 1\n" * 10


async def test_errors_say_what_to_call_next(tools):
    for name, arguments in [
        ("ls", {"path": "nope"}),
        ("run", {"command": "echo hi", "cwd": "nope"}),
        ("glob", {"pattern": "*", "path": "nope"}),
        ("grep", {"pattern": "x", "path": "nope"}),
    ]:
        result = await tools.call(name, arguments)
        assert result_is_error(result), name
        assert "Call " in text(result), name


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot contain a colon")
async def test_grep_keeps_a_file_name_with_a_colon(tools, tmp_path):
    (tmp_path / "a:b.txt").write_text("needle\n")
    result = await tools.call("grep", {"pattern": "needle"})
    assert "a:b.txt:1:needle" in text(result)


async def test_grep_stops_at_the_match_limit(tools, tmp_path, monkeypatch):
    monkeypatch.setattr("rigout.tools.GREP_LIMIT", 5)
    (tmp_path / "many.txt").write_text("hit\n" * 100)
    result = await tools.call("grep", {"pattern": "hit"})
    assert "first 5 matches shown" in text(result)


async def test_grep_on_a_single_file_names_the_file(tools, tmp_path):
    (tmp_path / "one.txt").write_text("alpha\nneedle\n")
    result = await tools.call("grep", {"pattern": "needle", "path": "one.txt"})
    assert "one.txt:2:needle" in text(result)


async def test_glob_matches_like_pathlib(tools, tmp_path):
    (tmp_path / "top.py").write_text("")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "test_one.py").write_text("")
    (tmp_path / "src" / "pkg" / "one.py").write_text("")

    top = text(await tools.call("glob", {"pattern": "*.py"}))
    assert top.split() == ["top.py"]

    everywhere = set(text(await tools.call("glob", {"pattern": "**/*.py"})).split())
    assert everywhere == {"top.py", "src/pkg/test_one.py", "src/pkg/one.py"}

    tests_only = text(await tools.call("glob", {"pattern": "src/**/test_*.py"})).split()
    assert tests_only == ["src/pkg/test_one.py"]


async def test_glob_never_walks_into_dependency_folders(tools, tmp_path, monkeypatch):
    import os as real_os

    real_walk = real_os.walk  # kept before patching: rigout.tools.os is this same module

    (tmp_path / "node_modules" / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "node_modules" / "deep" / "deeper" / "x.py").write_text("")
    (tmp_path / "app.py").write_text("")
    visited = []

    def spying_walk(top, *args, **kwargs):
        for folder, dirs, names in real_walk(top, *args, **kwargs):
            visited.append(folder)
            yield folder, dirs, names

    monkeypatch.setattr("rigout.tools.os.walk", spying_walk)
    result = text(await tools.call("glob", {"pattern": "**/*.py"}))
    assert result.split() == ["app.py"]
    assert not any("node_modules" in folder for folder in visited)


async def test_glob_keeps_a_leading_dot_folder_after_dot_slash(tools, tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("")
    result = text(await tools.call("glob", {"pattern": "./.github/*.yml"}))
    assert result.split() == [".github/ci.yml"]


def test_glob_bracket_classes_follow_fnmatch():
    from rigout.tools import _glob_regex

    assert _glob_regex("[]]").match("]")
    assert not _glob_regex("[]]").match("a")
    assert _glob_regex("[^a]").match("^")
    assert _glob_regex("[^a]").match("a")
    assert not _glob_regex("[^a]").match("b")
    assert _glob_regex("[!a]").match("b")
    assert not _glob_regex("[!a]").match("a")
    assert _glob_regex("[!]]").match("a")
    assert not _glob_regex("[!]]").match("]")


async def test_glob_invalid_pattern_says_how_to_fix_it(tools, tmp_path):
    (tmp_path / "a.txt").write_text("")
    result = await tools.call("glob", {"pattern": "[z-a]"})
    assert result_is_error(result)
    assert text(result).startswith("glob:")


def test_read_only_and_destructive_split():
    """Four read, four change the machine. The README states this split; a tool whose hint changes
    fails here, which is the prompt to update that sentence too."""
    definitions = tool_definitions()
    # Read from the wire names, which both mcp majors send; 2.x renamed the Python attributes.
    hints = {t.name: t.model_dump(by_alias=True)["annotations"] for t in definitions}
    assert sorted(n for n, h in hints.items() if h["readOnlyHint"]) == ["glob", "grep", "ls", "read"]
    assert sorted(n for n, h in hints.items() if h["destructiveHint"]) == ["edit", "process", "run", "write"]
