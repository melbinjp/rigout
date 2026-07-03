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

If `rigout` is not found after installation, Python installed the console script outside your shell PATH. Use the module launcher or add Python's Scripts directory to PATH:

```bash
python -m rigout.mcp_url_launcher --tunnel cloudflare
```

## Cloud Agent Cannot Connect

Confirm the generated connection file:

```bash
python -m json.tool ai_agent_connection.json
```

If bearer auth is enabled, plain `/connection.json` also requires the same `Authorization` header. Use either the local `ai_agent_connection.json` file or the printed agent setup URL as the credential source.

Confirm the health URL from the same network as the agent if possible:

```bash
curl https://your-public-host/health
```

If using Cloudflare quick tunnels, remember that the URL changes each time the tunnel restarts.

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

Public/tunnel mode writes a bearer token into `ai_agent_connection.json`. The MCP client must send:

```http
Authorization: Bearer <token>
```

If you pass `--auth-token`, use that exact token in the client headers.

If the agent only accepts a URL, use the printed agent setup URL rather than plain `/connection.json`. That setup URL is credential-bearing and should be shared only with the intended agent.

## Command Blocked

Rigout blocks common destructive command patterns by default. If the user explicitly requested an operation and accepts the risk, the agent can call `execute_command` with `bypass_security: true`.

## Local Fallback Is Used

This is expected when no SSH endpoint has been registered. Local fallback means tools execute on the machine running Rigout.

## Package Build Fails

Use a clean build:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/rigout-*
```

If the wheel contains the wrong package name, check `[tool.hatch.build.targets.wheel]` in `pyproject.toml`.
