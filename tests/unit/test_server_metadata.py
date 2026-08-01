import ast
import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rigout import __version__, _version
from rigout import server as rigout_server
from rigout.server import handle_list_tools, main, server


@pytest.mark.unit
def test_server_advertises_package_version():
    assert __version__ == "0.3.1"
    assert server.version == __version__


@pytest.mark.unit
def test_source_checkout_version_wins_over_stale_distribution_metadata():
    with patch.object(_version, "distribution_version", return_value="0.1.0"):
        assert _version.resolve_version() == "0.3.1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stdio_initialization_advertises_package_version():
    read_stream = object()
    write_stream = object()

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        yield read_stream, write_stream

    run = AsyncMock()
    with patch("rigout.server.stdio_server", fake_stdio_server), patch.object(server, "run", run):
        await main()

    options = run.await_args.args[2]
    assert options.server_version == __version__


def _dispatched_tool_names() -> set[str]:
    """Tool names the dispatch chain in `_dispatch_tool` actually handles.

    Read from the source with `ast` rather than by executing handlers, so this stays
    accurate when the chain moves and never runs a real tool to find out.
    """
    source_file = Path(rigout_server.__file__)
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    dispatcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "_dispatch_tool"
    )

    names: set[str] = set()
    for node in ast.walk(dispatcher):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "name" or not isinstance(node.ops[0], ast.Eq):
            continue
        target = node.comparators[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            names.add(target.value)
    return names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_advertised_tools_and_dispatch_chain_agree():
    """Every advertised tool is dispatched, and nothing is dispatched that is not advertised.

    The tool list and the if/elif chain are two hand-maintained copies of the same names
    with nothing linking them. Adding to one and forgetting the other yields a runtime
    "Unknown tool" that no type check or existing test would catch.
    """
    advertised = {tool.name for tool in await handle_list_tools()}
    dispatched = _dispatched_tool_names()

    assert advertised, "handle_list_tools returned no tools; the parse below cannot be trusted"
    assert dispatched, "no dispatch branches found; _dispatch_tool may have been restructured"

    assert advertised - dispatched == set(), (
        f"advertised but never dispatched, so calling these returns 'Unknown tool': {sorted(advertised - dispatched)}"
    )
    assert dispatched - advertised == set(), (
        f"dispatched but not advertised, so no client can discover these: {sorted(dispatched - advertised)}"
    )


@pytest.mark.unit
def test_import_does_not_create_a_cwd_log_file(tmp_path):
    source_root = Path(__file__).resolve().parents[2] / "src"
    subprocess.run(
        [sys.executable, "-c", "import rigout.server"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert not (tmp_path / "mcp-hardware-server.log").exists()


def hints(tool):
    """Annotation hints by their wire names, on either mcp major.

    2.x renamed the attributes to snake_case while keeping the camelCase spellings as
    construction aliases, so reading `.readOnlyHint` off the model works on 1.x and
    raises on 2.x. Dumping by alias gives the names the protocol actually uses, which
    are the same on both.
    """
    return tool.annotations.model_dump(by_alias=True)


@pytest.mark.unit
class TestToolAnnotations:
    """Every tool must say what it does to the machine before a client runs it.

    Rigout spans the whole range - a tool that reads a CPU count and a tool that runs
    arbitrary commands as root - and advertised them identically until now, leaving each
    client to guess which was which.
    """

    @staticmethod
    def _tools():
        return asyncio.run(rigout_server.handle_list_tools())

    def test_every_advertised_tool_is_classified(self):
        """A tool added without an entry ships unclassified, which is the failure this
        test exists to prevent; the table is easy to forget and invisible when missed."""
        missing = [t.name for t in self._tools() if t.annotations is None]

        assert not missing, f"tools with no annotations: {missing}"

    def test_every_tool_has_a_human_readable_title(self):
        assert all(t.title for t in self._tools())

    def test_the_table_has_no_entries_for_tools_that_do_not_exist(self):
        """A rename that updates one place and not the other leaves a dead entry and an
        unclassified tool, and both are silent."""
        advertised = {t.name for t in self._tools()}

        assert set(rigout_server.TOOL_ANNOTATIONS) == advertised

    @pytest.mark.parametrize(
        "name",
        [
            "execute_command",
            "execute_in_terminal",
            "install_software",
            "file_operations",
            "docker_operations",
            "bulk_file_transfer",
            "environment_setup",
            "manage_tunnels",
        ],
    )
    def test_tools_that_change_the_machine_are_marked_destructive(self, name):
        tool = next(t for t in self._tools() if t.name == name)

        assert hints(tool)["readOnlyHint"] is False
        assert hints(tool)["destructiveHint"] is True

    @pytest.mark.parametrize(
        "name", ["get_hardware_info", "get_server_activity", "system_monitoring", "list_terminal_sessions"]
    )
    def test_tools_that_only_look_are_marked_read_only(self, name):
        tool = next(t for t in self._tools() if t.name == name)

        assert hints(tool)["readOnlyHint"] is True
        assert hints(tool)["destructiveHint"] is False

    def test_anything_running_a_callers_command_is_not_idempotent(self):
        """What such a tool does is decided by the caller and cannot be known here, so
        claiming a repeat call changes nothing would be a guess stated as a fact."""
        for name in ("execute_command", "execute_in_terminal"):
            tool = next(t for t in self._tools() if t.name == name)
            assert hints(tool)["idempotentHint"] is False

    def test_read_only_tools_are_never_also_destructive(self):
        """The two contradict each other, and a client reading either alone would be
        told something untrue."""
        for tool in self._tools():
            if hints(tool)["readOnlyHint"]:
                assert hints(tool)["destructiveHint"] is False, tool.name
