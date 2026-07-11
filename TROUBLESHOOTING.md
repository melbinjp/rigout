# Troubleshooting

## Server Does Not Start

Check dependencies:

```bash
pip install -e ".[dev]"
python -m pytest tests/integration/test_http_transport.py -q
```

Check whether the port is already in use:

```bash
python -c "import socket; s=socket.socket(); print(s.connect_ex(('127.0.0.1', 8765)))"
```

Use another port if needed:

```bash
rigout --port 9000
```

For a detached instance, inspect managed state before starting another copy:

```bash
rigout status
rigout logs --tail 100
```

`rigout status` exits with status 1 when Rigout is not running. A stale `starting`, `running`, or `stopping` record is normalized to `stopped` when its recorded process no longer exists.

If `rigout` is not found after installation, Python installed the console script outside your shell PATH. Use the module launcher or add Python's Scripts directory to PATH:

```bash
python -m rigout.mcp_url_launcher --tunnel cloudflare
```

## Cloud Agent Cannot Connect

Confirm the generated connection file reported by `rigout status`:

```bash
rigout status --output json
```

If bearer auth is enabled, plain `/connection.json` also requires the same `Authorization` header. Use either the owner-scoped `connection.json` file or the printed agent setup URL as the credential source.

Confirm the health URL from the same network as the agent if possible:

```bash
curl https://your-public-host/health
```

If using Cloudflare quick tunnels, remember that the URL changes each time the tunnel restarts.

The printed setup token expires 15 minutes after the server starts by default. If the setup URL now returns 401, restart Rigout to issue a fresh setup URL or configure the agent from the owner-scoped connection file. The bearer token remains the MCP credential after bootstrap.

## Cloudflare Download Fails

`rigout --tunnel cloudflare` automatically downloads `cloudflared` when it is not already installed. If the machine is offline, blocks GitHub downloads, or uses an unsupported CPU architecture, install `cloudflared` manually and pass:

```bash
rigout --tunnel cloudflare --cloudflared-path /path/to/cloudflared
```

To require a preinstalled binary and prevent automatic downloads:

```bash
rigout --tunnel cloudflare --no-cloudflared-download
```

## Unauthorized

Public/tunnel mode writes a bearer token into the managed `connection.json`. The MCP client must send:

```http
Authorization: Bearer <token>
```

If you pass `--auth-token`, use that exact token in the client headers.

If the agent only accepts a URL, use the printed agent setup URL rather than plain `/connection.json`. That setup URL is credential-bearing and should be shared only with the intended agent.

A standards-compliant 401 response includes `WWW-Authenticate: Bearer`; this is an authentication challenge, not a server failure. Rigout also sends `Cache-Control: no-store` and `Pragma: no-cache` on protected connection responses. Query credentials are redacted from Rigout-controlled access logs, but proxies, browser history, and other intermediaries remain outside that guarantee.

## Inspecting Server Output

For a detached process, use:

```bash
rigout logs --tail 100
rigout logs --follow
```

For an MCP-connected agent, call `get_server_activity`. It returns a sanitized JSON object with status and a bounded set of recent lines (50 by default, 200 maximum). An agent connected only through MCP cannot see the terminal window in which Rigout was launched, and raw terminal scraping would risk exposing credentials and unrelated output.

Use `rigout logs --tail 100 --output json` for finite machine-readable output. `--output json` cannot be combined with `--follow`.

## Tool Call Returned HTTP 200 but Failed

MCP tool errors are carried inside the JSON-RPC response. Check `result.isError`; operational failures and unknown tools set it to `true`. Rigout preserves a nonempty explicit error, stderr, or a deterministic `Command exited with status N` fallback.

HTTP 202 is normal for MCP notifications that do not require a response body.

## Pydantic Validation Warning

Malformed MCP parameters, such as an array where `tools/call.params.arguments` requires an object, are client errors and should be rejected. This is expected behavior, not a Rigout crash. Rigout keeps the JSON-RPC validation error while reducing the server-side Pydantic union dump to one concise, credential-free log message.

## Command Blocked

Rigout blocks common destructive command patterns by default. If the user explicitly requested an operation and accepts the risk, the agent can call `execute_command` with `bypass_security: true`.

## Local Fallback Is Used

This is expected when no SSH endpoint has been registered. Local fallback means tools execute on the machine running Rigout.

## Managed State Is in the Wrong Location

Rigout uses a per-user state directory:

- Windows: `%LOCALAPPDATA%\rigout\state`
- macOS: `~/Library/Application Support/rigout`
- Linux: `$XDG_STATE_HOME/rigout` or `~/.local/state/rigout`

Set `RIGOUT_STATE_DIR` or pass `--state-dir PATH` consistently to `start`, `status`, `logs`, and `stop`. In containers, mount that directory if lifecycle state and logs must survive container replacement.

## Package Build Fails

Use a clean build:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/rigout-*
```

If the wheel contains the wrong package name, check `[tool.hatch.build.targets.wheel]` in `pyproject.toml`.
