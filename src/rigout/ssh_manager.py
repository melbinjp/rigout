import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import paramiko

from .config_manager import (
    DEFAULT_MAX_REQUESTS_PER_MINUTE,
    STRICT_HOST_KEYS_ENV,
    SecurityConfig,
    resolve_known_hosts_path,
    resolve_strict_host_keys,
)
from .terminal_session import LocalTerminalSession, TerminalSession

logger = logging.getLogger(__name__)

try:
    from .security_validator import security_validator
except ImportError:
    security_validator = None  # type: ignore
    logger.warning("Security validator not available - running with reduced security")


def shell_quote(value: Any) -> str:
    """Quote a value for a POSIX shell command executed over SSH."""
    return shlex.quote(str(value))


def shell_join(values: list[Any]) -> str:
    """Quote and join multiple POSIX shell arguments."""
    return " ".join(shell_quote(value) for value in values)


def build_env_assignments(environment: dict[str, Any]) -> str:
    """Build safe inline environment assignments for POSIX shells."""
    assignments = []
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            raise ValueError(f"Invalid environment variable name: {key}")
        assignments.append(f"{key}={shell_quote(value)}")
    return " ".join(assignments)


def build_write_command(content: str, destination: str, append: bool = False) -> str:
    r"""Build a POSIX shell command that writes content to destination byte for byte.

    Safety: content becomes a single shell-quoted argument to printf, and the format
    string is the literal `%s`, so nothing in the content is expanded, re-parsed or
    read as an escape - not `$(...)`, not a backtick span, not a leading `-`, not a
    stray `%`, not a backslash. shell_quote also has no in-band terminator that
    content could reach, the way a heredoc delimiter can be reached, so there is no
    byte a caller could supply that ends the quoting early.

    Byte-exactness: this used to emit `cat > dest <<'EOF'`. A heredoc body always
    ends with a newline, because the newline before the closing delimiter is part of
    the body, so every remote write gained one byte the caller never supplied while
    the local branch of the same MCP tool wrote the content exactly. Empty content
    produced a one-byte file, and content that already ended in a newline got a
    second one. printf has no such edge.

    printf is a POSIX shell builtin, so the content does not pass through execve and
    is not bounded by ARG_MAX.

    One caveat for anyone re-measuring this on Windows: Git Bash strips `\r` from the
    command text it is handed, so CRLF content looks like it loses a byte there. That
    is the test shell normalising its own input - a POSIX shell preserves `\r` inside
    single quotes, and the old heredoc lost it in exactly the same way.
    """
    redirect = ">>" if append else ">"
    return f"printf '%s' {shell_quote(content)} {redirect} {shell_quote(destination)}"


def heredoc_redirect(content: str, destination: str) -> str:
    """Deprecated alias for build_write_command. No heredoc is involved any more.

    Kept because v0.2.0 exported this name from the package, so removing it would
    break `from rigout import heredoc_redirect` for anyone already on it. New code
    should call build_write_command, which also spells append. Removal is a 0.4.0
    item, alongside the strict_host_keys default flip.
    """
    return build_write_command(content, destination)


def _run_ssh_command(
    ssh_client: paramiko.SSHClient,
    command: str,
    timeout: int,
) -> tuple[str, str, int]:
    """Run Paramiko's blocking command/read sequence outside the event loop."""
    _stdin, stdout, stderr = ssh_client.exec_command(command, timeout=timeout)
    stdout_data = stdout.read().decode("utf-8", errors="replace")
    stderr_data = stderr.read().decode("utf-8", errors="replace")
    exit_code = int(stdout.channel.recv_exit_status())
    return stdout_data, stderr_data, exit_code


def load_ssh_private_key(path: str) -> paramiko.PKey:
    """Load a private key of any supported type (Ed25519, RSA, ECDSA)."""
    try:
        if hasattr(paramiko.PKey, "from_path"):
            return paramiko.PKey.from_path(path)
    except paramiko.ssh_exception.PasswordRequiredException as exc:
        raise SecurityError("Private key is password protected - not supported") from exc
    except (paramiko.SSHException, OSError, ValueError):
        pass  # fall back to explicit per-type attempts below

    last_error: Exception | None = None
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key_file(path)
        except paramiko.ssh_exception.PasswordRequiredException as exc:
            raise SecurityError("Private key is password protected - not supported") from exc
        except Exception as exc:
            last_error = exc
    raise SecurityError(f"Failed to load private key: {last_error}")


def format_key_fingerprint(key: paramiko.PKey) -> str:
    """Format a host key the way OpenSSH prints it, e.g. SHA256:abc123..."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class WarningAutoAddPolicy(paramiko.MissingHostKeyPolicy):
    """Accept an unknown host key like AutoAddPolicy, but say so once per host.

    This is the 0.3.0 default. Auto-accept stays, so no existing deployment
    stops working, but the trust-on-first-use moment becomes visible: a user who
    sees a second, different fingerprint for a host they have already connected
    to knows something changed underneath them.
    """

    def __init__(self, warned_hosts: set[str]) -> None:
        self._warned_hosts = warned_hosts

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        if hostname not in self._warned_hosts:
            self._warned_hosts.add(hostname)
            logger.warning(
                "Accepting unverified SSH host key for %s (%s %s). This key was not checked "
                "against known_hosts, so a host on the network path could be impersonating it. "
                "Set %s=1 to require a known_hosts entry instead.",
                hostname,
                key.get_name(),
                format_key_fingerprint(key),
                STRICT_HOST_KEYS_ENV,
            )
        client.get_host_keys().add(hostname, key.get_name(), key)


def _local_memory_gb() -> float:
    """Best-effort total physical memory of the machine running Rigout."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) / (1024**2), 2)
        elif sys.platform == "darwin":
            output = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5, check=True
            )
            return round(int(output.stdout.strip()) / (1024**3), 2)
        elif sys.platform == "win32":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                total_physical = int(status.ullTotalPhys)
                return round(total_physical / (1024**3), 2)
    except Exception:
        pass
    return 0.0


def _local_gpu_info() -> list[str]:
    """Best-effort GPU names on the machine running Rigout."""
    if shutil.which("nvidia-smi"):
        try:
            output = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            names = [line.strip() for line in output.stdout.splitlines() if line.strip()]
            if names:
                return names
        except Exception:
            pass
    return []


class SecurityError(Exception):
    """Raised when security validation fails"""

    pass


class ConnectionError(Exception):
    """Raised when connection operations fail"""

    pass


class ConfigurationError(Exception):
    """Raised when configuration is invalid"""

    pass


DEFAULT_SSH_PORT = 22


@dataclass
class TunnelEndpoint:
    """Represents a tunnel endpoint with connection details"""

    hostname: str
    username: str
    private_key_path: str
    port: int = DEFAULT_SSH_PORT
    platform: str = "unknown"
    status: str = "unknown"  # active, inactive, failed, testing
    last_tested: datetime | None = None
    response_time: float | None = None
    tunnel_id: str | None = None
    purpose: str = "primary"  # primary, backup, load-balance
    created: datetime | None = None
    max_connections: int = 5
    current_connections: int = 0

    def __post_init__(self):
        """Validate endpoint data after initialization"""
        if not self.hostname or not isinstance(self.hostname, str):
            raise ConfigurationError("Invalid hostname")
        if not self.username or not isinstance(self.username, str):
            raise ConfigurationError("Invalid username")
        if not self.private_key_path or not isinstance(self.private_key_path, str):
            raise ConfigurationError("Invalid private key path")
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ConfigurationError("Invalid port number")

        # Validate hostname format
        if not self._is_valid_hostname(self.hostname):
            raise SecurityError("Invalid hostname format")

    def _is_valid_hostname(self, hostname: str) -> bool:
        """Validate hostname format for security"""
        if len(hostname) > 253:
            return False

        # Check for consecutive dots
        if ".." in hostname:
            return False

        # Check each label (part between dots)
        labels = hostname.split(".")
        for label in labels:
            if not label:  # Empty label
                return False
            if label.startswith("-") or label.endswith("-"):
                return False
            if len(label) > 63:  # Max label length
                return False

        # Check for malicious patterns
        malicious_patterns = [";", "&", "|", "`", "$", "(", ")", "<", ">", '"', "'", "\\", "\n", "\r", "\0"]
        if any(char in hostname for char in malicious_patterns):
            return False

        allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
        return all(c in allowed_chars for c in hostname)


@dataclass
class HardwareInfo:
    """Hardware information from remote system"""

    cpu_count: int
    memory_gb: float
    gpu_info: list[str]
    disk_space_gb: float
    platform: str
    architecture: str
    last_updated: datetime


class TunnelManager:
    """Manages multiple tunnel endpoints with failover and load balancing"""

    def __init__(self, config_file: str = "mcp-server-config.json"):
        self.config_file = config_file
        self.endpoints: list[TunnelEndpoint] = []
        self.active_endpoint: TunnelEndpoint | None = None
        self.hardware_cache: dict[str, HardwareInfo] = {}
        self.terminal_sessions: dict[str, TerminalSession | LocalTerminalSession] = {}
        self.cf_email: str | None = None
        self.cf_api_key: str | None = None
        self.ssh_private_key: str | None = None
        self.ssh_public_key: str | None = None
        self.domain: str = ""
        self._connection_pool: dict[str, list[paramiko.SSHClient]] = {}
        self._max_pool_size: int = 5
        self._session_cleanup_task: asyncio.Task | None = None
        self._health_check_task: asyncio.Task | None = None
        self._rate_limiter: dict[str, list[float]] = {}
        self._max_requests_per_minute: int = DEFAULT_MAX_REQUESTS_PER_MINUTE
        self._background_tasks_started: bool = False
        self.enable_local_endpoint: bool = os.getenv("RIGOUT_LOCAL_MODE", "1").lower() not in {"0", "false", "no"}
        self._local_endpoint: TunnelEndpoint | None = None

        # Host key policy. Defaults here so the environment still applies when
        # there is no configuration file at all; load_config layers config on top.
        self.strict_host_keys: bool = resolve_strict_host_keys(False)
        self.known_hosts_path: Path | None = resolve_known_hosts_path("")
        self._host_key_warned: set[str] = set()
        self._known_hosts_warned: set[str] = set()

        # Load config first
        self.load_config()
        self._warn_if_strict_without_known_hosts()

        # Start background tasks (only if event loop is running)
        try:
            self._start_background_tasks()
        except RuntimeError:
            # No event loop running, skip background tasks
            logger.info("No event loop running, background tasks will be started later")

    def __del__(self):
        """Cleanup when object is destroyed"""
        with contextlib.suppress(BaseException):
            self.cleanup()

    def cleanup(self):
        """Clean up resources and background tasks"""
        if self._session_cleanup_task and not self._session_cleanup_task.done():
            self._session_cleanup_task.cancel()
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()

        # Close all SSH connections
        for pool in self._connection_pool.values():
            for ssh_client in pool:
                with contextlib.suppress(BaseException):
                    ssh_client.close()

        # Close all terminal sessions
        for session in list(self.terminal_sessions.values()):
            with contextlib.suppress(Exception):
                session.close()

        self.terminal_sessions.clear()
        self._connection_pool.clear()

    def _start_background_tasks(self):
        """Start background maintenance tasks"""
        try:
            asyncio.get_running_loop()
            self._session_cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())
            self._health_check_task = asyncio.create_task(self._periodic_health_check())
            logger.info("Background tasks started successfully")
        except RuntimeError:
            # No event loop running
            raise

    async def _cleanup_expired_sessions(self):
        """Periodically clean up expired terminal sessions"""
        while True:
            try:
                expired_sessions = [
                    session_id for session_id, session in self.terminal_sessions.items() if session.is_expired()
                ]

                for session_id in expired_sessions:
                    logger.info(f"Cleaning up expired session: {session_id}")
                    self.close_terminal_session(session_id)

                await asyncio.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"Error in session cleanup: {e}")
                await asyncio.sleep(60)  # Retry after 1 minute on error

    async def _periodic_health_check(self):
        """Periodically check endpoint health"""
        while True:
            try:
                for endpoint in self.endpoints:
                    await self.test_endpoint(endpoint)
                await asyncio.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"Error in health check: {e}")
                await asyncio.sleep(60)

    def _check_rate_limit(self, identifier: str) -> bool:
        """Check if request is within rate limits"""
        now = time.time()
        if identifier not in self._rate_limiter:
            self._rate_limiter[identifier] = []

        # Remove old requests (older than 1 minute)
        self._rate_limiter[identifier] = [
            req_time for req_time in self._rate_limiter[identifier] if now - req_time < 60
        ]

        # Check if under limit
        if len(self._rate_limiter[identifier]) >= self._max_requests_per_minute:
            return False

        # Add current request
        self._rate_limiter[identifier].append(now)
        return True

    def _execute_rate_limit_key(self, endpoint: "TunnelEndpoint") -> str:
        """One command budget per endpoint, shared by one-shot and terminal-session commands."""
        if self._is_local_endpoint(endpoint):
            return "execute_local"
        return f"execute_{endpoint.hostname}"

    def _apply_security_config(self, security_data: dict[str, Any]) -> None:
        """Apply the security_config section: rate limit and host key policy.

        Every branch here keeps pre-0.3.0 behaviour when the section is absent or
        silent: 60 requests per minute, host keys auto-accepted.
        """
        if not isinstance(security_data, dict):
            # A malformed section used to be ignored silently; keep it non-fatal.
            logger.warning(f"Ignoring security_config: expected an object, got {type(security_data).__name__}")
            security_data = {}

        known_fields = {k: v for k, v in security_data.items() if k in SecurityConfig.__dataclass_fields__}
        try:
            security = SecurityConfig(**known_fields)
        except TypeError as e:
            logger.warning(f"Ignoring unusable security_config section: {e}")
            security = SecurityConfig()

        limit = security.max_requests_per_minute
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            logger.warning(
                f"Ignoring invalid security_config.max_requests_per_minute={limit!r}; "
                f"using {DEFAULT_MAX_REQUESTS_PER_MINUTE}"
            )
            limit = DEFAULT_MAX_REQUESTS_PER_MINUTE
        elif limit != DEFAULT_MAX_REQUESTS_PER_MINUTE:
            # The effective limit changed in 0.3.0 for anyone who had already set this
            # key, because before 0.3.0 nothing read it. Say so rather than surprise them.
            logger.info(
                f"Rate limit taken from configuration: {limit} requests/minute per endpoint "
                f"(built-in default is {DEFAULT_MAX_REQUESTS_PER_MINUTE})"
            )
        self._max_requests_per_minute = limit

        if not security.enable_rate_limiting:
            logger.warning(
                "security_config.enable_rate_limiting=false is not honoured in this release; "
                f"rate limiting stays on at {self._max_requests_per_minute} requests/minute."
            )

        self.strict_host_keys = resolve_strict_host_keys(security.strict_host_keys)
        self.known_hosts_path = resolve_known_hosts_path(security.known_hosts_path)

    def _warn_if_strict_without_known_hosts(self) -> None:
        """Strict mode with no readable known_hosts rejects everything - say so early."""
        if not self.strict_host_keys:
            return
        if self.known_hosts_path is None:
            logger.warning(
                "Strict host key checking is on but known_hosts loading is turned off; "
                "every SSH connection will be rejected."
            )
        elif not self.known_hosts_path.exists():
            logger.warning(
                f"Strict host key checking is on but no known_hosts file exists at "
                f"{self.known_hosts_path}; every SSH connection will be rejected until "
                f"the host keys are recorded there."
            )

    def _warn_known_hosts_once(self, message: str) -> None:
        """Report a known_hosts problem once per process instead of once per connection."""
        if message not in self._known_hosts_warned:
            self._known_hosts_warned.add(message)
            logger.warning(message)

    def _load_known_hosts(self, ssh_client: paramiko.SSHClient) -> None:
        """Load the user's known_hosts read-only, so Paramiko never rewrites it."""
        path = self.known_hosts_path
        if path is None:
            return
        if not path.exists():
            # Normal on a machine that has never used OpenSSH; strict mode warns separately.
            logger.debug(f"No known_hosts file at {path}")
            return
        try:
            ssh_client.load_system_host_keys(str(path))
        except Exception as e:
            self._warn_known_hosts_once(f"Could not read known_hosts file {path}: {e}")

    def _apply_host_key_policy(self, ssh_client: paramiko.SSHClient) -> None:
        """Load known_hosts and set the missing-host-key policy for one client.

        known_hosts is loaded in BOTH modes. That distinction matters: a host that
        is absent from known_hosts is a first contact and is accepted with a warning
        (strict mode rejects it), but a host that is PRESENT with a different key is
        refused by Paramiko either way. Only the second case is the actual attack
        signature, and refusing it cannot break a deployment whose own ssh client
        would already be refusing the same connection.
        """
        self._load_known_hosts(ssh_client)
        if self.strict_host_keys:
            ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            ssh_client.set_missing_host_key_policy(WarningAutoAddPolicy(self._host_key_warned))

    def _ssh_error_message(self, hostname: str, exc: paramiko.SSHException) -> str:
        """Explain an SSH failure, naming the way out of the one users will hit first.

        Paramiko's RejectPolicy raises a plain SSHException reading "Server '[host]:port'
        not found in known_hosts", which states the refusal and nothing about resolving
        it. It is the first thing anyone meets after turning strict checking on, and
        unlike a key mismatch it is usually not an attack, just a host nobody has
        recorded yet. Everything else is passed through unchanged.
        """
        text = str(exc)
        if "not found in known_hosts" not in text:
            return f"SSH error: {text}"

        source = self.known_hosts_path or "your known_hosts file"
        return (
            f"Host key verification failed for {hostname}: strict host key checking is on and "
            f"this host has no entry in {source}. Record its key first, having checked that the "
            f"fingerprint is the one you expect: ssh-keyscan -p PORT {hostname} >> {source}. "
            f"To connect without a recorded key instead, unset {STRICT_HOST_KEYS_ENV}, which "
            "warns rather than refuses."
        )

    def _host_key_mismatch_error(self, hostname: str, exc: paramiko.BadHostKeyException) -> str:
        """Explain a changed host key in terms the operator can act on."""
        source = self.known_hosts_path or "known_hosts"
        return (
            f"Host key verification failed for {hostname}: the key offered by the server "
            f"({format_key_fingerprint(exc.key)}) does not match the one recorded in {source} "
            f"({format_key_fingerprint(exc.expected_key)}). If you rebuilt this host, remove the "
            f"stale entry with: ssh-keygen -R {hostname}. Otherwise the connection may be intercepted."
        )

    def load_config(self):
        """Load configuration with security validation"""
        try:
            if not os.path.exists(self.config_file):
                logger.warning(f"Configuration file not found: {self.config_file}")
                self._create_default_config()
                return

            with open(self.config_file, encoding="utf-8") as f:
                data = json.load(f)

            # Validate configuration structure
            self._validate_config_structure(data)

            # Security settings that the request path actually uses
            self._apply_security_config(data.get("security_config") or {})

            # Load Cloudflare config from environment or config
            cf_config = data.get("cloudflare_config", {})
            self.cf_email = os.getenv("CLOUDFLARE_EMAIL") or cf_config.get("email")
            self.cf_api_key = os.getenv("CLOUDFLARE_API_KEY") or cf_config.get("api_key")
            self.domain = cf_config.get("domain", "")

            # Load SSH config
            ssh_config = data.get("ssh_config", {})
            self.ssh_private_key = ssh_config.get("private_key_path")
            self.ssh_public_key = ssh_config.get("public_key_content")

            # Validate SSH key paths (only if not empty and not a test path)
            if (
                self.ssh_private_key
                and not self.ssh_private_key.startswith("/test/")
                and not os.path.exists(self.ssh_private_key)
            ):
                raise ConfigurationError(f"SSH private key not found: {self.ssh_private_key}")

            # Load and validate endpoints
            self.endpoints = []
            for endpoint_data in data.get("endpoints", []):
                try:
                    endpoint = TunnelEndpoint(**endpoint_data)
                    # Convert string dates back to datetime objects
                    if endpoint.last_tested and isinstance(endpoint.last_tested, str):
                        endpoint.last_tested = datetime.fromisoformat(endpoint.last_tested)
                    if endpoint.created and isinstance(endpoint.created, str):
                        endpoint.created = datetime.fromisoformat(endpoint.created)
                    self.endpoints.append(endpoint)
                except Exception as e:
                    logger.error(f"Invalid endpoint configuration: {e}")

            logger.info(f"Loaded {len(self.endpoints)} endpoints from configuration")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            raise ConfigurationError(f"Invalid configuration file format: {e}")
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise ConfigurationError(f"Failed to load configuration: {e}")

    def _validate_config_structure(self, data: dict):
        """Validate configuration file structure"""
        # Non-strict validation to support config files that only contain endpoints
        if "endpoints" not in data and "mcp_server" not in data:
            raise ConfigurationError("Configuration structure is invalid: neither 'endpoints' nor 'mcp_server' found")

    def _create_default_config(self):
        """Create a default configuration file"""
        default_config = {
            "mcp_server": {
                "name": "enhanced-hardware-server",
                "version": "1.0.0",
                "created": datetime.now().isoformat(),
            },
            "ssh_config": {
                "private_key_path": "",
                "public_key_path": "",
                "public_key_content": "",
                "username": "agent",
            },
            "cloudflare_config": {"email": "", "api_key": "", "domain": ""},
            "endpoints": [],
            "settings": {
                "auto_failover": True,
                "health_check_interval": 300,
                "max_connections_per_endpoint": 5,
                "session_timeout": 3600,
            },
        }

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)

        logger.info(f"Created default configuration file: {self.config_file}")

    def save_config(self):
        """Save tunnel endpoints to configuration file, preserving other sections"""
        data: dict[str, Any] = {}
        config_path = Path(self.config_file)
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    data = existing
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Could not read existing config, rewriting it: {e}")

        data["endpoints"] = [asdict(endpoint) for endpoint in self.endpoints]
        data["last_updated"] = datetime.now().isoformat()
        # Convert datetime objects to strings for JSON serialization
        for endpoint_data in data["endpoints"]:
            if endpoint_data.get("last_tested"):
                endpoint_data["last_tested"] = (
                    endpoint_data["last_tested"].isoformat()
                    if isinstance(endpoint_data["last_tested"], datetime)
                    else endpoint_data["last_tested"]
                )
            if endpoint_data.get("created"):
                endpoint_data["created"] = (
                    endpoint_data["created"].isoformat()
                    if isinstance(endpoint_data["created"], datetime)
                    else endpoint_data["created"]
                )

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)

    async def test_endpoint(self, endpoint: TunnelEndpoint) -> bool:
        """Test if an endpoint is accessible with comprehensive validation"""
        if self._is_local_endpoint(endpoint):
            endpoint.status = "active"
            endpoint.response_time = 0.0
            endpoint.last_tested = datetime.now()
            return True

        if not self._check_rate_limit(f"test_{endpoint.hostname}"):
            logger.warning(f"Rate limit exceeded for endpoint testing: {endpoint.hostname}")
            return False

        start_time = time.time()
        ssh_client = None

        try:
            # Validate endpoint configuration
            if not endpoint.private_key_path or not os.path.exists(endpoint.private_key_path):
                raise ConnectionError(f"SSH private key not found: {endpoint.private_key_path}")

            # Create SSH client with security settings
            ssh_client = paramiko.SSHClient()
            self._apply_host_key_policy(ssh_client)

            private_key = load_ssh_private_key(endpoint.private_key_path)

            # Connect with security settings
            ssh_client.connect(
                hostname=endpoint.hostname,
                port=endpoint.port,
                username=endpoint.username,
                pkey=private_key,
                timeout=10,
                auth_timeout=10,
                banner_timeout=10,
                look_for_keys=False,
                allow_agent=False,
            )

            # Test basic command execution
            stdin, stdout, stderr = ssh_client.exec_command('echo "connection_test_$(date +%s)"', timeout=5)
            result = stdout.read().decode("utf-8", errors="ignore").strip()
            error_output = stderr.read().decode("utf-8", errors="ignore").strip()

            if error_output:
                logger.warning(f"Command execution warning on {endpoint.hostname}: {error_output}")

            # Validate response
            if result.startswith("connection_test_"):
                endpoint.status = "active"
                endpoint.response_time = time.time() - start_time
                endpoint.last_tested = datetime.now()
                logger.info(f"Endpoint test successful: {endpoint.hostname} ({endpoint.response_time:.2f}s)")
                return True
            else:
                endpoint.status = "failed"
                logger.error(f"Unexpected response from {endpoint.hostname}: {result}")
                return False

        except paramiko.BadHostKeyException as e:
            endpoint.status = "failed"
            logger.error(self._host_key_mismatch_error(endpoint.hostname, e))
            return False
        except paramiko.AuthenticationException as e:
            endpoint.status = "failed"
            logger.error(f"Authentication failed for {endpoint.hostname}: {e}")
            return False
        except paramiko.SSHException as e:
            endpoint.status = "failed"
            # `manage_tunnels add` runs this test, so a strict-mode refusal here is the
            # first thing an operator sees; the log line is where they will look.
            logger.error(self._ssh_error_message(endpoint.hostname, e))
            return False
        except TimeoutError as e:
            endpoint.status = "failed"
            logger.error(f"Connection timeout for {endpoint.hostname}: {e}")
            return False
        except Exception as e:
            endpoint.status = "failed"
            endpoint.last_tested = datetime.now()
            logger.error(f"Endpoint test failed for {endpoint.hostname}: {e}")
            return False
        finally:
            if ssh_client:
                with contextlib.suppress(BaseException):
                    ssh_client.close()

    async def find_best_endpoint(self) -> TunnelEndpoint | None:
        """Find the best available endpoint (fastest response time)"""
        active_endpoints = []

        # Test all endpoints concurrently
        tasks = [self.test_endpoint(endpoint) for endpoint in self.endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for endpoint, result in zip(self.endpoints, results, strict=False):
            if result is True and endpoint.status == "active":
                active_endpoints.append(endpoint)

        if not active_endpoints:
            return None

        # Sort by response time (fastest first)
        active_endpoints.sort(key=lambda x: x.response_time or float("inf"))
        return active_endpoints[0]

    async def get_hardware_info(self, endpoint: TunnelEndpoint) -> HardwareInfo | None:
        """Get hardware information from remote system"""
        if self._is_local_endpoint(endpoint):
            try:
                _total, _used, free = shutil.disk_usage(Path.cwd())
            except Exception:
                free = 0
            info = HardwareInfo(
                cpu_count=os.cpu_count() or 0,
                memory_gb=_local_memory_gb(),
                gpu_info=_local_gpu_info(),
                disk_space_gb=round(free / (1024**3), 2) if free else 0.0,
                platform=platform.system(),
                architecture=platform.machine(),
                last_updated=datetime.now(),
            )
            self.hardware_cache[endpoint.hostname] = info
            return info

        try:
            ssh = paramiko.SSHClient()
            self._apply_host_key_policy(ssh)

            private_key = load_ssh_private_key(endpoint.private_key_path)
            ssh.connect(
                hostname=endpoint.hostname, port=endpoint.port, username=endpoint.username, pkey=private_key, timeout=10
            )

            # Get system information
            commands = {
                "cpu_count": "nproc",
                "memory": "free -g | awk '/^Mem:/{print $2}'",
                "gpu_info": 'lspci | grep -i vga || lspci | grep -i display || echo "No GPU detected"',
                "disk_space": "df -BG / | awk 'NR==2{print $2}' | sed 's/G//'",
                "platform": "uname -s",
                "architecture": "uname -m",
            }

            results = {}
            for key, cmd in commands.items():
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
                output = stdout.read().decode("utf-8", errors="ignore").strip()
                results[key] = output

            ssh.close()

            # Parse results
            hardware_info = HardwareInfo(
                cpu_count=int(results.get("cpu_count", 0)),
                memory_gb=float(results.get("memory", 0)),
                gpu_info=results.get("gpu_info", "").split("\n"),
                disk_space_gb=float(results.get("disk_space", 0)),
                platform=results.get("platform", "unknown"),
                architecture=results.get("architecture", "unknown"),
                last_updated=datetime.now(),
            )

            # Cache the hardware info
            self.hardware_cache[endpoint.hostname] = hardware_info
            return hardware_info

        except paramiko.BadHostKeyException as e:
            logger.error(self._host_key_mismatch_error(endpoint.hostname, e))
            return None
        except Exception as e:
            logger.error(f"Failed to get hardware info from {endpoint.hostname}: {e}")
            return None

    async def execute_command(
        self,
        endpoint: TunnelEndpoint,
        command: str,
        timeout: int = 30,
        allow_sudo: bool = False,
        bypass_security: bool = False,
        working_directory: str | None = None,
        environment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute command on remote system with security validation"""
        if self._is_local_endpoint(endpoint):
            return await self._execute_local_command(
                endpoint, command, timeout, allow_sudo, bypass_security, working_directory, environment
            )

        # Rate limiting check
        if not self._check_rate_limit(self._execute_rate_limit_key(endpoint)):
            return {
                "success": False,
                "error": "Rate limit exceeded",
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }

        # Security validation (can be bypassed for AI agents)
        if security_validator is not None:
            if not bypass_security:
                is_safe, error_msg = security_validator.validate_command(command, allow_sudo)
                if not is_safe:
                    security_validator.log_security_event(
                        "BLOCKED_COMMAND", f"Blocked dangerous command on {endpoint.hostname}: {command}", "WARNING"
                    )
                    return {
                        "success": False,
                        "error": f"Security validation failed: {error_msg}. "
                        "Pass bypass_security=true if this command is intentional.",
                        "command": command,
                        "endpoint": endpoint.hostname,
                        "timestamp": datetime.now().isoformat(),
                    }
            elif bypass_security:
                security_validator.log_security_event(
                    "SECURITY_BYPASS",
                    f"AI agent bypassed security for command on {endpoint.hostname}: {command[:50]}...",
                    "INFO",
                )

        # Apply working directory and environment on the remote (POSIX) side
        remote_prefix = ""
        if working_directory and working_directory != "~":
            remote_prefix += f"cd {shell_quote(working_directory)} && "
        if environment:
            remote_prefix += f"{build_env_assignments(environment)} "
        remote_command = remote_prefix + command

        ssh_client = None
        try:
            # Get connection from pool or create new one
            ssh_client = await self._get_ssh_connection(endpoint)

            # Paramiko's exec/read/status APIs are synchronous. Offload the
            # complete blocking sequence so independent MCP work (including
            # monitoring metrics) can make progress concurrently.
            try:
                stdout_data, stderr_data, exit_code = await asyncio.to_thread(
                    _run_ssh_command,
                    ssh_client,
                    remote_command,
                    timeout,
                )
            except TimeoutError:
                return {
                    "success": False,
                    "error": "Command execution timeout",
                    "command": command,
                    "endpoint": endpoint.hostname,
                    "timestamp": datetime.now().isoformat(),
                }

            # Sanitize output for security
            if security_validator is not None:
                stdout_data = security_validator.sanitize_command_output(stdout_data)
                stderr_data = security_validator.sanitize_command_output(stderr_data)

            # Log command execution
            logger.info(f"Command executed on {endpoint.hostname}: {command[:50]}... (exit: {exit_code})")

            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout_data,
                "stderr": stderr_data,
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }

        except paramiko.BadHostKeyException as e:
            message = self._host_key_mismatch_error(endpoint.hostname, e)
            logger.error(message)
            return {
                "success": False,
                "error": message,
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }
        except paramiko.AuthenticationException as e:
            logger.error(f"Authentication failed for {endpoint.hostname}: {e}")
            return {
                "success": False,
                "error": f"Authentication failed: {e}",
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }
        except paramiko.SSHException as e:
            message = self._ssh_error_message(endpoint.hostname, e)
            logger.error(f"SSH error for {endpoint.hostname}: {e}")
            return {
                "success": False,
                "error": message,
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"Command execution failed on {endpoint.hostname}: {e}")
            return {
                "success": False,
                "error": str(e),
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }
        finally:
            if ssh_client:
                await self._return_ssh_connection(endpoint, ssh_client)

    async def _get_ssh_connection(self, endpoint: TunnelEndpoint) -> paramiko.SSHClient:
        """Get SSH connection from pool or create new one"""
        pool_key = f"{endpoint.hostname}:{endpoint.port}"

        # Check if we have available connections in pool
        if pool_key in self._connection_pool and self._connection_pool[pool_key]:
            ssh_client = self._connection_pool[pool_key].pop()
            # Reuse only if the transport is still alive
            transport = ssh_client.get_transport()
            if transport is not None and transport.is_active():
                endpoint.current_connections += 1
                return ssh_client
            with contextlib.suppress(Exception):
                ssh_client.close()

        # Create new connection
        if endpoint.current_connections >= endpoint.max_connections:
            raise ConnectionError(f"Maximum connections reached for {endpoint.hostname}")

        ssh_client = paramiko.SSHClient()
        self._apply_host_key_policy(ssh_client)

        private_key = load_ssh_private_key(endpoint.private_key_path)

        # Connect with security settings
        ssh_client.connect(
            hostname=endpoint.hostname,
            port=endpoint.port,
            username=endpoint.username,
            pkey=private_key,
            timeout=10,
            auth_timeout=10,
            banner_timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )

        endpoint.current_connections += 1
        return ssh_client

    async def _return_ssh_connection(self, endpoint: TunnelEndpoint, ssh_client: paramiko.SSHClient):
        """Return SSH connection to pool or close it"""
        pool_key = f"{endpoint.hostname}:{endpoint.port}"

        try:
            transport = ssh_client.get_transport()
            pool = self._connection_pool.setdefault(pool_key, [])
            if transport is not None and transport.is_active() and len(pool) < self._max_pool_size:
                pool.append(ssh_client)
            else:
                ssh_client.close()
        except Exception:
            # Connection is dead, just close it
            with contextlib.suppress(BaseException):
                ssh_client.close()
        finally:
            endpoint.current_connections = max(0, endpoint.current_connections - 1)

    async def create_terminal_session(
        self, endpoint: TunnelEndpoint, session_id: str | None = None
    ) -> TerminalSession | LocalTerminalSession | None:
        """Create a persistent terminal session"""
        if not session_id:
            session_id = str(uuid.uuid4())[:8]

        if session_id in self.terminal_sessions:
            logger.error(f"Terminal session already exists: {session_id}")
            return None

        if self._is_local_endpoint(endpoint):
            try:
                local_session = await asyncio.to_thread(LocalTerminalSession, session_id, endpoint)
            except Exception as e:
                logger.error(f"Failed to create local terminal session: {e}")
                return None
            self.terminal_sessions[session_id] = local_session
            return local_session

        try:
            ssh = paramiko.SSHClient()
            self._apply_host_key_policy(ssh)

            private_key = load_ssh_private_key(endpoint.private_key_path)
            ssh.connect(
                hostname=endpoint.hostname, port=endpoint.port, username=endpoint.username, pkey=private_key, timeout=10
            )

            # Create interactive channel
            channel = ssh.invoke_shell()
            channel.settimeout(1.0)

            # Create session object
            session = TerminalSession(
                session_id=session_id,
                endpoint=endpoint,
                ssh_client=ssh,
                channel=channel,
                created=datetime.now(),
                last_activity=datetime.now(),
                is_interactive=True,
            )

            self.terminal_sessions[session_id] = session
            return session

        except paramiko.BadHostKeyException as e:
            logger.error(self._host_key_mismatch_error(endpoint.hostname, e))
            return None
        except Exception as e:
            logger.error(f"Failed to create terminal session: {e}")
            return None

    async def execute_in_session(
        self,
        session_id: str,
        command: str,
        timeout: int = 30,
        allow_sudo: bool = False,
        bypass_security: bool = False,
    ) -> dict[str, Any]:
        """Execute command in existing terminal session with security validation"""
        if session_id not in self.terminal_sessions:
            return {"success": False, "error": f"Terminal session {session_id} not found"}

        session = self.terminal_sessions[session_id]

        # Rate limiting check, sharing the endpoint's one-shot budget
        if not self._check_rate_limit(self._execute_rate_limit_key(session.endpoint)):
            return {
                "success": False,
                "error": "Rate limit exceeded",
                "session_id": session_id,
                "command": command,
                "timestamp": datetime.now().isoformat(),
            }

        # Security validation (can be bypassed for AI agents)
        if security_validator is not None:
            if not bypass_security:
                is_safe, error_msg = security_validator.validate_command(command, allow_sudo)
                if not is_safe:
                    security_validator.log_security_event(
                        "BLOCKED_SESSION_COMMAND",
                        f"Blocked dangerous command in terminal session {session_id}: {command}",
                        "WARNING",
                    )
                    return {
                        "success": False,
                        "error": f"Security validation failed: {error_msg}. "
                        "Pass bypass_security=true if this command is intentional.",
                        "session_id": session_id,
                        "command": command,
                        "timestamp": datetime.now().isoformat(),
                    }
            else:
                security_validator.log_security_event(
                    "SESSION_SECURITY_BYPASS",
                    f"AI agent bypassed security for terminal session command: {command[:50]}...",
                    "INFO",
                )

        if isinstance(session, LocalTerminalSession):
            local_result = await asyncio.to_thread(session.execute, command, timeout)

            # Sanitize output for security
            if security_validator is not None and local_result.get("output"):
                local_result["output"] = security_validator.sanitize_command_output(local_result["output"])

            return local_result

        try:
            # Send command
            session.channel.send(command + "\n")
            session.last_activity = datetime.now()

            # Wait for output
            output = ""
            start_time = time.time()

            while time.time() - start_time < timeout:
                if session.channel.recv_ready():
                    data = session.channel.recv(4096).decode("utf-8", errors="ignore")
                    output += data

                    # Check if command completed (simple heuristic)
                    if data.endswith("$ ") or data.endswith("# "):
                        break
                else:
                    await asyncio.sleep(0.1)

            # Sanitize output for security
            if security_validator is not None:
                output = security_validator.sanitize_command_output(output)

            return {
                "success": True,
                "output": output,
                "session_id": session_id,
                "command": command,
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": str(e), "session_id": session_id, "command": command}

    def close_terminal_session(self, session_id: str) -> bool:
        """Close a terminal session"""
        session = self.terminal_sessions.pop(session_id, None)
        if session is None:
            return False
        with contextlib.suppress(Exception):
            session.close()
        return True

    def add_endpoint(
        self,
        hostname: str,
        username: str,
        private_key_path: str,
        platform: str = "unknown",
        purpose: str = "primary",
        port: int = DEFAULT_SSH_PORT,
    ) -> TunnelEndpoint:
        """Add a new tunnel endpoint.

        `port` is accepted here because TunnelEndpoint has always carried one and the
        SSH connect path has always used it, while every endpoint added through this
        method was pinned to 22. A host reached on any other port - a container with SSH
        forwarded, a jump host, anything behind a NAT rule - could not be registered.
        """
        endpoint = TunnelEndpoint(
            hostname=hostname,
            username=username,
            private_key_path=private_key_path,
            platform=platform,
            purpose=purpose,
            port=port,
            created=datetime.now(),
            status="unknown",
        )

        self.endpoints.append(endpoint)
        self.save_config()
        return endpoint

    async def auto_failover(self) -> TunnelEndpoint | None:
        """Automatically failover to best available endpoint"""
        if self.active_endpoint and await self.test_endpoint(self.active_endpoint):
            return self.active_endpoint

        # Current endpoint failed, find new one
        new_endpoint = await self.find_best_endpoint()

        if new_endpoint:
            self.active_endpoint = new_endpoint
            self.save_config()
            return new_endpoint

        if self.enable_local_endpoint:
            local_endpoint = self.get_local_endpoint()
            await self.test_endpoint(local_endpoint)
            self.active_endpoint = local_endpoint
            return local_endpoint

        return None

    def get_local_endpoint(self) -> TunnelEndpoint:
        """Return an in-process endpoint for controlling the machine running Rigout."""
        if self._local_endpoint is None:
            self._local_endpoint = TunnelEndpoint(
                hostname="local-device",
                username=os.getenv("USERNAME") or os.getenv("USER") or "local",
                private_key_path="__local__",
                port=1,
                platform=platform.system().lower(),
                status="active",
                purpose="local",
                created=datetime.now(),
            )
        return self._local_endpoint

    def _is_local_endpoint(self, endpoint: TunnelEndpoint) -> bool:
        return endpoint.private_key_path == "__local__"

    async def _execute_local_command(
        self,
        endpoint: TunnelEndpoint,
        command: str,
        timeout: int = 30,
        allow_sudo: bool = False,
        bypass_security: bool = False,
        working_directory: str | None = None,
        environment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a command on the Rigout host without SSH."""
        if not self._check_rate_limit(self._execute_rate_limit_key(endpoint)):
            return {
                "success": False,
                "error": "Rate limit exceeded",
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }

        if security_validator is not None:
            if not bypass_security:
                is_safe, error_msg = security_validator.validate_command(command, allow_sudo)
                if not is_safe:
                    security_validator.log_security_event(
                        "BLOCKED_LOCAL_COMMAND",
                        f"Blocked dangerous local command: {command}",
                        "WARNING",
                    )
                    return {
                        "success": False,
                        "error": f"Security validation failed: {error_msg}. "
                        "Pass bypass_security=true if this command is intentional.",
                        "command": command,
                        "endpoint": endpoint.hostname,
                        "timestamp": datetime.now().isoformat(),
                    }
            else:
                security_validator.log_security_event(
                    "LOCAL_SECURITY_BYPASS",
                    f"AI agent bypassed security for local command: {command[:50]}...",
                    "INFO",
                )

        cwd = None
        if working_directory and working_directory != "~":
            cwd = str(Path(working_directory).expanduser())
            if not Path(cwd).is_dir():
                return {
                    "success": False,
                    "error": f"Working directory does not exist: {cwd}",
                    "command": command,
                    "endpoint": endpoint.hostname,
                    "timestamp": datetime.now().isoformat(),
                }
        env = None
        if environment:
            env = {**os.environ, **{str(key): str(value) for key, value in environment.items()}}

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            if security_validator is not None:
                stdout = security_validator.sanitize_command_output(stdout)
                stderr = security_validator.sanitize_command_output(stderr)
            logger.info(f"Local command executed: {command[:50]}... (exit: {completed.returncode})")
            return {
                "success": completed.returncode == 0,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Command execution timeout",
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"Local command execution failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "command": command,
                "endpoint": endpoint.hostname,
                "timestamp": datetime.now().isoformat(),
            }


# Initialize tunnel manager (will be created when needed)
tunnel_manager = None


def get_tunnel_manager():
    """Get or create tunnel manager instance"""
    global tunnel_manager
    if tunnel_manager is None:
        tunnel_manager = TunnelManager()
    return tunnel_manager
