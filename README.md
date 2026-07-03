# Rigout

Let AI agents use your computer through MCP.

Rigout runs a Streamable HTTP MCP server on a device so an AI agent can control that device through MCP tools. Start Rigout on the machine, give the agent the generated MCP URL, and the agent can run commands, inspect the system, manage files, use Docker, and prepare development environments.

Use Rigout only on hardware, VMs, or containers you are willing to let an agent control.

## Quick Start

Install from PyPI:

```bash
pip install rigout
```

Start a local MCP server:

```bash
rigout
```

If your shell does not find the `rigout` command after installation, run the same launcher with:

```bash
python -m rigout.mcp_url_launcher
```

This serves MCP at:

```text
http://127.0.0.1:8765/mcp
```

It also writes `ai_agent_connection.json`. Give the agent the `mcp.url` and, if present, `mcp.headers` values from that file.

For a cloud agent, expose the server with a Cloudflare quick tunnel:

```bash
rigout --tunnel cloudflare
```

If `cloudflared` is not already installed, Rigout downloads the matching official Cloudflare release into a user-local Rigout cache and runs it from there. Public/tunnel mode automatically generates a bearer token and writes it into the connection file.

For a smooth handoff, public/tunnel mode also prints an agent setup URL. Copy the `Agent setup URL` line and paste it to your AI agent so it can fetch its own MCP configuration. Treat the setup URL like a password: it can retrieve the bearer token.

## Source Checkout

From a cloned repo:

```bash
python -m pip install -e .
rigout --tunnel cloudflare
```

The shell helpers are optional:

```bash
./rigout.sh --background
./rigout.sh status
./rigout.sh stop
```

```powershell
.\rigout.ps1 -Background
.\rigout.ps1 status
.\rigout.ps1 stop
```

## MCP Tools

Rigout exposes:

- `execute_command`: run shell commands with timeout, working directory, environment variables, and optional security bypass.
- `file_operations`: read, write, append, delete, copy, move, chmod, and chown files.
- `bulk_file_transfer`: upload, download, or sync content and paths.
- `system_monitoring`: inspect CPU, memory, disk, network, processes, and GPU where available.
- `docker_operations`: list, run, exec, stop, remove, build, pull, logs, and inspect containers.
- `environment_setup`: create Python, Node, Docker, or Conda workspaces.
- `manage_tunnels`: add, list, test, and fail over to SSH endpoints.
- `connect_hardware` and `get_hardware_info`: verify available hardware.
- `create_terminal_session`, `execute_in_terminal`, `list_terminal_sessions`, `close_terminal_session`: persistent SSH-backed terminal sessions.

If no SSH endpoint is configured, Rigout uses a local-device endpoint. That makes a fresh one-command server immediately useful on the machine running Rigout.

## Security Model

Rigout is powerful by design. Treat the MCP URL and bearer token like remote shell credentials.

Default controls:

- Public/tunnel mode generates bearer auth unless `--no-auth` is passed.
- Localhost mode has no bearer auth unless `--auth-token` is passed.
- Command validation blocks common destructive patterns unless the caller explicitly uses `bypass_security`.
- Outputs are sanitized for common secret patterns before returning to the agent.
- Command activity and security events are written to `mcp-hardware-server.log`.
- Per-endpoint command rate limiting is enabled.

Do not expose Rigout publicly with `--no-auth` unless the network is private and trusted. For serious agent work, run Rigout inside an isolated VM or container.

## Public URL Reliability

Cloudflare quick tunnels are useful for one-command setup and testing, but their public URLs are ephemeral. For long-running or production use, put Rigout behind a stable tunnel or gateway such as a named Cloudflare Tunnel, Tailscale, a reverse proxy, or a dedicated VM with explicit network controls.

Rigout's automatic `cloudflared` download does not require administrator privileges. To use a pinned or system-managed binary instead:

```bash
rigout --tunnel cloudflare --cloudflared-path /path/to/cloudflared
```

To disable automatic downloads:

```bash
rigout --tunnel cloudflare --no-cloudflared-download
```

To avoid printing a credential-bearing setup URL:

```bash
rigout --tunnel cloudflare --no-agent-setup-url
```

To provide your own setup URL token:

```bash
rigout --tunnel cloudflare --setup-token "$RIGOUT_SETUP_TOKEN"
```

## Validation

Run the test suite:

```bash
python -m pytest -q
```

Run the readiness check:

```bash
python production_validation.py
```

Build the package:

```bash
python -m build
```

Check the package metadata:

```bash
python -m twine check dist/rigout-*
```

Development standards for future contributors and agents are in [DEVELOPMENT_STANDARDS.md](DEVELOPMENT_STANDARDS.md).

## Project Layout

```text
src/rigout/
  server.py              # MCP tool definitions and stdio transport
  mcp_http_server.py     # Streamable HTTP MCP server
  mcp_url_launcher.py    # one-command server/tunnel launcher
  ssh_manager.py         # SSH endpoints and local fallback execution
  tools/                 # command, file, Docker, environment, monitoring tools
tests/                   # pytest unit and integration coverage
.github/workflows/       # CI and tagged release publishing
```
