import json
import os
import tempfile
from unittest.mock import patch

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from rigout.config_manager import ConfigManager
from rigout.security_validator import SecurityValidator
from rigout.server import handle_call_tool, handle_call_tool_result, handle_list_tools, server
from rigout.ssh_manager import TunnelEndpoint, get_tunnel_manager


@pytest.mark.integration
class TestMCPServerIntegration:
    """Integration tests for MCP Server and tools"""

    @pytest.mark.asyncio
    async def test_server_startup(self):
        """Test server starts and initializes tunnel manager"""
        manager = get_tunnel_manager()
        assert manager is not None
        assert isinstance(manager.endpoints, list)

    @pytest.mark.asyncio
    async def test_tool_definitions(self):
        """Test all expected tools are listed in tool definitions"""
        tools = await handle_list_tools()
        expected_tools = {
            "connect_hardware",
            "execute_command",
            "create_terminal_session",
            "execute_in_terminal",
            "list_terminal_sessions",
            "close_terminal_session",
            "get_hardware_info",
            "get_server_activity",
            "manage_tunnels",
            "install_software",
            "file_operations",
            "system_monitoring",
            "docker_operations",
            "bulk_file_transfer",
            "environment_setup",
        }
        tool_names = {tool.name for tool in tools}
        for expected in expected_tools:
            assert expected in tool_names

    @pytest.mark.asyncio
    async def test_configuration_system(self):
        """Test configuration system loading and validation"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            test_config = {
                "server_config": {"name": "test-server", "version": "1.0.0"},
                "ssh_config": {"username": "testuser", "private_key_path": "/test/key"},
                "cloudflare_config": {"domain": "test.com"},
                "security_config": {"ai_agent_mode": True, "enable_rate_limiting": True},
            }
            json.dump(test_config, f)
            f.flush()
            temp_file = f.name

        try:
            config_mgr = ConfigManager(temp_file)
            assert config_mgr.load_config() is True
            assert config_mgr.security_config.ai_agent_mode is True
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)

    @pytest.mark.asyncio
    async def test_security_validation_e2e(self):
        """End-to-end validation of commands and hostnames"""
        validator = SecurityValidator()

        # Hostnames
        is_valid, _ = validator.validate_hostname("example.com")
        assert is_valid is True

        is_valid, _ = validator.validate_hostname("evil.com; rm -rf /")
        assert is_valid is False

        # Commands
        is_valid, _ = validator.validate_command("ls -la")
        assert is_valid is True

        is_valid, _ = validator.validate_command("rm -rf /")
        assert is_valid is False

    @pytest.mark.asyncio
    async def test_mock_tool_calls(self):
        """Test calling tools with mock endpoint to ensure graceful errors or successes"""
        # Test manage_tunnels list should succeed even without active endpoints
        result = await handle_call_tool("manage_tunnels", {"action": "list"})
        assert result and len(result) > 0
        assert "Configured Tunnel Endpoints" in result[0].text or "No tunnel endpoints configured" in result[0].text

        # Test execute_command should fail gracefully when no endpoints are active
        with patch("rigout.ssh_manager.TunnelManager.auto_failover", return_value=None):
            result = await handle_call_tool_result("execute_command", {"command": "ls"})
            assert result.isError is True
            assert "No available hardware endpoints" in result.content[0].text

    @pytest.mark.asyncio
    async def test_registered_handler_preserves_mcp_error_flag(self):
        """The SDK-facing handler must emit isError for unknown tools."""
        handler = server.request_handlers[CallToolRequest]
        response = await handler(
            CallToolRequest(params=CallToolRequestParams(name="definitely_unknown_tool", arguments={}))
        )

        assert response.root.isError is True
        assert "Unknown tool" in response.root.content[0].text

    @pytest.mark.asyncio
    async def test_live_endpoint_when_configured(self):
        """Run connectivity check if live environment credentials exist"""
        ssh_host = os.getenv("SSH_HOSTNAME")
        ssh_user = os.getenv("SSH_USERNAME")
        ssh_key = os.getenv("SSH_PRIVATE_KEY_PATH")

        if not (ssh_host and ssh_user and ssh_key and os.path.exists(ssh_key)):
            pytest.skip("No real/live SSH endpoint credentials configured")

        manager = get_tunnel_manager()
        endpoint = TunnelEndpoint(hostname=ssh_host, username=ssh_user, private_key_path=ssh_key)

        connection_result = await manager.test_endpoint(endpoint)
        assert connection_result is True

        cmd_result = await manager.execute_command(endpoint, "echo 'hello'")
        assert cmd_result["success"] is True
        assert "hello" in cmd_result["stdout"]
