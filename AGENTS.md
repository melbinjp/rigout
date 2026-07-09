# Rigout Agent Guide

Rigout is a Streamable HTTP MCP server for controlled device access. Agents should connect to the MCP URL in `ai_agent_connection.json` or use the provided **Agent Setup URL**, and then use the listed MCP tools directly.

For repository changes, follow [DEVELOPMENT_STANDARDS.md](DEVELOPMENT_STANDARDS.md). Keep edits small, tested, and aligned with the `rigout` package and CLI.

## Connection

If you are given an **Agent Setup URL** (e.g., `.../connection.json?setup_token=...`), fetch that URL to retrieve the full MCP client configuration.

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

## Expected Use

1. Call `get_hardware_info` or `system_monitoring` before heavy work.
2. Use `execute_command` for shell tasks.
3. Use `file_operations` or `bulk_file_transfer` for file changes.
4. Use `environment_setup` for Python, Node, Docker, or Conda workspaces.
5. Use `docker_operations` for container workflows.
6. Clean up temporary files, containers, and long-running processes when finished.

## Safety

Rigout can provide broad control over the host device. Agents must treat access as high privilege:

- Do not run destructive commands without explicit user intent.
- Prefer scoped working directories.
- Use timeouts for long-running commands.
- Avoid printing secrets or connection tokens.
- Report meaningful command output and failures back to the user.
- Use `bypass_security` only when the requested task genuinely requires it.

## Local Fallback

If no SSH endpoint is registered, Rigout controls the local machine running the server. Cloud agents can still use that local fallback when the server is exposed through a reachable MCP URL and the correct bearer token.
