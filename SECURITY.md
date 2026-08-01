# Security

Rigout exposes powerful device-control tools over MCP. Treat every public MCP URL, bearer token, setup URL, connection file, and activity log as sensitive.

## Advisories

Unauthenticated non-loopback bind. Affects 0.2.0 and earlier, fixed in 0.3.0.

Those versions decided whether to generate a bearer token from the tunnel and public-URL options alone and never consulted the bind address. A server started with `--host 0.0.0.0`, or any other non-loopback address, was therefore treated as local: no bearer token was generated, the MCP route was served with no authentication layer in front of it, and every tool, including `execute_command`, could be called by anything able to reach that address on the network. `--no-auth`, the documented way to opt out of authentication, was a no-op on that path, because there was no token generation for it to disable.

0.3.0 treats any bind address that is not loopback as public and generates a bearer token for it, the same as for a tunnel or an explicit public URL.

If you have run 0.2.0 or earlier with a non-loopback bind, upgrade with `pip install --upgrade rigout`. Assume that anything able to reach that address could have driven the server, and review that host and the network it ran on accordingly.

Terminal sessions skipped command validation and output sanitization. Affects 0.2.0 and earlier, fixed in 0.3.0.

In those versions `execute_command` validated the command against destructive patterns, scrubbed common credential patterns out of the output, and counted the call against the endpoint's rate limit. `execute_in_session`, the code behind `execute_in_terminal`, did none of the three. The same command was checked on one path and unchecked on the other, and the session path took no bypass argument, so a user could not ask for the protection it skipped or know they were not getting it.

Output sanitization is the half that reaches furthest. Command output is returned into an agent's context and travels onward to whatever model serves that agent. Anything credential-bearing that a session command printed, `env` being the plain example, was returned verbatim where the same command through `execute_command` was scrubbed.

0.3.0 applies all three to terminal sessions, in the same place and the same order as the one-shot path, and gives `execute_in_terminal` the same `use_sudo` and `bypass_security` arguments so the two paths behave identically. `bypass_security` waives validation only; it does not disable output sanitization or rate limiting on either path.

If you ran commands through a terminal session on 0.2.0 or earlier, upgrade with `pip install --upgrade rigout`. Treat any credential those commands could have printed as having reached your agent and its model provider unscrubbed, and rotate it accordingly. Also assume no destructive-command check was applied to anything you ran that way.

Connection file written before its permissions were restricted. Affects 0.2.0 and earlier, fixed in 0.3.0.

`connection.json` holds the bearer token. 0.2.0 wrote it with `write_text` and then narrowed it to owner-only with `chmod` on POSIX, so the file existed at its final path under whatever the process umask allowed for the time between the two calls. 0.1.0 wrote the same file with no permission handling at all, so it kept its umask permissions for as long as it existed.

0.3.0 writes the file through a temporary file created owner-only and moves it into place, so it is never present at its final path under looser permissions.

If another account could read the directory that held your connection file, treat the bearer token in it as exposed and rotate it: stop the server, delete the connection file, and start again to generate a new token.

## Recommended Deployment

- Run Rigout in a VM, container, or dedicated machine when giving an agent broad control.
- Keep bearer auth on for any deployment that reaches beyond this machine. Rigout generates a token automatically for a tunnel, an explicit public URL, or any non-loopback bind address; `--no-auth` disables that and warns.
- Prefer stable private networking for long-running usage: named Cloudflare Tunnel, Tailscale, VPN, or a reverse proxy with access controls.
- Rotate generated connection files and tokens after sharing them with an agent.
- Treat the printed agent setup URL like a bearer token; for 15 minutes after server startup by default, it can retrieve the full MCP client configuration.
- Use `rigout logs` locally or `get_server_activity` through MCP to review bounded, sanitized managed activity.
- Keep the per-user state directory private. On POSIX, Rigout creates its own state directory owner-only and always writes runtime files owner-only, but it will not rewrite the mode of a directory it did not create, such as a `--state-dir` pointed at `/tmp`; it warns on stderr instead. Operators must still protect backups, container mounts, and platform ACLs.

## Credential Handling

- A start that reaches beyond this machine generates bearer authentication unless `--no-auth` is explicitly passed. That means a tunnel, an explicit public URL, or any bind address that is not loopback.
- The launcher passes generated bearer and setup tokens to child processes through environment variables, not command-line arguments.
- Protected connection responses use `Cache-Control: no-store` and `Pragma: no-cache`; HTTP 401 responses include `WWW-Authenticate: Bearer`.
- Rigout redacts `setup_token` from its controlled access-log view. A query-string credential can still be recorded by a browser, proxy, tunnel provider, or other intermediary, so share it only through a trusted channel.
- The setup token is time-limited but not single-use. Rotate the bearer token or restart the server if the setup URL was exposed.
- `--output json` lifecycle output is credential-free and points to the owner-scoped connection file instead of printing its bearer token.

## Command Controls

- Both command paths, `execute_command` and `execute_in_terminal`, apply the same controls: the command is checked against destructive patterns, `sudo` requires `use_sudo`, and output is scrubbed for common credential patterns before it reaches the agent. This holds for local execution and over SSH.
- `bypass_security` is the one documented escape from validation, and it works the same on both paths. It disables the destructive-pattern check for that single call, is recorded as a security event, and disables neither output sanitization nor rate limiting. Pass it only for a command you intend to be destructive.
- Rate limiting is per endpoint and counts every command Rigout runs on it, one-shot or inside a terminal session, against one budget of 60 per minute. Opening more sessions does not buy more throughput.
- These are pattern-based defenses, not a sandbox. An agent holding a valid token can still run arbitrary commands, so isolate the host rather than relying on validation.

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
