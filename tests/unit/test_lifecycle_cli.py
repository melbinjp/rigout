import json
import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from rigout.lifecycle import (
    RuntimePaths,
    default_state_dir,
    process_is_running,
    read_tail,
    redact_sensitive_text,
    runtime_status,
    write_json_secure,
    write_pid,
)
from rigout.mcp_url_launcher import (
    build_managed_child_command,
    main,
    parse_args,
    prepare_start_args,
    run_foreground,
    start_detached,
    start_server,
)


@pytest.mark.unit
def test_default_state_dir_honors_environment_override(tmp_path, monkeypatch):
    configured = tmp_path / "custom-state"
    monkeypatch.setenv("RIGOUT_STATE_DIR", str(configured))

    assert default_state_dir() == configured.resolve()


@pytest.mark.unit
def test_legacy_cloudflare_command_uses_managed_state_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGOUT_STATE_DIR", str(tmp_path))
    args = parse_args(["--tunnel", "cloudflare"])
    paths = RuntimePaths.resolve(args.state_dir)

    assert args.explicit_lifecycle is False
    assert prepare_start_args(args, paths) is True
    assert args.connection_file == str(paths.connection_file)


@pytest.mark.unit
def test_start_server_does_not_inject_source_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "user-supplied-pythonpath")
    args = Namespace(
        host="127.0.0.1",
        port=8765,
        path="/mcp",
        connection_file=str(tmp_path / "connection.json"),
        json_response=False,
        stateless=False,
        auth_token=None,
    )

    with patch("rigout.mcp_url_launcher.subprocess.Popen") as popen:
        start_server(args, runtime_cwd=tmp_path)

    assert popen.call_args.kwargs["env"]["PYTHONPATH"] == "user-supplied-pythonpath"
    assert popen.call_args.kwargs["env"]["RIGOUT_STATE_DIR"] == str(tmp_path)
    assert popen.call_args.kwargs["cwd"] == str(tmp_path)


@pytest.mark.unit
def test_managed_child_command_keeps_credentials_out_of_process_arguments(tmp_path):
    args = parse_args(
        [
            "start",
            "--detach",
            "--tunnel",
            "cloudflare",
            "--state-dir",
            str(tmp_path),
            "--auth-token",
            "bearer-secret",
            "--setup-token",
            "setup-secret",
        ]
    )
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)

    command = build_managed_child_command(args, paths)

    assert "bearer-secret" not in command
    assert "setup-secret" not in command
    assert command[:4] == [os.sys.executable, "-m", "rigout.mcp_url_launcher", "start"]


@pytest.mark.unit
def test_activity_redaction_removes_setup_and_bearer_tokens():
    line = 'GET /connection.json?setup_token=setup-secret HTTP/1.1 "Authorization: Bearer bearer-secret"\n'

    redacted = redact_sensitive_text(line)

    assert "setup-secret" not in redacted
    assert "bearer-secret" not in redacted
    assert redacted.count("<redacted>") == 2


@pytest.mark.unit
def test_runtime_status_marks_dead_pid_state_as_stale(tmp_path):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    write_pid(paths, 999_999_999)
    write_json_secure(paths.runtime_file, {"status": "running", "pid": 999_999_999, "managed": True})

    status = runtime_status(paths)

    assert status["running"] is False
    assert status["status"] == "stopped"
    assert status["stale_state"] is True


@pytest.mark.unit
def test_log_tail_is_bounded_to_requested_lines(tmp_path):
    log = tmp_path / "activity.log"
    log.write_text("".join(f"line-{index}\n" for index in range(20)), encoding="utf-8")

    assert read_tail(log, 3) == ["line-17", "line-18", "line-19"]


@pytest.mark.unit
def test_log_tail_rejects_unbounded_cli_request():
    with pytest.raises(SystemExit):
        parse_args(["logs", "--tail", "10001"])


@pytest.mark.unit
def test_status_json_is_one_parseable_object(tmp_path, capsys):
    exit_code = main(["status", "--state-dir", str(tmp_path), "--output", "json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "stopped"
    assert output["running"] is False
    assert output["connection_file"] == str((tmp_path / "connection.json").resolve())


@pytest.mark.unit
def test_foreground_start_refuses_existing_instance_without_touching_state(tmp_path, capsys):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    paths.pid_file.write_text("24680\n", encoding="utf-8")
    paths.runtime_file.write_text('{"status":"running","sentinel":"keep"}\n', encoding="utf-8")
    paths.log_file.write_text("existing log\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (paths.pid_file, paths.runtime_file, paths.log_file)}
    args = parse_args(["--state-dir", str(tmp_path), "--port", "18768"])
    prepare_start_args(args, paths)

    with patch(
        "rigout.mcp_url_launcher.runtime_status",
        return_value={"status": "running", "running": True, "pid": 24680},
    ):
        exit_code = run_foreground(args, paths, managed=True)

    assert exit_code == 1
    assert "already running with PID 24680" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.unit
def test_detached_child_does_not_refuse_parent_reserved_pid(tmp_path, monkeypatch):
    """The parent reserves lifecycle state before its managed child starts."""
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    monkeypatch.setenv("RIGOUT_DETACHED_CHILD", "1")
    already_reserved = {
        "status": "running",
        "running": True,
        "pid": 12345,
        "connection_file": str(paths.connection_file),
        "activity_log": str(paths.log_file),
    }

    with (
        patch("rigout.mcp_url_launcher.runtime_status", return_value=already_reserved),
        patch("rigout.mcp_url_launcher.start_server", side_effect=RuntimeError("child reached startup")) as start,
    ):
        exit_code = run_foreground(args, paths, managed=True)

    assert exit_code == 1
    start.assert_called_once()


class FakeDetachedProcess:
    """Minimal running Popen result for deterministic startup tests."""

    pid = 43210
    returncode = None

    def poll(self):
        return None


@pytest.mark.unit
def test_detached_json_handoff_is_credential_free(tmp_path, capsys):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    args = parse_args(
        [
            "start",
            "--detach",
            "--tunnel",
            "cloudflare",
            "--state-dir",
            str(tmp_path),
            "--output",
            "json",
            "--auth-token",
            "bearer-secret",
            "--setup-token",
            "setup-secret",
        ]
    )
    prepare_start_args(args, paths)
    Path(args.connection_file).write_text(
        json.dumps(
            {
                "agent_setup_url": "https://agent.example/connection.json?setup_token=setup-secret",
                "mcp": {
                    "transport": "streamable-http",
                    "url": "https://agent.example/mcp",
                    "health_url": "https://agent.example/health",
                    "headers": {"Authorization": "Bearer bearer-secret"},
                },
            }
        ),
        encoding="utf-8",
    )
    stopped = {
        "status": "stopped",
        "pid": None,
        "running": False,
        "state_dir": str(paths.root),
        "connection_file": str(paths.connection_file),
        "activity_log": str(paths.log_file),
    }
    running = {
        **stopped,
        "status": "running",
        # Windows venv launchers can return a redirector PID while the managed
        # Python child records a different OS PID.
        "pid": 98765,
        "running": True,
        "instance_id": "test-instance",
    }

    with (
        patch("rigout.mcp_url_launcher.launch_detached", return_value=FakeDetachedProcess()) as launch,
        patch("rigout.mcp_url_launcher.secrets.token_urlsafe", return_value="test-instance"),
        patch("rigout.mcp_url_launcher.runtime_status", side_effect=[stopped, running]),
    ):
        exit_code = start_detached(args, paths)

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert exit_code == 0
    assert output["mcp_url"] == "https://agent.example/mcp"
    assert "setup-secret" not in output_text
    assert "bearer-secret" not in output_text
    assert output["pid"] == 98765
    command = launch.call_args.args[0]
    env = launch.call_args.args[2]
    assert "setup-secret" not in command
    assert "bearer-secret" not in command
    assert env["RIGOUT_SETUP_TOKEN"] == "setup-secret"
    assert env["RIGOUT_AUTH_TOKEN"] == "bearer-secret"
    assert env["RIGOUT_INSTANCE_ID"] == "test-instance"


@pytest.mark.unit
def test_process_check_rejects_nonexistent_pid():
    assert process_is_running(999_999_999) is False


@pytest.mark.unit
def test_stop_refuses_live_pid_with_mismatched_process_identity(tmp_path, capsys):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    write_pid(paths, os.getpid())
    write_json_secure(
        paths.runtime_file,
        {
            "status": "running",
            "pid": os.getpid(),
            "managed": True,
            "process_identity": "identity-from-an-old-process",
        },
    )

    exit_code = main(["stop", "--state-dir", str(tmp_path), "--output", "json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert "Refusing to stop" in output["error"]
    assert process_is_running(os.getpid()) is True
