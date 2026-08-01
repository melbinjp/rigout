import ast
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
    assert __version__ == "0.3.0"
    assert server.version == __version__


@pytest.mark.unit
def test_source_checkout_version_wins_over_stale_distribution_metadata():
    with patch.object(_version, "distribution_version", return_value="0.1.0"):
        assert _version.resolve_version() == "0.3.0"


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
    """Tool names the dispatch chain in `_handle_call_tool_result` actually handles.

    Read from the source with `ast` rather than by executing handlers, so this stays
    accurate when the chain moves and never runs a real tool to find out.
    """
    source_file = Path(rigout_server.__file__)
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    dispatcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name == "_handle_call_tool_result"
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
    assert dispatched, "no dispatch branches found; _handle_call_tool_result may have been restructured"

    assert advertised - dispatched == set(), (
        "advertised but never dispatched, so calling these returns 'Unknown tool': "
        f"{sorted(advertised - dispatched)}"
    )
    assert dispatched - advertised == set(), (
        "dispatched but not advertised, so no client can discover these: "
        f"{sorted(dispatched - advertised)}"
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
