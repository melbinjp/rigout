# Rigout

Let AI agents use your computer through MCP.

Rigout runs a Streamable HTTP MCP server on a device so an AI agent can control that device through MCP tools. Start Rigout on the machine, give the agent the generated setup URL, and the agent can run commands, inspect the system, manage files, use Docker, and prepare development environments.

Use Rigout only on hardware, VMs, or containers you are willing to let an agent control.

## Quick start

Install from PyPI:

```bash
pip install rigout
```

Rigout requires Python 3.10 or newer.

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

If a step does not work, [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the common failures: the port already in use, `rigout` not on your PATH, unauthorized responses, and managed state in an unexpected location.

## Connect your agent

Rigout writes what a client needs into `connection.json` in the state directory. `rigout status` prints that path, and `rigout start --detach --output json` reports it as `connection_file`.

The `mcp` object in that file describes the connection:

```json
{
  "mcp": {
    "transport": "streamable-http",
    "url": "http://127.0.0.1:8765/mcp",
    "health_url": "http://127.0.0.1:8765/health",
    "headers": {}
  }
}
```

Register `url` with your MCP client as a streamable HTTP server, and send every header in `headers` on each request. The field names on the client side belong to that client rather than to Rigout, so check its documentation for where server definitions live.

`headers` is empty for a plain local server, which has no bearer auth. Once Rigout generates a token, or you pass `--auth-token`, it becomes `{"Authorization": "Bearer <token>"}`. The connection file is the only place that token is written, so treat the file as a credential.

On a tunnel the printed agent setup URL is quicker: hand it to the agent and it retrieves this same configuration itself.

## Background lifecycle and JSON output

The installed package includes lifecycle commands; the source-only shell wrappers are not required:

```bash
rigout start --tunnel cloudflare --detach
rigout status
rigout url
rigout logs --tail 100
rigout logs --follow
rigout stop
```

`rigout url` reprints the agent setup URL. In the foreground the URL that `start` prints scrolls away behind activity, and Ctrl+C in that terminal stops the server rather than copying anything; this reads the value back from the connection file, works whether Rigout was started in the foreground or detached, and does not touch the running server.

Nothing but the URL goes to stdout, so it can be moved without selecting it out of a terminal:

```bash
rigout url > setup-url.txt
rigout url | xclip -selection clipboard
ssh HOST rigout url
```

On macOS pipe to `pbcopy`, and on Windows to `clip.exe`. Use `--which mcp` or `--which health` for the other two endpoints. Only the setup URL is credential-equivalent, and the warning saying so goes to stderr, so a pipe stays clean.

Use JSON for automation and agent-to-agent handoff:

```bash
rigout start --tunnel cloudflare --detach --output json
rigout status --output json
rigout logs --tail 50 --output json
rigout stop --output json
```

Detached startup JSON is finite and intentionally excludes credentials. It reports the MCP/health URLs, PID, lifecycle status, state directory, and paths to the connection and activity files. `rigout logs --follow` is a text stream and cannot be combined with `--output json`.

JSON is the supported machine interface for connection data, lifecycle results, and activity snapshots. The default text output remains optimized for humans.

`status --output json` always reports the same keys, whether or not Rigout is running, so a caller can read a field without first checking the state. Fields that do not apply are `null` rather than absent:

| Key | Meaning |
| --- | --- |
| `status`, `running` | Lifecycle state, and whether a managed server is live |
| `pid` | Process ID of the managed launcher, or `null` |
| `mcp_url`, `health_url`, `local_health_url` | Endpoint URLs, or `null` before a start |
| `host`, `port`, `path`, `tunnel` | How the running server was started, or `null` |
| `state_dir`, `connection_file`, `activity_log` | Where managed state lives |
| `started_at`, `stopped_at`, `last_error` | Timestamps and the most recent failure, or `null` |

The default state directory is:

- Windows: `%LOCALAPPDATA%\rigout\state`
- macOS: `~/Library/Application Support/rigout`
- Linux: `$XDG_STATE_HOME/rigout`, or `~/.local/state/rigout`

Override it with `--state-dir PATH` or `RIGOUT_STATE_DIR`. Managed state includes `connection.json`, `activity.log`, `runtime.json`, `rigout.pid`, and `rigout.lock`. The lock file holds no content: it exists so that two lifecycle commands running at once cannot both conclude that Rigout is stopped and write conflicting records. Rigout recreates it whenever it needs it, so it is safe to delete while no Rigout command is running. On POSIX, Rigout creates its own state directory owner-only and writes runtime files owner-only; a directory it did not create is left as it found it, with a warning on stderr if it is group- or world-reachable. Keep the directory private on every platform because the connection file contains the bearer token.

If `stop` refuses because the recorded PID cannot be verified as a managed Rigout launcher, `rigout stop --force` clears the recorded state. It signals nothing: Rigout will not send a signal to a process it cannot prove is its own, so a stray server still has to be stopped by you.

Exit codes, for scripting:

| Code | Meaning |
| --- | --- |
| 0 | The command succeeded. For `status`, Rigout is running. |
| 1 | Nothing to report, or the command failed: `status` when Rigout is not running, `logs` when no activity log exists, `stop` when it refuses an unverified PID or the process does not stop. |
| 2 | Usage error: `--output json` without `--detach`, `logs --follow` combined with `--output json`, or an internal-only option passed directly. |

`stop` exits 0 when nothing was running, because the requested outcome already holds. `status` exits 1 in that same situation, because it is a query rather than a request to change anything.

## Source checkout

From a cloned repo:

```bash
python -m pip install -e .
rigout --tunnel cloudflare
```

The repository also ships `rigout.sh` and `rigout.ps1`, a convenience shim for running from a source checkout without installing the package:

```bash
./rigout.sh
./rigout.sh --background
./rigout.sh status
./rigout.sh stop
```

```powershell
.\rigout.ps1
.\rigout.ps1 -Background
.\rigout.ps1 status
.\rigout.ps1 stop
```

Both are deprecated as of 0.3.0 and say so when they run, on a stream other than stdout, so anything reading their output is unaffected. They hold no lifecycle state of their own: each action translates to the same lifecycle command the installed CLI runs, invoked as `python -m rigout.mcp_url_launcher` from the checkout so that no installation is needed. A server started by `./rigout.sh --background` is therefore visible to `rigout status` and can be stopped with `rigout stop`, and the reverse holds too. One difference to know: the wrappers keep their historical default of a Cloudflare tunnel, where `rigout start` defaults to no tunnel.

The installed CLI is the supported path and accepts many options the wrappers do not. If an older checkout left `ai_agent_connection.json` or `.rigout.pid` behind, `status` reports them as stale and points at the live state, and `stop` clears them.

## MCP tools

Rigout exposes:

- `execute_command`: run shell commands with timeout, working directory, environment variables, and optional security bypass.
- `file_operations`: read, write, append, delete, copy, move, chmod, and chown files.
- `bulk_file_transfer`: upload, download, or sync content and paths.
- `system_monitoring`: inspect CPU, memory, disk, network, processes, and GPU where available.
- `docker_operations`: list, run, exec, stop, remove, build, pull, logs, and inspect containers.
- `environment_setup`: create Python, Node, Docker, or Conda workspaces.
- `install_software`: install packages with apt, yum, dnf, pacman, pip, npm, Homebrew, or Chocolatey. The default `auto` mode picks apt, yum, Homebrew, or Chocolatey from the endpoint platform; name any other manager explicitly.
- `manage_tunnels`: add, remove, list, test, and fail over to SSH endpoints. `add` takes an optional `port` for a host that does not answer SSH on 22, such as a container with SSH forwarded; `list` shows a non-default port beside the hostname.
- `connect_hardware` and `get_hardware_info`: verify available hardware.
- `get_server_activity`: return bounded, sanitized JSON containing managed lifecycle status and 1-200 recent activity lines.
- `create_terminal_session`, `execute_in_terminal`, `list_terminal_sessions`, `close_terminal_session`: persistent terminal sessions that keep shell state between commands, on the local device or over SSH. `execute_in_terminal` takes the same `use_sudo` and `bypass_security` flags as `execute_command` and applies the same validation and output sanitization.

If no SSH endpoint is configured, Rigout uses a local-device endpoint. That makes a fresh one-command server immediately useful on the machine running Rigout.

## Security model

Rigout is powerful by design. Treat the MCP URL and bearer token like remote shell credentials.

Default controls:

- Public/tunnel mode generates bearer auth unless `--no-auth` is passed.
- Localhost mode has no bearer auth unless `--auth-token` is passed or `RIGOUT_AUTH_TOKEN` is set.
- Tokens are handed to the server process through environment variables, not command-line arguments, so they do not appear in the process list.
- Managed connection and activity files live in a per-user state directory and are written with owner-only permissions on POSIX systems.
- Setup tokens expire after 15 minutes by default, are redacted from Rigout-controlled access logs, and credential responses use `Cache-Control: no-store` and `Pragma: no-cache`.
- HTTP 401 responses advertise `WWW-Authenticate: Bearer` without echoing credential material.
- Command validation applies to both command paths, one-shot `execute_command` and `execute_in_terminal`. It blocks common destructive patterns, gates `sudo` behind `use_sudo`, allows routine pipelines and command chains, and logs unrecognized commands for auditing rather than blocking them. One route skips it, the same on either path: the caller passing `bypass_security`, which is recorded as a security event.
- Command output is sanitized for common secret patterns before returning to the agent, on the one-shot path and in terminal sessions, local and over SSH. Sanitization is pattern matching, not a guarantee that output contains no secrets.
- Managed runtime output is captured in `activity.log`; agents can read a bounded, sanitized view with `get_server_activity`.
- Operational tool failures and unknown tools set MCP `isError: true` and preserve useful stderr or exit-status diagnostics.
- Per-endpoint command rate limiting is enabled: 60 command executions per minute for each endpoint, including the local-device endpoint. One-shot commands and commands run inside a terminal session draw on that same per-endpoint budget; `bypass_security` does not lift it.

Do not expose Rigout publicly with `--no-auth` unless the network is private and trusted. For serious agent work, run Rigout inside an isolated VM or container.

## Public URL reliability

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

Rigout is written by AI coding agents working under human direction: the maintainer sets the product scope, safety boundaries, acceptance criteria, and release decisions, and agents write the implementation, tests, documentation, and release changes. Development standards for future contributors and agents are in [DEVELOPMENT_STANDARDS.md](DEVELOPMENT_STANDARDS.md).

## Project layout

```text
src/rigout/
  server.py              # MCP tool definitions and stdio transport
  mcp_http_server.py     # Streamable HTTP MCP server
  mcp_url_launcher.py    # one-command launcher and lifecycle CLI
  lifecycle.py           # per-user state and detached process management
  ssh_manager.py         # SSH endpoints and local fallback execution
  tools/                 # command, file, Docker, environment, monitoring tools
tests/                   # pytest unit and integration coverage
.github/workflows/       # CI and tagged release publishing
```

An end-to-end walkthrough of how one request travels through Rigout, from arrival at the HTTP server to the tool response, is in [docs/REQUEST_PATH.md](docs/REQUEST_PATH.md).
