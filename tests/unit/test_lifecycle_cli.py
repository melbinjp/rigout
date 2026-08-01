import json
import os
import signal
import stat
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from rigout import lifecycle
from rigout.lifecycle import (
    RuntimePaths,
    default_state_dir,
    process_is_running,
    read_tail,
    redact_home_path,
    redact_sensitive_text,
    runtime_status,
    secure_descriptor,
    secure_directory,
    terminate_process,
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


class FakeChildProcess:
    """Stand-in for a spawned launcher child (uvicorn or cloudflared)."""

    def __init__(self, pid: int):
        self.pid = pid
        self.stdout = iter(())
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.mark.unit
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", ""])
def test_foreground_start_authenticates_a_non_loopback_bind(tmp_path, host):
    """A LAN-reachable bind is public even without a tunnel, so it needs a bearer token."""
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path), "--host", host])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)

    with patch("rigout.mcp_url_launcher.start_server", side_effect=RuntimeError("startup stops here")):
        exit_code = run_foreground(args, paths, managed=True)

    assert exit_code == 1
    assert args.auth_token


@pytest.mark.unit
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.53", "127.1.2.3"])
def test_foreground_start_leaves_a_loopback_bind_unauthenticated(tmp_path, host):
    """All of 127.0.0.0/8 is loopback, not just 127.0.0.1."""
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path), "--host", host])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)

    with patch("rigout.mcp_url_launcher.start_server", side_effect=RuntimeError("startup stops here")):
        exit_code = run_foreground(args, paths, managed=True)

    assert exit_code == 1
    assert args.auth_token is None


@pytest.mark.unit
def test_foreground_start_honors_no_auth_opt_out_but_says_so(tmp_path, capsys):
    """--no-auth stays an honest opt-out on the new path, and must not disable auth silently."""
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path), "--host", "0.0.0.0", "--no-auth"])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)

    with patch("rigout.mcp_url_launcher.start_server", side_effect=RuntimeError("startup stops here")):
        run_foreground(args, paths, managed=True)

    assert args.auth_token is None
    assert "--no-auth disables bearer authentication" in capsys.readouterr().err


@pytest.mark.unit
def test_detached_start_warns_when_no_auth_exposes_a_non_loopback_bind(tmp_path, capsys):
    with patch("rigout.mcp_url_launcher.start_detached", return_value=0):
        main(["start", "--detach", "--state-dir", str(tmp_path), "--host", "0.0.0.0", "--no-auth"])

    assert "--no-auth disables bearer authentication" in capsys.readouterr().err


@pytest.mark.unit
def test_loopback_start_does_not_warn_about_no_auth(tmp_path, capsys):
    with patch("rigout.mcp_url_launcher.start_detached", return_value=0):
        main(["start", "--detach", "--state-dir", str(tmp_path), "--host", "127.0.0.1", "--no-auth"])

    assert capsys.readouterr().err == ""


@pytest.mark.unit
def test_explicit_auth_token_is_not_reported_as_unauthenticated(tmp_path, capsys):
    """--no-auth alongside an explicit token still leaves the server authenticated."""
    with patch("rigout.mcp_url_launcher.start_detached", return_value=0):
        main(
            [
                "start",
                "--detach",
                "--state-dir",
                str(tmp_path),
                "--host",
                "0.0.0.0",
                "--no-auth",
                "--auth-token",
                "explicit-token",
            ]
        )

    assert capsys.readouterr().err == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "expects_token"),
    [
        ("0.0.0.0", True),
        ("::", True),
        ("192.168.1.10", True),
        ("127.0.0.1", False),
        ("localhost", False),
        ("127.0.0.53", False),
    ],
)
def test_detached_start_authenticates_a_non_loopback_bind(tmp_path, host, expects_token):
    """The detach path duplicates the public-mode decision and must agree with the foreground one."""
    captured = {}

    def capture(args, _paths):
        captured["auth_token"] = args.auth_token
        return 0

    with patch("rigout.mcp_url_launcher.start_detached", side_effect=capture):
        exit_code = main(["start", "--detach", "--state-dir", str(tmp_path), "--host", host])

    assert exit_code == 0
    assert bool(captured["auth_token"]) is expects_token


@pytest.mark.unit
def test_managed_start_records_child_pids_for_recovery(tmp_path):
    """A launcher that dies without its handler must still leave its children findable."""
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path), "--tunnel", "cloudflare"])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    server = FakeChildProcess(31415)
    tunnel = FakeChildProcess(27182)

    with (
        patch(
            "rigout.mcp_url_launcher.start_cloudflare_tunnel",
            return_value=(tunnel, "https://example.trycloudflare.com"),
        ),
        patch("rigout.mcp_url_launcher.start_server", return_value=server),
        patch("rigout.mcp_url_launcher.wait_for_health", return_value=False),
    ):
        exit_code = run_foreground(args, paths, managed=True)

    runtime = json.loads(paths.runtime_file.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert runtime["server_pid"] == 31415
    assert runtime["tunnel_pid"] == 27182


@pytest.mark.unit
def test_stop_reaps_children_recorded_by_a_crashed_launcher(tmp_path, capsys):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    write_json_secure(
        paths.runtime_file,
        {
            "status": "running",
            "managed": True,
            "server_pid": 31415,
            "server_process_identity": "server-identity",
            "tunnel_pid": 27182,
            "tunnel_process_identity": "tunnel-identity",
        },
    )

    with (
        patch("rigout.mcp_url_launcher.process_is_running", return_value=True),
        patch("rigout.mcp_url_launcher.process_matches_identity", return_value=True),
        patch("rigout.mcp_url_launcher.terminate_process", return_value=True) as terminate,
    ):
        exit_code = main(["stop", "--state-dir", str(tmp_path), "--output", "json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert sorted(output["stopped_children"]) == [27182, 31415]
    assert {call.args[0] for call in terminate.call_args_list} == {27182, 31415}


@pytest.mark.unit
def test_stop_refuses_recorded_child_pid_that_was_recycled(tmp_path):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    write_json_secure(
        paths.runtime_file,
        {"status": "running", "managed": True, "server_pid": 31415, "server_process_identity": "identity-from-a-child"},
    )

    with (
        patch("rigout.mcp_url_launcher.process_is_running", return_value=True),
        patch("rigout.mcp_url_launcher.process_matches_identity", return_value=False),
        patch("rigout.mcp_url_launcher.terminate_process", return_value=True) as terminate,
    ):
        exit_code = main(["stop", "--state-dir", str(tmp_path)])

    assert exit_code == 0
    terminate.assert_not_called()


@pytest.mark.unit
def test_terminate_escalates_to_the_process_group_on_posix():
    """A detached launcher leads its own group, so SIGKILL must reach its whole tree."""
    group_killed = {"value": False}
    expected_signal = getattr(signal, "SIGKILL", signal.SIGTERM)

    def kill_group(_pid, _signal_number):
        group_killed["value"] = True

    with (
        patch("rigout.lifecycle.os.name", "posix"),
        patch("rigout.lifecycle.process_is_running", side_effect=lambda _pid: not group_killed["value"]),
        patch("rigout.lifecycle.os.kill") as kill,
        patch("rigout.lifecycle.os.getpgid", return_value=4242, create=True),
        patch("rigout.lifecycle.os.killpg", side_effect=kill_group, create=True) as killpg,
    ):
        stopped = terminate_process(4242, timeout=0.01)

    assert stopped is True
    assert killpg.call_args.args == (4242, expected_signal)
    # Only the initial SIGTERM goes to the single PID; the escalation goes to the group
    assert kill.call_args_list == [((4242, signal.SIGTERM),)]


@pytest.mark.unit
def test_terminate_does_not_signal_a_process_group_it_does_not_lead():
    """A foreground launcher shares the caller's group, which must never be signalled."""
    expected_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    kill_calls: list[tuple[int, int]] = []

    with (
        patch("rigout.lifecycle.os.name", "posix"),
        patch("rigout.lifecycle.process_is_running", side_effect=lambda _pid: len(kill_calls) < 2),
        patch("rigout.lifecycle.os.kill", side_effect=lambda pid, number: kill_calls.append((pid, number))),
        patch("rigout.lifecycle.os.getpgid", return_value=99, create=True),
        patch("rigout.lifecycle.os.killpg", create=True) as killpg,
    ):
        terminate_process(4242, timeout=0.01)

    killpg.assert_not_called()
    assert kill_calls == [(4242, signal.SIGTERM), (4242, expected_signal)]


@pytest.mark.unit
def test_secure_directory_tightens_only_a_directory_rigout_created(tmp_path):
    """A fresh per-user state directory is still made owner-only."""
    fresh = tmp_path / "state"

    with patch("rigout.lifecycle.os.name", "posix"), patch.object(Path, "chmod") as chmod:
        secure_directory(fresh)

    assert fresh.is_dir()
    assert chmod.call_args.args == (stat.S_IRWXU,)


@pytest.mark.unit
def test_secure_directory_leaves_a_shared_directory_alone(tmp_path, capsys):
    """`--state-dir /tmp` must not reset the permissions of a directory everyone shares."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared_mode = stat.S_IFDIR | 0o777
    lifecycle._SHARED_DIRECTORY_WARNINGS.discard(str(shared))

    with (
        patch("rigout.lifecycle.os.name", "posix"),
        patch("rigout.lifecycle.os.stat", return_value=os.stat_result((shared_mode,) + (0,) * 9)),
        patch.object(Path, "chmod") as chmod,
    ):
        secure_directory(shared)

    chmod.assert_not_called()
    warning = capsys.readouterr().err
    assert "reachable by other users" in warning
    assert "drwxrwxrwx" in warning


@pytest.mark.unit
def test_secure_directory_stays_quiet_about_an_existing_owner_only_directory(tmp_path, capsys):
    """The normal case, a state directory from an earlier run, must not nag the operator."""
    existing = tmp_path / "state"
    existing.mkdir()
    lifecycle._SHARED_DIRECTORY_WARNINGS.discard(str(existing))

    with (
        patch("rigout.lifecycle.os.name", "posix"),
        patch("rigout.lifecycle.os.stat", return_value=os.stat_result((stat.S_IFDIR | 0o700,) + (0,) * 9)),
        patch.object(Path, "chmod") as chmod,
    ):
        secure_directory(existing)

    chmod.assert_not_called()
    assert capsys.readouterr().err == ""


@pytest.mark.unit
def test_secure_descriptor_applies_owner_only_mode():
    """Runtime files carrying credentials are restricted before they are published."""
    with (
        patch("rigout.lifecycle.os.name", "posix"),
        patch("rigout.lifecycle.os.fchmod", create=True) as fchmod,
    ):
        secure_descriptor(7)

    assert fchmod.call_args.args == (7, stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.unit
def test_redact_home_path_hides_the_account_name_without_touching_other_paths():
    home = str(Path.home())

    with patch.object(Path, "home", return_value=Path(home)):
        assert redact_home_path(f"{home}{os.sep}rigout{os.sep}state") == f"~{os.sep}rigout{os.sep}state"
        assert redact_home_path(f"{home}-other{os.sep}state") == f"{home}-other{os.sep}state"
        assert redact_home_path(f"{os.sep}srv{os.sep}rigout") == f"{os.sep}srv{os.sep}rigout"


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
