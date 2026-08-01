# Security

Rigout exposes powerful device-control tools over MCP. Treat every public MCP URL, bearer token, setup URL, connection file, and activity log as sensitive.

## Advisories

Unauthenticated non-loopback bind. Affects 0.2.0 and earlier, fixed in 0.3.0.

Those versions decided whether to generate a bearer token from the tunnel and public-URL options alone and never consulted the bind address. A server started with `--host 0.0.0.0`, or any other non-loopback address, was therefore treated as local: no bearer token was generated, the MCP route was served with no authentication layer in front of it, and every tool, including `execute_command`, could be called by anything able to reach that address on the network. `--no-auth`, the documented way to opt out of authentication, was a no-op on that path, because there was no token generation for it to disable.

0.3.0 treats any bind address that is not loopback as public and generates a bearer token for it, the same as for a tunnel or an explicit public URL.

If you have run 0.2.0 or earlier with a non-loopback bind, upgrade with `pip install --upgrade rigout`. Assume that anything able to reach that address could have driven the server, and review that host and the network it ran on accordingly.

## Recommended Deployment

- Run Rigout in a VM, container, or dedicated machine when giving an agent broad control.
- Keep bearer auth on for any deployment that reaches beyond this machine. Rigout generates a token automatically for a tunnel, an explicit public URL, or any non-loopback bind address; `--no-auth` disables that and warns.
- Prefer stable private networking for long-running usage: named Cloudflare Tunnel, Tailscale, VPN, or a reverse proxy with access controls.
- Rotate generated connection files and tokens after sharing them with an agent.
- Treat the printed agent setup URL like a bearer token; for 15 minutes after server startup by default, it can retrieve the full MCP client configuration.
- Use `rigout logs` locally or `get_server_activity` through MCP to review bounded, sanitized managed activity.
- Keep the per-user state directory private. Rigout enforces owner-only modes on POSIX, but operators must still protect backups, container mounts, and platform ACLs.

## Credential Handling

- A start that reaches beyond this machine generates bearer authentication unless `--no-auth` is explicitly passed. That means a tunnel, an explicit public URL, or any bind address that is not loopback.
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
- Running with `--no-auth` on a tunnel, a public URL, or a non-loopback bind, unless another trusted network layer provides equivalent protection.
- Treating an ephemeral `trycloudflare.com` quick-tunnel URL as a durable production endpoint.

## Reporting

Report security issues privately to the repository owner. Do not open public issues containing exploit details, credentials, tokens, or connection files.
