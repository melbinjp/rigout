import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from rigout import lifecycle
from rigout._version import __version__
from rigout.lifecycle import (
    RuntimePaths,
    default_state_dir,
    process_identity,
    process_is_running,
    ps_identity,
    read_pid,
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
    STARTUP_INTERRUPTED,
    StartupInterruptedError,
    build_managed_child_command,
    download_file,
    main,
    parse_args,
    prepare_start_args,
    print_start_result,
    run_foreground,
    runtime_metadata,
    start_detached,
    start_server,
    wait_for_health,
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
    """The parent reserves lifecycle state before its managed child starts.

    The reservation reads as running, so the child has to tell its own parent's
    reservation apart from another instance's: it does that by the instance id its parent
    put in the record and passed down in the environment.
    """
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    monkeypatch.setenv("RIGOUT_DETACHED_CHILD", "1")
    monkeypatch.setenv("RIGOUT_INSTANCE_ID", "the-parent-reservation")
    already_reserved = {
        "status": "running",
        "running": True,
        "pid": 12345,
        "instance_id": "the-parent-reservation",
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

    # Deliberately not this process's PID: a Windows venv launcher can return a
    # redirector PID while the managed Python child records a different OS PID.
    pid = 43210
    returncode = None

    def poll(self):
        return None


class FakeManagedChild(FakeDetachedProcess):
    """A launched child that publishes its own running record, as run_foreground does.

    The parent polls `poll()` once per pass, so publishing on the first call reproduces
    the real handover deterministically: the parent's reservation is on disk first, then
    the child replaces it with its own verified PID and identity.
    """

    def __init__(self, paths, instance_id, extra=None):
        self.paths = paths
        self.instance_id = instance_id
        self.extra = extra or {}
        self.published = False

    def poll(self):
        if not self.published:
            self.published = True
            write_json_secure(
                self.paths.runtime_file,
                {
                    "status": "running",
                    "managed": True,
                    "pid": os.getpid(),
                    "process_identity": process_identity(os.getpid()),
                    "instance_id": self.instance_id,
                    **self.extra,
                },
            )
            write_pid(self.paths, os.getpid())
        return None


@pytest.fixture
def live_foreign_process():
    """A real, unrelated OS process, for tests about PIDs Rigout does not own."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield process
    finally:
        process.kill()
        process.wait(timeout=10)


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
    child = FakeManagedChild(paths, "test-instance", {"connection_file": str(paths.connection_file)})

    with (
        patch("rigout.mcp_url_launcher.launch_detached", return_value=child) as launch,
        patch("rigout.mcp_url_launcher.secrets.token_urlsafe", return_value="test-instance"),
    ):
        exit_code = start_detached(args, paths)

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert exit_code == 0
    assert output["mcp_url"] == "https://agent.example/mcp"
    assert "setup-secret" not in output_text
    assert "bearer-secret" not in output_text
    # The PID reported is the one the child recorded, not the launcher handle's.
    assert output["pid"] == os.getpid() != FakeDetachedProcess.pid
    command = launch.call_args.args[0]
    env = launch.call_args.args[2]
    assert "setup-secret" not in command
    assert "bearer-secret" not in command
    assert env["RIGOUT_SETUP_TOKEN"] == "setup-secret"
    assert env["RIGOUT_AUTH_TOKEN"] == "bearer-secret"
    assert env["RIGOUT_INSTANCE_ID"] == "test-instance"


def wedged_reservation(paths, args, foreign_pid):
    """Write the state start_detached leaves if its parent dies before the child reports.

    This is the record start_detached builds at the top of its reservation, and the PID
    file it writes one step later, so the fixture cannot drift away from the source.
    """
    write_json_secure(
        paths.runtime_file,
        runtime_metadata(args, paths, status="starting", pid=0, instance_id="abandoned-instance"),
    )
    write_pid(paths, foreign_pid)


@pytest.mark.unit
def test_status_does_not_trust_a_live_process_at_an_unverified_reserved_pid(tmp_path, live_foreign_process):
    """A pending reservation plus a recycled PID must not read as a running Rigout.

    Trusting any live process at a reserved PID wedges the whole CLI: status reports
    starting, stop refuses to touch the PID, and start refuses to launch, with no
    supported command left that can clear it.
    """
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    wedged_reservation(paths, args, live_foreign_process.pid)

    status = runtime_status(paths)

    assert process_is_running(live_foreign_process.pid) is True
    assert status["running"] is False
    assert status["status"] == "stopped"
    assert status["stale_state"] is True


@pytest.mark.unit
def test_detached_start_is_not_blocked_by_an_abandoned_reservation(tmp_path, live_foreign_process, capsys):
    """start must be able to recover the state directory an abandoned reservation left."""
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    wedged_reservation(paths, args, live_foreign_process.pid)
    child = FakeManagedChild(paths, "fresh-instance")

    with (
        patch("rigout.mcp_url_launcher.launch_detached", return_value=child) as launch,
        patch("rigout.mcp_url_launcher.secrets.token_urlsafe", return_value="fresh-instance"),
    ):
        exit_code = start_detached(args, paths)

    assert "already running" not in capsys.readouterr().err
    assert exit_code == 0
    launch.assert_called_once()


@pytest.mark.unit
def test_the_detached_reservation_is_verifiable_while_the_child_starts(tmp_path, live_foreign_process):
    """Removing the "trust any live PID" path must not remove the protection it provided.

    Between the launch and the child's first report, the reservation still has to read as
    running so a second start is refused. The launched child's own creation fingerprint
    now carries that, so the same window is protected and every claim in it is checkable.
    """
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    child = FakeManagedChild(paths, "fresh-instance")
    child.pid = live_foreign_process.pid  # a real live process stands in for the child
    reported = child.poll
    seen = {}

    def snapshot_then_report():
        if not child.published:
            seen["runtime"] = json.loads(paths.runtime_file.read_text(encoding="utf-8"))
            seen["pid_file"] = read_pid(paths)
            seen["status"] = runtime_status(paths)
        return reported()

    child.poll = snapshot_then_report

    with (
        patch("rigout.mcp_url_launcher.launch_detached", return_value=child),
        patch("rigout.mcp_url_launcher.secrets.token_urlsafe", return_value="fresh-instance"),
    ):
        exit_code = start_detached(args, paths)

    assert exit_code == 0
    assert seen["pid_file"] == live_foreign_process.pid
    assert seen["runtime"]["process_identity"] == process_identity(live_foreign_process.pid)
    assert seen["runtime"]["process_identity"] is not None
    assert seen["status"]["running"] is True


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


@pytest.fixture(autouse=True)
def clear_interrupt_state():
    """No test may leak an interrupt, or a launcher's signal handler, into the next one.

    run_foreground installs SIGINT and SIGTERM handlers and never restores them, so
    without this a later test inherits a handler closed over a finished run.
    """
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    STARTUP_INTERRUPTED.clear()
    yield
    STARTUP_INTERRUPTED.clear()
    for number, handler in handlers.items():
        signal.signal(number, handler)


class ExitingChildProcess(FakeChildProcess):
    """A child that runs for a couple of polls and then exits, so the serve loop ends."""

    def __init__(self, pid=31415, polls_before_exit=1):
        super().__init__(pid)
        self.polls = 0
        self.polls_before_exit = polls_before_exit

    def poll(self):
        self.polls += 1
        return 0 if self.polls > self.polls_before_exit else None


def interrupt_now():
    """Deliver a SIGINT to whatever handler the launcher has installed."""
    handler = signal.getsignal(signal.SIGINT)
    handler(signal.SIGINT, None)


class SilentCloudflared:
    """A cloudflared that never publishes a URL and never exits, like a stalled tunnel.

    Complete enough to stand in for a real Popen, including the context manager protocol.
    A double that implements only the parts the author's own platform happens to call
    passes there and fails everywhere else.
    """

    def __init__(self, *_args, **_kwargs):
        self.stdout = iter(())
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True

    def communicate(self, *_args, **_kwargs):
        return "", ""

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        return False


def cloudflared_only(fake_process, real_popen=subprocess.Popen):
    """Fake the cloudflared launch and let every other subprocess through.

    Patching subprocess.Popen wholesale also captures subprocess.run, which is built on
    `with Popen(...) as process:`. process_identity uses subprocess.run for `ps` wherever
    /proc is absent, which is every macOS run, so a blanket patch hands the `ps` lookup a
    cloudflared double and the failure surfaces nowhere near the test's subject.
    """

    def popen(command, *args, **kwargs):
        if command and "cloudflared" in str(command[0]):
            return fake_process
        return real_popen(command, *args, **kwargs)

    return popen


@pytest.mark.unit
def test_health_wait_ends_promptly_when_the_startup_is_interrupted():
    """The health wait is 30 seconds, and Ctrl+C used to leave the user inside all of it.

    The old handler stopped two processes that do not exist during startup and returned,
    so every wait ran to its full timeout while the user had been told it was stopping.
    """
    threading.Timer(0.3, STARTUP_INTERRUPTED.set).start()
    started = time.monotonic()

    # Port 1 is closed, so every attempt fails and the loop runs its full timeout.
    healthy = wait_for_health("http://127.0.0.1:1/health", timeout=30)

    elapsed = time.monotonic() - started
    assert healthy is False
    assert elapsed < 5, f"the interrupt was not noticed for {elapsed:.1f}s"


@pytest.mark.unit
def test_cloudflared_download_stops_when_the_startup_is_interrupted(tmp_path):
    """The download is ~35 MB and is the longest wait a first run can hit."""

    class EndlessResponse:
        def __init__(self):
            self.reads = 0

        def read(self, _size):
            self.reads += 1
            if self.reads == 2:
                STARTUP_INTERRUPTED.set()
            return b"x" * 1024

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    response = EndlessResponse()
    with patch("rigout.mcp_url_launcher.urllib.request.urlopen", return_value=response):
        with pytest.raises(StartupInterruptedError):
            download_file("https://example.invalid/cloudflared", tmp_path / "download")

    assert response.reads < 10, "the download kept going after the interrupt"


@pytest.mark.unit
def test_a_real_interrupt_ends_a_stalled_tunnel_wait(tmp_path, capsys):
    """Ctrl+C during the tunnel wait: a real signal, the real handler, the real loop.

    This is the worst first-run experience in the CLI, because the user is both stuck and
    told the opposite: the handler announced a shutdown and stopped two processes that do
    not exist during the tunnel wait, then returned, and the 45 second wait carried on.

    The signal is raised rather than the handler called, so the whole chain is under test:
    CPython delivers it to the main thread exactly as a console Ctrl+C does.
    """
    args = parse_args(["start", "--state-dir", str(tmp_path), "--tunnel", "cloudflare", "--port", "19804"])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    started = time.monotonic()

    with (
        patch("rigout.mcp_url_launcher.resolve_cloudflared_binary", return_value="cloudflared"),
        patch("rigout.mcp_url_launcher.subprocess.Popen", side_effect=cloudflared_only(SilentCloudflared())),
        patch("rigout.mcp_url_launcher.start_server") as start_server_call,
    ):
        threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGINT)).start()
        exit_code = run_foreground(args, paths, managed=True)

    elapsed = time.monotonic() - started
    output = capsys.readouterr()
    combined = output.out + output.err
    assert exit_code == 130
    assert elapsed < 15, f"the tunnel wait ran on for {elapsed:.1f}s after the interrupt"
    start_server_call.assert_not_called()
    # No server or tunnel exists yet, so naming one is telling the user the opposite.
    assert "Interrupting startup..." in combined
    assert "Stopping MCP server..." not in combined
    assert "Startup interrupted. Nothing is running." in combined


@pytest.mark.unit
def test_an_interrupted_startup_is_recorded_as_stopped_not_failed(tmp_path, capsys):
    """An operator-requested stop is not a failure, and must not read as one afterwards."""
    args = parse_args(["start", "--state-dir", str(tmp_path), "--port", "19802"])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)

    def interrupt_instead_of_starting(*_args, **_kwargs):
        interrupt_now()
        return ExitingChildProcess()

    with patch("rigout.mcp_url_launcher.start_server", side_effect=interrupt_instead_of_starting):
        exit_code = run_foreground(args, paths, managed=True)

    runtime = json.loads(paths.runtime_file.read_text(encoding="utf-8"))
    assert exit_code == 130
    assert runtime["status"] == "stopped"
    assert "last_error" not in runtime
    assert runtime_status(paths)["running"] is False


@pytest.mark.unit
@pytest.mark.parametrize("argv", [["--version"], ["-V"], ["status", "--version"], ["stop", "-V"]])
def test_version_is_reachable_from_every_command(argv, capsys):
    """A first run reaches for --version before anything else, and it did not exist."""
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv)

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"rigout {__version__}"


@pytest.mark.unit
def test_unknown_command_names_the_commands_that_exist(capsys):
    """A typo used to be reported against start's usage, which never lists the commands."""
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["stpo"])

    error = capsys.readouterr().err
    assert exit_info.value.code == 2
    assert "unknown command 'stpo'" in error
    for command in ("start", "status", "logs", "stop"):
        assert command in error


@pytest.mark.unit
def test_help_states_defaults_and_documents_every_visible_option(capsys):
    with pytest.raises(SystemExit):
        parse_args(["start", "--help"])

    # Collapsed, because argparse wraps to the terminal width and a fixed substring
    # would otherwise pass or fail depending on how wide the running console is.
    help_text = " ".join(capsys.readouterr().out.split())
    assert "(default:" in help_text
    # These three carried no help text at all, so --help listed them without saying
    # what they do.
    for option in ("--json-response", "--stateless", "--output"):
        assert option in help_text
    assert "server-sent event stream" in help_text
    assert "without a persistent session" in help_text


@pytest.mark.unit
def test_a_blocked_state_directory_is_a_message_not_a_traceback(tmp_path, capsys):
    """paths.prepare() sits outside every try, so this reached the user as a traceback."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")

    exit_code = main(["start", "--detach", "--state-dir", str(blocker / "state")])

    error = capsys.readouterr().err
    assert exit_code == 1
    assert "cannot use the runtime state directory" in error
    assert "--state-dir" in error
    assert "Traceback" not in error


@pytest.mark.unit
def test_logs_without_a_log_file_says_so_like_follow_already_did(tmp_path, capsys):
    """The default path printed zero bytes and exited 1; --follow printed a message."""
    exit_code = main(["logs", "--state-dir", str(tmp_path)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert f"No activity log exists at {RuntimePaths.resolve(tmp_path).log_file}" in output.err


@pytest.mark.unit
def test_status_json_keys_do_not_change_between_states(tmp_path, capsys):
    """`json.load(...)["mcp_url"]` must not raise before the first start."""
    stopped_code = main(["status", "--state-dir", str(tmp_path), "--output", "json"])
    stopped = json.loads(capsys.readouterr().out)

    paths = RuntimePaths.resolve(tmp_path)
    write_json_secure(
        paths.runtime_file,
        {
            "status": "running",
            "managed": True,
            "pid": os.getpid(),
            "process_identity": process_identity(os.getpid()),
            "mcp_url": "http://127.0.0.1:8765/mcp",
            "health_url": "http://127.0.0.1:8765/health",
            "host": "127.0.0.1",
            "port": 8765,
        },
    )
    write_pid(paths, os.getpid())
    running_code = main(["status", "--state-dir", str(tmp_path), "--output", "json"])
    running = json.loads(capsys.readouterr().out)

    assert stopped_code == 1
    assert running_code == 0
    assert stopped["mcp_url"] is None
    assert running["mcp_url"] == "http://127.0.0.1:8765/mcp"
    assert set(stopped) <= set(running), f"keys vanish when stopped: {set(running) - set(stopped)}"
    for key in lifecycle.OPTIONAL_STATUS_KEYS:
        assert key in stopped, f"{key} is missing before the first start"


@pytest.mark.unit
def test_logs_follow_json_refusal_is_itself_json(tmp_path, capsys):
    """The caller asked for JSON, so the refusal has to be parseable by that caller."""
    exit_code = main(["logs", "--state-dir", str(tmp_path), "--output", "json", "--follow"])

    output = capsys.readouterr().out
    assert exit_code == 2
    # The reason is preserved, not reduced to a bare refusal.
    assert "follow output is a text stream" in json.loads(output)["error"]


@pytest.mark.unit
def test_json_start_refusal_is_itself_json(tmp_path, capsys):
    exit_code = main(["start", "--state-dir", str(tmp_path), "--output", "json"])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "requires --detach" in json.loads(output)["error"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["--host", "127.0.0.1"], "Auth: none, and none needed"),
        (["--host", "0.0.0.0"], "Auth: bearer token required"),
        (["--host", "0.0.0.0", "--no-auth"], "Auth: NONE, and this server is reachable beyond this machine."),
    ],
)
def test_the_running_banner_always_states_the_auth_posture(tmp_path, capsys, extra, expected):
    """The unauthenticated public case used to be marked by the Auth line being absent.

    Absence is the one posture a reader cannot notice, and it is the dangerous one: the
    --no-auth warning has scrolled away behind tunnel output by the time this prints.
    """
    args = parse_args(["start", "--state-dir", str(tmp_path), "--port", "19803", *extra])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)

    with (
        patch("rigout.mcp_url_launcher.start_server", return_value=ExitingChildProcess()),
        patch("rigout.mcp_url_launcher.wait_for_health", return_value=True),
        patch("rigout.mcp_url_launcher.write_connection_file"),
    ):
        run_foreground(args, paths, managed=True)

    output = capsys.readouterr().out
    assert expected in output
    assert "Auth:" in output


@pytest.mark.unit
def test_already_running_tells_the_user_what_to_do_next(tmp_path, live_foreign_process, capsys):
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    winning_record(paths, live_foreign_process.pid)

    exit_code = start_detached(args, paths)

    error = capsys.readouterr().err
    assert exit_code == 1
    assert "already running" in error
    assert "rigout status" in error
    assert "rigout stop" in error


@pytest.mark.unit
def test_process_check_rejects_nonexistent_pid():
    assert process_is_running(999_999_999) is False


@pytest.mark.unit
def test_managed_child_is_rejected_without_the_marker_its_parent_sets(tmp_path, capsys):
    """The suppressed internal flag skips the single-instance check, so a user must not
    be able to reach it: one typed flag would otherwise put two launchers on one state
    directory."""
    # Patched so that a regression here fails the test instead of launching a real server.
    with patch("rigout.mcp_url_launcher.run_foreground", return_value=0) as foreground:
        exit_code = main(["start", "--managed-child", "--state-dir", str(tmp_path)])

    assert exit_code == 2
    assert "not a supported option" in capsys.readouterr().err
    foreground.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_managed_child_is_accepted_from_the_parent_that_sets_the_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGOUT_DETACHED_CHILD", "1")

    with patch("rigout.mcp_url_launcher.run_foreground", return_value=0) as foreground:
        exit_code = main(["start", "--managed-child", "--state-dir", str(tmp_path)])

    assert exit_code == 0
    foreground.assert_called_once()


@pytest.mark.unit
def test_follow_reseeks_after_a_restart_truncates_the_log(tmp_path, capsys, monkeypatch):
    """A follower holding an offset past a truncated log shows nothing, forever.

    `start` truncates the activity log, so `logs --follow` left running across a restart
    silently stops reporting while it looks like it is still working.
    """
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    paths.log_file.write_text("stale instance line\n" * 4, encoding="utf-8")
    monkeypatch.setattr("rigout.mcp_url_launcher.FOLLOW_LIVENESS_SECONDS", 0)
    monkeypatch.setattr("rigout.mcp_url_launcher.time.sleep", lambda _seconds: None)
    calls = {"count": 0}

    def restart_then_stop(_paths):
        calls["count"] += 1
        if calls["count"] == 1:
            # A concurrent `rigout start` truncates the log and writes the new instance.
            paths.log_file.write_text("new instance line\n", encoding="utf-8")
            return {"running": True, "status": "running"}
        return {"running": False, "status": "stopped"}

    with patch("rigout.mcp_url_launcher.runtime_status", side_effect=restart_then_stop):
        exit_code = main(["logs", "--state-dir", str(tmp_path), "--tail", "0", "--follow"])

    assert exit_code == 0
    assert "new instance line" in capsys.readouterr().out


@pytest.mark.unit
def test_logs_redacts_credentials_the_way_the_activity_tool_does(tmp_path, capsys):
    """`rigout logs` and get_server_activity read one file; the operator view must not be
    the less redacted of the two."""
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    paths.log_file.write_text(
        "GET /connection.json?setup_token=setup-secret\n"
        'reply "Authorization: Bearer bearer-secret"\n'
        "resolved auth_token=third-secret\n",
        encoding="utf-8",
    )

    exit_code = main(["logs", "--state-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "setup-secret" not in output
    assert "bearer-secret" not in output
    assert "third-secret" not in output


@pytest.mark.unit
def test_logs_json_redacts_credentials(tmp_path, capsys):
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    paths.log_file.write_text("GET /connection.json?setup_token=setup-secret\n", encoding="utf-8")

    exit_code = main(["logs", "--state-dir", str(tmp_path), "--output", "json"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "setup-secret" not in output
    assert json.loads(output)["lines"][0] == "GET /connection.json?setup_token=***"


@pytest.mark.unit
def test_ps_identity_is_cached_within_the_granularity_it_already_has(tmp_path):
    """`ps` costs a fork and an exec, and status is polled several times a second.

    The cache is bounded by the resolution `ps -o lstart=` already reports, one second,
    so it cannot make the PID-reuse guard any less precise than its own source.
    """
    lifecycle._PS_IDENTITY_CACHE.clear()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="Sat Aug  1 00:00:00 2026\n", stderr="")

    with patch("rigout.lifecycle.subprocess.run", return_value=completed) as run:
        first = ps_identity(4242)
        second = ps_identity(4242)
        other = ps_identity(4243)

    assert first == second == "ps-lstart:Sat Aug  1 00:00:00 2026"
    assert other == first
    assert run.call_count == 2  # one per PID, not one per question

    with (
        patch("rigout.lifecycle.subprocess.run", return_value=completed) as run,
        patch("rigout.lifecycle.time.monotonic", return_value=time_after_cache_expiry()),
    ):
        ps_identity(4242)

    assert run.call_count == 1
    lifecycle._PS_IDENTITY_CACHE.clear()


def time_after_cache_expiry():
    import time as _time

    return _time.monotonic() + lifecycle.PS_IDENTITY_CACHE_SECONDS + 1


@pytest.mark.unit
def test_detached_startup_polling_does_not_verify_the_process_every_tick(tmp_path):
    """Verification costs a `ps` on systems without /proc, and startup polls five times a
    second for up to ninety seconds."""
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()

    class SlowChild(FakeManagedChild):
        def __init__(self, *call_args):
            super().__init__(*call_args)
            self.polls = 0

        def poll(self):
            self.polls += 1
            if self.polls < 12:
                return None  # still starting
            return super().poll()

    child = SlowChild(paths, "slow-instance")
    child.pid = os.getpid()

    with (
        patch("rigout.mcp_url_launcher.launch_detached", return_value=child),
        patch("rigout.mcp_url_launcher.secrets.token_urlsafe", return_value="slow-instance"),
        patch("rigout.mcp_url_launcher.time.sleep"),
        # Patched inside lifecycle, which is where runtime_status reaches it: the launcher
        # holds its own reference, so patching that name would miss every polled lookup.
        patch("rigout.lifecycle.process_identity", side_effect=process_identity) as identity,
    ):
        exit_code = start_detached(args, paths)

    assert exit_code == 0
    assert child.polls >= 12
    # Verification belongs on the terminal answer, not on every one of those polls.
    assert identity.call_count <= 2


LOCK_WORKER = """
import json, os, sys, time
sys.path.insert(0, {source!r})
from rigout.lifecycle import RuntimePaths, state_lock

paths = RuntimePaths.resolve(sys.argv[1])
counter = paths.root / "counter.json"
increments = int(sys.argv[2])
held = True
for _ in range(increments):
    with state_lock(paths) as locked:
        held = held and locked
        # Read-modify-write with a real gap: without exclusion the writers that read the
        # same value both write value+1 and one increment is lost.
        value = json.loads(counter.read_text(encoding="utf-8"))["value"]
        time.sleep(0.001)
        counter.write_text(json.dumps({{"value": value + 1}}), encoding="utf-8")
print("locked" if held else "unlocked")
"""


@pytest.mark.unit
def test_state_lock_serializes_writers_in_separate_os_processes(tmp_path):
    """The lock has to hold across processes, which is the only place it is ever used.

    Real `rigout` commands are separate OS processes, so an in-process check would prove
    nothing. Concurrent read-modify-write of one counter loses increments unless the lock
    actually excludes; the assertion is on the total, not on timing.
    """
    workers, increments = 4, 25
    paths = RuntimePaths.resolve(tmp_path)
    paths.prepare()
    (paths.root / "counter.json").write_text(json.dumps({"value": 0}), encoding="utf-8")
    source = str(Path(lifecycle.__file__).resolve().parent.parent)
    script = tmp_path / "lock_worker.py"
    script.write_text(LOCK_WORKER.format(source=source), encoding="utf-8")

    def run_worker(_index):
        return subprocess.run(
            [sys.executable, str(script), str(tmp_path), str(increments)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run_worker, range(workers)))

    for result in results:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "locked", result.stdout
    assert json.loads((paths.root / "counter.json").read_text(encoding="utf-8"))["value"] == workers * increments


def winning_record(paths, pid):
    """A live, verified record owned by another instance, on a real running process."""
    winner = {
        "status": "running",
        "managed": True,
        "pid": pid,
        "process_identity": process_identity(pid),
        "instance_id": "the-winner",
        "mcp_url": "http://127.0.0.1:8765/mcp",
    }
    write_json_secure(paths.runtime_file, winner)
    write_pid(paths, pid)
    return winner


@pytest.mark.unit
def test_a_managed_child_refuses_a_directory_another_instance_already_runs(tmp_path, live_foreign_process, capsys):
    """A child whose parent lost the race must not take over the winner's directory.

    The single-instance check used to be skipped for managed children entirely, so a
    child of a losing parent walked in and started rewriting the winner's state.
    """
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    winner = winning_record(paths, live_foreign_process.pid)

    with (
        patch.dict(os.environ, {"RIGOUT_DETACHED_CHILD": "1", "RIGOUT_INSTANCE_ID": "the-loser"}),
        patch("rigout.mcp_url_launcher.start_server") as start,
    ):
        exit_code = run_foreground(args, paths, managed=True)

    assert exit_code == 1
    assert "already running" in capsys.readouterr().err
    start.assert_not_called()
    assert json.loads(paths.runtime_file.read_text(encoding="utf-8")) == winner


@pytest.mark.unit
def test_a_shutdown_does_not_overwrite_a_record_another_instance_now_owns(tmp_path, live_foreign_process):
    """The shutdown path must write only its own instance's outcome.

    A launcher that fails after another instance has taken the directory would otherwise
    write "stopped" over a live record, which makes status report a running server as
    stopped and makes stop refuse it: a server nobody can reach through the CLI.
    """
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    taken_over = {}

    def lose_the_directory(*_args, **_kwargs):
        taken_over["winner"] = winning_record(paths, live_foreign_process.pid)
        raise OSError("address already in use")

    with (
        patch.dict(os.environ, {"RIGOUT_DETACHED_CHILD": "1", "RIGOUT_INSTANCE_ID": "the-loser"}),
        patch("rigout.mcp_url_launcher.start_server", side_effect=lose_the_directory),
    ):
        exit_code = run_foreground(args, paths, managed=True)

    status = runtime_status(paths)
    assert exit_code == 1
    assert json.loads(paths.runtime_file.read_text(encoding="utf-8")) == taken_over["winner"]
    assert status["running"] is True
    assert status["status"] == "running"


@pytest.mark.unit
def test_child_pids_are_not_recorded_against_another_instances_record(tmp_path, live_foreign_process):
    """stop reaps whatever child PIDs the record names, so a stray instance writing its
    own children there points a later stop at processes that instance never started."""
    args = parse_args(["start", "--managed-child", "--state-dir", str(tmp_path), "--tunnel", "cloudflare"])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    taken_over = {}

    def lose_the_directory(*_args, **_kwargs):
        taken_over["winner"] = winning_record(paths, live_foreign_process.pid)
        return FakeChildProcess(27182), "https://example.trycloudflare.com"

    with (
        patch.dict(os.environ, {"RIGOUT_DETACHED_CHILD": "1", "RIGOUT_INSTANCE_ID": "the-loser"}),
        patch("rigout.mcp_url_launcher.start_cloudflare_tunnel", side_effect=lose_the_directory),
        patch("rigout.mcp_url_launcher.start_server", return_value=FakeChildProcess(31415)),
        patch("rigout.mcp_url_launcher.wait_for_health", return_value=False),
    ):
        run_foreground(args, paths, managed=True)

    runtime = json.loads(paths.runtime_file.read_text(encoding="utf-8"))
    assert runtime == taken_over["winner"]
    assert "server_pid" not in runtime
    assert "tunnel_pid" not in runtime


@pytest.mark.unit
def test_stop_force_clears_an_unverifiable_reservation_without_signalling_it(tmp_path, live_foreign_process, capsys):
    """The way out of a state the CLI cannot verify must not be to kill an unknown PID."""
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    wedged_reservation(paths, args, live_foreign_process.pid)

    refused = main(["stop", "--state-dir", str(tmp_path)])
    refusal = capsys.readouterr().err
    forced = main(["stop", "--state-dir", str(tmp_path), "--force"])
    forced_output = capsys.readouterr().out

    assert refused == 1
    assert "rigout stop --force" in refusal
    assert forced == 0
    assert "was not signalled" in forced_output
    assert paths.pid_file.exists() is False
    assert live_foreign_process.poll() is None  # never signalled
    assert runtime_status(paths)["running"] is False


@pytest.mark.unit
def test_status_explains_a_live_recorded_pid_that_is_not_rigout(tmp_path, live_foreign_process, capsys):
    """A live PID printed under a stopped status reads as a contradiction on its own."""
    args = parse_args(["start", "--detach", "--state-dir", str(tmp_path)])
    paths = RuntimePaths.resolve(args.state_dir)
    prepare_start_args(args, paths)
    paths.prepare()
    wedged_reservation(paths, args, live_foreign_process.pid)

    exit_code = main(["status", "--state-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Rigout status: stopped" in output
    assert "has reused it" in output
    assert "rigout stop --force" in output


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


SETUP_URL = "https://example.trycloudflare.com/connection.json?setup_token=TOKEN123"


def write_connection(paths, *, setup_url=SETUP_URL):
    """Write a connection file shaped like the one a public start produces."""
    paths.prepare()
    connection = {
        "server": {"name": "rigout", "version": __version__},
        "mcp": {
            "transport": "streamable-http",
            "url": "https://example.trycloudflare.com/mcp",
            "health_url": "https://example.trycloudflare.com/health",
        },
    }
    if setup_url:
        connection["agent_setup_url"] = setup_url
    paths.connection_file.write_text(json.dumps(connection), encoding="utf-8")
    return paths


@pytest.mark.unit
def test_url_prints_only_the_url_on_stdout(tmp_path, capsys):
    """The point of the command: stdout is pipeable, so it carries the URL and nothing else.

    `rigout url | xclip` and `rigout url > file` are the reason this command exists.
    Any label, banner or warning on stdout would end up in the clipboard with it.
    """
    write_connection(RuntimePaths.resolve(tmp_path))

    exit_code = main(["url", "--state-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{SETUP_URL}\n"
    assert "password" in captured.err


@pytest.mark.unit
def test_url_works_without_any_runtime_state(tmp_path, capsys):
    """A foreground start records no runtime file, and that is the case that needs it most."""
    paths = write_connection(RuntimePaths.resolve(tmp_path))
    assert not paths.runtime_file.exists()

    exit_code = main(["url", "--state-dir", str(tmp_path)])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == SETUP_URL


@pytest.mark.unit
@pytest.mark.parametrize(
    ("which", "expected"),
    [
        ("mcp", "https://example.trycloudflare.com/mcp"),
        ("health", "https://example.trycloudflare.com/health"),
    ],
)
def test_url_selects_the_requested_endpoint(tmp_path, capsys, which, expected):
    write_connection(RuntimePaths.resolve(tmp_path))

    exit_code = main(["url", "--state-dir", str(tmp_path), "--which", which])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{expected}\n"
    # Only the setup URL is credential-equivalent, so only it carries the warning.
    assert "password" not in captured.err


@pytest.mark.unit
def test_url_reports_a_missing_connection_file_without_polluting_stdout(tmp_path, capsys):
    exit_code = main(["url", "--state-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Start Rigout first" in captured.err


@pytest.mark.unit
def test_url_explains_that_a_loopback_server_has_no_setup_url(tmp_path, capsys):
    write_connection(RuntimePaths.resolve(tmp_path), setup_url=None)

    exit_code = main(["url", "--state-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "--which mcp" in captured.err


@pytest.mark.unit
def test_url_json_output_is_one_parseable_object(tmp_path, capsys):
    write_connection(RuntimePaths.resolve(tmp_path))

    exit_code = main(["url", "--state-dir", str(tmp_path), "--output", "json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["setup_url"] == SETUP_URL
    assert output["mcp_url"] == "https://example.trycloudflare.com/mcp"


@pytest.mark.unit
def test_url_is_parsed_as_a_command_not_a_flagless_start():
    assert parse_args(["url"]).command == "url"


@pytest.mark.unit
def test_started_url_is_printed_alone_on_its_own_line(capsys):
    """Selectability is the whole point: a label on the URL's line gets copied with it.

    Three URLs are on screen after a tunnel start and only one is the one to hand over.
    It is printed bare at the left margin, so a double- or triple-click selects the URL
    and nothing else, and so it is visually distinct from the two that are informational.
    """
    result = {
        "pid": 4321,
        "connection_file": "/state/connection.json",
        "activity_log": "/state/activity.log",
        "mcp_url": "https://example.trycloudflare.com/mcp",
        "health_url": "https://example.trycloudflare.com/health",
    }

    with patch("rigout.mcp_url_launcher.copy_to_clipboard", return_value=None):
        print_start_result(result, SETUP_URL, "text")

    lines = capsys.readouterr().out.splitlines()
    assert SETUP_URL in lines, "the setup URL must occupy a line by itself"
    # The informational URLs stay labelled; only the actionable one stands alone.
    assert result["mcp_url"] not in lines
    assert result["health_url"] not in lines


@pytest.mark.unit
def test_started_url_is_copied_to_the_clipboard_by_default(capsys):
    result = {"pid": 1, "connection_file": "c", "activity_log": "a"}

    with patch("rigout.mcp_url_launcher.copy_to_clipboard", return_value="pbcopy") as copy:
        print_start_result(result, SETUP_URL, "text")

    copy.assert_called_once_with(SETUP_URL)
    assert "Already copied to your clipboard (via pbcopy)" in capsys.readouterr().out


@pytest.mark.unit
def test_no_copy_url_leaves_the_clipboard_alone(capsys):
    """Copying a credential-equivalent URL must be refusable."""
    result = {"pid": 1, "connection_file": "c", "activity_log": "a"}

    with patch("rigout.mcp_url_launcher.copy_to_clipboard") as copy:
        print_start_result(result, SETUP_URL, "text", copy_url=False)

    copy.assert_not_called()
    assert SETUP_URL in capsys.readouterr().out.splitlines()


@pytest.mark.unit
def test_no_copy_url_flag_is_accepted_and_defaults_to_copying():
    assert parse_args(["--tunnel", "cloudflare"]).copy_url is True
    assert parse_args(["--no-copy-url"]).copy_url is False
    assert parse_args(["start", "--no-copy-url"]).copy_url is False


@pytest.mark.unit
def test_json_startup_output_never_prints_the_url_banner(capsys):
    """`--output json` must stay one parseable object; a banner would corrupt it."""
    result = {"pid": 1, "connection_file": "c", "activity_log": "a"}

    with patch("rigout.mcp_url_launcher.copy_to_clipboard") as copy:
        print_start_result(result, SETUP_URL, "json")

    copy.assert_not_called()
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.unit
def test_url_command_copies_only_when_asked(tmp_path, capsys):
    write_connection(RuntimePaths.resolve(tmp_path))

    with patch("rigout.mcp_url_launcher.copy_to_clipboard", return_value="xclip") as copy:
        main(["url", "--state-dir", str(tmp_path)])
        copy.assert_not_called()

        main(["url", "--state-dir", str(tmp_path), "--copy"])
        copy.assert_called_once_with(SETUP_URL)

    captured = capsys.readouterr()
    # stdout stays pipeable: the URL twice, and the copy report on stderr.
    assert captured.out == f"{SETUP_URL}\n{SETUP_URL}\n"
    assert "Copied to the clipboard via xclip." in captured.err
