# Request path

How one MCP tool call travels through Rigout, end to end. Every step below names a real
function and a `file.py:line`. Traced against the code on `main` as of 2026-08-01, with the
resolved runtime dependencies `mcp` 1.15.0, `starlette` 0.47.1 and `uvicorn` 0.35.0.

Read the narration first. The sections after it are the backing detail.

## The two-minute narration

Read the numbered lines out loud. The `Code:` line under each step is for checking the
claim afterwards, not for saying.

1. An MCP client sends a tool call over one of two transports. Over stdio the client
   launched `rigout-stdio` itself and writes JSON-RPC to its standard input. Over HTTP it
   posts JSON-RPC to `/mcp`.
   Code: `server.py:408`, `mcp_http_server.py:353`

2. Authentication happens first, at the ASGI layer, before any MCP code runs.
   `BearerAuthASGIApp` compares the whole `Authorization` header against the literal bytes
   `Bearer <token>`, in constant time. A mismatch returns 401 with `{"error":
   "unauthorized"}` and a `WWW-Authenticate: Bearer` challenge.
   Code: `mcp_http_server.py:124`, `mcp_http_server.py:47`, `mcp_http_server.py:56`

3. The caveat: that wrapper only exists if a token was configured, and Rigout only generates
   one when the start reaches beyond this machine, meaning a tunnel, a `--public-url`, or a
   non-loopback bind address. A server on loopback has no auth at all.
   Code: `mcp_http_server.py:286`, `mcp_url_launcher.py:90`, `mcp_url_launcher.py:705`

4. Both transports converge here on one handler. The SDK looks the tool name up in a cache
   built from `handle_list_tools`, then validates the arguments against that tool's JSON
   schema. Bad arguments never reach Rigout code.
   Code: `server.py:50`, `mcp_http_server.py:133`

5. Rigout's own dispatch is a fifteen-branch if/elif chain, no registry table. An unknown
   name falls through to `Unknown tool`, flagged as an error.
   Code: `server.py:331`, `server.py:364`

6. The handler picks an endpoint. `auto_failover` reuses the active SSH endpoint if it
   answers, otherwise takes the fastest that does, and falls back to a synthetic local
   endpoint meaning this machine.
   Code: `ssh_manager.py:1013`, `ssh_manager.py:1034`

7. Security validation runs down in the tunnel manager, not in the tool handler, at all
   three entry points: remote command, local command, terminal session. `validate_command`
   tokenizes with shlex, masks quoted text, and rejects destructive commands like a real
   `rm -rf /`. A rejection is a result dict, never an exception. The caller can skip all of
   it with `bypass_security: true`.
   Code: `ssh_manager.py:681`, `:1072`, `:918`, `security_validator.py:287`, `server.py:91`

8. Execution is local or remote, both on a worker thread so the event loop keeps serving.
   Local is `subprocess.run` with `shell=True`. Remote is a pooled Paramiko client running
   `exec_command`. Terminal sessions are a third case: a long-lived shell fed a sentinel
   echo to mark the end of output.
   Code: `ssh_manager.py:1113`, `ssh_manager.py:720`, `terminal_session.py:110`

9. On the way out, stdout and stderr go through `sanitize_command_output`, which masks
   things like `password=` and `token=`.
   Code: `security_validator.py:445`

10. The result becomes a `CallToolResult` with one `TextContent` block. One wrinkle: an
    error result is turned back into a raised `RuntimeError`, which the SDK catches and
    rebuilds as an error result. The client sees `isError: true` either way.
    Code: `tools/_results.py:7`, `server.py:377`

That is the whole path: arrival, auth, dispatch, validation, execution, response.

## 1. Arrival

Rigout ships two console scripts (`pyproject.toml`, `[project.scripts]`):

- `rigout` runs `rigout.mcp_url_launcher:main` (`mcp_url_launcher.py:968`).
- `rigout-stdio` runs `rigout.server:stdio_main` (`server.py:408`).

Both expose the same `Server` object, `server = Server("enhanced-hardware-server")` at
`server.py:47`. The transport is the only difference.

### Stdio

The MCP client starts `rigout-stdio` as a child process and talks JSON-RPC over its stdin
and stdout. `stdio_main` (`server.py:408`) calls `main` (`server.py:391`), which opens
`stdio_server()` and hands the read and write streams to `server.run`. Logging is pinned to
stderr at `server.py:40` so log lines can never corrupt the protocol stream on stdout.

There is no authentication on this transport. The client already owns the process.

### HTTP, and what `rigout start` actually starts

`rigout start` does not serve HTTP itself. It is a supervisor. The chain is:

1. `main` (`mcp_url_launcher.py:968`) parses the command and resolves `RuntimePaths`
   (`lifecycle.py:140`), the per-user state directory holding `rigout.pid`, `runtime.json`,
   `activity.log` and `connection.json`.
2. With `--detach`, `start_detached` (`mcp_url_launcher.py:618`) relaunches Rigout as a
   background copy of itself via `launch_detached` (`lifecycle.py:359`) and then waits for
   `runtime.json` to say `running`. Without `--detach` it goes straight to `run_foreground`
   (`mcp_url_launcher.py:694`).
3. `run_foreground` optionally starts a Cloudflare quick tunnel
   (`mcp_url_launcher.py:307`), then calls `start_server` (`mcp_url_launcher.py:244`), which
   spawns a second process: `python -m rigout.mcp_http_server`. Tokens are passed in that
   child's environment, not on its command line (`mcp_url_launcher.py:277`), because argv is
   readable by other users on most systems.
4. It waits for `/health` to answer 200 (`mcp_url_launcher.py:768`), then writes
   `connection.json` (`mcp_http_server.py:241`), owner-only on POSIX.
5. The child process runs `mcp_http_server.main` (`mcp_http_server.py:392`), builds the
   Starlette app with `create_app` (`mcp_http_server.py:258`) and serves it with uvicorn
   (`mcp_http_server.py:420`).

So in HTTP mode there are two or three Rigout processes: the launcher, the HTTP server, and
optionally cloudflared. `rigout status` and `rigout stop` work off the PID and process
identity recorded in `runtime.json` (`lifecycle.py:314`, `mcp_url_launcher.py:906`).

The routes are registered at `mcp_http_server.py:350`:

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

`BearerAuthASGIApp` (`mcp_http_server.py:117`). It is an ASGI wrapper placed around the MCP
app, not middleware over the whole site:

```
mcp_app = StreamableHTTPASGIApp(session_manager)                        # :285
protected_mcp_app = BearerAuthASGIApp(mcp_app, auth_token) if auth_token else mcp_app  # :286
```

Read that second line carefully. If `auth_token` is `None`, the MCP endpoint is served
completely unwrapped. There is no auth layer to bypass because none was built.

The check itself is at `mcp_http_server.py:124`. It reads the raw `authorization` header out
of the ASGI scope and compares the full header value against the precomputed bytes
`b"Bearer <token>"` with `tokens_match` (`mcp_http_server.py:47`), which uses
`hmac.compare_digest` for a constant-time compare. The comparison is exact: the scheme is
case sensitive and a single extra space fails.

Because this wrapper sits in front of the session manager, an unauthenticated request is
rejected before any JSON-RPC parsing, before any tool lookup and before any MCP session is
created.

### What an unauthorized request gets

`unauthorized_response` (`mcp_http_server.py:56`). Verified against a running app:

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

One predicate decides it, `is_public_start` (`mcp_url_launcher.py:90`):

```
return bool(args.tunnel != "none" or args.public_url or not is_loopback_host(str(args.host or "")))
```

`run_foreground` (`mcp_url_launcher.py:704`) and the detached path
(`mcp_url_launcher.py:984`) both call it, and generate a token only when it is true.

Read `is_loopback_host` (`mcp_url_launcher.py:74`) too, because the interesting decisions are
there. It covers all of 127.0.0.0/8 rather than string-matching 127.0.0.1, accepts the name
`localhost`, and returns False for a wildcard bind (`0.0.0.0`, `::`, empty) or anything it
cannot parse. False means "needs auth", so an unrecognized bind gets a token rather than
silence. That default is the whole point: the failure mode of guessing wrong is an
unauthenticated server on a routable address.

So a plain `rigout start` on loopback runs with no bearer token, and anything else on that
machine can call every tool. `--auth-token` or `RIGOUT_AUTH_TOKEN` sets one explicitly.
`--no-auth` suppresses generation, and on a non-loopback bind it prints a warning first
(`mcp_url_launcher.py:68`, emitted at `mcp_url_launcher.py:707`). `mcp_http_server.py:399`
also supports `--generate-token` when the HTTP server is run directly.

### The setup token

The setup token is a separate, short-lived credential and it protects exactly one thing:
`/connection.json`. It never grants access to `/mcp`.

- It is generated in `run_foreground` (`mcp_url_launcher.py:710`) only when a bearer token
  exists, the server is public, and `--no-agent-setup-url` was not passed.
- The expiry is computed once when the app is built: `setup_token_expires_at =
  time.monotonic() + setup_token_ttl_seconds` (`mcp_http_server.py:275`). Default TTL is 15
  minutes (`mcp_http_server.py:41`). It is a monotonic deadline, so a clock change does not
  extend it, and it is checked on every request at `mcp_http_server.py:310`.
- It is accepted from the `X-Setup-Token` header or the `setup_token` query parameter
  (`mcp_http_server.py:305`). The header is preferred; the comment at `mcp_http_server.py:304` says so.
- The point of it: `/connection.json` returns the bearer token inside
  `mcp.headers.Authorization` (`mcp_http_server.py:186`). So the setup URL is a one-shot,
  time-boxed way to hand an agent its real credential. The launcher prints exactly that
  warning at `mcp_url_launcher.py:614` for a detached start and `mcp_url_launcher.py:808` in
  the foreground.
- `/connection.json` is only protected when a bearer token exists
  (`mcp_http_server.py:314`). With no bearer token it is public.

### What is redacted from logs

Three separate mechanisms, all worth naming:

1. `RedactSetupTokenQueryMiddleware` (`mcp_http_server.py:78`) rewrites
   `scope["query_string"]` at the moment the response starts (`mcp_http_server.py:90`), replacing the
   `setup_token` value with `REDACTED`. Uvicorn builds its access log line after that, so
   the token never reaches the access log. This only hides it from Rigout's own log. The
   token is still in the URL, so a tunnel operator or proxy can see it.
2. `redact_sensitive_text` (`lifecycle.py:27`) strips `setup_token=...` and `Bearer ...` from
   any text before it is written to `activity.log`. Every line of child process output goes
   through it in `stream_process_output` (`mcp_url_launcher.py:299`), which covers both the
   HTTP server's output and cloudflared's.
3. `ConciseMCPValidationFilter` (`mcp_http_server.py:97`) replaces the MCP SDK's verbose
   "Failed to validate request:" dumps, which can echo request bodies, with a fixed one-line
   summary. It is installed on the root logger and on `mcp.shared.session` for the lifetime
   of the app (`mcp_http_server.py:331`).

The activity log is opened owner-only on POSIX (`lifecycle.py:344`). The connection file,
which can hold a bearer token, is written through `write_text_secure`
(`mcp_http_server.py:252`, implemented at `lifecycle.py:156`): it writes to a temporary file,
applies owner-only mode to the descriptor before any content is written, then renames into
place. The mode is set before the content is ever visible at the final path, rather than
chmod'ed afterwards.

## 3. Dispatch

Dispatch happens twice: once in the SDK, once in Rigout.

### Where the two transports converge

This is the point where stdio and HTTP stop being different. Over HTTP the request first
passes through `StreamableHTTPASGIApp` (`mcp_http_server.py:133`), a thin adapter over the
SDK's `StreamableHTTPSessionManager`, which parses the JSON-RPC envelope, manages the
session and routes the request to the handler registered for `CallToolRequest`. Over stdio
`server.run` (`server.py:394`) reads the same JSON-RPC off the read stream and reaches the
same handler table. From here on there is one path, not two.

### SDK stage

`handle_list_tools` (`server.py:50`) returns a hardcoded list of 15 `Tool` objects with JSON
schemas. The SDK caches them by name (`_tool_cache`) when the list is served, and refreshes
the cache on a miss.

On `tools/call`, the SDK handler at `mcp/server/lowlevel/server.py:488`:

1. Looks up the cached tool definition. If the name is unknown it logs
   `Tool 'X' not listed, no validation will be performed` and continues with no validation.
2. If the tool is known, validates `arguments` against `inputSchema` with jsonschema.
   A failure returns immediately, without calling Rigout. Verified:
   `execute_command` with `{}` returns
   `Input validation error: 'command' is a required property`, `isError: true`.
3. Otherwise calls Rigout's registered function.

### Rigout stage

`handle_call_tool` (`server.py:377`) is the registered function. It delegates to
`_handle_call_tool_result` (`server.py:331`), which is a literal if/elif chain over the tool
name, one branch per handler, imported from `rigout.tools` (`server.py:17`). There is no
dispatch table and no dynamic lookup. The tool list at `server.py:50` and this chain are two
independently maintained lists of the same 15 names.

An unknown name falls to the `else` at `server.py:364` and returns
`Unknown tool: <name>` with `isError=True`. Verified end to end: the client receives
`{"content": [{"type": "text", "text": "Unknown tool: no_such_tool"}], "isError": true}`.

Any exception escaping a handler is caught at `server.py:369` and turned into
`Error executing tool '<name>': <message>`.

The handlers themselves live in `src/rigout/tools/`:

| Tool names | Module |
| --- | --- |
| `execute_command`, terminal session tools, `install_software` | `tools/command.py` |
| `file_operations`, `bulk_file_transfer` | `tools/file_ops.py` |
| `docker_operations` | `tools/docker.py` |
| `environment_setup` | `tools/environment.py` |
| `system_monitoring`, `get_hardware_info` | `tools/monitoring.py` |
| `connect_hardware`, `manage_tunnels` | `tools/tunnel.py` |
| `get_server_activity` | `tools/activity.py` |

### Choosing where the work runs

Almost every handler starts the same way, for example `tools/command.py:18`:

```
endpoint = await get_tunnel_manager().auto_failover()
if not endpoint:
    return error_result("No available hardware endpoints")
```

`get_tunnel_manager` (`ssh_manager.py:1160`) lazily creates one module-level `TunnelManager`
on the first tool call, reading `mcp-server-config.json` at that moment
(`ssh_manager.py:245`). `auto_failover` (`ssh_manager.py:1013`) reuses the active endpoint if
it still tests healthy, otherwise tests all configured endpoints concurrently and takes the
fastest (`ssh_manager.py:570`), and otherwise falls back to the local endpoint
(`ssh_manager.py:1026`). The local endpoint is a synthetic `TunnelEndpoint` with
`private_key_path == "__local__"` (`ssh_manager.py:1034`). That sentinel string is how every
later branch decides local versus SSH (`ssh_manager.py:1049`).

## 4. Validation

`SecurityValidator` (`security_validator.py:18`) is applied in three places, all inside
`TunnelManager`, none of them in the tool handlers:

| Path | Validation site |
| --- | --- |
| Remote one-shot command | `execute_command`, `ssh_manager.py:681` |
| Local one-shot command | `_execute_local_command`, `ssh_manager.py:1072` |
| Terminal session command | `execute_in_session`, `ssh_manager.py:918` |

So it guards anything that ends up as a shell command, including the commands that
`file_operations`, `docker_operations`, `install_software` and `environment_setup` build for
themselves.

In `execute_in_session` the check sits after the session lookup and before the branch on
session type (`ssh_manager.py:918`), so it covers the SSH-backed and local shells with one
call rather than two.

Before validation, both one-shot paths apply a rate limit of 60 requests per minute
(`ssh_manager.py:671` per endpoint, `ssh_manager.py:1063` for local). Over the limit returns
`{"success": false, "error": "Rate limit exceeded"}`. `execute_in_session` does not call
`_check_rate_limit` at all, so terminal-session commands are unlimited. See rough edge 8.

`validate_command` (`security_validator.py:287`) then does, in order:

1. Tokenize with `shlex` in POSIX mode with punctuation characters, so shell operators are
   real tokens (`security_validator.py:181`). Unbalanced quotes return
   `Invalid shell syntax: ...`.
2. Mask quoted literals so text inside quotes is treated as data, not syntax
   (`security_validator.py:149`). Double quotes are masked for most checks but left visible
   for the backtick and `$(...)` patterns, because command substitution still executes
   inside double quotes (`security_validator.py:311`).
3. Regex scan against `DANGEROUS_PATTERNS` (`security_validator.py:22`). That list covers
   `rm -rf /`, `dd if=... of=...`, `mkfs.`, `fdisk`, `parted`, Windows `format` and
   `del /s`, reads and writes to raw block devices and kernel memory, `curl ... | sh`,
   `wget ... | sh`, `eval $(`, backticks, `$(...)`, `; rm`, `&& rm`, `| rm`, and netcat file
   redirection. A hit returns
   `Command contains dangerous pattern: <pattern>`.
4. Semantic checks that quoting could otherwise hide (`security_validator.py:210`): raw
   device redirection, an actual `rm` with both `-r` and `-f` targeting `/`, and a recursive
   check of the payload of `bash -c` / `sh -c`.
5. Per-segment checks (`security_validator.py:330`). The command is split at `&&`, `||`,
   `;`, `|` and `&`, leading `VAR=value` assignments are stripped, and each segment's
   executable is inspected. `sudo` is rejected unless `allow_sudo` is set, which comes from
   the tool's `use_sudo` argument (`tools/command.py:29`).

One thing that surprises people: the allow list is not an allow list. A command not in
`ALLOWED_COMMANDS` is logged as a warning and then permitted (`security_validator.py:342`).
The comment at `security_validator.py:329` says why: blanket blocking breaks routine
pipelines like `ps | head`. The real gate is the deny list, not the allow list.

A rejection returns a plain dict, not an exception:

```
{"success": False,
 "error": "Security validation failed: <reason>. Pass bypass_security=true if this command is intentional.",
 ...}
```

And `bypass_security: true` in the tool arguments skips validation entirely. It is an
advertised argument on both `execute_command` (`server.py:91`, read at
`tools/command.py:16`) and `execute_in_terminal` (`server.py:124`, read at
`tools/command.py:76`). The bypass is logged and nothing else stops it:
`SECURITY_BYPASS` at `ssh_manager.py:698`, `LOCAL_SECURITY_BYPASS` at
`ssh_manager.py:1091`, `SESSION_SECURITY_BYPASS` at `ssh_manager.py:937`.

`execute_in_terminal` also gained a `use_sudo` argument (`server.py:119`,
`tools/command.py:75`), which prefixes `sudo` and sets `allow_sudo` for the validator, the
same shape `execute_command` already had.

## 5. Execution

Three execution shapes.

### Local, one-shot

`_execute_local_command` (`ssh_manager.py:1052`). After validation it resolves the working
directory (`ssh_manager.py:1096`), merges the environment over `os.environ` (`ssh_manager.py:1107`), then runs:

```
completed = await asyncio.to_thread(
    subprocess.run, command, shell=True, capture_output=True, text=True,
    timeout=timeout, cwd=cwd, env=env,
)
```

`shell=True` means the host shell parses the command, which is exactly why the validator
works on shell syntax rather than on an argv list. The `asyncio.to_thread` offload is the
process boundary that matters for concurrency: the event loop stays free, so other MCP calls
progress while a command blocks. A timeout returns
`{"success": false, "error": "Command execution timeout"}` (`ssh_manager.py:1140`).

### Remote over SSH, one-shot

`execute_command` (`ssh_manager.py:654`). The working directory and environment are applied
by prefixing the remote command with `cd '<dir>' && ` and shell-quoted `KEY='value'`
assignments (`ssh_manager.py:704`, `ssh_manager.py:42`). Connections come from a per-host
pool capped at 5 (`_get_ssh_connection`, `ssh_manager.py:784`); a pooled client is only
reused if its transport is still active. The blocking Paramiko sequence of
`exec_command`, two `read()` calls and `recv_exit_status` is wrapped in `_run_ssh_command`
(`ssh_manager.py:58`) and pushed onto a thread at `ssh_manager.py:720`. The client is
returned to the pool in a `finally` (`ssh_manager.py:780`).

Note `set_missing_host_key_policy(AutoAddPolicy())` at `ssh_manager.py:804`: unknown host
keys are accepted rather than verified.

### Persistent terminal sessions

`create_terminal_session` (`ssh_manager.py:842`) makes either a `TerminalSession` backed by
`ssh.invoke_shell()` or, for the local endpoint, a `LocalTerminalSession`
(`terminal_session.py:54`) which is a long-lived `bash` or `cmd.exe` child process.

`LocalTerminalSession.execute` (`terminal_session.py:110`) is the interesting part. It holds
a lock, drains stale output, writes the command followed by a sentinel
`echo __RIGOUT_DONE_<uuid>__ $?` (`terminal_session.py:126`), and reads lines until the
sentinel appears. The number after the marker is the exit code. This is how a session that
has no per-command process boundary still reports per-command completion and status. A
timeout returns with the partial output and a note that the command may still be running
(`terminal_session.py:143`).

Sessions expire after an hour of inactivity and are reaped by a background task every five
minutes (`ssh_manager.py:313`).

`execute_in_session` (`ssh_manager.py:893`) applies the same gates as the one-shot paths,
in this order: session lookup, rate limit against the endpoint's shared budget, security
validation, then the branch on session type. Because validation happens before that branch,
one call covers both the SSH-backed and the local shell. Output is sanitized separately on
each branch afterwards, local at `ssh_manager.py:946` and SSH at `ssh_manager.py:972`.

Note the boundary this does not cross. The validator sees the command Rigout sends. It does
not see what the shell does with it afterwards, and a session is stateful, so a command that
was validated in isolation runs against whatever state earlier commands left behind.

## 6. Response

Handlers build `CallToolResult` objects directly from `mcp.types`.

Success is a single `TextContent` block of human-readable text, for example
`tools/command.py:36`. `get_server_activity` is the exception: it returns JSON as text
(`tools/activity.py:52`).

Failure goes through two helpers in `tools/_results.py`:

- `error_result(message)` (`tools/_results.py:7`) returns
  `CallToolResult(content=[TextContent(...)], isError=True)`. This is the single place the
  error flag is set in Rigout's own code.
- `failure_detail(result, fallback)` (`tools/_results.py:12`) builds a non-empty diagnostic
  from a failed command dict, preferring `error`, then `stderr`, then
  `Command exited with status N`, then the fallback. It exists so an error is never an empty
  string.

Sanitization on the way out happens in the manager, not the handler:
`sanitize_command_output` (`security_validator.py:445`) is applied on all four paths: the
SSH one-shot path (`ssh_manager.py:736`), the local one-shot path (`ssh_manager.py:1125`),
and both terminal-session branches (`ssh_manager.py:946` local, `ssh_manager.py:972` SSH). It
regex-replaces `password=`, `token=`, `key=`, `secret=`, `api_key=` and `auth_token=` values
with `***`, case insensitive. `get_server_activity` additionally runs
`redact_sensitive_text` over every log line before sanitizing (`tools/activity.py:38`), and
puts the two paths it reports through `sanitized_path` (`tools/activity.py:16`), which
rewrites the home directory to `~` via `redact_home_path` (`lifecycle.py:54`). That last one
is not about credentials: an absolute state-directory path carries the operating system
account name, which a remote agent has no reason to learn.

Then the shape changes once more. `handle_call_tool` (`server.py:377`) is what the SDK
actually calls, and it does this:

```
result = await _handle_call_tool_result(name, arguments)
if result.isError:
    message = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
    raise RuntimeError(message or f"Tool '{name}' failed")
return result.content
```

So an error result is converted into an exception, the SDK catches it at
`mcp/server/lowlevel/server.py:541` and rebuilds `CallToolResult(content=[TextContent(...)],
isError=True)`. The client sees the same thing either way. `handle_call_tool_result`
(`server.py:386`) is a second public entry point that skips the raise and hands back the
full object, which is what the tests use.

The result then travels back out the transport it came in on: JSON-RPC on stdout for stdio,
or an SSE event or JSON body on the streamable HTTP connection.

## Rough edges

Things in this path that are genuinely hard to follow or worth questioning. Not softened.

Two items from the first version of this list have since been fixed and are marked FIXED
below rather than deleted, because the fix is the interesting part. Everything unmarked is
still true of the code as it stands.

1. **No auth on a loopback bind.** `is_public_start` (`mcp_url_launcher.py:90`) decides
   whether a token is needed, and `mcp_url_launcher.py:705` generates one only when it says
   yes: a tunnel, a `--public-url`, or a non-loopback `--host`. A default `rigout start` on
   127.0.0.1:8765 still serves every tool, including `execute_command`, with no credential,
   so any other process or user on the machine can drive it. That is a deliberate
   local-convenience choice, but nothing in the code says so.
   Narrower than it looks now, and worth knowing the history: before 0.3.0 the same check
   consulted only `--tunnel` and `--public-url`, never the bind address, so `--host 0.0.0.0`
   with the default `--tunnel none` served every tool unauthenticated to the whole network.
   `is_loopback_host` (`mcp_url_launcher.py:74`) now covers all of 127.0.0.0/8 and treats a
   wildcard or unparseable bind as non-loopback, which is the safe default, and `--no-auth`
   on a non-loopback bind prints a warning (`mcp_url_launcher.py:68`, emitted at
   `mcp_url_launcher.py:707`).
2. **The auth layer is conditional, not a middleware.** `mcp_http_server.py:286` swaps in an
   unprotected app when there is no token. Reading `create_app` top to bottom, it is easy to
   see `BearerAuthASGIApp` and assume the endpoint is always protected.
3. **Dispatch is a 15-branch if/elif chain.** `server.py:331-368`. The tool list at
   `server.py:50` and the chain are separate hand-maintained copies of the same names, so
   adding a tool to one and forgetting the other produces `Unknown tool` at runtime with no
   test or type error.
4. **Errors change shape three times.** Handler returns `isError=True`
   (`tools/_results.py:7`), `handle_call_tool` raises `RuntimeError`
   (`server.py:382`), and the SDK converts the exception back into an error result. The
   round trip also drops any content block that is not `TextContent` (`server.py:381`).
   Tracing an error message back to its origin means knowing all three hops.
5. **Half of `SecurityValidator` is dead code.** `validate_file_path`
   (`security_validator.py:348`), `validate_hostname` (`security_validator.py:241`) and
   `validate_ssh_key` (`security_validator.py:395`) are never called from the request path.
   A grep across `src/` finds only their definitions. In particular, `file_operations`
   shell-quotes its paths but never runs the traversal or sensitive-path checks that
   `validate_file_path` implements.
6. **The allow list does not allow-list.** `security_validator.py:342` logs unknown commands
   and lets them through. The name `ALLOWED_COMMANDS` reads like a gate and is not one.
7. **Validation is opt-out per request.** `bypass_security: true` skips `validate_command`
   entirely on all three paths (`ssh_manager.py:682`, `ssh_manager.py:1073`,
   `ssh_manager.py:919`). It is advertised in the public tool schema as a normal argument on
   both `execute_command` (`server.py:91`) and `execute_in_terminal` (`server.py:124`). Still
   true, and now true in one more place: closing the terminal-session gap in item 8 also
   extended the escape hatch to that path. That is a defensible trade, since a hatch that
   exists on one route and not another is worse than one that is uniform, but it is a trade
   and not a free win.
8. **FIXED. Terminal sessions used to skip validation and the rate limit.**
   `execute_in_session` (`ssh_manager.py:893`) now checks the rate limit
   (`ssh_manager.py:908`) and validates the command (`ssh_manager.py:920`) before running it,
   and sanitizes output on both branches (`ssh_manager.py:946`, `ssh_manager.py:972`).
   Validation sits before the branch on session type, so one call covers the SSH-backed and
   local shells. The rate limit shares the endpoint's one-shot budget through
   `_execute_rate_limit_key` (`ssh_manager.py:360`) rather than opening a second budget.
   What remains is not a gap but a boundary worth being able to state: the validator sees the
   command Rigout sends, not what the shell does with it, and a session is stateful, so a
   command validated in isolation still runs against state earlier commands left behind.
9. **`__local__` as a magic string.** The local endpoint is identified by
   `private_key_path == "__local__"` (`ssh_manager.py:1049`), and three tool modules
   re-implement that comparison inline instead of asking the manager
   (`tools/file_ops.py:20`, `tools/file_ops.py:74`, `tools/environment.py:16`).
10. **The config file path is relative and resolved late.** `TunnelManager.__init__`
    defaults to `"mcp-server-config.json"` (`ssh_manager.py:245`) and the manager is a lazily
    created module global (`ssh_manager.py:1160`), so which config gets read depends on the
    working directory of whichever process first handles a tool call. In managed mode that
    directory is set to the state dir at `mcp_url_launcher.py:758`, which is not obvious from
    either file alone.
11. **`session_name` is really `session_id`.** `handle_create_terminal_session`
    (`tools/command.py:52`) passes the schema's `session_name` into the `session_id`
    parameter of `create_terminal_session` (`ssh_manager.py:842`). Reusing a name returns
    `None`, which surfaces to the client as the generic
    `Failed to create terminal session` (`tools/command.py:68`).
12. **SSH host keys are auto-accepted.** `AutoAddPolicy` at `ssh_manager.py:804`,
    `ssh_manager.py:511`, `ssh_manager.py:610` and `ssh_manager.py:864`. Remote endpoints are
    authenticated by key on the client side only; the server is not verified.
13. **Query redaction is log-only.** `RedactSetupTokenQueryMiddleware`
    (`mcp_http_server.py:78`) rewrites the scope so uvicorn's access line is clean. The setup
    token is still transmitted in the URL, so it is visible to the tunnel provider and any
    intermediary. The header form (`X-Setup-Token`) avoids that, but the generated setup URL
    at `mcp_http_server.py:236` uses the query form.
14. **The configurable rate limit is not the one that runs.**
    `SecurityConfig.max_requests_per_minute` (`config_manager.py:59`) is a real setting: it is
    range-checked (`config_manager.py:267`) and reported back out
    (`config_manager.py:298`). Nothing on the request path reads it. `TunnelManager` hardcodes
    its own `self._max_requests_per_minute = 60` (`ssh_manager.py:261`) and `_check_rate_limit`
    compares against that (`ssh_manager.py:353`). `ssh_manager.py` does not import
    `config_manager` at all; the only reference to it anywhere in the package is the re-export
    in `__init__.py:3`. So an operator can set the value, have it validated, read it back, and
    change nothing. This is the same failure shape as item 5: code that looks authoritative
    and is never called.
