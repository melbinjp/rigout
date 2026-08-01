"""Tests that evaluate the command strings the tool handlers actually build.

The existing handler tests mock `get_tunnel_manager` to return success and never
look at what was sent, so a Mac running PowerShell, a Windows setup that did
nothing, and a removed endpoint that stayed reachable all passed. These tests
capture the real command text per platform and exercise the local branch.
"""

import asyncio
import os
import subprocess
from unittest.mock import patch

import pytest

from rigout.tools._platform import (
    LINUX,
    MACOS,
    WINDOWS,
    is_macos_platform,
    is_windows_platform,
    platform_family,
)
from rigout.tools.command import handle_install_software
from rigout.tools.docker import handle_docker_operations
from rigout.tools.environment import handle_environment_setup
from rigout.tools.monitoring import handle_system_monitoring
from rigout.tools.tunnel import handle_manage_tunnels

LOCAL_KEY = "__local__"


class FakeEndpoint:
    """A real object, not a MagicMock, so `platform` is a string a handler can test."""

    def __init__(self, platform="Linux", private_key_path="/home/agent/id_ed25519", hostname="test-host"):
        self.platform = platform
        self.private_key_path = private_key_path
        self.hostname = hostname
        self.username = "agent"
        self.port = 22
        self.status = "unknown"
        self.purpose = "primary"
        self.response_time = 0.1
        self.max_connections = 4
        self.current_connections = 0


class RecordingManager:
    """Captures every command string a handler asks to execute."""

    def __init__(self, endpoint=None, success=True, stdout="ok"):
        self.endpoint = endpoint if endpoint is not None else FakeEndpoint()
        self.commands: list[str] = []
        self.success = success
        self.stdout = stdout
        self.saved = 0

    async def auto_failover(self):
        return self.endpoint

    async def execute_command(self, endpoint, command, timeout=30, **kwargs):
        self.commands.append(command)
        return {
            "success": self.success,
            "exit_code": 0 if self.success else 1,
            "stdout": self.stdout,
            "stderr": "",
            "command": command,
            "endpoint": endpoint.hostname,
        }

    def save_config(self):
        self.saved += 1

    @property
    def only_command(self) -> str:
        assert len(self.commands) == 1, f"expected one command, got {self.commands}"
        return self.commands[0]

    @property
    def joined(self) -> str:
        return "\n".join(self.commands)


def patch_manager(module: str, manager):
    return patch(f"rigout.tools.{module}.get_tunnel_manager", return_value=manager)


def text_of(result) -> str:
    return result.content[0].text


# --------------------------------------------------------------------------
# The predicate itself: "win" in "darwin" is True, which is the whole bug
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestPlatformPredicate:
    def test_darwin_is_never_windows(self):
        assert "win" in "darwin"  # the substring test every call site used
        assert is_windows_platform("darwin") is False
        assert is_macos_platform("darwin") is True
        assert platform_family("darwin") == MACOS

    @pytest.mark.parametrize(
        "label", ["Windows", "windows", "Windows 11 Pro", "win32", "WIN", "Microsoft Windows Server 2022", "cygwin"]
    )
    def test_windows_labels(self, label):
        assert platform_family(label) == WINDOWS

    @pytest.mark.parametrize("label", ["Darwin", "macOS 14.2", "mac", "OSX", "Mac OS X"])
    def test_macos_labels(self, label):
        assert platform_family(label) == MACOS

    @pytest.mark.parametrize("label", ["Linux", "Ubuntu 22.04", "debian", "", "unknown", None, 7])
    def test_everything_else_falls_back_to_posix(self, label):
        assert platform_family(label) == LINUX


# --------------------------------------------------------------------------
# FIX 1: a Mac must never be sent PowerShell
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestMonitoringPlatformBranch:
    async def test_macos_monitoring_never_runs_powershell(self):
        manager = RecordingManager(FakeEndpoint(platform="Darwin", private_key_path=LOCAL_KEY))
        with patch_manager("monitoring", manager):
            result = await handle_system_monitoring({"metrics": ["all"]})

        assert result.isError is False
        assert manager.commands, "no monitoring commands were built"
        assert "powershell" not in manager.joined.lower()
        assert "Get-CimInstance" not in manager.joined
        assert any("top -l 1" in command for command in manager.commands)
        assert any("vm_stat" in command for command in manager.commands)

    async def test_windows_monitoring_still_uses_powershell(self):
        manager = RecordingManager(FakeEndpoint(platform="Windows", private_key_path=LOCAL_KEY))
        with patch_manager("monitoring", manager):
            await handle_system_monitoring({"metrics": ["cpu", "memory"]})

        assert all("powershell -NoProfile" in command for command in manager.commands)

    async def test_linux_monitoring_uses_posix_tools(self):
        manager = RecordingManager(FakeEndpoint(platform="Ubuntu 22.04"))
        with patch_manager("monitoring", manager):
            await handle_system_monitoring({"metrics": ["memory"]})

        assert manager.only_command == "free -h"


@pytest.mark.unit
@pytest.mark.asyncio
class TestInstallSoftwarePlatformBranch:
    async def test_macos_uses_homebrew_not_chocolatey(self):
        manager = RecordingManager(FakeEndpoint(platform="Darwin", private_key_path=LOCAL_KEY))
        with patch_manager("command", manager):
            result = await handle_install_software({"packages": ["ripgrep"]})

        assert manager.only_command == "brew install ripgrep"
        assert "choco" not in text_of(result)

    async def test_windows_uses_chocolatey(self):
        manager = RecordingManager(FakeEndpoint(platform="Windows"))
        with patch_manager("command", manager):
            await handle_install_software({"packages": ["ripgrep"]})

        assert manager.only_command == "choco install -y ripgrep"

    async def test_linux_uses_apt(self):
        manager = RecordingManager(FakeEndpoint(platform="Ubuntu 22.04"))
        with patch_manager("command", manager):
            await handle_install_software({"packages": ["ripgrep"]})

        assert manager.only_command == "sudo apt update && sudo apt install -y ripgrep"


# --------------------------------------------------------------------------
# FIX 2: environment_setup on a local Windows endpoint
# --------------------------------------------------------------------------

# A local endpoint is this machine, so its platform is this machine's: the local-Windows
# branch can only be reached on a Windows host. The tests below hand the handler a
# workspace path taken from the runner's own filesystem, which makes the runner decide
# what is being tested. On Windows tmp_path has a drive and passes through; on Linux it
# starts with /tmp and is mapped onto the temp directory; on macOS it is
# /private/var/folders/... which the handler correctly refuses as an unmappable POSIX
# absolute path, so no command is built at all. The handler then emits backslash paths
# that no POSIX host can stat, so every assertion here can only hold on Windows.
#
# The two tests in this class without this mark do not touch the host filesystem and are
# deliberately left running everywhere.
windows_host_only = pytest.mark.skipif(
    os.name != "nt",
    reason="the local-Windows shell branch exists only on a Windows host; see the note above",
)


@pytest.mark.unit
@pytest.mark.asyncio
class TestEnvironmentSetupWindowsLocal:
    @staticmethod
    def _windows_manager():
        return RecordingManager(FakeEndpoint(platform="Windows", private_key_path=LOCAL_KEY))

    @windows_host_only
    async def test_no_if_guard_swallows_the_setup_chain(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()  # the case that used to make cmd.exe skip everything
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            result = await handle_environment_setup(
                {"environment_type": "python", "workspace_path": str(workspace), "requirements": ["numpy"]}
            )

        command = manager.only_command
        assert result.isError is False
        assert "if not exist" not in command
        assert command.startswith(f'cd /d "{workspace}"')
        assert "python -m venv venv" in command
        assert 'venv\\Scripts\\python.exe -m pip install "numpy"' in command

    @windows_host_only
    async def test_default_workspace_is_a_usable_windows_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            result = await handle_environment_setup({"environment_type": "python"})

        assert result.isError is False
        assert "/tmp/ai_workspace" not in manager.only_command
        assert str(tmp_path / "ai_workspace") in manager.only_command
        assert (tmp_path / "ai_workspace").is_dir(), "the workspace was never created"

    @windows_host_only
    async def test_workspace_directory_is_created_before_use(self, tmp_path):
        workspace = tmp_path / "fresh" / "nested"
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            await handle_environment_setup({"environment_type": "custom", "workspace_path": str(workspace)})

        assert workspace.is_dir()

    async def test_unmappable_posix_path_is_refused_with_guidance(self, tmp_path):
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            result = await handle_environment_setup(
                {"environment_type": "python", "workspace_path": "/home/agent/work"}
            )

        assert result.isError is True
        assert "POSIX absolute path" in text_of(result)
        assert manager.commands == [], "a doomed command was sent anyway"

    @windows_host_only
    async def test_docker_dockerfile_is_written_not_shell_quoted(self, tmp_path):
        workspace = tmp_path / "ws"
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            result = await handle_environment_setup(
                {
                    "environment_type": "docker",
                    "workspace_path": str(workspace),
                    "requirements": ["python:3.12-slim", "pip install numpy"],
                }
            )

        assert result.isError is False
        assert "powershell" not in manager.only_command.lower()
        dockerfile = workspace / "Dockerfile"
        assert dockerfile.is_file()
        assert dockerfile.read_text(encoding="utf-8") == "FROM python:3.12-slim\nRUN pip install numpy\n"
        assert "Dockerfile written" in text_of(result)

    @windows_host_only
    async def test_unquotable_requirement_cannot_break_out_of_the_command(self, tmp_path):
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            result = await handle_environment_setup(
                {
                    "environment_type": "python",
                    "workspace_path": str(tmp_path / "ws"),
                    "requirements": ['numpy" && echo pwned && rem '],
                }
            )

        assert result.isError is True
        assert "cannot be quoted for cmd.exe" in text_of(result)
        assert manager.commands == []

    @windows_host_only
    async def test_pip_specifiers_are_still_accepted(self, tmp_path):
        manager = self._windows_manager()
        with patch_manager("environment", manager):
            result = await handle_environment_setup(
                {
                    "environment_type": "python",
                    "workspace_path": str(tmp_path / "ws"),
                    "requirements": ["numpy>=1.26,<2"],
                }
            )

        assert result.isError is False
        assert 'venv\\Scripts\\python.exe -m pip install "numpy>=1.26,<2"' in manager.only_command

    async def test_windows_path_translation(self, tmp_path, monkeypatch):
        from rigout.tools.environment import windows_workspace_path

        monkeypatch.setattr("tempfile.gettempdir", lambda: r"C:\Temp")
        assert windows_workspace_path("/tmp/ai_workspace") == r"C:\Temp\ai_workspace"
        assert windows_workspace_path(r"D:\work\env") == r"D:\work\env"
        assert windows_workspace_path("D:/work/env") == r"D:\work\env"
        with pytest.raises(ValueError):
            windows_workspace_path("/opt/rigout")


@pytest.mark.unit
@pytest.mark.asyncio
class TestEnvironmentSetupPosixLocal:
    async def test_macos_local_setup_uses_posix_syntax(self, tmp_path):
        workspace = tmp_path / "ws"
        manager = RecordingManager(FakeEndpoint(platform="Darwin", private_key_path=LOCAL_KEY))
        with patch_manager("environment", manager):
            result = await handle_environment_setup({"environment_type": "python", "workspace_path": str(workspace)})

        command = manager.only_command
        assert result.isError is False
        assert "if not exist" not in command
        assert "cd /d" not in command
        assert "python3 -m venv venv" in command
        assert workspace.is_dir()

    async def test_tilde_workspace_is_expanded_before_the_shell_sees_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        manager = RecordingManager(FakeEndpoint(platform="Darwin", private_key_path=LOCAL_KEY))
        with patch_manager("environment", manager):
            result = await handle_environment_setup({"environment_type": "custom", "workspace_path": "~/work"})

        assert "~" not in manager.only_command, "the shell was given a quoted ~ it cannot expand"
        assert (tmp_path / "work").is_dir()
        assert str(tmp_path / "work") in text_of(result)

    async def test_remote_endpoint_still_gets_mkdir_p(self):
        manager = RecordingManager(FakeEndpoint(platform="Ubuntu 22.04"))
        with patch_manager("environment", manager):
            await handle_environment_setup({"environment_type": "python", "workspace_path": "/tmp/env"})

        assert manager.only_command.startswith("mkdir -p /tmp/env && cd /tmp/env && python3 -m venv venv")


@pytest.mark.unit
@pytest.mark.skipif(os.name != "nt", reason="documents cmd.exe parsing, only meaningful on Windows")
def test_cmd_if_guard_binds_the_whole_chain(tmp_path):
    """The platform behaviour behind FIX 2, pinned so it cannot be reintroduced.

    cmd.exe binds every `&&` term to the body of the leading `if`, so when the
    workspace already exists the whole setup is skipped and cmd still exits 0.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guarded = f'if not exist "{workspace}" mkdir "{workspace}" && cd /d "{workspace}" && echo RIGOUT_MARKER'
    plain = f'cd /d "{workspace}" && echo RIGOUT_MARKER'

    guarded_run = subprocess.run(guarded, shell=True, capture_output=True, text=True)
    plain_run = subprocess.run(plain, shell=True, capture_output=True, text=True)

    assert guarded_run.returncode == 0  # reported as success ...
    assert "RIGOUT_MARKER" not in guarded_run.stdout  # ... having run nothing
    assert plain_run.returncode == 0
    assert "RIGOUT_MARKER" in plain_run.stdout


# --------------------------------------------------------------------------
# FIX 3: removing an endpoint must actually cut it off
# --------------------------------------------------------------------------


class FakeSSHClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.closed = False

    def close(self):
        self.closed = True


class FakeTunnelManager:
    def __init__(self, endpoints=None, test_result=True):
        self.endpoints = list(endpoints or [])
        self.active_endpoint = None
        self._connection_pool: dict[str, list] = {}
        self.terminal_sessions: dict[str, FakeSession] = {}
        self.saved = 0
        self.added: list[tuple] = []
        self.tested: list = []
        self.test_result = test_result

    def save_config(self):
        self.saved += 1

    def add_endpoint(self, hostname, username, private_key_path, platform="unknown"):
        self.added.append((hostname, username, private_key_path, platform))
        endpoint = FakeEndpoint(platform=platform, private_key_path=private_key_path, hostname=hostname)
        self.endpoints.append(endpoint)
        self.saved += 1
        return endpoint

    async def test_endpoint(self, endpoint):
        self.tested.append(endpoint)
        return self.test_result

    def close_terminal_session(self, session_id):
        session = self.terminal_sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True


@pytest.mark.unit
@pytest.mark.asyncio
class TestManageTunnelsRemove:
    async def test_remove_closes_pooled_clients_and_sessions(self):
        endpoint = FakeEndpoint(hostname="gone.example.com")
        manager = FakeTunnelManager([endpoint])
        client = FakeSSHClient()
        other_client = FakeSSHClient()
        manager._connection_pool = {"gone.example.com:22": [client], "keep.example.com:22": [other_client]}
        session = FakeSession(endpoint)
        manager.terminal_sessions = {"sess-1": session}

        with patch_manager("tunnel", manager):
            result = await handle_manage_tunnels({"action": "remove", "hostname": "gone.example.com"})

        assert result.isError is False
        assert client.closed is True, "a pooled SSH client stayed open and authenticated"
        assert session.closed is True, "a terminal session still reaches the removed host"
        assert "gone.example.com:22" not in manager._connection_pool
        assert manager.terminal_sessions == {}
        assert other_client.closed is False, "an unrelated endpoint's connection was closed"
        assert "Closed pooled SSH connections: 1" in text_of(result)
        assert "Closed terminal sessions: 1" in text_of(result)


@pytest.mark.unit
@pytest.mark.asyncio
class TestManageTunnelsAdd:
    @staticmethod
    def _key(tmp_path):
        key = tmp_path / "id_ed25519"
        key.write_text("not a real key", encoding="utf-8")
        return key

    async def test_missing_hostname_is_a_clear_error_not_a_keyerror(self):
        manager = FakeTunnelManager()
        with patch_manager("tunnel", manager):
            result = await handle_manage_tunnels({"action": "add", "username": "agent"})

        assert result.isError is True
        assert "hostname" in text_of(result)
        assert manager.added == []

    async def test_connection_is_actually_tested_and_reported(self, tmp_path):
        manager = FakeTunnelManager(test_result=True)
        with patch_manager("tunnel", manager):
            result = await handle_manage_tunnels(
                {
                    "action": "add",
                    "hostname": "box.example.com",
                    "username": "agent",
                    "private_key_path": str(self._key(tmp_path)),
                }
            )

        assert len(manager.tested) == 1, "add claimed to test the connection but never did"
        assert result.isError is False
        assert "connection test PASSED" in text_of(result)

    async def test_failed_connection_test_is_reported_as_failure(self, tmp_path):
        manager = FakeTunnelManager(test_result=False)
        with patch_manager("tunnel", manager):
            result = await handle_manage_tunnels(
                {
                    "action": "add",
                    "hostname": "box.example.com",
                    "username": "agent",
                    "private_key_path": str(self._key(tmp_path)),
                }
            )

        assert result.isError is True
        assert "connection test FAILED" in text_of(result)

    async def test_missing_private_key_is_refused(self, tmp_path):
        manager = FakeTunnelManager()
        with patch_manager("tunnel", manager):
            result = await handle_manage_tunnels(
                {
                    "action": "add",
                    "hostname": "box.example.com",
                    "username": "agent",
                    "private_key_path": str(tmp_path / "typo_id_ed25519"),
                }
            )

        assert result.isError is True
        assert "Private key not found" in text_of(result)
        assert manager.added == [], "an endpoint with an unusable key was saved anyway"

    async def test_duplicate_hostname_is_refused(self, tmp_path):
        manager = FakeTunnelManager([FakeEndpoint(hostname="box.example.com")])
        with patch_manager("tunnel", manager):
            result = await handle_manage_tunnels(
                {
                    "action": "add",
                    "hostname": "box.example.com",
                    "username": "agent",
                    "private_key_path": str(self._key(tmp_path)),
                }
            )

        assert result.isError is True
        assert "already configured" in text_of(result)
        assert manager.added == []


# --------------------------------------------------------------------------
# FIX 4: duration is honoured or not claimed
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestMonitoringDuration:
    async def test_no_duration_means_one_sample_and_says_so(self):
        manager = RecordingManager(FakeEndpoint(platform="Linux"))
        with patch_manager("monitoring", manager):
            result = await handle_system_monitoring({"metrics": ["cpu", "memory"]})

        assert len(manager.commands) == 2
        assert "Samples: 1 (instantaneous" in text_of(result)
        assert "Duration: 10s" not in text_of(result)

    async def test_duration_produces_more_than_one_sample(self):
        manager = RecordingManager(FakeEndpoint(platform="Linux"))
        started = asyncio.get_running_loop().time()
        with patch_manager("monitoring", manager):
            result = await handle_system_monitoring({"metrics": ["cpu"], "duration": 1})
        elapsed = asyncio.get_running_loop().time() - started

        assert len(manager.commands) == 2, "duration was accepted and then ignored"
        assert elapsed >= 0.9, "the reported observation window never elapsed"
        assert "Samples: 2 over" in text_of(result)
        assert "(requested 1s)" in text_of(result)

    async def test_oversized_duration_is_capped_and_stated(self, monkeypatch):
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda _delay: real_sleep(0))
        manager = RecordingManager(FakeEndpoint(platform="Linux"))
        with patch_manager("monitoring", manager):
            result = await handle_system_monitoring({"metrics": ["cpu"], "duration": 9000})

        assert "capped at 120s" in text_of(result)
        assert len(manager.commands) == 4  # MAX_SAMPLES, not 1800 rounds

    async def test_non_integer_duration_is_rejected(self):
        manager = RecordingManager(FakeEndpoint(platform="Linux"))
        with patch_manager("monitoring", manager):
            result = await handle_system_monitoring({"metrics": ["cpu"], "duration": "60"})

        assert result.isError is True
        assert "must be an integer" in text_of(result)
        assert manager.commands == []


# --------------------------------------------------------------------------
# FIX 6: bounded docker logs, and a visible --rm
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestDockerBounds:
    async def test_logs_are_bounded_by_default(self):
        manager = RecordingManager()
        with patch_manager("docker", manager):
            result = await handle_docker_operations({"operation": "logs", "container_name": "api"})

        assert manager.only_command == "docker logs --tail 200 api"
        assert "last 200 lines" in text_of(result)

    async def test_logs_tail_is_caller_settable(self):
        manager = RecordingManager()
        with patch_manager("docker", manager):
            await handle_docker_operations({"operation": "logs", "container_name": "api", "options": {"tail": 10}})

        assert manager.only_command == "docker logs --tail 10 api"

    async def test_logs_tail_all_is_explicit_and_stated(self):
        manager = RecordingManager()
        with patch_manager("docker", manager):
            result = await handle_docker_operations(
                {"operation": "logs", "container_name": "api", "options": {"tail": "all"}}
            )

        assert manager.only_command == "docker logs api"
        assert "the entire log, as requested" in text_of(result)

    async def test_oversized_output_is_truncated_with_a_note(self):
        manager = RecordingManager(stdout="x" * 50000)
        with patch_manager("docker", manager):
            result = await handle_docker_operations({"operation": "logs", "container_name": "api"})

        body = text_of(result)
        assert len(body) < 25000
        assert "output truncated: 30000 more characters" in body

    async def test_run_states_that_the_container_is_removed_on_exit(self):
        manager = RecordingManager()
        with patch_manager("docker", manager):
            result = await handle_docker_operations({"operation": "run", "image": "python:3.12"})

        assert "--rm" in manager.only_command
        assert "Auto-remove: --rm applied" in text_of(result)

    async def test_run_can_keep_the_container(self):
        manager = RecordingManager()
        with patch_manager("docker", manager):
            result = await handle_docker_operations(
                {"operation": "run", "image": "python:3.12", "options": {"remove": False}}
            )

        assert "--rm" not in manager.only_command
        assert "Auto-remove: disabled" in text_of(result)


# --------------------------------------------------------------------------
# FIX 5: the deleted module must stay deleted
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_workspace_manager_module_is_gone():
    """It registered replacement @server.list_tools/@server.call_tool handlers.

    Importing and calling it would have silently erased all fifteen rigout tools.
    """
    with pytest.raises(ImportError):
        import rigout.mcp_workspace_manager  # noqa: F401
