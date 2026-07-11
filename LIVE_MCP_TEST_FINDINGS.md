# Live MCP Test Findings

Date: 2026-07-11
Remediation revalidation: 2026-07-12

This note records sanitized findings from testing a live Rigout 0.2.0 server
through a Cloudflare Quick Tunnel. It intentionally excludes the live URL,
setup token, bearer token, session IDs, and other credentials.

## Confirmed working

- The setup URL returns a usable Streamable HTTP MCP configuration.
- Bearer authentication protects the MCP endpoint; an unauthenticated
  initialization request returns HTTP 401.
- MCP initialization, tool discovery, hardware inventory, system monitoring,
  tunnel listing, and read-only command execution work through the public
  tunnel.
- The health endpoint reports the server as healthy.
- The live server exposes Rigout 0.2.0 with MCP SDK 1.28.1.

## Confirmed issues and current-checkout status

The observations below are retained as the historical live-test record. Status
lines describe the remediation now present in this checkout. The combined
behavior was revalidated through a fresh public quick tunnel from an isolated
installation of the rebuilt wheel on 2026-07-12.

### 1. Setup credentials can leak through query-string handling

**Status: Partially fixed in the current checkout.** Rigout now gives setup
tokens a 15-minute default TTL, redacts `setup_token` before its access logger
sees the response-time query string, and sends `Cache-Control: no-store` plus
`Pragma: no-cache` on protected connection responses and authorization
failures. Integration tests cover expiry, query redaction, and cache headers.
The token remains reusable until expiry, and intermediaries outside Rigout can
still retain a query-string credential; `X-Setup-Token` remains the safer
header form when a client can supply it.

The printed Agent Setup URL places `setup_token` in the query string. Uvicorn's
default access logger includes the full query string, so fetching that URL can
copy the credential into terminal output and any collected access logs. Query
strings may also be retained by intermediaries outside Rigout's control.

The live setup token remained reusable across multiple fetches. In addition,
the credential-bearing `/connection.json` response did not include
`Cache-Control: no-store`, `Pragma: no-cache`, or an expiry directive.

Original fix plan:

1. Redact `setup_token` from Rigout-controlled access logs immediately.
2. Add `Cache-Control: no-store` and related defensive response headers to
   `/connection.json` responses, including authorization failures.
3. Make setup credentials short-lived and preferably single-use.
4. Keep `X-Setup-Token` support and consider a bootstrap flow that exchanges a
   fragment or one-time code for the header without transmitting a reusable
   credential in the URL query.
5. Add integration tests that assert secrets are absent from captured logs and
   that credential responses cannot be cached.

### 2. Bearer authentication responses omit the challenge header

**Status: Fixed in the current checkout.** A shared unauthorized response now
adds `WWW-Authenticate: Bearer` without reflecting token material for both the
MCP endpoint and protected connection endpoint. HTTP integration tests cover
both surfaces.

Missing or incorrect credentials correctly return HTTP 401, but the response
does not contain `WWW-Authenticate: Bearer`. Some clients use that challenge to
recognize the authentication scheme and initiate credential discovery.

The planned fix was to add a standards-compatible bearer challenge without
echoing any token material, and cover both `/mcp` and `/connection.json`.

### 3. Failed tools are frequently reported as successful MCP results

**Status: Fixed in the current checkout.** Operational failures across command,
terminal, file, transfer, environment, Docker, monitoring, and tunnel handlers
now return `CallToolResult(isError=True)`. Unknown tools and unexpected handler
exceptions do the same. Unit and MCP dispatch tests assert the flag directly.

Live failures returned HTTP 200 with `result.isError: false`, including:

- A command exiting with status 1.
- A nonexistent working directory.
- Reading a nonexistent file or a directory as a file.
- Docker being unavailable.
- Executing in or closing a nonexistent terminal session.
- An unknown tool name.
- A command blocked by Rigout's security validator.

Input-schema validation errors correctly set `isError: true`, so the transport
supports the correct behavior. The planned change was to set `isError=True`
for operational failures and unknown tools, with tests asserting the flag
rather than only searching returned text.

### 4. Failed-command diagnostics can be blank or misleading

**Status: Fixed in the current checkout.** A shared formatter now chooses a
nonempty explicit error, then stderr, then `Command exited with status N`, and
command-backed handlers use it consistently. Focused tests cover blank-error,
stderr, and Docker-unavailable paths.

A command that exited with status 1 returned an empty `Error:` line. Docker
listing returned `Unknown error` even though the underlying shell result has an
exit code and stderr. Several handlers read only `result.error` and discard
`stderr` or the nonzero exit status.

The planned fix was a shared result formatter that selects a nonempty explicit
error, then stderr, then a deterministic exit-status fallback, used
consistently by command, Docker, environment, file-transfer, and terminal
handlers.

### 5. Invalid MCP requests create extremely noisy Pydantic logs

**Status: Fixed in the current checkout.** The JSON-RPC validation response is
unchanged, while a logging filter replaces the SDK's large union-validation
dump with one concise, credential-free summary. The HTTP integration test
asserts the summary and verifies that malformed input content is absent.

A malformed `tools/call` request with `params.arguments` set to an array instead
of an object is correctly rejected, but one input mistake expands into roughly
30 union-validation messages in the server terminal and audit log.

This is not a server crash. The malformed requests observed during testing were
created by the diagnostic client, not by Rigout. The planned fix was to
preserve the JSON-RPC error response while logging one concise validation
summary.

### 6. Command validation and auditing are not quote-aware

**Status: Fixed in the current checkout.** Validation now tokenizes shell
operators without splitting quoted literals, permits quoted destructive text
as data, and retains semantic blocking for executable `rm -rf /`, nested
`sh -c`/`bash -c`, command substitution, and raw-device redirection. Regression
tests cover the observed quoted `grep` and safe-string cases.

The command validator splits pipelines and chains with a regular expression
that does not respect shell quoting. A harmless expression such as a quoted
`grep -E` alternation is consequently audited as several nonexistent commands.

The same raw-text matching also blocks harmless quoted data such as printing
the literal text `rm -rf /`. The block is conservative and prevented execution,
but it is a false positive.

The planned fix was to use quote-aware shell tokenization for audit
classification and distinguish executable syntax from quoted literal data
while retaining the existing destructive-pattern protections.

### 7. Version reporting is inconsistent

**Status: Fixed in the current checkout.** `rigout.__version__`, the MCP server
object, and stdio initialization now use one package-metadata resolver with a
source-checkout fallback to `pyproject.toml`. Tests cover the installed, stdio,
and Streamable HTTP initialization paths.

- `pyproject.toml` declares Rigout 0.2.0.
- `rigout.__version__` reports 0.1.0.
- The stdio initialization path declares server version 1.0.0.
- The live HTTP initialization reports 1.28.1, matching the MCP SDK version
  rather than the Rigout package version.

The planned fix was to derive every advertised Rigout version from one package-
metadata source and add coverage for both HTTP and stdio initialization.

### 8. Runtime state is working-directory-relative

**Status: Fixed for the packaged launcher in the current checkout.** Foreground
and detached launcher modes use a platform-appropriate per-user state root and
run the HTTP child there. `--state-dir` and `RIGOUT_STATE_DIR` provide explicit
overrides. Managed connection, runtime, PID, and activity files are owner-only
on POSIX. Container operators must still mount the chosen state directory when
persistence across container replacement is required.

The containerized live server uses `/` as its working directory, producing
`/mcp-server-config.json` and `/mcp-hardware-server.log`. These files may be
lost with the container overlay and their location is surprising.

The planned fix was to select an explicit platform-appropriate state directory,
allow a CLI/environment override, and document persistence requirements for
containers.

### 9. The current source tree has one mypy failure

**Status: Fixed in the current checkout.** The ctypes value is converted to
`float` at the typed boundary, and `production_validation.py` now runs mypy as
a required quality gate.

`src/rigout/ssh_manager.py` returns a ctypes-derived value typed as `Any` from a
function declared to return `float`.

The planned fix was an explicit numeric conversion at the boundary plus
targeted test coverage.

### 10. Production validation misses live error-semantics defects

**Status: Fixed for the observed gaps in the current checkout.** Production
validation now runs the complete test suite plus Ruff lint, Ruff formatting,
and mypy, and reports production ready only when every category has no issues.
The test suite now includes HTTP auth/cache/TTL/redaction checks, MCP error-
flag assertions, failure diagnostics, lifecycle behavior, activity
sanitization, and version consistency. A rebuilt-wheel public-tunnel smoke test
also exercised setup bootstrap, MCP initialization, tool discovery, and server
activity end to end.

`production_validation.py` reported all eight categories passing and declared
the checkout production ready even though the live matrix confirmed incorrect
`isError` flags, blank failure diagnostics, version drift, missing credential
cache controls, and the current mypy failure.

The planned fix was not to weaken the readiness claim manually, but to extend
production validation with authenticated HTTP transport checks, failure-result
semantics, credential-response headers, version consistency, and the required
mypy gate.

## Expected warnings and behavior

- A missing `mcp-server-config.json` on first use is followed immediately by
  creation of a default file. This is expected initialization, although `INFO`
  would be a better log level than `WARNING`.
- Commands outside the allowlist are intentionally logged rather than blocked
  when they do not match a destructive pattern.
- HTTP 200 can contain a JSON-RPC validation error; HTTP 202 is normal for MCP
  notifications that do not have a response body.
- The bare MCP URL is an endpoint identifier, not an authentication mechanism.
  It is useful with a separately configured bearer header, an authenticating
  reverse proxy, or intentionally unauthenticated private/local deployment. It
  must not grant unauthenticated access through a public tunnel.

## Continuing test plan

1. Verify missing, malformed, and valid authentication behavior.
2. Exercise initialization, notifications, repeated requests, session deletion,
   and reuse-after-delete behavior.
3. Validate all advertised tool schemas and error envelopes.
4. Exercise read-only monitoring, hardware, file-read, Docker-list, and tunnel-
   list paths.
5. Create, use, list, and close one isolated MCP terminal session.
6. Review resulting terminal and audit logs for crashes, misleading warnings,
   leaked credentials, inconsistent status codes, and session lifecycle issues.
7. Re-run the local test, lint, format, and type-check baselines after the note
   is updated with evidence.

## Live test evidence

### Authentication and HTTP surface

- Public root: HTTP 200, no credential material in the response.
- Public health endpoint: HTTP 200.
- `/connection.json` without credentials: HTTP 401.
- `/connection.json` with a wrong setup token: HTTP 401.
- `/mcp` without credentials or with a wrong bearer token: HTTP 401.
- Authenticated `/connection.json`: HTTP 200, but without no-cache headers.
- All tested HTTP 401 responses omitted `WWW-Authenticate`.

### MCP transport and session lifecycle

- Initialization and five repeated pings in one session succeeded.
- `notifications/initialized` returned the expected HTTP 202.
- Tool discovery succeeded after repeated requests.
- A fabricated session ID returned HTTP 404 with `Session not found`.
- Session deletion returned HTTP 200.
- Reusing the deleted session returned HTTP 404.
- Required `Accept` negotiation was enforced with HTTP 406.
- Invalid request content type was rejected with HTTP 400.

### Tools

- Hardware, monitoring, connection selection, tunnel listing, and file reading
  succeeded.
- An isolated terminal session was created, listed, used, changed directory
  persistently, closed, and confirmed absent afterward.
- Docker listing failed because Docker was unavailable in the container; the
  failure exposed the error-reporting issues described above.
- Schema errors for missing required fields and invalid enum values correctly
  returned `isError: true`.

### Audit log

- Post-test log review found zero entries at `ERROR` level and no server crash.
- Ten verbose invalid-request warnings came from deliberately or accidentally
  malformed diagnostic client requests.
- The remaining warnings were first-use configuration, audit allowlist noise,
  one intentionally blocked safe-string test, and an intentionally invalid
  content-type test.

## Historical local verification after live testing

- `git diff --check`: passed.
- `python -m ruff check .`: passed.
- `python -m ruff format --check .`: passed, 32 files formatted correctly.
- `python -m pytest -q`: 131 passed, 4 skipped.
- `python production_validation.py`: 8 of 8 categories passed, while missing
  the live issues documented above.
- `python -m mypy src --ignore-missing-imports`: failed with the known
  `ssh_manager.py:111` `no-any-return` error.

## Current-checkout remediation evidence

- HTTP integration coverage now asserts setup-token expiry, no-store/no-cache
  headers, bearer challenges, setup-query redaction, and concise malformed-
  request logging.
- MCP/unit coverage now asserts `isError: true`, deterministic failure
  diagnostics, quote-aware validation, version consistency, lifecycle state,
  and bounded activity sanitization.
- The packaged CLI now exposes foreground compatibility plus detached
  `start`, `status`, `logs`, and `stop` commands with JSON output and a
  platform-specific state directory.
- `get_server_activity` returns credential-free lifecycle status and at most
  200 sanitized recent lines; it deliberately replaces unbounded raw terminal
  scraping for agent-visible diagnostics.
- Full Windows verification passed: Ruff lint and formatting, mypy, 173 tests
  (4 environment-dependent skips), all 11 production-validation categories,
  and fresh wheel/metadata builds.
- A rebuilt Rigout 0.2.0 wheel was installed into an isolated virtual
  environment. Packaged detached start/status/logs/stop passed; health and
  connection metadata both reported 0.2.0.
- A second installed-wheel smoke used a fresh Cloudflare quick tunnel. The
  setup response returned HTTP 200 with `no-store`, the MCP client initialized,
  discovered 15 tools, called `get_server_activity`, and observed the server as
  running. The raw setup token was absent from the captured activity log.
- The installed-wheel smoke exposed and then verified a Windows-only lifecycle
  fix: virtual-environment redirector processes can have a different PID from
  the managed Python child, so detached startup now correlates state with a
  per-instance identifier instead of assuming both PIDs are equal.
