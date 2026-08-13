import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from rigout.lifecycle import (
    RuntimePaths,
    append_activity,
    process_identity,
    redact_home_path,
    write_json_secure,
    write_pid,
)
from rigout.tools._results import result_is_error
from rigout.tools.activity import MAX_ACTIVITY_LINES, handle_get_server_activity


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_activity_returns_bounded_sanitized_json(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("RIGOUT_STATE_DIR", str(state_dir))
    paths = RuntimePaths.resolve()
    paths.prepare()
    write_pid(paths, os.getpid())
    write_json_secure(
        paths.runtime_file,
        {
            "status": "running",
            "pid": os.getpid(),
            "process_identity": process_identity(os.getpid()),
        },
    )
    append_activity(
        paths,
        "setup=https://example.test/connection.json?setup_token=secret-setup\n"
        "Authorization: Bearer secret-bearer\n"
        "password=secret-password\n"
        "ready\n",
    )

    result = await handle_get_server_activity({"lines": 3})

    assert result_is_error(result) is False
    payload = json.loads(result.content[0].text)
    assert set(payload) == {"status", "running", "pid", "state_dir", "activity_log", "lines"}
    assert payload["status"] == "running"
    assert payload["running"] is True
    assert payload["pid"] == os.getpid()
    assert payload["state_dir"] == redact_home_path(str(paths.root))
    assert payload["activity_log"] == redact_home_path(str(paths.log_file))
    assert payload["lines"][-1] == "ready"
    serialized = json.dumps(payload)
    assert "secret-setup" not in serialized
    assert "secret-bearer" not in serialized
    assert "secret-password" not in serialized


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_activity_does_not_leak_the_operator_home_directory(tmp_path, monkeypatch):
    """Runtime paths reach a remote agent, so they must not name the OS account."""
    home = tmp_path.resolve()
    state_dir = home / "state"
    monkeypatch.setenv("RIGOUT_STATE_DIR", str(state_dir))
    paths = RuntimePaths.resolve()
    paths.prepare()
    append_activity(paths, "ready\n")

    with patch.object(Path, "home", return_value=home):
        result = await handle_get_server_activity({"lines": 1})

    payload = json.loads(result.content[0].text)
    assert payload["state_dir"] == f"~{os.sep}state"
    assert payload["activity_log"] == f"~{os.sep}state{os.sep}activity.log"
    assert str(home) not in json.dumps(payload)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("line_count", [0, MAX_ACTIVITY_LINES + 1, "10", True])
async def test_server_activity_rejects_unbounded_or_invalid_line_counts(line_count):
    result = await handle_get_server_activity({"lines": line_count})

    assert result_is_error(result) is True
    assert "lines argument" in result.content[0].text
