# Rigout

Let AI agents use your computer through MCP.

Rigout runs a Streamable HTTP MCP server on a device so an AI agent can control that device through MCP tools. Start Rigout on the machine, give the agent the generated setup URL, and the agent can run commands, inspect the system, manage files, use Docker, and prepare development environments.

Use Rigout only on hardware, VMs, or containers you are willing to let an agent control.

## Quick Start

Install from PyPI:

```bash
pip install rigout
```

For a cloud agent, start the foreground shortcut:

```bash
rigout --tunnel cloudflare
```

Rigout starts a Cloudflare quick tunnel, generates bearer authentication, and prints an `Agent setup URL`. Paste that URL to the intended agent promptly. The setup token expires 15 minutes after server startup by default; the agent uses it to fetch the bearer-authenticated MCP configuration. Press Ctrl+C to stop Rigout.

If `cloudflared` is not already installed, Rigout downloads the matching official Cloudflare release into a user-local Rigout cache and runs it from there.

To serve only on the local device instead:

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

The launcher writes its connection and activity files under a per-user state directory. Give an agent the `mcp.url` and `mcp.headers` values only through an authorized channel. Public/tunnel mode automatically generates a bearer token unless `--no-auth` is explicitly used.

## Background Lifecycle and JSON Output

The installed package includes lifecycle commands; the source-only shell wrappers are not required:

```bash
rigout start --tunnel cloudflare --detach
rigout status
rigout logs --tail 100
rigout logs --follow
rigout stop
```

Use JSON for automation and agent-to-agent handoff:

```bash
rigout start --tunnel cloudflare --detach --output json
rigout status --output json
rigout logs --tail 50 --output json
rigout stop --output json
```

Detached startup JSON is finite and intentionally excludes credentials. It reports the MCP/health URLs, PID, lifecycle status, state directory, and paths to the connection and activity files. `rigout logs --follow` is a text stream and cannot be combined with `--output json`.

JSON is the supported machine interface for connection data, lifecycle results, and activity snapshots: it preserves URL, header, boolean, number, and list types without YAML parser dialects or implicit type coercion. The default text output remains optimized for humans.

The default state directory is:

- Windows: `%LOCALAPPDATA%\rigout\state`
- macOS: `~/Library/Application Support/rigout`
- Linux: `$XDG_STATE_HOME/rigout`, or `~/.local/state/rigout`

Override it with `--state-dir PATH` or `RIGOUT_STATE_DIR`. Managed state includes `connection.json`, `activity.log`, `runtime.json`, and `rigout.pid`. Rigout applies owner-only directory/file modes on POSIX; keep the directory private on every platform because the connection file contains the bearer token.

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
- `manage_tunnels`: add, remove, list, test, and fail over to SSH endpoints.
- `connect_hardware` and `get_hardware_info`: verify available hardware.
- `get_server_activity`: return bounded, sanitized JSON containing managed lifecycle status and 1-200 recent activity lines.
- `create_terminal_session`, `execute_in_terminal`, `list_terminal_sessions`, `close_terminal_session`: persistent terminal sessions that keep shell state between commands, on the local device or over SSH.

If no SSH endpoint is configured, Rigout uses a local-device endpoint. That makes a fresh one-command server immediately useful on the machine running Rigout.

## Security Model

Rigout is powerful by design. Treat the MCP URL and bearer token like remote shell credentials.

Default controls:

- Public/tunnel mode generates bearer auth unless `--no-auth` is passed.
- Localhost mode has no bearer auth unless `--auth-token` is passed.
- Tokens are handed to the server process through environment variables, not command-line arguments, so they do not appear in the process list.
- Managed connection and activity files live in a per-user state directory and are written with owner-only permissions on POSIX systems.
- Setup tokens expire after 15 minutes by default, are redacted from Rigout-controlled access logs, and credential responses use `Cache-Control: no-store` and `Pragma: no-cache`.
- HTTP 401 responses advertise `WWW-Authenticate: Bearer` without echoing credential material.
- Command validation blocks common destructive patterns unless the caller explicitly uses `bypass_security`. Routine pipelines and command chains are allowed; unrecognized commands are logged for auditing rather than blocked.
- Outputs are sanitized for common secret patterns before returning to the agent.
- Managed runtime output is captured in `activity.log`; agents can read a bounded, sanitized view with `get_server_activity`.
- Operational tool failures and unknown tools set MCP `isError: true` and preserve useful stderr or exit-status diagnostics.
- Per-endpoint command rate limiting is enabled.

Do not expose Rigout publicly with `--no-auth` unless the network is private and trusted. For serious agent work, run Rigout inside an isolated VM or container.

## Public URL Reliability

Cloudflare quick tunnels are useful for one-command setup and testing, but their public URLs are ephemeral. For long-running or production use, put Rigout behind a stable tunnel or gateway such as a named Cloudflare Tunnel, Tailscale, a reverse proxy, or a dedicated VM with explicit network controls.

Restarting a quick tunnel changes its URL and invalidates the old connection. Use `rigout status` to inspect the current managed URL; do not build durable automation around a `trycloudflare.com` address.

Rigout's automatic `cloudflared` download does not require administrator privileges. To use a pinned or system-managed binary instead:

```bash
rigout --tunnel cloudflare --cloudflared-path /path/to/cloudflared
```

To disable automatic downloads:

```bash
rigout --tunnel cloudflare --no-cloudflared-download
```

To verify the downloaded binary against a known checksum:

```bash
RIGOUT_CLOUDFLARED_SHA256=<sha256> rigout --tunnel cloudflare
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
  lifecycle.py           # per-user state and detached process management
  ssh_manager.py         # SSH endpoints and local fallback execution
  tools/                 # command, file, Docker, environment, monitoring tools
tests/                   # pytest unit and integration coverage
.github/workflows/       # CI and tagged release publishing
```
