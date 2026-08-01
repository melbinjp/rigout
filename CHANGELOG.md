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
- `build_write_command` is exported from the package root. It builds the
  command that writes content to a path, and spells append as well as write.

### Changed
- `connection.json` and `/health` now report the running package version, and
  `connection.json` advertises the `server_activity` capability.
- GitHub Actions bumped: `checkout` v4 to v7, `setup-python` v5 to v6,
  `upload-artifact` v4 to v7, `download-artifact` v4 to v8 (#17).
- Recursive force deletes are now refused by target rather than by pattern.
  The old check matched any absolute path, so `rm -rf /tmp/build` was refused
  while a quoted or long-flag spelling such as `'rm' -Rf /usr` went through.
  A recursive forced delete is now refused when its target is the filesystem
  root or one of the top-level system directories, and permitted below them.
  Both halves are behaviour changes: `/usr` and its siblings are protected
  against spellings that previously evaded the check, and ordinary work such
  as `rm -rf /tmp/build` or `rm -rf /home/you/project` is no longer blocked.
  If you relied on the old check to refuse deletes anywhere under an absolute
  path, it no longer will.
- `file_operations` reads at most 1 MiB and states in its output when a file
  was truncated, rather than returning it whole. A remote read is bounded at
  the source with `head -c`, so the transfer is capped rather than only the
  reported output. The tool gained a `recursive` argument, and `delete` now
  refuses a directory unless it is set, where a local delete previously
  removed the tree outright and the remote path errored. That last one is
  breaking for any agent that relied on `delete` recursing on a local path.
- `dd` is judged by its operands rather than by its flags. Any `dd` naming both
  an input and an output was refused, so `dd if=/dev/urandom of=/tmp/testfile`
  was blocked. It is refused now when either operand names a raw device, which
  keeps both directions covered: writing over a disk, and copying a disk or
  kernel memory out to a readable file.
- A chained `rm` is judged the same way the unchained command would be.
  `cd /tmp && rm stale.log` and `make clean; rm -f core.dump` were refused for
  following an operator at all, while the identical commands on their own were
  permitted. A chain that reaches a protected directory, such as
  `ls; rm -rf /`, is still refused.

### Deprecated
- `heredoc_redirect` is an alias for `build_write_command` and no longer
  involves a heredoc. It stays exported because 0.2.0 exported it, so removing
  it would break anyone already importing it. Removal is planned for 0.4.0.

### Removed
- `security.audit_log` is gone from `connection.json`, and the
  `mcp-hardware-server.log` file it named is no longer written anywhere. A
  client reading that field breaks on upgrade. Read activity through
  `security.activity_access` instead, which names the `get_server_activity`
  tool and its 200-line ceiling.
- `SecurityValidator.validate_file_path` and `SecurityValidator.validate_ssh_key`
  are gone. `SecurityValidator` is exported from the package, so this is a
  public API removal and an external caller of either method breaks on
  upgrade. Nothing in Rigout called them. Both were worse than no check: the
  path check was routed around in one step, because `execute_command` hands
  over the same files with `cat`, while it refused any relative path
  containing `..` and any write under `/root`, which breaks container
  workflows where root is the login user. The key check rejected every key on
  Windows, where `st_mode` reports `0o666` whatever the ACLs say.

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
  protection is retained and extended to raw device redirection, commands
  nested inside `bash -c`, and destructive executables whose names were hidden
  by quoting: `mkfs`, `fdisk`, `parted`, `format`, `dd` with both `if=` and
  `of=`, Windows `del`/`rmdir` with `/s` or `/q`, and `curl` or `wget` piped
  into a shell. Recursive deletes are covered by the protected-root rule
  described under Changed.
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
- `--state-dir` and `RIGOUT_STATE_DIR` no longer rewrite the permissions of a
  directory Rigout did not create. The state directory was created tolerating
  an existing one and then set to owner-only unconditionally, so pointing
  Rigout at a shared directory such as `/tmp` narrowed it for every other user
  on the machine. Rigout now creates its own directory owner-only and leaves
  an existing one as it found it, warning on stderr when runtime state lands
  somewhere group- or world-reachable. New in 0.3.0: the state directory ships
  for the first time in this release.
- `get_server_activity` reports its state directory and activity log
  home-relative rather than absolute, so a remote agent no longer learns the
  operator's account name from the path. New in 0.3.0: the tool ships for the
  first time in this release.
- A remote `write` or `append` is validated in full and no longer has the
  file's contents inspected as though they were shell syntax. Writing a file
  that merely mentions a dangerous command is no longer refused, while writing
  to a raw device still is. Nothing is waived to achieve it: the content
  travels as quoted data, which the quoted-text rule above already treats as
  inert, so the write is never exempted from validation the way it once was.
- A remote `write` or `append` no longer adds a byte the caller did not ask
  for. Every remote write gained exactly one trailing newline, so content that
  had none grew by a byte and empty content produced a one-byte file, while
  the local branch of the same tool wrote the bytes exactly. The same call
  produced different files depending on which endpoint answered, and both
  reported success. Anyone who wrote a checksum, a PEM block, base64 or a JSON
  document to a remote host on 0.2.0 or earlier has a file that differs by one
  byte from what they asked for.
- The error text of a failed local `file_operations` or `bulk_file_transfer`
  is scrubbed for credentials, alongside the content those paths return. Error
  text is worth scrubbing in its own right, because a decoding failure quotes
  the bytes that caused it, so a file's contents can leave through the error
  channel as readily as through the result.
- macOS is no longer treated as Windows in local mode. The local endpoint
  records what `platform.system()` reports, which is `darwin`, and the tools
  asked whether the platform contained `win` - true of `darwin`. A Mac was
  therefore sent PowerShell commands, `if not exist` against `/bin/sh`, and
  Chocolatey rather than Homebrew. Platform detection now matches whole tokens,
  and the Windows predicate refuses any macOS token outright, so a future
  caller that checks only for Windows still cannot fire on a Mac.
- `rigout stop` can always exit a state it could not previously leave. A stale
  reservation combined with a reused PID left `status` reporting running,
  `stop` refusing to act on a PID it could not verify, and `start` refusing to
  launch, with no command able to clear it - the state directory had to be
  deleted by hand. `stop --force` clears state that cannot be verified as
  Rigout's. It signals no process it cannot confirm, so if a stray server is
  genuinely running, it survives and is still yours to stop.
- A command rejected inside `bash -c` reports `Nested shell command rejected:`
  followed by the reason, instead of wrapping the reason in the outer
  dangerous-pattern prefix a second time.
- An MCP error result describes non-text content blocks instead of dropping
  them. The SDK renders an error as a single string, so an image or embedded
  resource cannot survive as a block; it is now named in the message rather
  than disappearing without trace.
- `security_config.max_requests_per_minute` is read and enforced. The key was
  validated and reported but never applied, so every install ran at the
  built-in 60 per endpoint whatever the configuration said. Anyone who set it
  now gets the value they chose, which is a tightening for those who set it
  lower and a loosening for those who set it higher; the effective limit is
  logged when it differs from the default. Setting
  `security_config.enable_rate_limiting` to false is still not honoured, and
  now says so on startup rather than being ignored silently.

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
- Commands run through `execute_in_terminal` skipped protections that
  `execute_command` applied. A terminal session never validated the command,
  so destructive-command blocking did not apply even when the caller had not
  asked to skip it; never sanitized the output, so a session reading
  environment variables returned credentials verbatim where the one-shot path
  scrubbed them; and never drew on the endpoint's rate-limit budget. The
  scrubber is the one that reaches furthest, because that output flows into an
  agent's context and onward to whatever model serves it. This affects 0.2.0
  and earlier, where the session path takes neither a validation nor a bypass
  argument, so there was no way to ask for the protection it skipped. The
  session path now validates, sanitizes on both the local and SSH branches,
  and shares the endpoint's one-shot rate-limit budget. `execute_in_terminal`
  gained the same explicit `bypass_security` opt-in the one-shot tool already
  had, so a deliberately dangerous command remains possible but has to be
  asked for. That opt-out covers the command check only: output is scrubbed
  either way.
- The connection file was created with default permissions and narrowed to
  owner-only immediately afterwards, so on POSIX a file holding the bearer
  token existed briefly at its final path readable by whatever the umask
  allowed. It is now written through the same atomic helper the other runtime
  files use, which creates the file owner-only and moves it into place, so the
  token is never present at the destination under loose permissions. This
  affects 0.2.0 and earlier.
- A local `file_operations` read and a local `bulk_file_transfer` download
  returned file contents without scrubbing credentials from them, so reading a
  file of secrets handed them to the calling agent and onward to whatever model
  serves it. The same read against an SSH endpoint was scrubbed, because that
  path goes through `execute_command`; only the local branch was exposed. Local
  mode is on unless `RIGOUT_LOCAL_MODE` is disabled, and it answers whenever no
  SSH endpoint is configured or reachable, so anyone running Rigout against
  their own machine was on the unscrubbed path for every read. Using SSH
  endpoints exclusively was not affected. Both local paths now scrub. This
  affects 0.2.0 and earlier. If an agent read a credentials file, a private key
  or a `.env` through local mode, treat those secrets as disclosed to your
  model provider and rotate them.

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
