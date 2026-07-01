import asyncio
import contextlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import paramiko

from .terminal_session import TerminalSession

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


def heredoc_redirect(content: str, destination: str) -> str:
    """Create a shell heredoc that writes content to destination safely."""
    delimiter = f"EOF_{uuid.uuid4().hex}"
    return f"cat > {shell_quote(destination)} <<'{delimiter}'\n{content}\n{delimiter}"


class SecurityError(Exception):
    """Raised when security validation fails"""

    pass


class ConnectionError(Exception):
    """Raised when connection operations fail"""

    pass


class ConfigurationError(Exception):
    """Raised when configuration is invalid"""

    pass


@dataclass
class TunnelEndpoint:
    """Represents a tunnel endpoint with connection details"""

    hostname: str
    username: str
    private_key_path: str
    port: int = 22
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
        self.terminal_sessions: dict[str, TerminalSession] = {}
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
        self._max_requests_per_minute: int = 60
        self._background_tasks_started: bool = False
        self.enable_local_endpoint: bool = os.getenv("RIGOUT_LOCAL_MODE", "1").lower() not in {"0", "false", "no"}
        self._local_endpoint: TunnelEndpoint | None = None

        # Load config first
        self.load_config()

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
            try:
                session.channel.close()
                session.ssh_client.close()
            except Exception:
                pass

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
        """Save tunnel endpoints to configuration file"""
        data = {
            "endpoints": [asdict(endpoint) for endpoint in self.endpoints],
            "last_updated": datetime.now().isoformat(),
        }
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
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # Load private key with error handling
            try:
                if endpoint.private_key_path.endswith(".pem") or "rsa" in endpoint.private_key_path.lower():
                    private_key = paramiko.RSAKey.from_private_key_file(endpoint.private_key_path)
                else:
                    private_key = paramiko.Ed25519Key.from_private_key_file(endpoint.private_key_path)
            except paramiko.ssh_exception.PasswordRequiredException:
                raise SecurityError("Private key is password protected - not supported")
            except Exception as e:
                raise SecurityError(f"Failed to load private key: {e}")

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

        except paramiko.AuthenticationException as e:
            endpoint.status = "failed"
            logger.error(f"Authentication failed for {endpoint.hostname}: {e}")
            return False
        except paramiko.SSHException as e:
            endpoint.status = "failed"
            logger.error(f"SSH connection failed for {endpoint.hostname}: {e}")
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
                memory_gb=0.0,
                gpu_info=["Not probed"],
                disk_space_gb=round(free / (1024**3), 2) if free else 0.0,
                platform=platform.system(),
                architecture=platform.machine(),
                last_updated=datetime.now(),
            )
            self.hardware_cache[endpoint.hostname] = info
            return info

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            private_key = paramiko.Ed25519Key.from_private_key_file(endpoint.private_key_path)
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
    ) -> dict[str, Any]:
        """Execute command on remote system with security validation"""
        if self._is_local_endpoint(endpoint):
            return await self._execute_local_command(endpoint, command, timeout, allow_sudo, bypass_security)

        # Rate limiting check
        if not self._check_rate_limit(f"execute_{endpoint.hostname}"):
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
                        "error": f"Security validation failed: {error_msg}",
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

        ssh_client = None
        try:
            # Get connection from pool or create new one
            ssh_client = await self._get_ssh_connection(endpoint)

            # Execute command with timeout
            stdin, stdout, stderr = ssh_client.exec_command(command, timeout=timeout)

            # Get results with proper encoding and error handling
            try:
                stdout_data = stdout.read().decode("utf-8", errors="replace")
                stderr_data = stderr.read().decode("utf-8", errors="replace")
                exit_code = stdout.channel.recv_exit_status()
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
            logger.error(f"SSH error for {endpoint.hostname}: {e}")
            return {
                "success": False,
                "error": f"SSH error: {e}",
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
            # Test if connection is still alive
            try:
                ssh_client.exec_command('echo "test"', timeout=5)
                endpoint.current_connections += 1
                return ssh_client
            except Exception:
                # Connection is dead, create new one
                pass

        # Create new connection
        if endpoint.current_connections >= endpoint.max_connections:
            raise ConnectionError(f"Maximum connections reached for {endpoint.hostname}")

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Load private key
        try:
            if endpoint.private_key_path.endswith(".pem") or "rsa" in endpoint.private_key_path.lower():
                private_key = paramiko.RSAKey.from_private_key_file(endpoint.private_key_path)
            else:
                private_key = paramiko.Ed25519Key.from_private_key_file(endpoint.private_key_path)
        except Exception as e:
            raise SecurityError(f"Failed to load private key: {e}")

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
            # Test if connection is still alive
            ssh_client.exec_command('echo "test"', timeout=5)

            # Add to pool if not full
            if pool_key not in self._connection_pool:
                self._connection_pool[pool_key] = []

            if len(self._connection_pool[pool_key]) < self._max_pool_size:
                self._connection_pool[pool_key].append(ssh_client)
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
    ) -> TerminalSession | None:
        """Create a persistent terminal session"""
        if not session_id:
            session_id = str(uuid.uuid4())[:8]

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            private_key = paramiko.Ed25519Key.from_private_key_file(endpoint.private_key_path)
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

        except Exception as e:
            logger.error(f"Failed to create terminal session: {e}")
            return None

    async def execute_in_session(self, session_id: str, command: str, timeout: int = 30) -> dict[str, Any]:
        """Execute command in existing terminal session"""
        if session_id not in self.terminal_sessions:
            return {"success": False, "error": f"Terminal session {session_id} not found"}

        session = self.terminal_sessions[session_id]

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
        if session_id in self.terminal_sessions:
            session = self.terminal_sessions[session_id]
            try:
                session.channel.close()
                session.ssh_client.close()
                del self.terminal_sessions[session_id]
                return True
            except Exception:
                pass
        return False

    def add_endpoint(
        self, hostname: str, username: str, private_key_path: str, platform: str = "unknown", purpose: str = "primary"
    ) -> TunnelEndpoint:
        """Add a new tunnel endpoint"""
        endpoint = TunnelEndpoint(
            hostname=hostname,
            username=username,
            private_key_path=private_key_path,
            platform=platform,
            purpose=purpose,
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
    ) -> dict[str, Any]:
        """Execute a command on the Rigout host without SSH."""
        if not self._check_rate_limit("execute_local"):
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
                        "error": f"Security validation failed: {error_msg}",
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

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
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
