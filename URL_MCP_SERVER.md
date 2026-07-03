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

This starts the local server, creates a Cloudflare quick tunnel, writes `ai_agent_connection.json`, and keeps running until stopped.

Agents should use the generated `mcp.url`, `mcp.transport`, and `mcp.headers`.

## Existing Public URL

When a reverse proxy or stable tunnel already exists:

```bash
rigout --public-url https://rigout.example.com
```

Rigout appends `/mcp` unless the URL already ends with the configured MCP path.
