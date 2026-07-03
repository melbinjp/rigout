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

## Cloud Agent Cannot Connect

Confirm the generated connection file:

```bash
python -m json.tool ai_agent_connection.json
```

Confirm the health URL from the same network as the agent if possible:

```bash
curl https://your-public-host/health
```

If using Cloudflare quick tunnels, remember that the URL changes each time the tunnel restarts.

## Unauthorized

Public/tunnel mode writes a bearer token into `ai_agent_connection.json`. The MCP client must send:

```http
Authorization: Bearer <token>
```

If you pass `--auth-token`, use that exact token in the client headers.

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
