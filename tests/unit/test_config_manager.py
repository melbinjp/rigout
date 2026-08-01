import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from rigout.config_manager import (
    DEFAULT_KNOWN_HOSTS_PATH,
    DEFAULT_MAX_REQUESTS_PER_MINUTE,
    ConfigManager,
    SecurityConfig,
    resolve_known_hosts_path,
    resolve_strict_host_keys,
)


@pytest.mark.unit
class TestConfigManager:
    """Tests for ConfigManager class"""

    def test_default_initialization(self):
        """Test default initialization values"""
        manager = ConfigManager("nonexistent_config.json")
        assert manager.server_config.name == "enhanced-hardware-server"
        assert manager.ssh_config.username == "agent"
        assert manager.cloudflare_config.domain == ""
        assert manager.security_config.enable_rate_limiting is True
        assert manager.security_config.ai_agent_mode is False

    def test_load_config_creates_default(self):
        """Test load_config creates a default config file if it does not exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "test_config.json"
            manager = ConfigManager(str(config_path))

            assert not config_path.exists()
            assert manager.load_config() is True
            assert config_path.exists()

            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            assert "server_config" in data
            assert "ssh_config" in data

    def test_load_config_valid(self):
        """Test loading a valid configuration file"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            test_config = {
                "server_config": {"name": "custom-name", "max_connections": 42},
                "ssh_config": {"username": "custom-user", "default_port": 2222},
                "cloudflare_config": {"domain": "custom.com"},
                "security_config": {"ai_agent_mode": True},
            }
            json.dump(test_config, f)
            f.flush()
            temp_path = f.name

        try:
            manager = ConfigManager(temp_path)
            assert manager.load_config() is True
            assert manager.server_config.name == "custom-name"
            assert manager.server_config.max_connections == 42
            assert manager.ssh_config.username == "custom-user"
            assert manager.ssh_config.default_port == 2222
            assert manager.cloudflare_config.domain == "custom.com"
            assert manager.security_config.ai_agent_mode is True
            # Check default post init values for AI agent mode
            assert "apt" in manager.security_config.allowed_sudo_commands
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_load_config_invalid_json(self):
        """Test load_config with invalid JSON content"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write("invalid json content")
            f.flush()
            temp_path = f.name

        try:
            manager = ConfigManager(temp_path)
            assert manager.load_config() is False
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_load_config_missing_required_sections(self):
        """Test load_config fails when required sections are missing"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            # Missing ssh_config
            test_config = {
                "server_config": {"name": "custom-name"},
            }
            json.dump(test_config, f)
            f.flush()
            temp_path = f.name

        try:
            manager = ConfigManager(temp_path)
            assert manager.load_config() is False
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_load_config_env_override(self):
        """Test environment variables override file settings"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            test_config = {
                "server_config": {},
                "ssh_config": {"username": "file-user", "private_key_path": "/file/key"},
                "cloudflare_config": {"email": "file@domain.com"},
            }
            json.dump(test_config, f)
            f.flush()
            temp_path = f.name

        try:
            env_vars = {
                "SSH_PRIVATE_KEY_PATH": "/env/key",
                "SSH_USERNAME": "env-user",
                "CLOUDFLARE_EMAIL": "env@domain.com",
                "CLOUDFLARE_API_KEY": "env-api-key",
            }
            with patch.dict(os.environ, env_vars):
                manager = ConfigManager(temp_path)
                assert manager.load_config() is True
                assert manager.ssh_config.private_key_path == "/env/key"
                assert manager.ssh_config.username == "env-user"
                assert manager.cloudflare_config.email == "env@domain.com"
                assert manager.cloudflare_config.api_key == "env-api-key"
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_save_config(self):
        """Test saving configuration and creating backup"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            manager = ConfigManager(str(config_path))
            manager.load_config()

            manager.server_config.name = "saved-name"
            assert manager.save_config() is True

            # Load it back
            new_manager = ConfigManager(str(config_path))
            assert new_manager.load_config() is True
            assert new_manager.server_config.name == "saved-name"

            # Check backup was created on second save
            manager.server_config.name = "second-save"
            assert manager.save_config() is True
            backup_path = config_path.with_suffix(".json.backup")
            assert backup_path.exists()

    def test_validate_config(self, tmp_path):
        """Test configuration validation rules"""
        config_path = tmp_path / "test_config.json"
        manager = ConfigManager(str(config_path))
        manager.load_config()

        # Missing key path should return issue
        manager.ssh_config.private_key_path = ""
        issues = manager.validate_config()
        assert any("private key path not configured" in issue for issue in issues)

        # Non-existent key file
        manager.ssh_config.private_key_path = "/nonexistent/key/file"
        issues = manager.validate_config()
        assert any("private key file not found" in issue for issue in issues)

        # Invalid username
        manager.ssh_config.private_key_path = ""
        manager.ssh_config.username = ""
        issues = manager.validate_config()
        assert any("username not configured" in issue for issue in issues)

        # Sane mock key path
        with tempfile.NamedTemporaryFile(delete=False) as f:
            key_path = f.name
        try:
            manager.ssh_config.private_key_path = key_path
            manager.ssh_config.username = "agent"
            manager.server_config.max_connections = 5
            manager.server_config.request_timeout = 10
            manager.security_config.max_requests_per_minute = 100
            assert len(manager.validate_config()) == 0
        finally:
            if os.path.exists(key_path):
                os.unlink(key_path)

    def test_get_config_summary(self, tmp_path):
        """Test retrieving configuration summary without sensitive info"""
        config_path = tmp_path / "test_config.json"
        manager = ConfigManager(str(config_path))
        manager.load_config()
        manager.cloudflare_config.email = "secret@cloudflare.com"
        manager.cloudflare_config.api_key = "sensitivekey"

        summary = manager.get_config_summary()
        assert "server" in summary
        assert "ssh" in summary
        assert "cloudflare" in summary
        assert "security" in summary
        assert summary["cloudflare"]["has_credentials"] is True
        # Ensure email and api_key are not exposed directly in summary
        assert "secret@cloudflare.com" not in str(summary)
        assert "sensitivekey" not in str(summary)

    def test_update_config(self, tmp_path):
        """Test updating configuration sections"""
        config_path = tmp_path / "test_config.json"
        manager = ConfigManager(str(config_path))
        manager.load_config()

        assert manager.update_config("server", {"name": "updated-server", "max_connections": 99}) is True
        assert manager.server_config.name == "updated-server"
        assert manager.server_config.max_connections == 99

        assert manager.update_config("ssh", {"username": "updated-user"}) is True
        assert manager.ssh_config.username == "updated-user"

        assert manager.update_config("cloudflare", {"domain": "updated.com"}) is True
        assert manager.cloudflare_config.domain == "updated.com"

        assert manager.update_config("security", {"ai_agent_mode": True}) is True
        assert manager.security_config.ai_agent_mode is True

        # Invalid section
        assert manager.update_config("invalid_section", {}) is False

    def test_reload_if_changed(self):
        """Test checking hash change and reloading config"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            test_config = {"server_config": {"name": "version-1"}, "ssh_config": {"username": "user1"}}
            json.dump(test_config, f)
            f.flush()
            temp_path = f.name

        try:
            manager = ConfigManager(temp_path)
            assert manager.load_config() is True
            assert manager.server_config.name == "version-1"
            assert manager.has_config_changed() is False

            # Modify config file
            with open(temp_path, "w", encoding="utf-8") as f2:
                test_config["server_config"]["name"] = "version-2"
                json.dump(test_config, f2)

            assert manager.has_config_changed() is True
            assert manager.reload_if_changed() is True
            assert manager.server_config.name == "version-2"
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


@pytest.fixture
def host_key_env(monkeypatch):
    """Neutralise host key environment variables so tests do not read the dev machine."""
    monkeypatch.delenv("RIGOUT_STRICT_HOST_KEYS", raising=False)
    monkeypatch.delenv("RIGOUT_KNOWN_HOSTS", raising=False)
    return monkeypatch


@pytest.mark.unit
class TestHostKeySettings:
    """The host key options an operator can set, and how they resolve."""

    def test_defaults_are_backwards_compatible(self, host_key_env):
        """0.3.0 must behave like 0.2.x for anyone who sets nothing."""
        security = SecurityConfig()

        assert security.strict_host_keys is False
        assert security.known_hosts_path == ""
        assert security.max_requests_per_minute == DEFAULT_MAX_REQUESTS_PER_MINUTE
        assert resolve_strict_host_keys(security.strict_host_keys) is False

    def test_environment_overrides_configured_strict_mode(self, host_key_env):
        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "yes")
        assert resolve_strict_host_keys(False) is True

        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "off")
        assert resolve_strict_host_keys(True) is False

        # An unreadable value is ignored rather than guessed at
        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "maybe")
        assert resolve_strict_host_keys(True) is True

    def test_known_hosts_path_resolution(self, host_key_env, tmp_path):
        assert resolve_known_hosts_path("") == Path(DEFAULT_KNOWN_HOSTS_PATH).expanduser()
        assert resolve_known_hosts_path(str(tmp_path / "kh")) == tmp_path / "kh"
        assert resolve_known_hosts_path("none") is None

        host_key_env.setenv("RIGOUT_KNOWN_HOSTS", str(tmp_path / "from-env"))
        assert resolve_known_hosts_path("ignored") == tmp_path / "from-env"

        host_key_env.setenv("RIGOUT_KNOWN_HOSTS", "none")
        assert resolve_known_hosts_path(str(tmp_path / "kh")) is None

    def test_validate_config_flags_strict_mode_without_known_hosts(self, host_key_env, tmp_path):
        manager = ConfigManager(str(tmp_path / "config.json"))
        manager.load_config()
        manager.ssh_config.private_key_path = ""
        manager.security_config.strict_host_keys = True
        manager.security_config.known_hosts_path = str(tmp_path / "absent")

        issues = manager.validate_config()
        assert any("known_hosts file not found" in issue for issue in issues)

        manager.security_config.known_hosts_path = "none"
        issues = manager.validate_config()
        assert any("known_hosts loading is turned off" in issue for issue in issues)

        (tmp_path / "known_hosts").write_text("", encoding="utf-8")
        manager.security_config.known_hosts_path = str(tmp_path / "known_hosts")
        issues = manager.validate_config()
        assert not any("known_hosts" in issue for issue in issues)

    def test_config_summary_reports_effective_host_key_settings(self, host_key_env, tmp_path):
        manager = ConfigManager(str(tmp_path / "config.json"))
        manager.load_config()

        summary = manager.get_config_summary()
        assert summary["security"]["strict_host_keys"] is False
        assert summary["security"]["max_requests_per_minute"] == DEFAULT_MAX_REQUESTS_PER_MINUTE

        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "1")
        assert manager.get_config_summary()["security"]["strict_host_keys"] is True

    def test_host_key_settings_round_trip_through_the_config_file(self, host_key_env, tmp_path):
        config_path = tmp_path / "config.json"
        manager = ConfigManager(str(config_path))
        manager.load_config()
        manager.security_config.strict_host_keys = True
        manager.security_config.known_hosts_path = str(tmp_path / "known_hosts")
        manager.security_config.max_requests_per_minute = 120
        assert manager.save_config() is True

        reloaded = ConfigManager(str(config_path))
        assert reloaded.load_config() is True
        assert reloaded.security_config.strict_host_keys is True
        assert reloaded.security_config.known_hosts_path == str(tmp_path / "known_hosts")
        assert reloaded.security_config.max_requests_per_minute == 120
