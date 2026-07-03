# URL MCP Server

Rigout's primary interface is Streamable HTTP MCP.

## Local

```bash
rigout
```

Endpoint:

```text
http://127.0.0.1:8765/mcp
```

Health check:

```bash
curl http://127.0.0.1:8765/health
```

## Public

```bash
rigout --tunnel cloudflare
```

This starts the local server, downloads `cloudflared` into a user-local Rigout cache if it is not already installed, creates a Cloudflare quick tunnel, writes `ai_agent_connection.json`, and keeps running until stopped.

Agents should use the generated `mcp.url`, `mcp.transport`, and `mcp.headers`. You can give the agent either the local `ai_agent_connection.json` content or the printed agent setup URL. If the agent can fetch a URL, paste the printed agent setup URL to the agent so it can configure itself.

When bearer auth is enabled, plain `/connection.json` also requires the same bearer token. The printed agent setup URL includes a setup token that can fetch the full MCP configuration without already knowing the bearer token. Treat that setup URL like a password.

## Existing Public URL

When a reverse proxy or stable tunnel already exists:

```bash
rigout --public-url https://rigout.example.com
```

Rigout appends `/mcp` unless the URL already ends with the configured MCP path.
