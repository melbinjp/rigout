import json
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from rigout.config_manager import ConfigManager


@pytest.fixture
def mock_ssh_client():
    """Provides a mocked paramiko SSHClient with pre-configured command execution results."""
    with patch("paramiko.SSHClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Default stdout mock
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"connection_test_1234567890"
        mock_stdout.channel.recv_exit_status.return_value = 0

        # Default stderr mock
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        # exec_command returns (stdin, stdout, stderr)
        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        # Transport mock
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client.get_transport.return_value = mock_transport

        # Channel mock for interactive shell
        mock_channel = MagicMock()
        mock_channel.recv.return_value = b"mock shell output"
        mock_client.invoke_shell.return_value = mock_channel

        yield mock_client


@pytest.fixture
def mock_config():
    """Provides a temporary, mock ConfigManager instance."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        config = {
            "server_config": {
                "name": "test-server",
                "version": "1.0.0",
                "log_level": "INFO",
                "max_connections": 10,
                "request_timeout": 30,
                "session_timeout": 3600,
            },
            "ssh_config": {
                "private_key_path": "/test/key",
                "public_key_path": "/test/key.pub",
                "username": "testuser",
                "default_port": 22,
            },
            "cloudflare_config": {
                "email": "test@example.com",
                "api_key": "testkey",
                "domain": "test.com",
                "auto_tunnel_creation": False,
            },
            "security_config": {
                "enable_rate_limiting": True,
                "max_requests_per_minute": 60,
                "enable_command_validation": True,
                "enable_audit_logging": True,
                "ai_agent_mode": True,
            },
            "endpoints": [],
        }
        json.dump(config, f)
        f.flush()
        temp_path = f.name

    try:
        manager = ConfigManager(temp_path)
        manager.load_config()
        yield manager
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


@pytest.fixture
def tmp_workspace():
    """Provides a temporary directory path that is automatically cleaned up."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)
