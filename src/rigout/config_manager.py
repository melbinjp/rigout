#!/usr/bin/env python3
"""
Configuration Management System for Enhanced MCP Hardware Server
Handles secure configuration loading, validation, and management
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Server configuration structure"""

    name: str = "enhanced-hardware-server"
    version: str = "1.0.0"
    log_level: str = "INFO"
    max_connections: int = 100
    request_timeout: int = 30
    session_timeout: int = 3600


@dataclass
class SSHConfig:
    """SSH configuration structure"""

    private_key_path: str = ""
    public_key_path: str = ""
    public_key_content: str = ""
    username: str = "agent"
    default_port: int = 22
    connection_timeout: int = 10
    auth_timeout: int = 10


@dataclass
class CloudflareConfig:
    """Cloudflare configuration structure"""

    email: str = ""
    api_key: str = ""
    domain: str = ""
    auto_tunnel_creation: bool = False
    tunnel_timeout: int = 60


@dataclass
class SecurityConfig:
    """Security configuration structure"""

    enable_rate_limiting: bool = True
    max_requests_per_minute: int = 60
    enable_command_validation: bool = True
    enable_audit_logging: bool = True
    ai_agent_mode: bool = False  # Enable maximum flexibility for AI agents
    allowed_sudo_commands: list[str] = None
    blocked_commands: list[str] = None

    def __post_init__(self):
        if self.allowed_sudo_commands is None:
            if self.ai_agent_mode:
                # In AI agent mode, allow most common commands
                self.allowed_sudo_commands = [
                    "apt",
                    "yum",
                    "dnf",
                    "systemctl",
                    "service",
                    "docker",
                    "pip",
                    "npm",
                    "make",
                    "cmake",
                    "git",
                    "wget",
                    "curl",
                    "tar",
                    "unzip",
                    "chmod",
                    "chown",
                    "mount",
                    "umount",
                    "iptables",
                    "ufw",
                    "firewall-cmd",
                ]
            else:
                self.allowed_sudo_commands = [
                    "apt",
                    "yum",
                    "dnf",
                    "systemctl",
                    "service",
                    "docker",
                    "pip",
                    "npm",
                    "make",
                    "cmake",
                ]
        if self.blocked_commands is None:
            if self.ai_agent_mode:
                # In AI agent mode, only block the most dangerous commands
                self.blocked_commands = [
                    "rm -rf /",
                    "dd if=/dev/zero",
                    "mkfs.ext4 /dev/sd",
                    "fdisk /dev/sd",
                    "parted /dev/sd",
                ]
            else:
                self.blocked_commands = [
                    "rm -rf /",
                    "dd if=",
                    "mkfs.",
                    "fdisk",
                    "parted",
                    "format",
                    "del /s",
                    "rmdir /s",
                ]


class ConfigManager:
    """Manages configuration loading, validation, and updates"""

    def __init__(self, config_file: str = "mcp-server-config.json"):
        self.config_file = Path(config_file)
        self.server_config = ServerConfig()
        self.ssh_config = SSHConfig()
        self.cloudflare_config = CloudflareConfig()
        self.security_config = SecurityConfig()
        self._config_hash = ""

    def load_config(self) -> bool:
        """Load configuration from file and environment variables"""
        try:
            # Create default config if doesn't exist
            if not self.config_file.exists():
                logger.info(f"Configuration file not found, creating default: {self.config_file}")
                self._create_default_config()
                return True

            # Load from file
            with open(self.config_file, encoding="utf-8") as f:
                data = json.load(f)

            # Validate structure
            self._validate_config_structure(data)

            # Load server config
            server_data = data.get("server_config", {})
            self.server_config = ServerConfig(
                **{k: v for k, v in server_data.items() if k in ServerConfig.__dataclass_fields__}
            )

            # Load SSH config with environment override
            ssh_data = data.get("ssh_config", {})
            self.ssh_config = SSHConfig(**{k: v for k, v in ssh_data.items() if k in SSHConfig.__dataclass_fields__})

            # Override with environment variables
            if os.getenv("SSH_PRIVATE_KEY_PATH"):
                self.ssh_config.private_key_path = os.getenv("SSH_PRIVATE_KEY_PATH")
            if os.getenv("SSH_USERNAME"):
                self.ssh_config.username = os.getenv("SSH_USERNAME")

            # Load Cloudflare config with environment override
            cf_data = data.get("cloudflare_config", {})
            self.cloudflare_config = CloudflareConfig(
                **{k: v for k, v in cf_data.items() if k in CloudflareConfig.__dataclass_fields__}
            )

            # Override with environment variables (more secure)
            if os.getenv("CLOUDFLARE_EMAIL"):
                self.cloudflare_config.email = os.getenv("CLOUDFLARE_EMAIL")
            if os.getenv("CLOUDFLARE_API_KEY"):
                self.cloudflare_config.api_key = os.getenv("CLOUDFLARE_API_KEY")

            # Load security config
            security_data = data.get("security_config", {})
            self.security_config = SecurityConfig(
                **{k: v for k, v in security_data.items() if k in SecurityConfig.__dataclass_fields__}
            )

            # Calculate config hash for change detection
            self._config_hash = self._calculate_config_hash(data)

            logger.info("Configuration loaded successfully")
            return True

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            return False
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            return False

    def save_config(self) -> bool:
        """Save current configuration to file"""
        try:
            config_data = {
                "server_config": asdict(self.server_config),
                "ssh_config": asdict(self.ssh_config),
                "cloudflare_config": asdict(self.cloudflare_config),
                "security_config": asdict(self.security_config),
                "last_updated": datetime.now().isoformat(),
                "version": "1.0.0",
            }

            # Don't save sensitive data to file (use environment variables)
            config_data["cloudflare_config"]["email"] = ""
            config_data["cloudflare_config"]["api_key"] = ""

            # Create backup of existing config
            if self.config_file.exists():
                backup_file = self.config_file.with_suffix(".json.backup")
                if backup_file.exists():
                    backup_file.unlink()
                self.config_file.rename(backup_file)
                logger.info(f"Created config backup: {backup_file}")

            # Write new config
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)

            logger.info(f"Configuration saved to: {self.config_file}")
            return True

        except Exception as e:
            logger.error(f"Error saving configuration: {e}")
            return False

    def validate_config(self) -> list[str]:
        """Validate current configuration and return list of issues"""
        issues = []

        # Validate SSH config
        if not self.ssh_config.private_key_path:
            issues.append("SSH private key path not configured")
        elif not Path(self.ssh_config.private_key_path).exists():
            issues.append(f"SSH private key file not found: {self.ssh_config.private_key_path}")

        if not self.ssh_config.username:
            issues.append("SSH username not configured")

        # Validate Cloudflare config (if auto tunnel creation is enabled)
        if self.cloudflare_config.auto_tunnel_creation:
            if not self.cloudflare_config.email:
                issues.append("Cloudflare email not configured (required for auto tunnel creation)")
            if not self.cloudflare_config.api_key:
                issues.append("Cloudflare API key not configured (required for auto tunnel creation)")

        # Validate server config
        if self.server_config.max_connections < 1:
            issues.append("Max connections must be at least 1")

        if self.server_config.request_timeout < 5:
            issues.append("Request timeout must be at least 5 seconds")

        # Validate security config
        if self.security_config.max_requests_per_minute < 1:
            issues.append("Max requests per minute must be at least 1")

        return issues

    def get_config_summary(self) -> dict[str, Any]:
        """Get a summary of current configuration (without sensitive data)"""
        return {
            "server": {
                "name": self.server_config.name,
                "version": self.server_config.version,
                "log_level": self.server_config.log_level,
                "max_connections": self.server_config.max_connections,
            },
            "ssh": {
                "username": self.ssh_config.username,
                "default_port": self.ssh_config.default_port,
                "has_private_key": bool(self.ssh_config.private_key_path),
                "key_exists": Path(self.ssh_config.private_key_path).exists()
                if self.ssh_config.private_key_path
                else False,
            },
            "cloudflare": {
                "domain": self.cloudflare_config.domain,
                "auto_tunnel_creation": self.cloudflare_config.auto_tunnel_creation,
                "has_credentials": bool(self.cloudflare_config.email and self.cloudflare_config.api_key),
            },
            "security": {
                "rate_limiting_enabled": self.security_config.enable_rate_limiting,
                "command_validation_enabled": self.security_config.enable_command_validation,
                "audit_logging_enabled": self.security_config.enable_audit_logging,
                "max_requests_per_minute": self.security_config.max_requests_per_minute,
            },
        }

    def update_config(self, section: str, updates: dict[str, Any]) -> bool:
        """Update specific configuration section"""
        try:
            if section == "server":
                for key, value in updates.items():
                    if hasattr(self.server_config, key):
                        setattr(self.server_config, key, value)
            elif section == "ssh":
                for key, value in updates.items():
                    if hasattr(self.ssh_config, key):
                        setattr(self.ssh_config, key, value)
            elif section == "cloudflare":
                for key, value in updates.items():
                    if hasattr(self.cloudflare_config, key):
                        setattr(self.cloudflare_config, key, value)
            elif section == "security":
                for key, value in updates.items():
                    if hasattr(self.security_config, key):
                        setattr(self.security_config, key, value)
            else:
                logger.error(f"Unknown configuration section: {section}")
                return False

            logger.info(f"Updated configuration section: {section}")
            return True

        except Exception as e:
            logger.error(f"Error updating configuration: {e}")
            return False

    def has_config_changed(self) -> bool:
        """Check if configuration file has changed since last load"""
        if not self.config_file.exists():
            return False

        try:
            with open(self.config_file, encoding="utf-8") as f:
                data = json.load(f)

            current_hash = self._calculate_config_hash(data)
            return current_hash != self._config_hash

        except Exception:
            return False

    def reload_if_changed(self) -> bool:
        """Reload configuration if it has changed"""
        if self.has_config_changed():
            logger.info("Configuration file changed, reloading...")
            return self.load_config()
        return True

    def _validate_config_structure(self, data: dict[str, Any]):
        """Validate configuration file structure"""
        required_sections = ["server_config", "ssh_config"]
        for section in required_sections:
            if section not in data:
                raise ValueError(f"Missing required configuration section: {section}")

    def _create_default_config(self):
        """Create default configuration file"""
        default_config = {
            "server_config": asdict(self.server_config),
            "ssh_config": asdict(self.ssh_config),
            "cloudflare_config": asdict(self.cloudflare_config),
            "security_config": asdict(self.security_config),
            "endpoints": [],
            "created": datetime.now().isoformat(),
            "version": "1.0.0",
        }

        # Create directory if it doesn't exist
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError):
            # If we can't create the directory, try current directory
            self.config_file = Path(self.config_file.name)

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2, ensure_ascii=False)

        logger.info(f"Created default configuration: {self.config_file}")

    def _calculate_config_hash(self, data: dict[str, Any]) -> str:
        """Calculate hash of configuration data for change detection"""
        import hashlib

        config_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(config_str.encode()).hexdigest()


# Global configuration manager instance
config_manager = ConfigManager()
