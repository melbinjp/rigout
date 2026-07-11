# Rigout Agent Guide

Rigout is a Streamable HTTP MCP server for controlled device access. Agents should use the provided **Agent Setup URL** or an owner-provided connection file, then use the listed MCP tools directly.

For repository changes, follow [DEVELOPMENT_STANDARDS.md](DEVELOPMENT_STANDARDS.md). Keep edits small, tested, and aligned with the `rigout` package and CLI.

## Connection

If you are given an **Agent Setup URL** (e.g., `.../connection.json?setup_token=...`), fetch it promptly to retrieve the full MCP client configuration. The setup token expires 15 minutes after the server starts by default. It is a bootstrap credential, not the MCP credential; use the returned bearer header for subsequent MCP requests.

Connection files have this shape:

```json
{
  "connection_method": "mcp_streamable_http",
  "mcp": {
    "transport": "streamable-http",
    "url": "https://example.trycloudflare.com/mcp",
    "health_url": "https://example.trycloudflare.com/health",
    "headers": {
      "Authorization": "Bearer ..."
    }
  }
}
```

Configure the MCP client with:

- transport: `streamable-http`
- url: `mcp.url`
- headers: `mcp.headers` when present

The plain `/mcp` URL does not bypass authentication. A public agent must also receive the bearer header, normally through the setup URL or the local connection file.

## Expected Use

1. Call `get_hardware_info` or `system_monitoring` before heavy work.
2. Call `get_server_activity` when you need managed server status or recent startup/runtime output.
3. Use `execute_command` for shell tasks.
4. Use `file_operations` or `bulk_file_transfer` for file changes.
5. Use `environment_setup` for Python, Node, Docker, or Conda workspaces.
6. Use `docker_operations` for container workflows.
7. Clean up temporary files, containers, and long-running processes when finished.

`get_server_activity` returns sanitized JSON with lifecycle status and 1-200 recent activity lines (50 by default). It is intentionally bounded and read-only. Do not assume that MCP access also grants access to the host's raw terminal window; use this tool instead of terminal scraping for Rigout diagnostics.

## Safety

Rigout can provide broad control over the host device. Agents must treat access as high privilege:

- Do not run destructive commands without explicit user intent.
- Prefer scoped working directories.
- Use timeouts for long-running commands.
- Avoid printing secrets or connection tokens.
- Report meaningful command output and failures back to the user.
- Treat an MCP tool result with `isError: true` as a failed operation even when the HTTP response itself is 200.
- Use `bypass_security` only when the requested task genuinely requires it.

## Starting Rigout Through Existing Access

For a human-attended session, the shortest public handoff remains:

```bash
rigout --tunnel cloudflare
```

It runs in the foreground and stops with Ctrl+C. If an agent already has another authorized way to operate the device and needs to install and manage Rigout itself, use the packaged lifecycle:

```bash
rigout start --tunnel cloudflare --detach --output json
rigout status --output json
rigout logs --output json
rigout stop --output json
```

The JSON output is credential-free and points to the owner-scoped connection and activity files. Read the connection file only through the already-authorized local channel; never print it into public logs.

## Local Fallback

If no SSH endpoint is registered, Rigout controls the local machine running the server. Cloud agents can still use that local fallback when the server is exposed through a reachable MCP URL and the correct bearer token.
