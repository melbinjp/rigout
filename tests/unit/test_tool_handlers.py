import asyncio
import json
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent

from rigout import server as rigout_server
from rigout.security_validator import security_validator
from rigout.ssh_manager import TunnelManager
from rigout.terminal_session import LocalTerminalSession
from rigout.tools import (
    file_ops,
    handle_bulk_file_transfer,
    handle_close_terminal_session,
    handle_connect_hardware,
    handle_create_terminal_session,
    handle_docker_operations,
    handle_environment_setup,
    handle_execute_command,
    handle_execute_in_terminal,
    handle_file_operations,
    handle_get_hardware_info,
    handle_install_software,
    handle_list_terminal_sessions,
    handle_manage_tunnels,
    handle_system_monitoring,
)


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolHandlers:
    """Tests for the individual tool handlers by mocking TunnelManager"""

    @pytest.fixture
    def mock_manager(self):
        with (
            patch("rigout.tools.command.get_tunnel_manager") as mock_get_command,
            patch("rigout.tools.docker.get_tunnel_manager") as mock_get_docker,
            patch("rigout.tools.environment.get_tunnel_manager") as mock_get_env,
            patch("rigout.tools.file_ops.get_tunnel_manager") as mock_get_file,
            patch("rigout.tools.monitoring.get_tunnel_manager") as mock_get_mon,
            patch("rigout.tools.tunnel.get_tunnel_manager") as mock_get_tun,
        ):
            manager = MagicMock()
            manager.auto_failover = AsyncMock()
            manager.execute_command = AsyncMock()
            manager.create_terminal_session = AsyncMock()
            manager.execute_in_session = AsyncMock()
            manager.close_terminal_session = MagicMock()
            manager.terminal_sessions = {}

            mock_get_command.return_value = manager
            mock_get_docker.return_value = manager
            mock_get_env.return_value = manager
            mock_get_file.return_value = manager
            mock_get_mon.return_value = manager
            mock_get_tun.return_value = manager

            yield manager

    async def test_handle_execute_command_success(self, mock_manager):
        """Test handle_execute_command when execution is successful"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": True,
            "endpoint": "test-host",
            "command": "ls",
            "exit_code": 0,
            "stdout": "file1.txt\nfile2.txt",
            "stderr": "",
        }

        args = {"command": "ls", "timeout": 15, "working_directory": "/tmp"}
        result = await handle_execute_command(args)

        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert "file1.txt" in result.content[0].text
        assert "Command executed successfully" in result.content[0].text
        mock_manager.execute_command.assert_called_once()

    async def test_handle_execute_command_failure(self, mock_manager):
        """Test handle_execute_command when execution fails"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": False,
            "endpoint": "test-host",
            "command": "ls invalid_dir",
            "exit_code": 2,
            "stderr": "No such file or directory",
        }

        args = {"command": "ls invalid_dir"}
        result = await handle_execute_command(args)

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert "Command failed" in result.content[0].text
        assert "No such file or directory" in result.content[0].text

    async def test_handle_execute_command_failure_falls_back_to_exit_status(self, mock_manager):
        """A silent nonzero command still returns a deterministic diagnostic."""
        mock_manager.auto_failover.return_value = MagicMock()
        mock_manager.execute_command.return_value = {
            "success": False,
            "endpoint": "test-host",
            "command": "false",
            "exit_code": 1,
            "stdout": "",
            "stderr": "",
        }

        result = await handle_execute_command({"command": "false"})

        assert result.isError is True
        assert "Command exited with status 1" in result.content[0].text

    async def test_handle_create_terminal_session(self, mock_manager):
        """Test handle_create_terminal_session"""
        endpoint = MagicMock()
        endpoint.hostname = "test-host"
        mock_manager.auto_failover.return_value = endpoint

        session = MagicMock()
        session.session_id = "sess-123"
        session.endpoint = endpoint
        session.created = MagicMock()
        session.created.isoformat.return_value = "2026-06-30T12:00:00"
        mock_manager.create_terminal_session.return_value = session

        args = {"session_name": "test_session"}
        result = await handle_create_terminal_session(args)

        assert isinstance(result, CallToolResult)
        assert "Terminal session created successfully" in result.content[0].text
        assert "sess-123" in result.content[0].text

    async def test_handle_execute_in_terminal(self, mock_manager):
        """Test handle_execute_in_terminal"""
        mock_manager.execute_in_session = AsyncMock(
            return_value={"success": True, "command": "whoami", "output": "agent"}
        )

        args = {"session_id": "sess-123", "command": "whoami"}
        result = await handle_execute_in_terminal(args)

        assert isinstance(result, CallToolResult)
        assert "agent" in result.content[0].text

    async def test_handle_list_terminal_sessions(self, mock_manager):
        """Test handle_list_terminal_sessions"""
        # Empty sessions
        mock_manager.terminal_sessions = {}
        result = await handle_list_terminal_sessions({})
        assert "No active terminal sessions" in result.content[0].text

        # With active session
        session = MagicMock()
        session.endpoint.hostname = "test-host"
        session.created.isoformat.return_value = "2026"
        session.last_activity.isoformat.return_value = "2026"
        session.is_interactive = False
        mock_manager.terminal_sessions = {"sess-123": session}

        result = await handle_list_terminal_sessions({})
        assert "Active Terminal Sessions" in result.content[0].text
        assert "sess-123" in result.content[0].text

    async def test_handle_close_terminal_session(self, mock_manager):
        """Test handle_close_terminal_session"""
        mock_manager.close_terminal_session.return_value = True
        result = await handle_close_terminal_session({"session_id": "sess-123"})
        assert "closed successfully" in result.content[0].text

        mock_manager.close_terminal_session.return_value = False
        result = await handle_close_terminal_session({"session_id": "sess-123"})
        assert result.isError is True
        assert "Failed to close" in result.content[0].text

    async def test_handle_install_software(self, mock_manager):
        """Test handle_install_software"""
        endpoint = MagicMock()
        endpoint.platform = "Ubuntu 22.04"
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": True,
            "endpoint": "test-host",
            "stdout": "Installed successfully",
        }

        args = {"packages": ["curl", "git"], "package_manager": "apt"}
        result = await handle_install_software(args)
        assert "completed successfully" in result.content[0].text

    async def test_handle_install_software_pacman(self, mock_manager):
        """The advertised pacman manager installs instead of erroring as unsupported"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": True,
            "endpoint": "test-host",
            "stdout": "Installed successfully",
        }

        args = {"packages": ["curl", "git"], "package_manager": "pacman"}
        result = await handle_install_software(args)

        assert result.isError is False
        assert "completed successfully" in result.content[0].text
        assert mock_manager.execute_command.call_args.args[1] == "sudo pacman -S --noconfirm curl git"

    async def test_handle_docker_operations(self, mock_manager):
        """Test handle_docker_operations"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": True,
            "endpoint": "test-host",
            "command": "docker ps",
            "stdout": "Docker command success",
        }

        args = {"operation": "list"}
        result = await handle_docker_operations(args)
        assert "Docker command success" in result.content[0].text

    async def test_handle_docker_failure_preserves_exit_status(self, mock_manager):
        """Docker failures must be flagged and never collapse to Unknown error."""
        mock_manager.auto_failover.return_value = MagicMock()
        mock_manager.execute_command.return_value = {
            "success": False,
            "endpoint": "test-host",
            "command": "docker ps -a",
            "exit_code": 127,
            "stdout": "",
            "stderr": "",
        }

        result = await handle_docker_operations({"operation": "list"})

        assert result.isError is True
        assert "Command exited with status 127" in result.content[0].text

    async def test_handle_environment_setup(self, mock_manager):
        """Test handle_environment_setup"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": True,
            "endpoint": "test-host",
            "stdout": "Virtual environment created",
        }

        args = {"environment_type": "python", "workspace_path": "/tmp/env"}
        result = await handle_environment_setup(args)
        assert "Virtual environment created" in result.content[0].text

    async def test_handle_file_operations(self, mock_manager):
        """Test handle_file_operations"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {"success": True, "endpoint": "test-host", "stdout": "file1\nfile2"}

        args = {"operation": "read", "path": "/tmp/file.txt"}
        result = await handle_file_operations(args)
        assert "file1" in result.content[0].text

    async def test_handle_bulk_file_transfer(self, mock_manager):
        """Test handle_bulk_file_transfer"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {
            "success": True,
            "endpoint": "test-host",
            "stdout": "uploaded 10 bytes",
        }

        args = {"operation": "upload", "source": "hello", "destination": "/tmp/hello.txt"}
        result = await handle_bulk_file_transfer(args)
        assert "uploaded 10 bytes" in result.content[0].text

    async def test_handle_system_monitoring(self, mock_manager):
        """Test handle_system_monitoring"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint
        mock_manager.execute_command.return_value = {"success": True, "endpoint": "test-host", "stdout": "CPU: 5%"}

        args = {"metrics": ["cpu"]}
        result = await handle_system_monitoring(args)
        assert "CPU: 5%" in result.content[0].text

    async def test_handle_system_monitoring_runs_metrics_concurrently(self, mock_manager):
        """Independent metrics no longer incur sequential command latency."""
        endpoint = MagicMock()
        endpoint.platform = "Linux"
        mock_manager.auto_failover.return_value = endpoint
        active = 0
        maximum_active = 0

        async def execute(_endpoint, command):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"success": True, "stdout": command, "stderr": "", "exit_code": 0}

        mock_manager.execute_command.side_effect = execute

        result = await handle_system_monitoring({"metrics": ["cpu", "memory", "disk"]})

        assert result.isError is False
        assert maximum_active > 1
        assert mock_manager.execute_command.call_count == 3

    async def test_handle_system_monitoring_keeps_partial_results(self, mock_manager):
        """One failed metric is flagged without discarding successful metrics."""
        endpoint = MagicMock()
        endpoint.platform = "Linux"
        mock_manager.auto_failover.return_value = endpoint

        async def execute(_endpoint, command):
            if command == "free -h":
                return {"success": False, "stderr": "memory unavailable", "exit_code": 9}
            return {"success": True, "stdout": "cpu available", "stderr": "", "exit_code": 0}

        mock_manager.execute_command.side_effect = execute

        result = await handle_system_monitoring({"metrics": ["cpu", "memory"]})

        assert result.isError is True
        assert "cpu available" in result.content[0].text
        assert "memory unavailable" in result.content[0].text

    async def test_handle_get_hardware_info(self, mock_manager):
        """Test handle_get_hardware_info"""
        endpoint = MagicMock()
        mock_manager.auto_failover.return_value = endpoint

        info = MagicMock()
        info.cpu_count = 8
        info.memory_gb = 16.0
        info.gpu_info = ["RTX 3080"]
        info.disk_space_gb = 512.0
        info.platform = "Linux"
        info.architecture = "x86_64"
        mock_manager.get_hardware_info = AsyncMock(return_value=info)

        result = await handle_get_hardware_info({})
        assert "Hardware Information" in result.content[0].text
        assert "CPUs: 8" in result.content[0].text
        assert "GPU: RTX 3080" in result.content[0].text

    async def test_handle_connect_hardware(self, mock_manager):
        """Test handle_connect_hardware"""
        endpoint = MagicMock()
        endpoint.hostname = "test-host"
        endpoint.username = "agent"
        endpoint.status = "active"
        endpoint.response_time = 0.25
        mock_manager.auto_failover.return_value = endpoint

        info = MagicMock()
        info.cpu_count = 4
        info.memory_gb = 8.0
        info.gpu_info = []
        info.disk_space_gb = 100.0
        info.platform = "Linux"
        info.architecture = "x86_64"
        mock_manager.get_hardware_info = AsyncMock(return_value=info)

        result = await handle_connect_hardware({})
        assert "Connected to hardware" in result.content[0].text
        assert "test-host" in result.content[0].text

    async def test_handle_manage_tunnels(self, mock_manager):
        """Test handle_manage_tunnels"""
        # list action
        endpoint = MagicMock()
        endpoint.hostname = "test-host"
        endpoint.username = "agent"
        endpoint.port = 22
        endpoint.purpose = "primary"
        endpoint.status = "active"
        endpoint.response_time = 0.25

        mock_manager.endpoints = [endpoint]

        result = await handle_manage_tunnels({"action": "list"})
        assert "Configured Tunnel Endpoints" in result.content[0].text
        assert "test-host" in result.content[0].text


class StubLocalSession(LocalTerminalSession):
    """A LocalTerminalSession that records commands instead of running a shell."""

    def __init__(self, session_id: str = "sess-local", output: str = ""):
        self.session_id = session_id
        self.endpoint = MagicMock()
        self.created = datetime.now()
        self.last_activity = datetime.now()
        self.is_interactive = True
        self.output = output
        self.executed: list[str] = []

    def execute(self, command: str, timeout: int = 30) -> dict:
        self.executed.append(command)
        return {
            "success": True,
            "exit_code": 0,
            "output": self.output,
            "session_id": self.session_id,
            "command": command,
        }


def make_ssh_session(output: str) -> MagicMock:
    """A non-local terminal session whose channel replays one chunk of output."""
    session = MagicMock()
    session.channel.recv_ready.return_value = True
    session.channel.recv.return_value = f"{output}\nuser@host:~$ ".encode()
    return session


@pytest.mark.unit
@pytest.mark.asyncio
class TestTerminalSessionSecurity:
    """Terminal sessions must validate commands and sanitize output like execute_command"""

    @pytest.fixture
    def temp_config_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            config = {
                "server_config": {"name": "test-server", "version": "1.0.0"},
                "ssh_config": {"private_key_path": "/test/key", "username": "testuser"},
                "cloudflare_config": {"domain": "test.com"},
                "security_config": {"ai_agent_mode": True},
                "endpoints": [],
            }
            json.dump(config, f)
            f.flush()
            yield f.name
        if os.path.exists(f.name):
            os.unlink(f.name)

    @pytest.fixture
    def real_manager(self, temp_config_file):
        """The real TunnelManager, wired into the command tool handlers."""
        with (
            patch("rigout.ssh_manager.TunnelManager._start_background_tasks"),
            patch("rigout.tools.command.get_tunnel_manager") as mock_get_command,
        ):
            manager = TunnelManager(config_file=temp_config_file)
            mock_get_command.return_value = manager
            yield manager

    async def test_local_terminal_output_is_sanitized(self, real_manager):
        """Secrets in local terminal session output are redacted before reaching the agent."""
        session = StubLocalSession(output="DB_PASSWORD=hunter2\napi_key=sk-live-abcdef\n")
        real_manager.terminal_sessions["sess-local"] = session

        result = await handle_execute_in_terminal({"session_id": "sess-local", "command": "env"})

        assert result.isError is False
        assert "hunter2" not in result.content[0].text
        assert "sk-live-abcdef" not in result.content[0].text
        assert "password=***" in result.content[0].text
        assert "api_key=***" in result.content[0].text

    async def test_ssh_terminal_output_is_sanitized(self, real_manager):
        """Secrets in SSH terminal session output are redacted before reaching the agent."""
        real_manager.terminal_sessions["sess-ssh"] = make_ssh_session("DB_PASSWORD=hunter2 token=abc123")

        result = await handle_execute_in_terminal({"session_id": "sess-ssh", "command": "env"})

        assert result.isError is False
        assert "hunter2" not in result.content[0].text
        assert "abc123" not in result.content[0].text
        assert "password=***" in result.content[0].text
        assert "token=***" in result.content[0].text

    async def test_destructive_command_in_terminal_is_rejected(self, real_manager):
        """A destructive command never reaches the session shell."""
        session = StubLocalSession()
        real_manager.terminal_sessions["sess-local"] = session

        result = await handle_execute_in_terminal({"session_id": "sess-local", "command": "rm -rf /"})

        assert result.isError is True
        assert "Security validation failed" in result.content[0].text
        assert session.executed == []

    async def test_bypass_security_runs_destructive_command_in_terminal(self, real_manager):
        """The documented escape hatch works the same way as on the one-shot path."""
        session = StubLocalSession(output="removed")
        real_manager.terminal_sessions["sess-local"] = session

        result = await handle_execute_in_terminal(
            {"session_id": "sess-local", "command": "rm -rf /", "bypass_security": True}
        )

        assert result.isError is False
        assert session.executed == ["rm -rf /"]

    async def test_sudo_in_terminal_requires_use_sudo(self, real_manager):
        """Sudo is gated by use_sudo in a session, exactly as on the one-shot path."""
        session = StubLocalSession(output="restarted")
        real_manager.terminal_sessions["sess-local"] = session

        blocked = await handle_execute_in_terminal(
            {"session_id": "sess-local", "command": "sudo systemctl restart nginx"}
        )

        assert blocked.isError is True
        assert "Sudo commands not allowed" in blocked.content[0].text
        assert session.executed == []

        allowed = await handle_execute_in_terminal(
            {"session_id": "sess-local", "command": "systemctl restart nginx", "use_sudo": True}
        )

        assert allowed.isError is False
        assert session.executed == ["sudo systemctl restart nginx"]

    async def test_terminal_session_commands_are_rate_limited(self, real_manager):
        """Commands in a session are counted, exactly as one-shot commands are."""
        session = StubLocalSession(output="agent")
        real_manager.terminal_sessions["sess-local"] = session
        real_manager._max_requests_per_minute = 2

        for _ in range(2):
            allowed = await handle_execute_in_terminal({"session_id": "sess-local", "command": "whoami"})
            assert allowed.isError is False

        limited = await handle_execute_in_terminal({"session_id": "sess-local", "command": "whoami"})

        assert limited.isError is True
        assert "Rate limit exceeded" in limited.content[0].text
        assert session.executed == ["whoami", "whoami"]

    async def test_terminal_session_shares_the_endpoint_command_budget(self, real_manager):
        """A session does not get a second budget of its own on top of the endpoint's."""
        endpoint = real_manager.get_local_endpoint()
        session = StubLocalSession(output="agent")
        session.endpoint = endpoint
        real_manager.terminal_sessions["sess-local"] = session
        real_manager._max_requests_per_minute = 1

        # Spend the endpoint's only slot the way execute_command would
        assert real_manager._check_rate_limit(real_manager._execute_rate_limit_key(endpoint)) is True

        limited = await handle_execute_in_terminal({"session_id": "sess-local", "command": "whoami"})

        assert limited.isError is True
        assert "Rate limit exceeded" in limited.content[0].text
        assert session.executed == []


@pytest.mark.unit
@pytest.mark.asyncio
class TestCallToolErrorChannel:
    """The MCP error round trip must not discard content blocks silently."""

    async def _raise_from(self, content):
        result = CallToolResult(content=content, isError=True)
        with patch("rigout.server._handle_call_tool_result", AsyncMock(return_value=result)):
            with pytest.raises(RuntimeError) as excinfo:
                await rigout_server.handle_call_tool("file_operations", {})
        return str(excinfo.value)

    async def test_text_only_error_message_is_unchanged(self):
        """The common case must keep the exact message it has today."""
        message = await self._raise_from([TextContent(type="text", text="File operation failed")])
        assert message == "File operation failed"

    async def test_empty_error_content_falls_back_to_tool_name(self):
        message = await self._raise_from([])
        assert message == "Tool 'file_operations' failed"

    async def test_non_text_error_content_is_not_dropped_silently(self):
        """An image or resource on an error path must survive into the message.

        The SDK can only signal isError by raising, and renders the exception as a
        single string, so these cannot survive as blocks. They must not vanish.
        """
        message = await self._raise_from(
            [
                TextContent(type="text", text="Capture failed"),
                ImageContent(type="image", data="aGk=", mimeType="image/png"),
                ResourceLink(
                    type="resource_link",
                    uri="file:///var/log/rigout.log",
                    name="rigout.log",
                ),
            ]
        )

        assert "Capture failed" in message
        assert "image" in message
        assert "file:///var/log/rigout.log" in message

    async def test_error_content_without_any_text_block_is_still_reported(self):
        """A purely non-text error must not collapse to a bare fallback."""
        message = await self._raise_from([ImageContent(type="image", data="aGk=", mimeType="image/png")])

        assert message != "Tool 'file_operations' failed"
        assert "image" in message


@pytest.mark.unit
@pytest.mark.asyncio
class TestFileOperationsParity:
    """The local and SSH branches of file_ops must not disagree about safety."""

    SECRET = "export API_KEY=sk-live-ABCDEFG\npassword=hunter2\ntoken=ghp_realtokenvalue\n"

    @pytest.fixture
    def local_manager(self):
        endpoint = MagicMock()
        endpoint.private_key_path = "__local__"
        manager = MagicMock()
        manager.auto_failover = AsyncMock(return_value=endpoint)
        with patch("rigout.tools.file_ops.get_tunnel_manager", return_value=manager):
            yield manager

    @pytest.fixture
    def remote_manager(self):
        endpoint = MagicMock()
        endpoint.private_key_path = "/home/agent/.ssh/id_ed25519"
        manager = MagicMock()
        manager.auto_failover = AsyncMock(return_value=endpoint)
        manager.execute_command = AsyncMock(return_value={"success": True, "endpoint": "test-host", "stdout": ""})
        with patch("rigout.tools.file_ops.get_tunnel_manager", return_value=manager):
            yield manager

    @pytest.fixture
    def secret_file(self, tmp_path):
        target = tmp_path / "secretfile.env"
        target.write_text(self.SECRET, encoding="utf-8")
        return target

    async def test_local_read_redacts_credentials(self, local_manager, secret_file):
        """A secret refused through execute_command must not be handed over here."""
        result = await handle_file_operations({"operation": "read", "path": str(secret_file)})

        text = result.content[0].text
        assert "sk-live-ABCDEFG" not in text
        assert "hunter2" not in text
        assert "ghp_realtokenvalue" not in text
        assert "api_key=***" in text
        assert "password=***" in text

    async def test_local_bulk_download_redacts_credentials(self, local_manager, secret_file, tmp_path):
        """The same leak was reachable through the transfer tool."""
        result = await handle_bulk_file_transfer(
            {"operation": "download", "source": str(secret_file), "destination": str(tmp_path / "out")}
        )

        text = result.content[0].text
        assert "sk-live-ABCDEFG" not in text
        assert "hunter2" not in text
        assert "api_key=***" in text

    async def test_local_read_is_bounded_and_says_so(self, local_manager, tmp_path):
        """An unbounded read exhausts memory and floods the model's context."""
        big = tmp_path / "big.txt"
        big.write_bytes(b"A" * (file_ops.MAX_READ_BYTES + 5000))

        result = await handle_file_operations({"operation": "read", "path": str(big)})

        text = result.content[0].text
        assert len(text) < file_ops.MAX_READ_BYTES + 500
        assert "truncated" in text

    async def test_local_delete_refuses_a_directory_without_recursive(self, local_manager, tmp_path):
        """Passing a directory by mistake must not silently destroy the tree."""
        target = tmp_path / "logdir"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "keep.txt").write_text("important", encoding="utf-8")

        result = await handle_file_operations({"operation": "delete", "path": str(target)})

        assert result.isError is True
        assert "recursive=true" in result.content[0].text
        assert (target / "nested" / "keep.txt").exists()

    async def test_local_delete_removes_a_directory_when_asked(self, local_manager, tmp_path):
        target = tmp_path / "logdir"
        target.mkdir()
        (target / "a.txt").write_text("x", encoding="utf-8")

        result = await handle_file_operations({"operation": "delete", "path": str(target), "recursive": True})

        assert result.isError is False
        assert not target.exists()

    async def test_remote_delete_is_recursive_only_when_asked(self, remote_manager):
        """The SSH branch must follow the same rule as the local branch."""
        await handle_file_operations({"operation": "delete", "path": "/tmp/logdir"})
        plain = remote_manager.execute_command.await_args.args[1]

        await handle_file_operations({"operation": "delete", "path": "/tmp/logdir", "recursive": True})
        recursive = remote_manager.execute_command.await_args.args[1]

        assert plain.startswith("rm -f ")
        assert recursive.startswith("rm -rf ")

    @pytest.mark.parametrize(
        "content",
        [
            "Use the `rm` builtin carefully.\n",
            "VER=$(git rev-parse HEAD)\n",
            "cd /tmp && rm stale.log\n",
            "it's got an apostrophe\n",
            "'; rm -rf / #\n",
            "-leading-dash\n",
            "100% coverage\n",
            "",
        ],
    )
    async def test_remote_write_and_append_accept_ordinary_file_content(self, remote_manager, content):
        """Ordinary file content must be writable and must stay inert.

        Content reaches the shell single-quoted, so it can never be read as syntax
        no matter what it holds. Every command built here therefore has to pass
        validation on its own merits: writes must not need a security bypass.
        """
        for operation in ("write", "append"):
            await handle_file_operations(
                {"operation": operation, "path": "/tmp/notes.md", "content": content}
            )
            command = remote_manager.execute_command.await_args.args[1]

            is_safe, err = security_validator.validate_command(command)
            assert is_safe is True, f"{operation} of {content!r} was refused: {err}"
            assert not remote_manager.execute_command.await_args.kwargs.get("bypass_security", False)

    async def test_remote_upload_accepts_ordinary_content_without_a_bypass(self, remote_manager):
        await handle_bulk_file_transfer({"operation": "upload", "source": "x=$(id)\n", "destination": "/tmp/x.sh"})

        call = remote_manager.execute_command.await_args
        assert not call.kwargs.get("bypass_security", False)
        assert security_validator.validate_command(call.args[1])[0] is True

    @pytest.mark.parametrize("operation", ["write", "append"])
    @pytest.mark.parametrize("device", ["/dev/sda", "/dev/nvme0n1", "/dev/mem"])
    async def test_remote_write_to_a_raw_device_is_still_refused(self, remote_manager, operation, device):
        """Not bypassing validation is what keeps this protection reachable.

        A write is a real command once the content is quoted, so redirecting one at
        a raw disk stays blocked. Passing bypass_security would have discarded this.

        Append is covered because it is a separate code path, not because it is
        fragile: two independent rules refuse it. `_semantic_danger` lists `>>` in
        its redirect tokens by design, and the `[<>]` DANGEROUS_PATTERNS entry also
        matches the first chevron of `>>`. Disabling either one alone leaves this
        test green, which is the point of asserting the property rather than the
        mechanism -- the test survives a rewrite of either rule.
        """
        await handle_file_operations({"operation": operation, "path": device, "content": "x"})

        command = remote_manager.execute_command.await_args.args[1]
        is_safe, err = security_validator.validate_command(command)
        assert is_safe is False, f"{operation} to {device} must be refused: {command!r}"
        assert "dangerous pattern" in err.lower()

    async def test_remote_operations_never_bypass_validation(self, remote_manager):
        """No file operation disables the validator, including the content ones."""
        for arguments in (
            {"operation": "read", "path": "/tmp/a"},
            {"operation": "write", "path": "/tmp/a", "content": "x"},
            {"operation": "append", "path": "/tmp/a", "content": "x"},
            {"operation": "delete", "path": "/tmp/a"},
            {"operation": "copy", "path": "/tmp/a", "destination": "/tmp/b"},
            {"operation": "move", "path": "/tmp/a", "destination": "/tmp/b"},
            {"operation": "chmod", "path": "/tmp/a", "permissions": "755"},
        ):
            await handle_file_operations(arguments)
            assert not remote_manager.execute_command.await_args.kwargs.get("bypass_security", False)

    async def test_remote_read_is_bounded_at_the_source(self, remote_manager):
        """Bounding only the local branch would recreate the asymmetry being fixed."""
        expected = f"head -c {file_ops.MAX_READ_BYTES + 1} "

        await handle_file_operations({"operation": "read", "path": "/tmp/big.log"})
        assert remote_manager.execute_command.await_args.args[1].startswith(expected)

        await handle_bulk_file_transfer({"operation": "download", "source": "/tmp/big.log", "destination": "/tmp/x"})
        assert remote_manager.execute_command.await_args.args[1].startswith(expected)
