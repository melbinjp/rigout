from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult

from rigout.tools import (
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
        assert "Command failed" in result.content[0].text
        assert "No such file or directory" in result.content[0].text

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
