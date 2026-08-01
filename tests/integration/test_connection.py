"""Live SSH endpoint tests, driven through Rigout's own TunnelManager.

These replace three tests that could never run. They required an `ssh_config` block
containing an inline `private_key`, a `hostname` and a `port`, read from the
connection file. No version of Rigout has ever written that: `build_connection_data`
emits no `ssh_config` at v0.1.0 or v0.2.0, and `SSHConfig` holds a
`private_key_path`, not key material. So they skipped silently for the project's
whole life, and their skip message named a missing file rather than a schema that
never existed.

They also drove `paramiko` directly, so had they run they would have tested paramiko
rather than Rigout. These go through `TunnelManager.execute_command`, which is the
code that actually ships, so they exercise the remote path's output sanitization and
command validation - both of which changed in 0.3.0.

Configure with environment variables and they run; leave them unset and they skip
with a reason that is true:

    RIGOUT_TEST_SSH_HOST      hostname or address of a throwaway machine
    RIGOUT_TEST_SSH_PORT      optional, defaults to 22
    RIGOUT_TEST_SSH_USER      optional, defaults to root
    RIGOUT_TEST_SSH_KEY       path to a private key file

Nothing here writes to the remote host or leaves anything behind.
"""

import os

import pytest

from rigout.ssh_manager import TunnelEndpoint, TunnelManager


def _endpoint() -> TunnelEndpoint:
    """Build an endpoint from the environment, or skip with an accurate reason."""
    host = os.getenv("RIGOUT_TEST_SSH_HOST")
    key = os.getenv("RIGOUT_TEST_SSH_KEY")
    if not host or not key:
        pytest.skip("No live SSH endpoint configured; set RIGOUT_TEST_SSH_HOST and RIGOUT_TEST_SSH_KEY")
    if not os.path.exists(key):
        pytest.skip(f"RIGOUT_TEST_SSH_KEY points at {key}, which does not exist")

    return TunnelEndpoint(
        name="integration",
        hostname=host,
        port=int(os.getenv("RIGOUT_TEST_SSH_PORT", "22")),
        username=os.getenv("RIGOUT_TEST_SSH_USER", "root"),
        private_key_path=key,
        platform="linux",
    )


@pytest.fixture
def manager_and_endpoint():
    endpoint = _endpoint()
    manager = TunnelManager()
    manager.endpoints[endpoint.name] = endpoint
    return manager, endpoint


@pytest.mark.integration
@pytest.mark.asyncio
async def test_command_executes_over_ssh(manager_and_endpoint):
    """A command runs on the remote host and its output comes back."""
    manager, endpoint = manager_and_endpoint

    result = await manager.execute_command(endpoint, "echo rigout-integration-ok")

    assert result["success"], result.get("error") or result.get("stderr")
    assert "rigout-integration-ok" in result["stdout"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credentials_are_scrubbed_from_remote_output(manager_and_endpoint):
    """Output crossing the SSH channel is sanitized before it reaches the caller.

    This is the property that keeps a secret out of an agent's context and out of
    whatever model serves it. Unit tests cover the scrubber; only a live endpoint
    proves it is applied on the path the shipped code actually takes.
    """
    manager, endpoint = manager_and_endpoint
    secret = "sk-live-INTEGRATION-SHOULD-NOT-APPEAR"

    result = await manager.execute_command(endpoint, f"echo 'api_key={secret}'")

    assert result["success"], result.get("error") or result.get("stderr")
    assert secret not in result["stdout"], "credential survived sanitization on the SSH branch"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_destructive_command_is_refused_over_ssh(manager_and_endpoint):
    """Validation governs the remote path, not just the local one."""
    manager, endpoint = manager_and_endpoint

    result = await manager.execute_command(endpoint, "rm -rf /")

    assert not result["success"], "a recursive delete of / was not refused over SSH"
    assert "dangerous" in (result.get("error") or "").lower()
