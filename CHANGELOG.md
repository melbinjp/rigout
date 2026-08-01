# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] - 2026-08-01

### Added
- Packaged process lifecycle commands: `rigout start --detach`, `status`,
  `logs`, and `stop`, with credential-free `--output json` results and a
  platform-appropriate per-user state directory configurable through
  `--state-dir` or `RIGOUT_STATE_DIR`. `logs` accepts `--tail` and `--follow`;
  `stop` accepts `--timeout`.
- `get_server_activity`, a read-only MCP tool returning lifecycle status and
  1-200 recent sanitized activity lines instead of exposing an unbounded raw
  terminal transcript.
- `--setup-token-ttl-seconds` on the HTTP server, setting how long a setup
  token may fetch `/connection.json` (default 900).
- `.github/jules-review-rules.md`: maintainer-authored review guidance loaded
  from the base branch, telling the reviewer not to flag unfamiliar
  dependency/Action versions as nonexistent from training knowledge alone
  (#18), and to check a version live with `git ls-remote` before blocking on
  it (#20).
- Grouped Dependabot updates: `github-actions` bumps arrive as a single PR;
  `pip` groups minor/patch only, leaving major bumps isolated for review (#17).
- Unit tests for `scripts/jules_review.py` covering its fail-closed
  guarantees: anchored verdict parsing, trusted-author gating, skip
  conditions, 404-tolerant polling, and the no-`automationMode` session
  payload (#19).
- Production validation gained three categories: the required lint, format,
  and type-check gates; live runtime contracts (server version, `isError` on
  unknown tools, connection-endpoint auth headers); and a wheel/sdist build.

### Changed
- `connection.json` and `/health` now report the running package version, and
  `connection.json` advertises the `server_activity` capability.
- GitHub Actions bumped: `checkout` v4 to v7, `setup-python` v5 to v6,
  `upload-artifact` v4 to v7, `download-artifact` v4 to v8 (#17).

### Removed
- `security.audit_log` is gone from `connection.json`, and the
  `mcp-hardware-server.log` file it named is no longer written anywhere. A
  client reading that field breaks on upgrade. Read activity through
  `security.activity_access` instead, which names the `get_server_activity`
  tool and its 200-line ceiling.

### Fixed
- Setup-token handling: tokens expire after 15 minutes by default, are
  redacted from Rigout-controlled access logs, and protected connection
  responses include no-store/no-cache headers. Unauthorized responses now
  include a bearer challenge.
- MCP operational failures and unknown tools now preserve `isError: true`;
  command-backed failures report an explicit error, stderr, or exit-status
  fallback instead of blank or misleading diagnostics.
- `install_software` handles `pacman`, which its tool schema has advertised
  as a valid `package_manager` since 0.1.0. Naming it explicitly returned
  "Unsupported package manager: pacman"; automatic selection never chose it,
  so only callers that requested it by name were affected.
- Malformed MCP requests retain their JSON-RPC validation response while the
  server logs one concise summary instead of a large Pydantic union dump.
- Shell validation now treats quoted text as data rather than syntax, so a
  quoted control operator no longer reads as one. Destructive-command
  protection is retained and extended to raw device redirection, an
  executable `rm -rf /`, and commands nested inside `bash -c`.
- Package, HTTP, and stdio server version reporting now use the same package
  metadata source; the stdio server previously advertised a hardcoded
  `1.0.0`.
- SSH command execution no longer blocks the event loop. Paramiko's
  synchronous exec/read/exit-status sequence runs on a worker thread, so
  independent MCP work proceeds while a command is in flight.
- Importing `rigout.server` no longer creates `mcp-hardware-server.log` in the
  current working directory.
- `rigout stop` reaps the server and tunnel children. `runtime.json` recorded
  only the launcher's PID, so a launcher that died without running its
  shutdown handler left them running while `status` reported stopped and a
  tunnel could still be serving. Their PIDs and process identities are now
  recorded alongside the launcher's, and `stop` terminates any that are still
  running and still match what was recorded, so a reused PID is never
  signalled. On POSIX the SIGKILL escalation now reaches the launcher's
  process group when the launcher leads one.
- Managed runtime files no longer depend on the launcher's current working
  directory, and the launcher no longer injects its source tree into child
  `PYTHONPATH`. Detached startup also handles Windows virtual-environment
  redirector processes whose PID differs from the managed Python child.
- System monitoring gathers independent metrics concurrently with a small
  bound while preserving partial failure diagnostics.
- Production validation only reports production ready when every category
  passes.
- The Jules review workflow executes the trusted reviewer script from the
  protected base revision with persisted checkout credentials disabled.
- Jules review skips cleanly on Dependabot PRs instead of failing with 401s -
  GitHub withholds repository secrets from Dependabot-triggered runs (#16).
- Auto-merge workflow: arming auto-merge needs `contents: write`, and now
  retries on every push instead of only on PR open/reopen (#16).
- Jules review verdict parsing anchored to a whole line, so verdict text
  quoted inside a finding can never be read as the real verdict (#18).
- Diff truncation for review prompts now cuts at a line boundary (#19).
- The `mcp` dependency is capped below 2.0. The requirement was `mcp>=1.0.0`
  with no upper bound, so once the MCP Python SDK published 2.0.0 a fresh
  `pip install rigout` resolved to it. Rigout uses the 1.x server API, and on
  2.0.0 `Server` has no `list_tools`, so importing `rigout.server` raised
  `AttributeError` and every entry point failed to start. Installs of 0.2.0
  and earlier are affected from the point 2.0.0 became the latest release;
  pin `mcp<2` or upgrade. Rigout is verified against 1.29.0, the newest 1.x.

### Security
- `rigout start` bound to a non-loopback host served every tool without
  authentication. The check deciding whether a bearer token was needed
  consulted `--tunnel` and `--public-url` but never the bind address, so
  `--host 0.0.0.0` with the default `--tunnel none` generated no token and the
  MCP application was mounted unprotected: anyone able to reach the port could
  call `execute_command`, `file_operations` and `install_software` with no
  credential. Passing `--auth-token` explicitly did protect the server, but
  `--no-auth`, the documented opt-out, was a no-op on that path. This affects
  0.2.0 and earlier, which ship the same predicate. Upgrade if you have ever
  started Rigout on a non-loopback host.
- A start now counts as public whenever the bind address is not loopback, at
  both the foreground and detached entry points, and `--no-auth` is honoured
  there and warns that the bind is reachable beyond this machine. Loopback is
  classified from the address rather than by string comparison, so all of
  127.0.0.0/8 and IPv6 loopback stay token-free.

## [0.2.0] - 2026-07-09

### Added
- Automated Jules PR review (`scripts/jules_review.py`): posts a code review
  comment on every PR and auto-approves it when no blocking issues are found
  and the PR author is trusted (default: the repo owner), since branch
  protection's "require approval of the most recent push" rule otherwise
  cannot be satisfied by the PR author themselves.
- `.github/workflows/auto-merge.yml`: arms GitHub's native auto-merge on PRs
  the repo owner opens against `main`, so they complete on their own once
  Jules' approval and all required checks land - deliberately independent
  of Jules internally, it only flips the same flag a human's approval would
  unblock. PRs from anyone else never get this flag set.
- Unit tests for the Cloudflare quick-tunnel bootstrap (`start_cloudflare_tunnel`,
  `resolve_public_mcp_url`), covering URL extraction and both failure paths.
- `.github/dependabot.yml` for automated dependency and GitHub Actions updates.

### Fixed
- `LocalTerminalSession` hung indefinitely on Windows: `cmd.exe /q` suppresses
  input echo but still prints the shell prompt, so the completion sentinel
  arrived prefixed (`C:\path>__RIGOUT_DONE_xxx__ 0`) instead of at the start
  of the line, and was discarded as a stale echo instead of being recognized.
- CI: removed a bogus `pip install curl` step in the Agent Connection Audit
  workflow (`curl` is a system binary, not a PyPI package).
- CI: pinned macOS runners to `macos-15` after GitHub's `macos-latest` ->
  `macos-26` migration caused runner-acquisition failures; updated branch
  protection's required status checks to match the renamed jobs.

## [0.1.0] - 2026-07-01

Initial PyPI release.
