# Security

Rigout exposes powerful device-control tools over MCP. Treat every public MCP URL, bearer token, setup URL, connection file, and activity log as sensitive.

## Recommended Deployment

- Run Rigout in a VM, container, or dedicated machine when giving an agent broad control.
- Use bearer auth for any non-localhost deployment.
- Prefer stable private networking for long-running usage: named Cloudflare Tunnel, Tailscale, VPN, or a reverse proxy with access controls.
- Rotate generated connection files and tokens after sharing them with an agent.
- Treat the printed agent setup URL like a bearer token; for 15 minutes after server startup by default, it can retrieve the full MCP client configuration.
- Use `rigout logs` locally or `get_server_activity` through MCP to review bounded, sanitized managed activity.
- Keep the per-user state directory private. Rigout enforces owner-only modes on POSIX, but operators must still protect backups, container mounts, and platform ACLs.

## Credential Handling

- Public/tunnel mode generates bearer authentication unless `--no-auth` is explicitly passed.
- The launcher passes generated bearer and setup tokens to child processes through environment variables, not command-line arguments.
- Protected connection responses use `Cache-Control: no-store` and `Pragma: no-cache`; HTTP 401 responses include `WWW-Authenticate: Bearer`.
- Rigout redacts `setup_token` from its controlled access-log view. A query-string credential can still be recorded by a browser, proxy, tunnel provider, or other intermediary, so share it only through a trusted channel.
- The setup token is time-limited but not single-use. Rotate the bearer token or restart the server if the setup URL was exposed.
- `--output json` lifecycle output is credential-free and points to the owner-scoped connection file instead of printing its bearer token.

## Activity Visibility

MCP access does not grant arbitrary visibility into the host's terminal emulator. `get_server_activity` exposes only lifecycle status and 1-200 recent sanitized activity lines. This bounded interface is preferable to raw terminal scraping because it limits unrelated output and reduces accidental credential disclosure.

Sanitization is defense in depth, not a guarantee that arbitrary command output contains no secrets. Do not deliberately print credentials, and restrict who can call Rigout tools.

## Not For

- Exposing a daily-use machine directly to the internet without isolation.
- Sharing bearer tokens in public issues, commits, logs, or chat transcripts.
- Running public/tunnel mode with `--no-auth` unless another trusted network layer provides equivalent protection.
- Treating an ephemeral `trycloudflare.com` quick-tunnel URL as a durable production endpoint.

## Reporting

Report security issues privately to the repository owner. Do not open public issues containing exploit details, credentials, tokens, or connection files.
