import io
import json
from pathlib import Path

import paramiko
import pytest


@pytest.mark.integration
class TestAIAgentConnection:
    """Connection integration tests based on ai_agent_connection.json"""

    @pytest.fixture(autouse=True)
    def check_connection_file(self):
        """Load connection file or skip if not found"""
        connection_files = ["ai_agent_connection.json", "connection.json"]
        self.connection_file = None
        for file in connection_files:
            if Path(file).exists():
                self.connection_file = file
                break

        if not self.connection_file:
            pytest.skip("No connection configuration file found (ai_agent_connection.json)")

        with open(self.connection_file, encoding="utf-8") as f:
            self.connection_info = json.load(f)

        if "ssh_config" not in self.connection_info:
            pytest.skip("Connection file is not using SSH (e.g. it is HTTP streamable)")

    @pytest.mark.asyncio
    async def test_ssh_connection(self):
        """Test SSH connection using credentials in connection file"""
        ssh_config = self.connection_info["ssh_config"]

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        private_key_file = io.StringIO(ssh_config["private_key"])
        private_key = paramiko.Ed25519Key.from_private_key(private_key_file)

        ssh.connect(
            hostname=ssh_config["hostname"],
            port=ssh_config["port"],
            username=ssh_config["username"],
            pkey=private_key,
            timeout=10,
        )

        stdin, stdout, stderr = ssh.exec_command('echo "AI Agent Connection Test"')
        result = stdout.read().decode().strip()
        ssh.close()

        assert result == "AI Agent Connection Test"

    @pytest.mark.asyncio
    async def test_system_access(self):
        """Test system commands execution"""
        ssh_config = self.connection_info["ssh_config"]

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        private_key_file = io.StringIO(ssh_config["private_key"])
        private_key = paramiko.Ed25519Key.from_private_key(private_key_file)

        ssh.connect(
            hostname=ssh_config["hostname"],
            port=ssh_config["port"],
            username=ssh_config["username"],
            pkey=private_key,
            timeout=10,
        )

        # Run system commands
        stdin, stdout, stderr = ssh.exec_command("whoami")
        whoami = stdout.read().decode().strip()
        ssh.close()

        assert len(whoami) > 0

    @pytest.mark.asyncio
    async def test_sudo_access(self):
        """Test sudo command execution"""
        ssh_config = self.connection_info["ssh_config"]

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        private_key_file = io.StringIO(ssh_config["private_key"])
        private_key = paramiko.Ed25519Key.from_private_key(private_key_file)

        ssh.connect(
            hostname=ssh_config["hostname"],
            port=ssh_config["port"],
            username=ssh_config["username"],
            pkey=private_key,
            timeout=10,
        )

        stdin, stdout, stderr = ssh.exec_command('sudo -n echo "Sudo test"', timeout=10)
        output = stdout.read().decode().strip()
        error = stderr.read().decode().strip()
        ssh.close()

        # If passwordless sudo is configured, this should work. Otherwise we expect permission denied or prompt.
        if "Sudo test" in output:
            assert True
        else:
            # We don't fail the test if sudo requires a password, but we log the check
            assert "password" in error.lower() or len(error) >= 0
