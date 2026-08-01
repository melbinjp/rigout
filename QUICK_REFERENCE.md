# Quick Reference

## Install

```bash
pip install rigout
```

Requires Python 3.10 or newer.

## Run

```bash
rigout --tunnel cloudflare
rigout
rigout --port 9000
rigout --public-url https://rigout.example.com
rigout --auth-token "$RIGOUT_TOKEN"
rigout --tunnel cloudflare --cloudflared-path /path/to/cloudflared
rigout --tunnel cloudflare --no-cloudflared-download
rigout --tunnel cloudflare --setup-token "$RIGOUT_SETUP_TOKEN"
rigout --tunnel cloudflare --no-agent-setup-url
python -m rigout.mcp_url_launcher --tunnel cloudflare
```

`rigout --tunnel cloudflare` is the primary foreground shortcut. It prints the agent setup URL and runs until Ctrl+C. Cloudflare quick-tunnel URLs are ephemeral.

## Managed lifecycle

```bash
rigout start --tunnel cloudflare --detach
rigout status
rigout url
rigout logs --tail 100
rigout logs --follow
rigout stop
rigout stop --force
```

`stop --force` clears recorded state that cannot be verified as Rigout's. It signals no process.

`rigout url` prints the agent setup URL alone on one line and nothing else, so it can be redirected or piped instead of selected out of a running terminal: `rigout url | xclip -selection clipboard`, `rigout url > url.txt`, or `ssh HOST rigout url`. It reads the connection file, so it works in foreground and detached mode alike and never touches the running server. `--which mcp` and `--which health` print the other endpoints. The "treat this like a password" warning goes to stderr, keeping stdout pipeable.

Machine-readable commands:

```bash
rigout start --tunnel cloudflare --detach --output json
rigout status --output json
rigout logs --tail 50 --output json
rigout stop --output json
```

`--output json` startup requires `--detach`. `logs --follow` is text-only.

Exit codes: 0 succeeded, or `status` found Rigout running. 1 nothing to report or the command failed, including `status` when Rigout is not running. 2 usage error. `stop` exits 0 when nothing was running.

## Connect your agent

Read `connection.json` from the state directory; `rigout status` prints its path. Its `mcp` object holds `transport` (`streamable-http`), `url`, `health_url`, and `headers`. Register `url` with your MCP client as a streamable HTTP server and send every header in `headers`. Client-side field names come from your client's documentation, not from Rigout.

`headers` is empty on a plain local server. With a token it is `{"Authorization": "Bearer <token>"}`, and the connection file is the only place that token is written.

## Source checkout

```bash
python -m pip install -e .
rigout --tunnel cloudflare
```

## Stdio MCP

```bash
rigout-stdio
```

## Files

- Windows state: `%LOCALAPPDATA%\rigout\state`
- macOS state: `~/Library/Application Support/rigout`
- Linux state: `$XDG_STATE_HOME/rigout` or `~/.local/state/rigout`
- `connection.json`: generated MCP client configuration; contains the bearer token.
- `activity.log`: managed startup/runtime output.
- `runtime.json` and `rigout.pid`: credential-free lifecycle metadata.
- `rigout.lock`: contentless lock that serializes concurrent lifecycle commands; recreated on demand.
- `pyproject.toml`: package metadata and build configuration.
- `src/rigout/`: package source.
- `tests/`: pytest coverage.
- User-local Rigout cache: stores auto-downloaded `cloudflared` when needed.

Use `--state-dir PATH` or `RIGOUT_STATE_DIR` to override the state root. Managed files use owner-only modes on POSIX.

## Agent diagnostics

Call `get_server_activity` for bounded, sanitized JSON containing lifecycle status and the most recent 1-200 activity lines (default 50). MCP access does not automatically expose the host's raw terminal window.

Tool failures use MCP `isError: true`; HTTP 200 alone does not mean a tool call succeeded.

## Verification

```bash
python -m pytest -q
python production_validation.py
python -m build
python -m twine check dist/rigout-*
```

## PyPI release

Rigout publishes from the GitHub Actions tag workflow through PyPI Trusted Publishing. No PyPI API token is required.

Trusted publisher fields:

- PyPI project: `rigout`
- GitHub owner: `melbinjp`
- GitHub repository: `rigout`
- Workflow filename: `release.yml`
- GitHub environment: `pypi`

Create and push a version tag only after the publisher is configured:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

## Cleanup before publishing

```bash
git status --short
```

Do not publish old local artifacts from `dist/`. GitHub Actions builds from a clean checkout on tags.
