# Rigout package
from ._version import __version__
from .config_manager import CloudflareConfig, ConfigManager, SecurityConfig, ServerConfig, SSHConfig
from .security_validator import SecurityValidator
from .ssh_manager import (
    ConfigurationError,
    ConnectionError,
    HardwareInfo,
    SecurityError,
    TunnelEndpoint,
    TunnelManager,
    build_env_assignments,
    build_write_command,
    get_tunnel_manager,
    heredoc_redirect,
    shell_join,
    shell_quote,
)
from .terminal_session import TerminalSession

__all__ = [
    "__version__",
    "ConfigurationError",
    "SecurityError",
    "ConnectionError",
    "TunnelEndpoint",
    "HardwareInfo",
    "TunnelManager",
    "get_tunnel_manager",
    "shell_quote",
    "shell_join",
    "build_env_assignments",
    "build_write_command",
    # Deprecated alias for build_write_command, kept because v0.2.0 exported it.
    # Removed in 0.4.0.
    "heredoc_redirect",
    "TerminalSession",
    "SecurityValidator",
    "ServerConfig",
    "SSHConfig",
    "CloudflareConfig",
    "SecurityConfig",
    "ConfigManager",
]
