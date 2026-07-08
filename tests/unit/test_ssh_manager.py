import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import paramiko
import pytest

from rigout.ssh_manager import (
    ConfigurationError,
    SecurityError,
    TunnelEndpoint,
    TunnelManager,
)
from rigout.terminal_session import TerminalSession


@pytest.mark.unit
class TestTunnelEndpoint:
    """Test TunnelEndpoint class functionality"""

    def test_valid_endpoint_creation(self):
        """Test creating a valid tunnel endpoint"""
        endpoint = TunnelEndpoint(
            hostname="test.example.com", username="testuser", private_key_path="/path/to/key", port=22
        )
        assert endpoint.hostname == "test.example.com"
        assert endpoint.username == "testuser"
        assert endpoint.port == 22
        assert endpoint.status == "unknown"

    def test_invalid_hostname_raises_error(self):
        """Test that invalid hostnames raise SecurityError"""
        with pytest.raises(SecurityError):
            TunnelEndpoint(hostname="invalid..hostname", username="testuser", private_key_path="/path/to/key")

    def test_invalid_port_raises_error(self):
        """Test that invalid ports raise ConfigurationError"""
        with pytest.raises(ConfigurationError):
            TunnelEndpoint(
                hostname="test.example.com",
                username="testuser",
                private_key_path="/path/to/key",
                port=70000,  # Invalid port
            )

    def test_empty_hostname_raises_error(self):
        """Test that empty hostname raises ConfigurationError"""
        with pytest.raises(ConfigurationError):
            TunnelEndpoint(hostname="", username="testuser", private_key_path="/path/to/key")

    def test_hostname_validation(self):
        """Test hostname validation logic"""
        endpoint = TunnelEndpoint(hostname="valid-hostname.com", username="testuser", private_key_path="/path/to/key")

        # Test valid hostnames
        assert endpoint._is_valid_hostname("example.com")
        assert endpoint._is_valid_hostname("sub.example.com")
        assert endpoint._is_valid_hostname("test-server.local")

        # Test invalid hostnames
        assert not endpoint._is_valid_hostname("-invalid.com")
        assert not endpoint._is_valid_hostname("invalid-.com")
        assert not endpoint._is_valid_hostname("invalid..com")
        assert not endpoint._is_valid_hostname("a" * 254)  # Too long


@pytest.mark.unit
class TestTerminalSession:
    """Test TerminalSession class functionality"""

    def test_session_creation(self):
        """Test creating a terminal session"""
        endpoint = TunnelEndpoint(hostname="test.example.com", username="testuser", private_key_path="/path/to/key")

        mock_ssh = Mock()
        mock_channel = Mock()

        session = TerminalSession(
            session_id="test-session",
            endpoint=endpoint,
            ssh_client=mock_ssh,
            channel=mock_channel,
            created=datetime.now(),
            last_activity=datetime.now(),
        )

        assert session.session_id == "test-session"
        assert session.endpoint == endpoint
        assert len(session.command_history) == 0

    def test_session_expiration(self):
        """Test session expiration logic"""
        endpoint = TunnelEndpoint(hostname="test.example.com", username="testuser", private_key_path="/path/to/key")

        # Create expired session
        old_time = datetime.now() - timedelta(hours=2)
        session = TerminalSession(
            session_id="expired-session",
            endpoint=endpoint,
            ssh_client=Mock(),
            channel=Mock(),
            created=old_time,
            last_activity=old_time,
            max_idle_time=3600,  # 1 hour
        )

        assert session.is_expired()

    def test_command_history(self):
        """Test command history management"""
        endpoint = TunnelEndpoint(hostname="test.example.com", username="testuser", private_key_path="/path/to/key")

        session = TerminalSession(
            session_id="test-session",
            endpoint=endpoint,
            ssh_client=Mock(),
            channel=Mock(),
            created=datetime.now(),
            last_activity=datetime.now(),
        )

        # Add commands
        session.add_command("ls -la")
        session.add_command("pwd")

        assert len(session.command_history) == 2
        assert session.command_history[0] == "ls -la"
        assert session.command_history[1] == "pwd"

        # Test history size limit
        for i in range(150):
            session.add_command(f"command_{i}")

        assert len(session.command_history) == 100  # Should be limited to 100


@pytest.mark.unit
class TestTunnelManager:
    """Test TunnelManager class functionality"""

    @pytest.fixture
    def temp_config_file(self):
        """Create a temporary config file for testing"""
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

    def test_tunnel_manager_initialization(self, temp_config_file):
        """Test TunnelManager initialization"""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            assert manager.config_file == temp_config_file
            assert isinstance(manager.endpoints, list)
            assert isinstance(manager.hardware_cache, dict)
            assert isinstance(manager.terminal_sessions, dict)

    def test_config_validation(self):
        """Test configuration validation raises exception on failure"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # Invalid config - missing required sections
            invalid_config = {"invalid": "config"}
            json.dump(invalid_config, f)
            f.flush()

            with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
                with pytest.raises(ConfigurationError):  # raises ConfigurationError on structural check
                    TunnelManager(config_file=f.name)

        if os.path.exists(f.name):
            os.unlink(f.name)

    def test_rate_limiting(self, temp_config_file):
        """Test rate limiting functionality"""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            manager._max_requests_per_minute = 50

            # Test normal requests
            for _i in range(50):
                assert manager._check_rate_limit("test_client")

            # Should be rate limited now
            assert not manager._check_rate_limit("test_client")

    @pytest.mark.asyncio
    async def test_endpoint_testing(self, temp_config_file):
        """Test endpoint connectivity testing"""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)

            endpoint = TunnelEndpoint(hostname="test.example.com", username="testuser", private_key_path="/test/key")

            # Mock SSH connection
            with patch("paramiko.SSHClient") as mock_ssh_class:
                mock_ssh = Mock()
                mock_ssh_class.return_value = mock_ssh

                # Mock successful connection
                mock_stdout = Mock()
                mock_stdout.read.return_value = b"connection_test_1234567890"
                mock_stderr = Mock()
                mock_stderr.read.return_value = b""

                mock_ssh.exec_command.return_value = (Mock(), mock_stdout, mock_stderr)

                # Mock key loading
                with patch("paramiko.Ed25519Key.from_private_key_file"):
                    with patch("os.path.exists", return_value=True):
                        result = await manager.test_endpoint(endpoint)

                        assert result is True
                        assert endpoint.status == "active"
                        assert endpoint.response_time is not None

    @pytest.mark.asyncio
    async def test_endpoint_testing_failure(self, temp_config_file):
        """Test endpoint testing with connection failure"""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)

            endpoint = TunnelEndpoint(hostname="test.example.com", username="testuser", private_key_path="/test/key")

            # Mock SSH connection failure
            with patch("paramiko.SSHClient") as mock_ssh_class:
                mock_ssh = Mock()
                mock_ssh_class.return_value = mock_ssh
                mock_ssh.connect.side_effect = paramiko.AuthenticationException("Auth failed")

                with patch("os.path.exists", return_value=True):
                    result = await manager.test_endpoint(endpoint)

                    assert result is False
                    assert endpoint.status == "failed"

    def test_add_endpoint(self, temp_config_file):
        """Test adding new endpoints"""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)

            initial_count = len(manager.endpoints)

            endpoint = manager.add_endpoint(hostname="new.example.com", username="newuser", private_key_path="/new/key")

            assert len(manager.endpoints) == initial_count + 1
            assert endpoint.hostname == "new.example.com"
            assert endpoint.username == "newuser"

    @pytest.mark.asyncio
    async def test_find_best_endpoint(self, temp_config_file):
        """Test finding the best available endpoint"""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)

            # Add test endpoints
            endpoint1 = TunnelEndpoint(hostname="slow.example.com", username="testuser", private_key_path="/test/key")
            endpoint2 = TunnelEndpoint(hostname="fast.example.com", username="testuser", private_key_path="/test/key")

            manager.endpoints = [endpoint1, endpoint2]

            # Mock endpoint testing
            async def mock_test_endpoint(endpoint):
                if endpoint.hostname == "slow.example.com":
                    endpoint.status = "active"
                    endpoint.response_time = 2.0
                    return True
                elif endpoint.hostname == "fast.example.com":
                    endpoint.status = "active"
                    endpoint.response_time = 0.5
                    return True
                return False

            manager.test_endpoint = mock_test_endpoint

            best_endpoint = await manager.find_best_endpoint()

            assert best_endpoint is not None
            assert best_endpoint.hostname == "fast.example.com"
            assert best_endpoint.response_time == 0.5

    @pytest.mark.asyncio
    async def test_auto_failover_returns_local_endpoint_when_no_ssh_endpoints(self, temp_config_file):
        """A fresh URL server should be usable without preconfigured SSH endpoints."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)

            endpoint = await manager.auto_failover()

            assert endpoint is not None
            assert endpoint.hostname == "local-device"
            assert endpoint.private_key_path == "__local__"
            assert endpoint.status == "active"

    @pytest.mark.asyncio
    async def test_local_endpoint_executes_commands(self, temp_config_file):
        """The local fallback can execute commands on the Rigout host."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            endpoint = manager.get_local_endpoint()

            result = await manager.execute_command(endpoint, "echo rigout-local-test")

            assert result["success"] is True
            assert "rigout-local-test" in result["stdout"]

    @pytest.mark.asyncio
    async def test_local_command_working_directory_and_environment(self, temp_config_file, tmp_path):
        """Local execution honors working_directory and environment natively."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            endpoint = manager.get_local_endpoint()

            command = "cd" if os.name == "nt" else "pwd"
            result = await manager.execute_command(endpoint, command, working_directory=str(tmp_path))
            assert result["success"] is True
            assert tmp_path.name in result["stdout"]

            command = "echo %RIGOUT_TEST_VAR%" if os.name == "nt" else "echo $RIGOUT_TEST_VAR"
            result = await manager.execute_command(endpoint, command, environment={"RIGOUT_TEST_VAR": "sentinel-42"})
            assert result["success"] is True
            assert "sentinel-42" in result["stdout"]

    @pytest.mark.asyncio
    async def test_local_command_rejects_missing_working_directory(self, temp_config_file, tmp_path):
        """A nonexistent working directory is reported instead of crashing."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            endpoint = manager.get_local_endpoint()

            result = await manager.execute_command(endpoint, "pwd", working_directory=str(tmp_path / "missing"))
            assert result["success"] is False
            assert "does not exist" in result["error"]

    @pytest.mark.asyncio
    async def test_local_terminal_session_lifecycle(self, temp_config_file):
        """Terminal sessions work on the local endpoint without SSH."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            endpoint = manager.get_local_endpoint()

            session = await manager.create_terminal_session(endpoint, "unit-session")
            assert session is not None
            assert session.session_id == "unit-session"
            assert "unit-session" in manager.terminal_sessions

            result = await manager.execute_in_session("unit-session", "echo rigout-session-test", timeout=15)
            assert result["success"] is True
            assert "rigout-session-test" in result["output"]

            # State persists between commands within the session
            set_var = "set RIGOUT_SESSION_VAR=persisted" if os.name == "nt" else "RIGOUT_SESSION_VAR=persisted"
            echo_var = "echo %RIGOUT_SESSION_VAR%" if os.name == "nt" else "echo $RIGOUT_SESSION_VAR"
            result = await manager.execute_in_session("unit-session", set_var, timeout=15)
            assert result["success"] is True
            result = await manager.execute_in_session("unit-session", echo_var, timeout=15)
            assert result["success"] is True
            assert "persisted" in result["output"]

            assert manager.close_terminal_session("unit-session") is True
            assert "unit-session" not in manager.terminal_sessions

    def test_save_config_preserves_other_sections(self, temp_config_file):
        """Saving endpoints must not destroy unrelated config sections."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            manager.save_config()

            with open(temp_config_file, encoding="utf-8") as f:
                data = json.load(f)

            assert data["server_config"] == {"name": "test-server", "version": "1.0.0"}
            assert data["ssh_config"] == {"private_key_path": "/test/key", "username": "testuser"}
            assert data["cloudflare_config"] == {"domain": "test.com"}
            assert data["endpoints"] == []
            assert "last_updated" in data
