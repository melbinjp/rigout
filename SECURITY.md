# Security

Rigout runs the commands an agent sends, as the user who started it. That is what it is for, so the
protection is who can reach it:

- The HTTP endpoint requires the bearer token. Replace a leaked token with `rigout token --reset`.
- `rigout serve` listens on 127.0.0.1 unless you pass `--host`.
- `rigout share` makes it reachable through Cloudflare. The token still applies.

To report a vulnerability, use private vulnerability reporting on this repository's Security tab.
