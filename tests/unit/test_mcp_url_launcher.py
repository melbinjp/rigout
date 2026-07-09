import io
import json
import os
import tarfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from rigout.mcp_http_server import connection_setup_url, tokens_match, write_connection_file
from rigout.mcp_url_launcher import (
    cloudflared_asset,
    cloudflared_cache_path,
    install_cloudflared,
    resolve_cloudflared_binary,
    resolve_public_mcp_url,
    start_cloudflare_tunnel,
    start_server,
)


@pytest.mark.unit
def test_cloudflared_asset_names_for_supported_platforms():
    assert cloudflared_asset("Linux", "x86_64") == ("cloudflared-linux-amd64", False)
    assert cloudflared_asset("Linux", "aarch64") == ("cloudflared-linux-arm64", False)
    assert cloudflared_asset("Darwin", "arm64") == ("cloudflared-darwin-arm64.tgz", True)
    assert cloudflared_asset("Windows", "AMD64") == ("cloudflared-windows-amd64.exe", False)


@pytest.mark.unit
def test_cloudflared_asset_rejects_unsupported_platform():
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        cloudflared_asset("FreeBSD", "x86_64")


@pytest.mark.unit
def test_connection_setup_url_uses_public_base_and_custom_path():
    setup_url = connection_setup_url("https://agent.example/custom-mcp", "/custom-mcp", "setup secret")

    assert setup_url == "https://agent.example/connection.json?setup_token=setup+secret"


@pytest.mark.unit
def test_write_connection_file_includes_agent_setup_url(tmp_path):
    connection_file = tmp_path / "connection.json"

    write_connection_file(
        connection_file,
        "https://agent.example/mcp",
        "127.0.0.1",
        8765,
        "/mcp",
        auth_token="secret-token",
        agent_setup_url="https://agent.example/connection.json?setup_token=setup",
    )

    data = json.loads(connection_file.read_text(encoding="utf-8"))
    assert data["agent_setup_url"] == "https://agent.example/connection.json?setup_token=setup"
    assert data["agent_setup_security"] == "credential_url"
    assert data["mcp"]["headers"]["Authorization"] == "Bearer secret-token"
    if os.name == "posix":
        # File contains a bearer token, so it must be owner-only
        assert (connection_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.unit
def test_tokens_match_is_none_safe_and_type_flexible():
    assert tokens_match("Bearer abc", b"Bearer abc")
    assert tokens_match(b"Bearer abc", "Bearer abc")
    assert not tokens_match(None, "Bearer abc")
    assert not tokens_match("Bearer abd", "Bearer abc")


def launcher_args(**overrides):
    values = {
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/mcp",
        "connection_file": "connection.json",
        "json_response": False,
        "stateless": False,
        "auth_token": None,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.mark.unit
def test_start_server_passes_public_url_and_tokens_via_environment():
    args = launcher_args(json_response=True, stateless=True, auth_token="auth-token")

    with patch("rigout.mcp_url_launcher.subprocess.Popen") as popen:
        start_server(args, public_url="https://agent.example/mcp", setup_token="setup-token")

    command = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert command[command.index("--public-url") + 1] == "https://agent.example/mcp"
    assert "--json-response" in command
    assert "--stateless" in command
    # Tokens must not appear in argv (visible in the process list)
    assert "auth-token" not in command
    assert "setup-token" not in command
    assert env["RIGOUT_AUTH_TOKEN"] == "auth-token"
    assert env["RIGOUT_SETUP_TOKEN"] == "setup-token"


@pytest.mark.unit
def test_start_server_omits_public_setup_arguments_for_local_start():
    args = launcher_args()

    with patch("rigout.mcp_url_launcher.subprocess.Popen") as popen:
        start_server(args)

    command = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert "--public-url" not in command
    assert "RIGOUT_AUTH_TOKEN" not in env
    assert "RIGOUT_SETUP_TOKEN" not in env


@pytest.mark.unit
def test_resolve_cloudflared_binary_uses_explicit_path(tmp_path):
    binary = tmp_path / "cloudflared"
    binary.write_text("mock", encoding="utf-8")

    assert resolve_cloudflared_binary(str(binary)) == str(binary)


@pytest.mark.unit
def test_resolve_cloudflared_binary_rejects_missing_explicit_path(tmp_path):
    with pytest.raises(RuntimeError, match="cloudflared binary not found"):
        resolve_cloudflared_binary(str(tmp_path / "missing-cloudflared"))


@pytest.mark.unit
def test_resolve_cloudflared_binary_downloads_when_missing(tmp_path, monkeypatch):
    installed = tmp_path / "cloudflared"
    installed.write_text("mock", encoding="utf-8")

    with (
        patch("rigout.mcp_url_launcher.shutil.which", return_value=None),
        patch("rigout.mcp_url_launcher.install_cloudflared", return_value=installed),
    ):
        assert resolve_cloudflared_binary() == str(installed)


@pytest.mark.unit
def test_resolve_cloudflared_binary_respects_download_opt_out():
    with patch("rigout.mcp_url_launcher.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="cloudflared is not installed"):
            resolve_cloudflared_binary(allow_download=False)


@pytest.mark.unit
def test_install_cloudflared_downloads_linux_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGOUT_CACHE_DIR", str(tmp_path))
    expected_path = cloudflared_cache_path("Linux", "x86_64")

    def fake_download(_url: str, destination: Path, _expected_sha256: str | None = None) -> None:
        destination.write_bytes(b"#!/bin/sh\n")

    with (
        patch("rigout.mcp_url_launcher.platform.system", return_value="Linux"),
        patch("rigout.mcp_url_launcher.platform.machine", return_value="x86_64"),
        patch("rigout.mcp_url_launcher.download_file", side_effect=fake_download),
    ):
        installed = install_cloudflared()

    assert installed == expected_path
    assert installed.exists()
    assert os.access(installed, os.X_OK)


@pytest.mark.unit
def test_install_cloudflared_extracts_macos_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGOUT_CACHE_DIR", str(tmp_path))
    expected_path = cloudflared_cache_path("Darwin", "arm64")

    def fake_download(_url: str, destination: Path, _expected_sha256: str | None = None) -> None:
        payload = b"#!/bin/sh\n"
        with tarfile.open(destination, "w:gz") as archive:
            info = tarfile.TarInfo("cloudflared")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    with (
        patch("rigout.mcp_url_launcher.platform.system", return_value="Darwin"),
        patch("rigout.mcp_url_launcher.platform.machine", return_value="arm64"),
        patch("rigout.mcp_url_launcher.download_file", side_effect=fake_download),
    ):
        installed = install_cloudflared()

    assert installed == expected_path
    assert installed.exists()
    assert installed.read_bytes() == b"#!/bin/sh\n"


class FakeCloudflaredProcess:
    """Stand-in for the subprocess.Popen handle to a running cloudflared."""

    def __init__(self, lines: list[str], exits_after_output: bool = False):
        self._lines = iter(lines)
        self.stdout = self
        self._exited = False
        self._exits_after_output = exits_after_output
        self.terminated = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._lines)
        except StopIteration:
            if self._exits_after_output:
                self._exited = True
            raise

    def poll(self):
        return 0 if self._exited else None

    def terminate(self):
        self.terminated = True
        self._exited = True


@pytest.mark.unit
def test_start_cloudflare_tunnel_extracts_url_from_process_output():
    fake_process = FakeCloudflaredProcess(
        ["cloudflared 2024.1.0\n", "Your quick Tunnel has been created!\n", "https://random-words.trycloudflare.com\n"]
    )

    with (
        patch("rigout.mcp_url_launcher.resolve_cloudflared_binary", return_value="cloudflared"),
        patch("rigout.mcp_url_launcher.subprocess.Popen", return_value=fake_process) as popen,
    ):
        process, url = start_cloudflare_tunnel(8765, timeout=5)

    assert process is fake_process
    assert url == "https://random-words.trycloudflare.com"
    command = popen.call_args.args[0]
    assert command == ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8765", "--no-autoupdate"]


@pytest.mark.unit
def test_start_cloudflare_tunnel_raises_when_process_exits_without_url():
    fake_process = FakeCloudflaredProcess(["failed to connect to origin\n"], exits_after_output=True)

    with (
        patch("rigout.mcp_url_launcher.resolve_cloudflared_binary", return_value="cloudflared"),
        patch("rigout.mcp_url_launcher.subprocess.Popen", return_value=fake_process),
    ):
        with pytest.raises(RuntimeError, match="exited before publishing a tunnel URL"):
            start_cloudflare_tunnel(8765, timeout=5)


@pytest.mark.unit
def test_start_cloudflare_tunnel_terminates_process_and_raises_on_timeout():
    fake_process = FakeCloudflaredProcess([])  # never publishes a URL, never exits

    with (
        patch("rigout.mcp_url_launcher.resolve_cloudflared_binary", return_value="cloudflared"),
        patch("rigout.mcp_url_launcher.subprocess.Popen", return_value=fake_process),
    ):
        with pytest.raises(RuntimeError, match="Timed out waiting for cloudflared"):
            start_cloudflare_tunnel(8765, timeout=0.3)

    assert fake_process.terminated is True


@pytest.mark.unit
def test_resolve_public_mcp_url_prefers_explicit_public_url():
    args = launcher_args(path="/mcp", public_url="https://agent.example")

    assert (
        resolve_public_mcp_url(args, tunnel_base_url="https://tunnel.trycloudflare.com") == "https://agent.example/mcp"
    )


@pytest.mark.unit
def test_resolve_public_mcp_url_uses_tunnel_base_url():
    args = launcher_args(path="/mcp", public_url=None)

    assert resolve_public_mcp_url(args, tunnel_base_url="https://tunnel.trycloudflare.com") == (
        "https://tunnel.trycloudflare.com/mcp"
    )


@pytest.mark.unit
def test_resolve_public_mcp_url_falls_back_to_local_url():
    args = launcher_args(host="127.0.0.1", port=8765, path="/mcp", public_url=None)

    assert resolve_public_mcp_url(args, tunnel_base_url=None) == "http://127.0.0.1:8765/mcp"
