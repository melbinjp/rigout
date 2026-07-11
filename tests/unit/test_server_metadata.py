import contextlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rigout import __version__, _version
from rigout.server import main, server


@pytest.mark.unit
def test_server_advertises_package_version():
    assert __version__ == "0.2.0"
    assert server.version == __version__


@pytest.mark.unit
def test_source_checkout_version_wins_over_stale_distribution_metadata():
    with patch.object(_version, "distribution_version", return_value="0.1.0"):
        assert _version.resolve_version() == "0.2.0"


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
