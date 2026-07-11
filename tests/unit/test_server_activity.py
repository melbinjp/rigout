import json
import os

import pytest

from rigout.lifecycle import RuntimePaths, append_activity, process_identity, write_json_secure, write_pid
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

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert set(payload) == {"status", "running", "pid", "state_dir", "activity_log", "lines"}
    assert payload["status"] == "running"
    assert payload["running"] is True
    assert payload["pid"] == os.getpid()
    assert payload["state_dir"] == str(paths.root)
    assert payload["activity_log"] == str(paths.log_file)
    assert payload["lines"][-1] == "ready"
    serialized = json.dumps(payload)
    assert "secret-setup" not in serialized
    assert "secret-bearer" not in serialized
    assert "secret-password" not in serialized


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("line_count", [0, MAX_ACTIVITY_LINES + 1, "10", True])
async def test_server_activity_rejects_unbounded_or_invalid_line_counts(line_count):
    result = await handle_get_server_activity({"lines": line_count})

    assert result.isError is True
    assert "lines argument" in result.content[0].text
