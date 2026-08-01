import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import paramiko
import pytest

from rigout.ssh_manager import (
    DEFAULT_SSH_PORT,
    ConfigurationError,
    SecurityError,
    TunnelEndpoint,
    TunnelManager,
    WarningAutoAddPolicy,
    build_write_command,
    format_key_fingerprint,
    heredoc_redirect,
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
    async def test_local_agent_commands_keep_shell_support(self, temp_config_file):
        """Pipelines and other agent-authored shell syntax remain supported."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            endpoint = manager.get_local_endpoint()
            completed = Mock(returncode=0, stdout="ok", stderr="")

            with patch("rigout.ssh_manager.subprocess.run", return_value=completed) as run:
                result = await manager.execute_command(endpoint, "echo ok | head -1")

            assert result["success"] is True
            assert run.call_args.kwargs["shell"] is True

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
    async def test_remote_command_io_does_not_block_event_loop(self, temp_config_file):
        """Paramiko command reads can overlap for independent remote work."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)
            endpoint = TunnelEndpoint(
                hostname="remote.example.com",
                username="testuser",
                private_key_path="/test/key",
            )
            tracker = {"active": 0, "maximum": 0}
            lock = threading.Lock()

            class BlockingStream:
                def __init__(self, payload):
                    self.payload = payload
                    self.channel = Mock()
                    self.channel.recv_exit_status.return_value = 0

                def read(self):
                    with lock:
                        tracker["active"] += 1
                        tracker["maximum"] = max(tracker["maximum"], tracker["active"])
                    time.sleep(0.03)
                    with lock:
                        tracker["active"] -= 1
                    return self.payload

            clients = []
            for output in (b"first", b"second"):
                client = Mock()
                client.exec_command.return_value = (
                    Mock(),
                    BlockingStream(output),
                    BlockingStream(b""),
                )
                clients.append(client)

            manager._get_ssh_connection = AsyncMock(side_effect=clients)
            manager._return_ssh_connection = AsyncMock()

            results = await asyncio.gather(
                manager.execute_command(endpoint, "echo first"),
                manager.execute_command(endpoint, "echo second"),
            )

            assert all(result["success"] for result in results)
            assert tracker["maximum"] > 1

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

    def test_rate_limit_defaults_to_60_when_key_is_absent(self, temp_config_file):
        """A security_config that does not set the key keeps the pre-0.3.0 limit."""
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=temp_config_file)

        assert manager._max_requests_per_minute == 60

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


WRITE_CASES = {
    "empty": "",
    "plain": "abcd",
    "no_trailing_newline": "no-trailing-newline",
    "one_trailing_newline": "line\n",
    "several_trailing_newlines": "a\tb\n\n\n",
    "two_lines": "a\nb",
    "single_quotes": "it's a 'quoted' word",
    "printf_format": "100%s %d %% \\n",
    "leading_dash": "-n -e",
    "shell_syntax": "$(whoami) `id` ${HOME}",
    "unicode": "café — 你好",
    "old_delimiter_shape": "EOF_deadbeef\nmore",
    "large": "x" * 200_000,
}


def _posix_shell():
    """A shell that can actually run the generated command, or None to skip.

    `bash` on PATH under Windows may be the WSL relay, which cannot reach the
    Windows path the test writes to, so probe the candidate rather than trust it.
    """
    candidates = [r"C:\Program Files\Git\usr\bin\bash.exe", "bash", "sh"]
    for candidate in candidates:
        executable = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if not executable or not os.path.exists(executable):
            continue
        try:
            probe = subprocess.run([executable, "-c", "printf ok"], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.stdout == b"ok":
            return executable
    return None


@pytest.mark.unit
class TestRemoteWriteCommand:
    """A remote write must land the same bytes the local branch would write."""

    @pytest.mark.parametrize("name", list(WRITE_CASES))
    def test_content_survives_shell_quoting_exactly(self, name):
        """Portable half: the quoted operand decodes back to the original content."""
        content = WRITE_CASES[name]
        command = build_write_command(content, "/tmp/target file.txt")

        tokens = shlex.split(command)
        assert tokens[0] == "printf"
        assert tokens[2] == content
        assert tokens[4] == "/tmp/target file.txt"

    def test_no_newline_is_appended_to_the_command(self):
        """The exact defect: a heredoc body carries a newline before its delimiter."""
        command = build_write_command("no-trailing-newline", "/tmp/a.txt")

        assert "\n" not in command
        assert "<<" not in command

    @pytest.mark.parametrize("name", [n for n in WRITE_CASES if n != "large"])
    def test_written_file_is_byte_identical(self, tmp_path, name):
        """Executed half: run the generated command in a real shell and compare bytes."""
        shell = _posix_shell()
        if shell is None:
            pytest.skip("no POSIX shell available to execute the generated command")

        content = WRITE_CASES[name]
        destination = tmp_path / "written.txt"
        command = build_write_command(content, destination.as_posix())

        completed = subprocess.run([shell, "-c", command], capture_output=True, timeout=30)

        assert completed.returncode == 0, completed.stderr
        assert destination.read_bytes() == content.encode("utf-8")

    def test_append_uses_the_same_quoting_with_a_double_chevron(self):
        """tools/file_ops.py hand-rolled its own append; the helper spells it now."""
        content = "it's appended"

        write = build_write_command(content, "/tmp/a.txt")
        append = build_write_command(content, "/tmp/a.txt", append=True)

        assert write == append.replace(" >> ", " > ", 1)
        assert " >> " in append
        assert shlex.split(append)[2] == content

    def test_append_adds_no_bytes_of_its_own(self, tmp_path):
        """Two appends of one character must produce a two-byte file, not four."""
        shell = _posix_shell()
        if shell is None:
            pytest.skip("no POSIX shell available to execute the generated command")

        destination = tmp_path / "appended.txt"
        subprocess.run([shell, "-c", build_write_command("a", destination.as_posix())], check=True, timeout=30)
        subprocess.run(
            [shell, "-c", build_write_command("b", destination.as_posix(), append=True)], check=True, timeout=30
        )

        assert destination.read_bytes() == b"ab"

    def test_deprecated_alias_still_builds_the_same_command(self):
        """v0.2.0 exported heredoc_redirect, so the name has to keep working."""
        assert heredoc_redirect("x\n", "/tmp/a.txt") == build_write_command("x\n", "/tmp/a.txt")
        assert "<<" not in heredoc_redirect("x", "/tmp/a.txt")

    @pytest.mark.parametrize("name", [n for n in WRITE_CASES if n != "large"])
    def test_remote_bytes_match_what_the_local_branch_writes(self, tmp_path, name):
        """The two endpoints of one MCP call must not disagree about the bytes.

        The local side is written with newline="" deliberately. Python text mode
        translates "\\n" to os.linesep, so on a Windows host the local branch of
        file_operations turns "a\\nb" into b"a\\r\\nb" - a second divergence, living
        in tools/file_ops.py rather than here. Comparing against the untranslated
        write keeps this test measuring the remote command, which is what this
        module controls.
        """
        shell = _posix_shell()
        if shell is None:
            pytest.skip("no POSIX shell available to execute the generated command")

        content = WRITE_CASES[name]
        local = tmp_path / "local.txt"
        remote = tmp_path / "remote.txt"

        with local.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        subprocess.run([shell, "-c", build_write_command(content, remote.as_posix())], check=True, timeout=30)

        assert remote.read_bytes() == local.read_bytes()


def _write_config(path, security_config=None):
    """Write a minimal TunnelManager config, optionally with a security_config section."""
    config = {
        "server_config": {"name": "test-server"},
        "ssh_config": {"private_key_path": "/test/key", "username": "testuser"},
        "endpoints": [],
    }
    if security_config is not None:
        config["security_config"] = security_config
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _make_manager(config_file):
    with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
        return TunnelManager(config_file=config_file)


@pytest.fixture
def host_key_env(monkeypatch):
    """Neutralise host key environment variables so tests do not read the dev machine."""
    monkeypatch.delenv("RIGOUT_STRICT_HOST_KEYS", raising=False)
    monkeypatch.setenv("RIGOUT_KNOWN_HOSTS", "none")
    return monkeypatch


def _mock_ssh_client(policy_holder):
    """A mocked SSHClient whose connect() drives whatever policy the manager set.

    Paramiko calls the missing-host-key policy from inside connect() when the host
    is not in known_hosts, so replaying that here exercises the real policy objects
    without needing a server.
    """
    client = Mock()
    key = paramiko.ECDSAKey.generate()
    policy_holder["key"] = key

    def set_policy(policy):
        policy_holder["policy"] = policy

    def connect(**kwargs):
        policy = policy_holder.get("policy")
        if policy is not None:
            policy.missing_host_key(client, kwargs["hostname"], key)

    client.set_missing_host_key_policy.side_effect = set_policy
    client.connect.side_effect = connect

    stdout = Mock()
    stdout.read.return_value = b"connection_test_1234567890"
    stderr = Mock()
    stderr.read.return_value = b""
    client.exec_command.return_value = (Mock(), stdout, stderr)
    return client


@pytest.mark.unit
class TestHostKeyPolicy:
    """Host key handling: auto-accept stays the default, but stops being silent."""

    @pytest.fixture
    def endpoint(self):
        return TunnelEndpoint(hostname="tunnel.example.com", username="testuser", private_key_path="/test/key")

    def test_unknown_host_is_accepted_with_a_warning_by_default(self, tmp_path, host_key_env, endpoint, caplog):
        """Default (non-strict) still connects, but names the host and fingerprint."""
        manager = _make_manager(_write_config(tmp_path / "config.json"))
        assert manager.strict_host_keys is False

        holder = {}
        client = _mock_ssh_client(holder)
        with patch("paramiko.SSHClient", return_value=client):
            with patch("paramiko.Ed25519Key.from_private_key_file"):
                with patch("os.path.exists", return_value=True):
                    with caplog.at_level(logging.WARNING, logger="rigout.ssh_manager"):
                        result = asyncio.run(manager.test_endpoint(endpoint))

        assert result is True
        assert isinstance(holder["policy"], WarningAutoAddPolicy)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("tunnel.example.com" in message for message in warnings)
        assert any(format_key_fingerprint(holder["key"]) in message for message in warnings)

    def test_auto_accept_warning_is_not_repeated_for_the_same_host(self, tmp_path, host_key_env, endpoint, caplog):
        """Once per host per process, not once per command."""
        manager = _make_manager(_write_config(tmp_path / "config.json"))

        holder = {}
        client = _mock_ssh_client(holder)
        with patch("paramiko.SSHClient", return_value=client):
            with patch("paramiko.Ed25519Key.from_private_key_file"):
                with patch("os.path.exists", return_value=True):
                    with caplog.at_level(logging.WARNING, logger="rigout.ssh_manager"):
                        asyncio.run(manager.test_endpoint(endpoint))
                        asyncio.run(manager.test_endpoint(endpoint))

        accepted = [r for r in caplog.records if "Accepting unverified SSH host key" in r.getMessage()]
        assert len(accepted) == 1

    def test_strict_mode_from_environment_rejects_unknown_host(self, tmp_path, host_key_env, endpoint):
        """RIGOUT_STRICT_HOST_KEYS=1 turns an unknown host into a refusal."""
        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "1")
        manager = _make_manager(_write_config(tmp_path / "config.json"))
        assert manager.strict_host_keys is True

        holder = {}
        client = _mock_ssh_client(holder)
        with patch("paramiko.SSHClient", return_value=client):
            with patch("paramiko.Ed25519Key.from_private_key_file"):
                with patch("os.path.exists", return_value=True):
                    result = asyncio.run(manager.test_endpoint(endpoint))

        assert result is False
        assert endpoint.status == "failed"
        assert isinstance(holder["policy"], paramiko.RejectPolicy)

    def test_strict_mode_can_be_enabled_from_the_config_file(self, tmp_path, host_key_env):
        """The option is reachable without an environment variable."""
        config = _write_config(tmp_path / "config.json", {"strict_host_keys": True})
        manager = _make_manager(config)

        assert manager.strict_host_keys is True

    def test_environment_overrides_config_for_strict_mode(self, tmp_path, host_key_env):
        """An operator can turn strict mode back off without editing the config file."""
        config = _write_config(tmp_path / "config.json", {"strict_host_keys": True})
        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "0")
        manager = _make_manager(config)

        assert manager.strict_host_keys is False

    def test_known_hosts_file_is_loaded_read_only(self, tmp_path, monkeypatch):
        """The user's known_hosts is parsed by Paramiko and never rewritten by us."""
        monkeypatch.delenv("RIGOUT_STRICT_HOST_KEYS", raising=False)
        key = paramiko.ECDSAKey.generate()
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text(f"tunnel.example.com {key.get_name()} {key.get_base64()}\n", encoding="utf-8")
        monkeypatch.setenv("RIGOUT_KNOWN_HOSTS", str(known_hosts))

        manager = _make_manager(_write_config(tmp_path / "config.json"))
        client = paramiko.SSHClient()
        manager._apply_host_key_policy(client)

        loaded = client._system_host_keys.lookup("tunnel.example.com")
        assert loaded is not None
        assert loaded[key.get_name()] == key
        # load_host_keys() would make AutoAddPolicy write to the user's file; we must not.
        assert client._host_keys_filename is None

    def test_known_hosts_loading_can_be_turned_off(self, tmp_path, monkeypatch):
        """RIGOUT_KNOWN_HOSTS=none restores exact pre-0.3.0 behaviour."""
        monkeypatch.setenv("RIGOUT_KNOWN_HOSTS", "none")
        manager = _make_manager(_write_config(tmp_path / "config.json"))

        assert manager.known_hosts_path is None
        client = Mock()
        manager._apply_host_key_policy(client)
        client.load_system_host_keys.assert_not_called()

    def test_missing_known_hosts_file_still_connects(self, tmp_path, monkeypatch, endpoint):
        """A machine that has never used OpenSSH is not locked out."""
        monkeypatch.delenv("RIGOUT_STRICT_HOST_KEYS", raising=False)
        monkeypatch.setenv("RIGOUT_KNOWN_HOSTS", str(tmp_path / "absent" / "known_hosts"))
        manager = _make_manager(_write_config(tmp_path / "config.json"))

        holder = {}
        client = _mock_ssh_client(holder)
        with patch("paramiko.SSHClient", return_value=client):
            with patch("paramiko.Ed25519Key.from_private_key_file"):
                with patch("os.path.exists", return_value=True):
                    result = asyncio.run(manager.test_endpoint(endpoint))

        assert result is True

    def test_changed_host_key_is_refused_with_an_actionable_error(self, tmp_path, host_key_env, endpoint):
        """A host already in known_hosts offering a different key is the attack signature."""
        manager = _make_manager(_write_config(tmp_path / "config.json"))
        expected = paramiko.ECDSAKey.generate()
        offered = paramiko.ECDSAKey.generate()

        client = Mock()
        client.connect.side_effect = paramiko.BadHostKeyException(endpoint.hostname, offered, expected)
        with patch("paramiko.SSHClient", return_value=client):
            with patch("paramiko.Ed25519Key.from_private_key_file"):
                with patch("os.path.exists", return_value=True):
                    assert asyncio.run(manager.test_endpoint(endpoint)) is False

        manager._get_ssh_connection = AsyncMock(
            side_effect=paramiko.BadHostKeyException(endpoint.hostname, offered, expected)
        )
        result = asyncio.run(manager.execute_command(endpoint, "uptime"))

        assert result["success"] is False
        assert "Host key verification failed" in result["error"]
        assert format_key_fingerprint(offered) in result["error"]
        assert format_key_fingerprint(expected) in result["error"]
        assert f"ssh-keygen -R {endpoint.hostname}" in result["error"]

    def test_every_ssh_client_gets_the_managed_policy(self, tmp_path, host_key_env, endpoint):
        """All four connection paths must go through the manager's host key policy."""
        host_key_env.setenv("RIGOUT_STRICT_HOST_KEYS", "1")
        manager = _make_manager(_write_config(tmp_path / "config.json"))
        policies = []

        def make_client():
            client = Mock()
            client.set_missing_host_key_policy.side_effect = lambda policy: policies.append(policy)
            client.connect.side_effect = paramiko.SSHException("Server not found in known_hosts")
            return client

        with patch("paramiko.SSHClient", side_effect=make_client):
            with patch("paramiko.Ed25519Key.from_private_key_file"):
                with patch("os.path.exists", return_value=True):
                    asyncio.run(manager.test_endpoint(endpoint))
                    asyncio.run(manager.get_hardware_info(endpoint))
                    asyncio.run(manager.create_terminal_session(endpoint, "policy-session"))
                    with pytest.raises(paramiko.SSHException):
                        asyncio.run(manager._get_ssh_connection(endpoint))

        assert len(policies) == 4
        assert all(isinstance(policy, paramiko.RejectPolicy) for policy in policies)

    def test_auto_accept_policy_works_against_a_real_ssh_client(self, tmp_path, host_key_env, caplog):
        """The policy is exercised by Paramiko itself, so it must fit the real API."""
        manager = _make_manager(_write_config(tmp_path / "config.json"))
        client = paramiko.SSHClient()
        manager._apply_host_key_policy(client)
        key = paramiko.ECDSAKey.generate()

        with caplog.at_level(logging.WARNING, logger="rigout.ssh_manager"):
            client._policy.missing_host_key(client, "tunnel.example.com", key)

        assert client.get_host_keys().lookup("tunnel.example.com")[key.get_name()] == key
        assert any("tunnel.example.com" in r.getMessage() for r in caplog.records)

    def test_fingerprint_is_openssh_shaped(self):
        """The fingerprint in the warning must be comparable with ssh-keygen output."""
        key = paramiko.ECDSAKey.generate()
        fingerprint = format_key_fingerprint(key)

        assert fingerprint.startswith("SHA256:")
        assert not fingerprint.endswith("=")
        assert fingerprint == format_key_fingerprint(key)
        assert fingerprint != format_key_fingerprint(paramiko.ECDSAKey.generate())


@pytest.mark.unit
class TestConfiguredRateLimit:
    """security_config.max_requests_per_minute must be the limit that actually runs."""

    def test_configured_limit_is_the_one_enforced(self, tmp_path, host_key_env):
        config = _write_config(tmp_path / "config.json", {"max_requests_per_minute": 5})
        manager = _make_manager(config)

        assert manager._max_requests_per_minute == 5
        for _ in range(5):
            assert manager._check_rate_limit("execute_host") is True
        assert manager._check_rate_limit("execute_host") is False

    def test_configured_limit_applies_on_the_request_path(self, tmp_path, host_key_env):
        """The limit is enforced by execute_command, not just stored."""
        config = _write_config(tmp_path / "config.json", {"max_requests_per_minute": 1})
        manager = _make_manager(config)
        endpoint = manager.get_local_endpoint()

        first = asyncio.run(manager.execute_command(endpoint, "echo one"))
        second = asyncio.run(manager.execute_command(endpoint, "echo two"))

        assert first["success"] is True
        assert second["success"] is False
        assert second["error"] == "Rate limit exceeded"

    def test_default_is_60_when_no_config_file_exists(self, tmp_path, host_key_env):
        """A fresh install with no config file keeps the built-in limit."""
        manager = _make_manager(str(tmp_path / "brand-new-config.json"))

        assert manager._max_requests_per_minute == 60
        for _ in range(60):
            assert manager._check_rate_limit("execute_host") is True
        assert manager._check_rate_limit("execute_host") is False

    @pytest.mark.parametrize("bad_value", [0, -5, "many", None, True])
    def test_invalid_limit_falls_back_to_the_default(self, tmp_path, host_key_env, bad_value):
        """A bad value must not lock the deployment out of its own endpoints."""
        config = _write_config(tmp_path / "config.json", {"max_requests_per_minute": bad_value})
        manager = _make_manager(config)

        assert manager._max_requests_per_minute == 60

    def test_malformed_security_config_does_not_stop_startup(self, tmp_path, host_key_env):
        """A broken section was ignored before 0.3.0; it must not become fatal now."""
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "server_config": {"name": "test-server"},
                    "ssh_config": {"private_key_path": "/test/key"},
                    "endpoints": [],
                    "security_config": ["not", "an", "object"],
                }
            ),
            encoding="utf-8",
        )
        manager = _make_manager(str(config_path))

        assert manager._max_requests_per_minute == 60
        assert manager.strict_host_keys is False

    def test_disabling_rate_limiting_is_reported_as_unsupported(self, tmp_path, host_key_env, caplog):
        """enable_rate_limiting is still not wired; say so instead of pretending."""
        config = _write_config(tmp_path / "config.json", {"enable_rate_limiting": False})
        with caplog.at_level(logging.WARNING, logger="rigout.ssh_manager"):
            manager = _make_manager(config)

        assert manager._max_requests_per_minute == 60
        assert any("enable_rate_limiting" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
class TestEndpointPortPersistence:
    """A port that is not saved and reloaded is a port that works until the first restart."""

    @pytest.fixture
    def config_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
            json.dump({"endpoints": [], "security_config": {}}, handle)
            handle.flush()
            yield handle.name
        if os.path.exists(handle.name):
            os.unlink(handle.name)

    def test_a_non_default_port_survives_save_and_reload(self, config_path):
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=config_path)
            manager.add_endpoint("pod.example.com", "root", "/tmp/key", "linux", port=39554)

            reloaded = TunnelManager(config_file=config_path)

        assert [endpoint.port for endpoint in reloaded.endpoints] == [39554]
        assert reloaded.endpoints[0].hostname == "pod.example.com"

    def test_an_endpoint_added_without_a_port_reloads_as_22(self, config_path):
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            manager = TunnelManager(config_file=config_path)
            manager.add_endpoint("box.example.com", "agent", "/tmp/key", "linux")

            reloaded = TunnelManager(config_file=config_path)

        assert reloaded.endpoints[0].port == DEFAULT_SSH_PORT


@pytest.mark.unit
class TestStrictHostKeyRefusalMessage:
    """The first error anyone meets after turning strict host key checking on.

    Paramiko's RejectPolicy raises "Server '[host]:port' not found in known_hosts",
    which states the refusal and nothing about resolving it. Unlike a key mismatch this
    is usually not an attack, just a host nobody has recorded yet, so the message has to
    say how to record it.
    """

    @pytest.fixture
    def manager(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
            json.dump({"endpoints": [], "security_config": {}}, handle)
            handle.flush()
            path = handle.name
        with patch("rigout.ssh_manager.TunnelManager._start_background_tasks"):
            yield TunnelManager(config_file=path)
        if os.path.exists(path):
            os.unlink(path)

    def test_an_unrecorded_host_is_told_how_to_record_it(self, manager):
        message = manager._ssh_error_message(
            "pod.example.com",
            paramiko.SSHException("Server 'pod.example.com' not found in known_hosts"),
        )

        assert "no entry" in message
        assert "ssh-keyscan" in message
        assert "RIGOUT_STRICT_HOST_KEYS" in message
        assert "pod.example.com" in message

    def test_the_message_says_to_check_the_fingerprint_first(self, manager):
        """ssh-keyscan trusts whatever answers, so recommending it without that
        caveat would be teaching the habit the check exists to prevent."""
        message = manager._ssh_error_message(
            "pod.example.com", paramiko.SSHException("Server 'x' not found in known_hosts")
        )

        assert "fingerprint" in message

    def test_other_ssh_errors_are_passed_through_unchanged(self, manager):
        message = manager._ssh_error_message("host", paramiko.SSHException("Error reading SSH protocol banner"))

        assert message == "SSH error: Error reading SSH protocol banner"
        assert "ssh-keyscan" not in message
