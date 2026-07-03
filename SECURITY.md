# Security

Rigout exposes powerful device-control tools over MCP. Treat every public MCP URL and bearer token as privileged access.

## Recommended Deployment

- Run Rigout in a VM, container, or dedicated machine when giving an agent broad control.
- Use bearer auth for any non-localhost deployment.
- Prefer stable private networking for long-running usage: named Cloudflare Tunnel, Tailscale, VPN, or a reverse proxy with access controls.
- Rotate generated connection files and tokens after sharing them with an agent.
- Treat the printed agent setup URL like a bearer token; it can retrieve the full MCP client configuration.
- Review `mcp-hardware-server.log` after agent sessions.

## Not For

- Exposing a daily-use machine directly to the internet without isolation.
- Sharing bearer tokens in public issues, commits, logs, or chat transcripts.
- Running public/tunnel mode with `--no-auth` unless another trusted network layer provides equivalent protection.

## Reporting

Report security issues privately to the repository owner. Do not open public issues containing exploit details, credentials, tokens, or connection files.
