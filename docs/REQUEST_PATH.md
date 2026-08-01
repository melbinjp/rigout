# Request path

How one MCP tool call travels through Rigout, end to end. Every step below names a real
symbol and the file that defines it, so a claim can be checked by searching for the name
rather than by trusting a line number. Traced against the code on `main` as of 2026-08-01,
with the resolved runtime dependencies `mcp` 1.15.0, `starlette` 0.47.1 and `uvicorn` 0.35.0.

`tests/unit/test_docs_truth.py` checks every reference below against the source at runtime,
so this document fails CI when it stops matching the code. What it cannot check is whether
an explanation is right. A summary can be wrong while every symbol in it resolves.

Read the narration first. The sections after it are the backing detail.

## The two-minute narration

Read the numbered lines out loud. The `Code:` line under each step is for checking the
claim afterwards, not for saying.

1. An MCP client sends a tool call over one of two transports. Over stdio the client
   launched `rigout-stdio` itself and writes JSON-RPC to its standard input. Over HTTP it
   posts JSON-RPC to `/mcp`.
   Code: `stdio_main` in `server.py`, `create_app` in `mcp_http_server.py`

2. Authentication happens first, at the ASGI layer, before any MCP code runs.
   `BearerAuthASGIApp` compares the whole `Authorization` header against the literal bytes
   `Bearer <token>`, in constant time. A mismatch returns 401 with `{"error":
   "unauthorized"}` and a `WWW-Authenticate: Bearer` challenge.
   Code: `BearerAuthASGIApp` in `mcp_http_server.py`, `tokens_match` in
   `mcp_http_server.py`, `unauthorized_response` in `mcp_http_server.py`

3. The caveat: that wrapper only exists if a token was configured, and Rigout only generates
   one when the start reaches beyond this machine, meaning a tunnel, a `--public-url`, or a
   non-loopback bind address. A server on loopback has no auth at all.
   Code: `is_public_start` in `mcp_url_launcher.py`, `create_app` in `mcp_http_server.py`,
   at `BearerAuthASGIApp(mcp_app, auth_token) if auth_token else mcp_app`

4. Both transports converge here on one handler. The SDK looks the tool name up in a cache
   built from `handle_list_tools`, then validates the arguments against that tool's JSON
   schema. Bad arguments never reach Rigout code.
   Code: `handle_list_tools` in `server.py`, `StreamableHTTPASGIApp` in `mcp_http_server.py`

5. Rigout's own dispatch is a fifteen-branch if/elif chain, no registry table. An unknown
   name falls through to `Unknown tool`, flagged as an error.
   Code: `_dispatch_tool` in `server.py`, at `text=f"Unknown tool: {name}"`

6. The handler picks an endpoint. `auto_failover` reuses the active SSH endpoint if it
   answers, otherwise takes the fastest that does, and falls back to a synthetic local
   endpoint meaning this machine.
   Code: `auto_failover` in `ssh_manager.py`, `get_local_endpoint` in `ssh_manager.py`

7. Security validation runs down in the tunnel manager, not in the tool handler, at all
   three entry points: remote command, local command, terminal session. `validate_command`
   tokenizes with shlex, masks quoted text, and rejects destructive commands like a real
   `rm -rf /`. A rejection is a result dict, never an exception. The caller can skip all of
   it with `bypass_security: true`.
   Code: `validate_command` in `security_validator.py`, `bypass_security` in `server.py`

8. Execution is local or remote, both on a worker thread so the event loop keeps serving.
   Local is `subprocess.run` with `shell=True`. Remote is a pooled Paramiko client running
   `exec_command`. Terminal sessions are a third case: a long-lived shell fed a sentinel
   echo to mark the end of output.
   Code: `_execute_local_command` in `ssh_manager.py`, at `subprocess.run,`;
   `_run_ssh_command` in `ssh_manager.py`; `execute` in `terminal_session.py`

9. On the way out, stdout and stderr go through `sanitize_command_output`, which masks
   things like `password=` and `token=`.
   Code: `sanitize_command_output` in `security_validator.py`

10. The result becomes a `CallToolResult` with one `TextContent` block. One wrinkle: an
    error result is turned back into a raised `RuntimeError`, which the SDK catches and
    rebuilds as an error result. The client sees `isError: true` either way.
    Code: `error_result` in `tools/_results.py`, `handle_call_tool` in `server.py`

That is the whole path: arrival, auth, dispatch, validation, execution, response.

## 1. Arrival

Rigout ships two console scripts (`pyproject.toml`, `[project.scripts]`):

- `rigout` runs `rigout.mcp_url_launcher:main`, which is `main` in `mcp_url_launcher.py`.
- `rigout-stdio` runs `rigout.server:stdio_main`, which is `stdio_main` in `server.py`.

Both expose the same `Server` object, built once at module level in `server.py`. The
transport is the only difference.

### Stdio

The MCP client starts `rigout-stdio` as a child process and talks JSON-RPC over its stdin
and stdout. `stdio_main` in `server.py` calls `main` in `server.py`, which opens
`stdio_server()` and hands the read and write streams to `server.run`.

Logging is pinned to stderr at `server.py:46` so log lines can never corrupt the protocol
stream on stdout. That is the one deliberate line number in this document. The claim is
about module-level code that no symbol encloses, so there is no definition to name and
nothing to anchor inside; a line number is the only reference that reaches it. It will rot
if lines are inserted above it, and the checker will say so loudly, which is the honest
failure mode. A symbol reference to `logging` would resolve and prove nothing.

There is no authentication on this transport. The client already owns the process.

### HTTP, and what `rigout start` actually starts

`rigout start` does not serve HTTP itself. It is a supervisor. The chain is:

1. `main` in `mcp_url_launcher.py` parses the command and resolves `RuntimePaths` in
   `lifecycle.py`, the per-user state directory holding `rigout.pid`, `rigout.lock`,
   `runtime.json`, `activity.log` and `connection.json`.
2. With `--detach`, `start_detached` in `mcp_url_launcher.py` relaunches Rigout as a
   background copy of itself via `launch_detached` in `lifecycle.py`, then waits for
   `runtime.json` to say `running`. Without `--detach` it goes straight to `run_foreground`
   in `mcp_url_launcher.py`.
3. `run_foreground` optionally starts a Cloudflare quick tunnel with
   `start_cloudflare_tunnel` in `mcp_url_launcher.py`, then calls `start_server` in
   `mcp_url_launcher.py`, which spawns a second process running
   `python -m rigout.mcp_http_server`. Tokens are passed in that child's environment, not on
   its command line, at `env["RIGOUT_AUTH_TOKEN"] = args.auth_token`, because argv is
   readable by other users on most systems.
4. It waits for `/health` to answer 200, then writes `connection.json` through
   `write_connection_file` in `mcp_http_server.py`, owner-only on POSIX.
5. The child process runs `main` in `mcp_http_server.py`, builds the Starlette app with
   `create_app` in `mcp_http_server.py` and serves it with uvicorn.

So in HTTP mode there are two or three Rigout processes: the launcher, the HTTP server, and
optionally cloudflared. `rigout status` and `rigout stop` work off the PID and process
identity recorded in `runtime.json`, read by `runtime_status` in `lifecycle.py` and acted on
by `handle_stop` in `mcp_url_launcher.py`. Liveness is not just a PID check:
`process_is_running` in `lifecycle.py` says whether something is there, and
`process_identity` in `lifecycle.py` fingerprints its creation time, so a recycled PID
belonging to some unrelated process is never signalled.

The routes are registered in `create_app` in `mcp_http_server.py`, at
`Route("/", endpoint=root, methods=["GET"])`:

| Path | Methods | Auth |
| --- | --- | --- |
| `/` | GET | none |
| `/health` | GET | none |
| `/connection.json` | GET | bearer or setup token, only when a bearer token exists |
| `/mcp` (or `--path`) | GET, POST, DELETE | bearer, only when a bearer token exists |

The MCP client talks to `/mcp` using the streamable HTTP transport. `GET` is the SSE stream,
`POST` carries requests, `DELETE` ends a session.

## 2. Authentication

This is the step worth knowing cold.

### Where the check happens

`BearerAuthASGIApp` in `mcp_http_server.py` is an ASGI wrapper placed around the MCP app,
not middleware over the whole site. The decision is made in `create_app` in
`mcp_http_server.py`, at `BearerAuthASGIApp(mcp_app, auth_token) if auth_token else mcp_app`.

Read that expression carefully. If `auth_token` is `None`, the MCP endpoint is served
completely unwrapped. There is no auth layer to bypass because none was built.

The check itself is in `BearerAuthASGIApp` in `mcp_http_server.py`, at
`if not tokens_match(headers.get(b"authorization"), self.expected)`. It reads the raw
`authorization` header out of the ASGI scope and compares the full header value against the
precomputed bytes `b"Bearer <token>"`. `tokens_match` in `mcp_http_server.py` uses
`hmac.compare_digest` for a constant-time compare. The comparison is exact: the scheme is
case sensitive and a single extra space fails.

Because this wrapper sits in front of the session manager, an unauthenticated request is
rejected before any JSON-RPC parsing, before any tool lookup and before any MCP session is
created.

### What an unauthorized request gets

`unauthorized_response` in `mcp_http_server.py`. Verified against a running app:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Cache-Control: no-store
Pragma: no-cache
Content-Type: application/json

{"error":"unauthorized"}
```

No detail about why it failed, and no timing signal from the compare.

### When a token exists at all

One predicate decides it, `is_public_start` in `mcp_url_launcher.py`, at
`args.tunnel != "none" or args.public_url or not is_loopback_host(`. Both the foreground
path in `run_foreground` and the detached path in `start_detached` call it, and generate a
token only when it is true.

Read `is_loopback_host` in `mcp_url_launcher.py` too, because the interesting decisions are
there. It covers all of 127.0.0.0/8 rather than string-matching 127.0.0.1, accepts the name
`localhost`, and returns False for a wildcard bind (`0.0.0.0`, `::`, empty) or anything it
cannot parse. False means "needs auth", so an unrecognized bind gets a token rather than
silence. That default is the whole point: the failure mode of guessing wrong is an
unauthenticated server on a routable address.

So a plain `rigout start` on loopback runs with no bearer token, and anything else on that
machine can call every tool. `--auth-token` or `RIGOUT_AUTH_TOKEN` sets one explicitly.
`--no-auth` suppresses generation, and on a non-loopback bind it prints `NO_AUTH_WARNING` in
`mcp_url_launcher.py` first. `main` in `mcp_http_server.py` also supports `--generate-token`
when the HTTP server is run directly.

### The setup token

The setup token is a separate, short-lived credential and it protects exactly one thing:
`/connection.json`. It never grants access to `/mcp`.

- It is generated in `run_foreground` in `mcp_url_launcher.py`, at
  `setup_token = args.setup_token or secrets.token_urlsafe(32)`, only when a bearer token
  exists, the server is public, and `--no-agent-setup-url` was not passed.
- The expiry is computed once when the app is built, in `create_app` in
  `mcp_http_server.py`, at `setup_token_expires_at = time.monotonic() + setup_token_ttl_seconds`.
  Default TTL is `DEFAULT_SETUP_TOKEN_TTL_SECONDS` in `mcp_http_server.py`, 15 minutes. It is
  a monotonic deadline, so a clock change does not extend it, and it is rechecked on every
  request at `and time.monotonic() < setup_token_expires_at`.
- It is accepted from the `X-Setup-Token` header or the `setup_token` query parameter, at
  `setup_token_header = request.headers.get("x-setup-token")`. The header is preferred.
- The point of it: `/connection.json` returns the bearer token inside
  `mcp.headers.Authorization`, built by `build_connection_data` in `mcp_http_server.py`, at
  `headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}`. So the setup
  URL is a one-shot, time-boxed way to hand an agent its real credential. That warning is
  printed by `print_start_result` in `mcp_url_launcher.py`, at
  `Treat it like a password: it can fetch the bearer token.`.
- `/connection.json` is only protected when a bearer token exists, at
  `if auth_token and not (bearer_authorized or setup_authorized)`. With no bearer token it is
  public.

### What is redacted from logs

Three separate mechanisms, all worth naming:

1. `RedactSetupTokenQueryMiddleware` in `mcp_http_server.py` rewrites `scope["query_string"]`
   at the moment the response starts, at `if message.get("type") == "http.response.start"`,
   replacing the `setup_token` value with `REDACTED`. Uvicorn builds its access log line
   after that, so the token never reaches the access log. This only hides it from Rigout's
   own log. The token is still in the URL, so a tunnel operator or proxy can see it.
2. `redact_sensitive_text` in `lifecycle.py` strips `setup_token=...` and `Bearer ...` from
   any text before it is written to `activity.log`. Every line of child process output goes
   through it in `stream_process_output` in `mcp_url_launcher.py`, at
   `safe_line = redact_sensitive_text(line)`, which covers both the HTTP server's output and
   cloudflared's.
3. `ConciseMCPValidationFilter` in `mcp_http_server.py` replaces the MCP SDK's verbose
   "Failed to validate request:" dumps, which can echo request bodies, with a fixed one-line
   summary. It is installed on the root logger and on `mcp.shared.session` for the lifetime
   of the app, at `validation_filter = ConciseMCPValidationFilter()`.

The activity log is opened owner-only on POSIX by `open_activity_log` in `lifecycle.py`. The
connection file, which can hold a bearer token, is written through `write_text_secure` in
`lifecycle.py`: it writes to a temporary file, applies owner-only mode to the descriptor
before any content is written, then renames into place. The mode is set before the content
is ever visible at the final path, rather than chmod'ed afterwards.

## 3. Dispatch

Dispatch happens twice: once in the SDK, once in Rigout.

### Where the two transports converge

This is the point where stdio and HTTP stop being different. Over HTTP the request first
passes through `StreamableHTTPASGIApp` in `mcp_http_server.py`, a thin adapter over the
SDK's `StreamableHTTPSessionManager`, which parses the JSON-RPC envelope, manages the
session and routes the request to the handler registered for `CallToolRequest`. Over stdio,
`main` in `server.py` reads the same JSON-RPC off the read stream and reaches the same
handler table. From here on there is one path, not two.

### SDK stage

`handle_list_tools` in `server.py` returns a hardcoded list of 15 `Tool` objects with JSON
schemas. The SDK caches them by name when the list is served, and refreshes the cache on a
miss.

On `tools/call`, the SDK handler at `mcp/server/lowlevel/server.py:488`:

1. Looks up the cached tool definition. If the name is unknown it logs
   `Tool 'X' not listed, no validation will be performed` and continues with no validation.
2. If the tool is known, validates `arguments` against `inputSchema` with jsonschema.
   A failure returns immediately, without calling Rigout. Verified:
   `execute_command` with `{}` returns
   `Input validation error: 'command' is a required property`, `isError: true`.
3. Otherwise calls Rigout's registered function.

Those two line numbers point into the installed `mcp` package rather than into Rigout, so
the checker verifies the file exists and nothing more. They move with the dependency.

### Rigout stage

`handle_call_tool` in `server.py` is the registered function. It delegates to
`_dispatch_tool` in `server.py`, which is a literal if/elif chain over the tool
name, one branch per handler, imported from `rigout.tools`. There is no dispatch table and no
dynamic lookup. The tool list in `handle_list_tools` and this chain are two independently
maintained lists of the same 15 names.

An unknown name falls to the `else` in `_dispatch_tool` in `server.py`, at
`text=f"Unknown tool: {name}"`, and returns `isError=True`. Verified end to end: the client
receives
`{"content": [{"type": "text", "text": "Unknown tool: no_such_tool"}], "isError": true}`.

Any exception escaping a handler is caught in the same function, at
`text=f"Error executing tool '{name}': {str(e)}"`.

The handlers themselves live in `src/rigout/tools/`:

| Tool names | Handler |
| --- | --- |
| `execute_command` and the terminal session tools | `handle_execute_command` in `tools/command.py` |
| `file_operations`, `bulk_file_transfer` | `handle_file_operations` in `tools/file_ops.py` |
| `docker_operations` | `handle_docker_operations` in `tools/docker.py` |
| `environment_setup` | `handle_environment_setup` in `tools/environment.py` |
| `system_monitoring`, `get_hardware_info` | `handle_system_monitoring` in `tools/monitoring.py` |
| `connect_hardware`, `manage_tunnels` | `handle_manage_tunnels` in `tools/tunnel.py` |
| `get_server_activity` | `handle_get_server_activity` in `tools/activity.py` |

### Choosing where the work runs

Almost every handler starts the same way, in `handle_execute_command` in `tools/command.py`,
at `endpoint = await get_tunnel_manager().auto_failover()`, returning
`No available hardware endpoints` when there is none.

`get_tunnel_manager` in `ssh_manager.py` lazily creates one module-level `TunnelManager` on
the first tool call, reading `mcp-server-config.json` at that moment. `auto_failover` in
`ssh_manager.py` reuses the active endpoint if it still passes `test_endpoint` in
`ssh_manager.py`, otherwise tests all configured endpoints concurrently through
`find_best_endpoint` in `ssh_manager.py` and takes the fastest, and otherwise falls back to
the local endpoint at
`if self.enable_local_endpoint:`. The local endpoint is a synthetic `TunnelEndpoint` built by
`get_local_endpoint` in `ssh_manager.py`, at `private_key_path="__local__"`. That sentinel
string is how every later branch decides local versus SSH, tested by `_is_local_endpoint` in
`ssh_manager.py`.

## 4. Validation

`SecurityValidator` in `security_validator.py` is applied in three places, all inside
`TunnelManager`, none of them in the tool handlers:

| Path | Validation site |
| --- | --- |
| Remote one-shot command | `execute_command` in `ssh_manager.py`, at `f"Blocked dangerous command on {endpoint.hostname}: {command}"` |
| Local one-shot command | `_execute_local_command` in `ssh_manager.py`, at `f"Blocked dangerous local command: {command}"` |
| Terminal session command | `execute_in_session` in `ssh_manager.py`, at `f"Blocked dangerous command in terminal session {session_id}: {command}"` |

So it guards anything that ends up as a shell command, including the commands that
`file_operations`, `docker_operations`, `install_software` and `environment_setup` build for
themselves.

In `execute_in_session` the check sits after the session lookup and before the branch on
session type, so it covers the SSH-backed and local shells with one call rather than two.

Before validation, all three paths apply a rate limit, checked by `_check_rate_limit` in
`ssh_manager.py`. The budget is per endpoint and shared between one-shot and terminal-session
commands, keyed by `_execute_rate_limit_key` in `ssh_manager.py`. Over the limit returns
`{"success": false, "error": "Rate limit exceeded"}`.

The limit defaults to `DEFAULT_MAX_REQUESTS_PER_MINUTE` in `ssh_manager.py`, 60 a minute, and
is configurable. The value comes from `SecurityConfig` in `config_manager.py` and is read by
`TunnelManager` in `ssh_manager.py`, at `limit = security.max_requests_per_minute`, which
rejects anything that is not a positive integer and logs when the configured value differs
from the built-in default.

`validate_command` in `security_validator.py` then does, in order:

1. Tokenize with `shlex` in POSIX mode with punctuation characters, so shell operators are
   real tokens, in `_tokenize_command` in `security_validator.py`. Unbalanced quotes return
   `Invalid shell syntax: ...`.
2. Mask quoted literals so text inside quotes is treated as data, not syntax, in
   `_mask_quoted_literals` in `security_validator.py`. Double quotes are masked for most
   checks but left visible for the backtick and `$(...)` patterns, because command
   substitution still executes inside double quotes.
3. Regex scan against `DANGEROUS_PATTERNS` in `security_validator.py`. That list is narrower
   than it once was and now covers filesystem and partition tools (`mkfs.`, at `mkfs\.`,
   plus `fdisk` and `parted`), Windows `format` and `del /s`, reads and writes to raw block
   devices and kernel memory, `curl ... | sh` and `wget ... | sh`, `eval $(`, backticks and
   `$(...)`, and netcat file redirection. A hit returns
   `Command contains dangerous pattern: <pattern>`.
4. Semantic checks in `_semantic_danger` in `security_validator.py`, which catch what quoting
   and regexes miss. These are the interesting ones, because they reason about the parsed
   command rather than its text: raw device redirection, a remote script piped to a shell,
   filesystem creation, `DESTRUCTIVE_EXECUTABLES` in `security_validator.py`, raw disk copy,
   recursive force delete, recursive deletion of a protected system directory, and a
   recursive check of the payload of `bash -c` or `sh -c`.
5. Per-segment checks in `validate_command` in `security_validator.py`, at
   `segments = self._command_segments(tokens)`. The command is split at `&&`, `||`, `;`, `|`
   and `&`, leading `VAR=value` assignments are stripped, and each segment's executable is
   inspected. `sudo` is rejected unless `allow_sudo` is set, which comes from the tool's
   `use_sudo` argument, read in `handle_execute_command` in `tools/command.py`.

### How `rm -rf /` is actually blocked

Worth knowing separately, because the obvious answer is now wrong. There is no `rm -rf /`
regex in `DANGEROUS_PATTERNS` any more. Deletion is judged semantically instead, against
`PROTECTED_ROOTS` in `security_validator.py`: `/` and the top-level system directories are
refused, and paths beneath them are allowed. The test is membership after normalisation, in
`_is_protected_root` in `security_validator.py`, at
`posixpath.normpath(target) in cls.PROTECTED_ROOTS`, so `/etc/../etc` is refused too.

The old regex matched any absolute path, so it refused `rm -rf /tmp/build` and every other
routine recursive delete, while `'rm' -Rf /usr` walked past it untouched. That trade is worse
than it looks in both directions. A control that refuses routine work teaches the operator to
reach for `bypass_security`, and the habit is already formed by the time the command is
genuinely dangerous.

Note this is a change in the permissive direction as well as the protective one: commands
that used to be refused now run.

One thing that surprises people: the allow list is not an allow list. A command not in
`ALLOWED_COMMANDS` in `security_validator.py` is logged as a warning and then permitted, at
`logger.warning(f"Command not in allowed list: {segment_command}")`. The comment above it
says why: blanket blocking breaks routine pipelines like `ps | head`. The real gate is the
deny list, not the allow list.

A rejection returns a plain dict, not an exception:

```
{"success": False,
 "error": "Security validation failed: <reason>. Pass bypass_security=true if this command is intentional.",
 ...}
```

And `bypass_security: true` in the tool arguments skips validation entirely. It is an
advertised argument on `execute_command` and on `execute_in_terminal`, declared as
`bypass_security` in `server.py` and read in `handle_execute_in_terminal` in
`tools/command.py`. It is no longer accepted by `file_operations`, which used to take it. The
bypass is logged and nothing else stops it: `SECURITY_BYPASS` in `execute_command`,
`LOCAL_SECURITY_BYPASS` in `_execute_local_command`, at
`f"AI agent bypassed security for local command: {command[:50]}..."`, and
`SESSION_SECURITY_BYPASS` in `execute_in_session`, at
`f"AI agent bypassed security for terminal session command: {command[:50]}..."`.

`execute_in_terminal` also takes a `use_sudo` argument, declared as `use_sudo` in
`server.py`, which prefixes `sudo` and sets `allow_sudo` for the validator, the same shape
`execute_command` already had.

## 5. Execution

Three execution shapes.

### Local, one-shot

`_execute_local_command` in `ssh_manager.py`. After validation it resolves the working
directory, merges the environment over `os.environ`, then runs `subprocess.run` on a worker
thread, at `subprocess.run,`.

`shell=True` means the host shell parses the command, which is exactly why the validator
works on shell syntax rather than on an argv list. The `asyncio.to_thread` offload is the
process boundary that matters for concurrency: the event loop stays free, so other MCP calls
progress while a command blocks. A timeout returns
`{"success": false, "error": "Command execution timeout"}`, at
`except subprocess.TimeoutExpired:`.

### Remote over SSH, one-shot

`execute_command` in `ssh_manager.py`. The working directory and environment are applied by
prefixing the remote command with `cd '<dir>' && ` and shell-quoted `KEY='value'`
assignments, built by `build_env_assignments` in `ssh_manager.py`. Connections come from a
per-host pool capped at 5, in `_get_ssh_connection` in `ssh_manager.py`; a pooled client is
only reused if its transport is still active. The blocking Paramiko sequence of
`exec_command`, two `read()` calls and `recv_exit_status` is wrapped in `_run_ssh_command` in
`ssh_manager.py` and pushed onto a thread. The client is handed back by
`_return_ssh_connection` in `ssh_manager.py` from a `finally`, which closes it instead of
pooling it if its transport has died.

### Host keys

Worth stating precisely, because the summary in either direction is wrong.

`_load_known_hosts` in `ssh_manager.py` runs in both modes, so a host that is present in
`known_hosts` and offers a different key is refused either way. That is the actual attack
signature and it is caught by default.

A host that is absent from `known_hosts` is a first contact. By default it is accepted with a
warning, by `WarningAutoAddPolicy` in `ssh_manager.py`, which says so once per host rather
than once per connection. Strict mode refuses it instead, using `paramiko.RejectPolicy()`,
and is opt-in through `RIGOUT_STRICT_HOST_KEYS` or config, resolved by
`resolve_strict_host_keys` in `config_manager.py`. Which `known_hosts` file is read is
resolved separately, by `resolve_known_hosts_path` in `config_manager.py`. Off is the deliberate 0.3.0 default,
because turning it on would lock out every deployment whose hosts are not already in
`known_hosts`; the comment on `strict_host_keys` in `config_manager.py` records that it flips
in 0.4.0.

So: key changes are caught, first contact is still trust-on-first-use.

### Persistent terminal sessions

`create_terminal_session` in `ssh_manager.py` makes either a `TerminalSession` backed by
`ssh.invoke_shell()` or, for the local endpoint, a `LocalTerminalSession` in
`terminal_session.py`, which is a long-lived `bash` or `cmd.exe` child process.

`execute` in `terminal_session.py` is the interesting part. It holds a lock, drains stale
output, writes the command followed by a sentinel, at
`marker = f"__RIGOUT_DONE_{uuid.uuid4().hex}__"`, and reads lines until the sentinel appears.
The number after the marker is the exit code. This is how a session that has no per-command
process boundary still reports per-command completion and status. A timeout returns with the
partial output and a note that the command may still be running, at
`f"Command timed out after {timeout}s (it may still be running)"`.

Sessions expire after an hour of inactivity and are reaped by `_cleanup_expired_sessions` in
`ssh_manager.py` every five minutes.

`execute_in_session` in `ssh_manager.py` applies the same gates as the one-shot paths, in
this order: session lookup, rate limit against the endpoint's shared budget, security
validation, then the branch on session type. Because validation happens before that branch,
one call covers both the SSH-backed and the local shell. Output is sanitized separately on
each branch afterwards, local at
`local_result["output"] = security_validator.sanitize_command_output(` and SSH at
`output = security_validator.sanitize_command_output(output)`.

Note the boundary this does not cross. The validator sees the command Rigout sends. It does
not see what the shell does with it afterwards, and a session is stateful, so a command that
was validated in isolation runs against whatever state earlier commands left behind.

## 6. Response

Handlers build `CallToolResult` objects directly from `mcp.types`.

Success is a single `TextContent` block of human-readable text, for example in
`handle_execute_command` in `tools/command.py`, at
`result_text = f"Command executed successfully on {result['endpoint']}\n\n"`.
`get_server_activity` is the exception: it returns JSON as text, built in
`handle_get_server_activity` in `tools/activity.py`, at
`text=json.dumps(payload, sort_keys=True)`.

Failure goes through two helpers in `tools/_results.py`:

- `error_result` in `tools/_results.py` returns
  `CallToolResult(content=[TextContent(...)], isError=True)`. This is the single place the
  error flag is set in Rigout's own code.
- `failure_detail` in `tools/_results.py` builds a non-empty diagnostic from a failed command
  dict, preferring `error`, then `stderr`, then `Command exited with status N`, then the
  fallback. It exists so an error is never an empty string.

Sanitization on the way out happens on every path that returns remote or local content:

- The SSH one-shot path, in `execute_command` in `ssh_manager.py`, at
  `stdout_data = security_validator.sanitize_command_output(stdout_data)`.
- The local one-shot path, in `_execute_local_command` in `ssh_manager.py`, at
  `stdout = security_validator.sanitize_command_output(stdout)`.
- Both terminal-session branches, in `execute_in_session` in `ssh_manager.py`, as above.
- File contents, which do not go through a command at all. `scrub` in `tools/file_ops.py`
  wraps `sanitize_command_output` for the local branches, which have no `execute_command` to
  inherit it from, and `_read_bounded` in `tools/file_ops.py` caps a read at 1 MiB with a
  stated truncation notice.

`sanitize_command_output` in `security_validator.py` regex-replaces `password=`, `token=`,
`key=`, `secret=`, `api_key=` and `auth_token=` values with `***`, case insensitive.

`get_server_activity` adds two more. It runs `redact_sensitive_text` in `lifecycle.py` over
every log line before sanitizing, and puts the two paths it reports through `sanitized_path`
in `tools/activity.py`, which rewrites the home directory to `~` via `redact_home_path` in
`lifecycle.py`. That last one is not about credentials: an absolute state-directory path
carries the operating system account name, which a remote agent has no reason to learn.

Then the shape changes once more. `handle_call_tool` in `server.py` is what the SDK actually
calls, and it raises rather than returning, at `raise RuntimeError(message or f"Tool '{name}' failed")`.

That is not clumsiness and it is not removable. The SDK's `call_tool` handler hardcodes
`isError=False` on its success path and reaches `isError=True` only by catching an exception.
Returning a `CallToolResult` instead would be treated as an iterable and shredded into its
field names. Raising is the only error channel that exists. `_error_message` in `server.py`
records that reasoning next to the code, which is the canonical statement of it.

`_error_message` in `server.py` also decides what happens to content that is not text. Blocks
are not preserved, because the SDK rebuilds a single `TextContent` from `str(e)`; they are
described instead, at `f"[{kind} content: {uri}]" if uri else f"[{kind} content]"`, so an
image or embedded resource on an error path is visible in the message rather than
disappearing without a trace.

`handle_call_tool_result` in `server.py` is a second public entry point that skips the raise
and hands back the full object, which is what the tests use.

The result then travels back out the transport it came in on: JSON-RPC on stdout for stdio,
or an SSE event or JSON body on the streamable HTTP connection.

## Rough edges

Things in this path that are genuinely hard to follow or worth questioning. Not softened.

Items fixed since the first version of this list are marked FIXED rather than deleted,
because the fix is the interesting part for anyone being asked about this code. Everything
unmarked is still true.

1. **No auth on a loopback bind.** `is_public_start` in `mcp_url_launcher.py` decides whether
   a token is needed: a tunnel, a `--public-url`, or a non-loopback `--host`. A default
   `rigout start` on 127.0.0.1:8765 still serves every tool, including `execute_command`,
   with no credential, so any other process or user on the machine can drive it. That is a
   deliberate local-convenience choice, but nothing in the code says so.
   Worth knowing the history: before 0.3.0 the same check consulted only `--tunnel` and
   `--public-url`, never the bind address, so `--host 0.0.0.0` with the default
   `--tunnel none` served every tool unauthenticated to the whole network.
   `is_loopback_host` in `mcp_url_launcher.py` now covers all of 127.0.0.0/8 and treats a
   wildcard or unparseable bind as non-loopback, which is the safe default.
2. **The auth layer is conditional, not a middleware.** `create_app` in `mcp_http_server.py`
   swaps in an unprotected app when there is no token. Reading it top to bottom, it is easy
   to see `BearerAuthASGIApp` and assume the endpoint is always protected.
3. **Dispatch is a 15-branch if/elif chain.** `_dispatch_tool` in `server.py`. The
   tool list in `handle_list_tools` and the chain are separate hand-maintained copies of the
   same names, so adding a tool to one and forgetting the other produces `Unknown tool` at
   runtime with no test or type error.
4. **Errors change shape three times, and that is the SDK's contract, not a defect.** A
   handler returns `isError=True` from `error_result` in `tools/_results.py`,
   `handle_call_tool` in `server.py` raises `RuntimeError`, and the SDK converts the
   exception back into an error result. This is worth being able to EXPLAIN rather than
   something to fix: raising is the only channel the SDK offers, as `_error_message` in
   `server.py` sets out. The real cost is tracing: an error message passes through three
   representations, so finding its origin means knowing all three hops.
   Previously listed here as dropping non-text content blocks silently. That half is fixed;
   blocks are now described rather than discarded. They still cannot survive as blocks.
5. **`file_operations` has no path validation, and no longer has an implementation to wire
   in.** `validate_file_path` and `validate_ssh_key` have been deleted from
   `SecurityValidator` in `security_validator.py`. That is a deliberate removal, not an
   oversight: the protection was unreachable in principle, because `execute_command` hands
   over the same files through `cat` and `echo >`, so a path check on `file_operations` alone
   was the appearance of a boundary rather than a boundary. It was also wrong in practice,
   rejecting `/root/...` writes and every relative path containing `..`, which breaks
   container workflows where root is the login user.
   The residual is real and should be stated plainly: `handle_file_operations` in
   `tools/file_ops.py` quotes its paths with `shell_quote` in `ssh_manager.py` and performs
   no traversal or sensitive-path check at all.
   Hostnames are NOT in the same position, and the earlier version of this finding implied
   they were. `validate_hostname` in `security_validator.py` is not called from the request
   path either, but hostname validation IS enforced, by `_is_valid_hostname` in
   `ssh_manager.py`, which runs from `TunnelEndpoint.__post_init__` on every endpoint
   construction and raises on a bad hostname.
6. **The allow list does not allow-list.** `ALLOWED_COMMANDS` in `security_validator.py` is
   consulted, and a command that is not in it is logged and permitted. The name reads like a
   gate and is not one.
7. **Validation is opt-out per request.** `bypass_security: true` skips `validate_command`
   entirely on all three paths. It is advertised in the public tool schema as a normal
   argument on `execute_command` and `execute_in_terminal`. Closing the terminal-session gap
   in item 8 also extended the escape hatch to that path. That is a defensible trade, since a
   hatch that exists on one route and not another is worse than one that is uniform, but it
   is a trade and not a free win. It has since been removed from `file_operations`, which
   narrows the surface.
8. **FIXED. Terminal sessions used to skip validation and the rate limit.**
   `execute_in_session` in `ssh_manager.py` now checks the rate limit and validates the
   command before running it, and sanitizes output on both branches. Validation sits before
   the branch on session type, so one call covers the SSH-backed and local shells. The rate
   limit shares the endpoint's one-shot budget through `_execute_rate_limit_key` in
   `ssh_manager.py` rather than opening a second budget.
   What remains is not a gap but a boundary worth being able to state: the validator sees the
   command Rigout sends, not what the shell does with it, and a session is stateful, so a
   command validated in isolation still runs against state earlier commands left behind.
9. **`__local__` as a magic string.** The local endpoint is identified by
   `private_key_path == "__local__"`, tested by `_is_local_endpoint` in `ssh_manager.py`, and
   tool modules re-implement that comparison inline instead of asking the manager. See
   `handle_environment_setup` in `tools/environment.py`, at
   `getattr(endpoint, "private_key_path", "") == "__local__"`.
10. **The config file path is relative and resolved late.** `TunnelManager` in
    `ssh_manager.py` defaults to `"mcp-server-config.json"`, at
    `def __init__(self, config_file: str = "mcp-server-config.json")`, and the manager is a
    lazily created module global built by `get_tunnel_manager` in `ssh_manager.py`, so which
    config gets read depends on the working directory of whichever process first handles a
    tool call. In managed mode that directory is set to the state dir by `run_foreground` in
    `mcp_url_launcher.py`, which is not obvious from either file alone.
11. **`session_name` is really `session_id`.** `handle_create_terminal_session` in
    `tools/command.py` passes the schema's `session_name` into the `session_id` parameter of
    `create_terminal_session` in `ssh_manager.py`. Reusing a name returns `None`, which
    surfaces to the client as the generic `Failed to create terminal session`.
12. **FIXED, with a residual. SSH host keys used to be accepted unconditionally.**
    `AutoAddPolicy` is gone from the code. `_load_known_hosts` in `ssh_manager.py` now runs in
    both modes, so a known host offering a changed key is refused either way, which is the
    real attack signature.
    The residual: a host absent from `known_hosts` is still accepted on first contact, with a
    warning, by `WarningAutoAddPolicy` in `ssh_manager.py`. Strict mode refuses it and is
    opt-in, resolved by `resolve_strict_host_keys` in `config_manager.py`, which documents
    that off is the deliberate 0.3.0 default and flips in 0.4.0. So "host keys are verified"
    would be an overclaim; "key changes are caught, first contact is trust-on-first-use" is
    the accurate statement.
13. **Query redaction is log-only.** `RedactSetupTokenQueryMiddleware` in
    `mcp_http_server.py` rewrites the scope so uvicorn's access line is clean. The setup token
    is still transmitted in the URL, so it is visible to the tunnel provider and any
    intermediary. The header form (`X-Setup-Token`) avoids that, but the setup URL built by
    `connection_setup_url` in `mcp_http_server.py` uses the query form.
14. **FIXED. The configurable rate limit is now the one that runs.**
    `max_requests_per_minute` in `config_manager.py` used to be range-checked and reported
    back while nothing on the request path read it; `TunnelManager` hardcoded 60 and
    `ssh_manager.py` did not import `config_manager` at all. It now reads the configured
    value, falls back to `DEFAULT_MAX_REQUESTS_PER_MINUTE` in `ssh_manager.py` when the value
    is unusable, and logs when the two differ.
