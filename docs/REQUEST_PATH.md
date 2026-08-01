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
   Code: `server.py:398`, `mcp_http_server.py:355`

2. Authentication happens first, at the ASGI layer, before any MCP code runs.
   `BearerAuthASGIApp` compares the whole `Authorization` header against the literal bytes
   `Bearer <token>`, in constant time. A mismatch returns 401 with `{"error":
   "unauthorized"}` and a `WWW-Authenticate: Bearer` challenge.
   Code: `mcp_http_server.py:124`, `mcp_http_server.py:47`, `mcp_http_server.py:56`

3. The caveat: that wrapper only exists if a token was configured, and Rigout only generates
   one when the server is public, meaning a tunnel or a `--public-url`. A default local
   server has no auth at all.
   Code: `mcp_http_server.py:288`, `mcp_url_launcher.py:645`

4. Both transports converge here on one handler. The SDK looks the tool name up in a cache
   built from `handle_list_tools`, then validates the arguments against that tool's JSON
   schema. Bad arguments never reach Rigout code.
   Code: `server.py:50`, `mcp_http_server.py:133`

5. Rigout's own dispatch is a fifteen-branch if/elif chain, no registry table. An unknown
   name falls through to `Unknown tool`, flagged as an error.
   Code: `server.py:321`, `server.py:354`

6. The handler picks an endpoint. `auto_failover` reuses the active SSH endpoint if it
   answers, otherwise takes the fastest that does, and falls back to a synthetic local
   endpoint meaning this machine.
   Code: `ssh_manager.py:955`, `ssh_manager.py:976`

7. Security validation runs inside `execute_command`, not in the tool handler.
   `validate_command` tokenizes with shlex, masks quoted text, and rejects destructive
   commands like a real `rm -rf /`. A rejection is a result dict, never an exception. The
   caller can skip all of it with `bypass_security: true`.
   Code: `ssh_manager.py:675`, `security_validator.py:287`, `server.py:91`

8. Execution is local or remote, both on a worker thread so the event loop keeps serving.
   Local is `subprocess.run` with `shell=True`. Remote is a pooled Paramiko client running
   `exec_command`. Terminal sessions are a third case: a long-lived shell fed a sentinel
   echo to mark the end of output.
   Code: `ssh_manager.py:1054`, `ssh_manager.py:714`, `terminal_session.py:110`

9. On the way out, stdout and stderr go through `sanitize_command_output`, which masks
   things like `password=` and `token=`.
   Code: `security_validator.py:445`

10. The result becomes a `CallToolResult` with one `TextContent` block. One wrinkle: an
    error result is turned back into a raised `RuntimeError`, which the SDK catches and
    rebuilds as an error result. The client sees `isError: true` either way.
    Code: `tools/_results.py:7`, `server.py:366`

That is the whole path: arrival, auth, dispatch, validation, execution, response.

## 1. Arrival

Rigout ships two console scripts (`pyproject.toml`, `[project.scripts]`):

- `rigout` runs `rigout.mcp_url_launcher:main` (`mcp_url_launcher.py:890`).
- `rigout-stdio` runs `rigout.server:stdio_main` (`server.py:398`).

Both expose the same `Server` object, `server = Server("enhanced-hardware-server")` at
`server.py:47`. The transport is the only difference.

### Stdio

The MCP client starts `rigout-stdio` as a child process and talks JSON-RPC over its stdin
and stdout. `stdio_main` (`server.py:398`) calls `main` (`server.py:381`), which opens
`stdio_server()` and hands the read and write streams to `server.run`. Logging is pinned to
stderr at `server.py:40` so log lines can never corrupt the protocol stream on stdout.

There is no authentication on this transport. The client already owns the process.

### HTTP, and what `rigout start` actually starts

`rigout start` does not serve HTTP itself. It is a supervisor. The chain is:

1. `main` (`mcp_url_launcher.py:890`) parses the command and resolves `RuntimePaths`
   (`lifecycle.py:86`), the per-user state directory holding `rigout.pid`, `runtime.json`,
   `activity.log` and `connection.json`.
2. With `--detach`, `start_detached` (`mcp_url_launcher.py:558`) relaunches Rigout as a
   background copy of itself via `launch_detached` (`lifecycle.py:305`) and then waits for
   `runtime.json` to say `running`. Without `--detach` it goes straight to `run_foreground`
   (`mcp_url_launcher.py:634`).
3. `run_foreground` optionally starts a Cloudflare quick tunnel
   (`mcp_url_launcher.py:280`), then calls `start_server` (`mcp_url_launcher.py:217`), which
   spawns a second process: `python -m rigout.mcp_http_server`. Tokens are passed in that
   child's environment, not on its command line (`mcp_url_launcher.py:246`), because argv is
   readable by other users on most systems.
4. It waits for `/health` to answer 200 (`mcp_url_launcher.py:702`), then writes
   `connection.json` (`mcp_http_server.py:241`), which is chmod 600 on POSIX.
5. The child process runs `mcp_http_server.main` (`mcp_http_server.py:394`), builds the
   Starlette app with `create_app` (`mcp_http_server.py:260`) and serves it with uvicorn
   (`mcp_http_server.py:422`).

So in HTTP mode there are two or three Rigout processes: the launcher, the HTTP server, and
optionally cloudflared. `rigout status` and `rigout stop` work off the PID and process
identity recorded in `runtime.json` (`lifecycle.py:260`, `mcp_url_launcher.py:840`).

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
mcp_app = StreamableHTTPASGIApp(session_manager)                        # :287
protected_mcp_app = BearerAuthASGIApp(mcp_app, auth_token) if auth_token else mcp_app  # :288
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

`run_foreground` (`mcp_url_launcher.py:644`):

```
is_public = bool(args.tunnel != "none" or args.public_url)
if not args.auth_token and not args.no_auth and is_public:
    args.auth_token = secrets.token_urlsafe(32)
```

A token is generated only when the server is exposed publicly. A plain `rigout start` bound
to 127.0.0.1 runs with no bearer token, so anything else on that machine can call every
tool. `--auth-token` or `RIGOUT_AUTH_TOKEN` sets one explicitly, and `--no-auth` suppresses
generation even for public servers. `mcp_http_server.py:399` also supports `--generate-token`
when the HTTP server is run directly.

### The setup token

The setup token is a separate, short-lived credential and it protects exactly one thing:
`/connection.json`. It never grants access to `/mcp`.

- It is generated in `run_foreground` (`mcp_url_launcher.py:648`) only when a bearer token
  exists, the server is public, and `--no-agent-setup-url` was not passed.
- The expiry is computed once when the app is built: `setup_token_expires_at =
  time.monotonic() + setup_token_ttl_seconds` (`mcp_http_server.py:277`). Default TTL is 15
  minutes (`mcp_http_server.py:41`). It is a monotonic deadline, so a clock change does not
  extend it, and it is checked on every request at `mcp_http_server.py:312`.
- It is accepted from the `X-Setup-Token` header or the `setup_token` query parameter
  (`mcp_http_server.py:307`). The header is preferred; the comment at `:306` says so.
- The point of it: `/connection.json` returns the bearer token inside
  `mcp.headers.Authorization` (`mcp_http_server.py:186`). So the setup URL is a one-shot,
  time-boxed way to hand an agent its real credential. The launcher prints exactly that
  warning at `mcp_url_launcher.py:554` for a detached start and `mcp_url_launcher.py:742` in
  the foreground.
- `/connection.json` is only protected when a bearer token exists
  (`mcp_http_server.py:316`). With no bearer token it is public.

### What is redacted from logs

Three separate mechanisms, all worth naming:

1. `RedactSetupTokenQueryMiddleware` (`mcp_http_server.py:78`) rewrites
   `scope["query_string"]` at the moment the response starts (`:89`), replacing the
   `setup_token` value with `REDACTED`. Uvicorn builds its access log line after that, so
   the token never reaches the access log. This only hides it from Rigout's own log. The
   token is still in the URL, so a tunnel operator or proxy can see it.
2. `redact_sensitive_text` (`lifecycle.py:27`) strips `setup_token=...` and `Bearer ...` from
   any text before it is written to `activity.log`. Every line of child process output goes
   through it in `stream_process_output` (`mcp_url_launcher.py:271`), which covers both the
   HTTP server's output and cloudflared's.
3. `ConciseMCPValidationFilter` (`mcp_http_server.py:97`) replaces the MCP SDK's verbose
   "Failed to validate request:" dumps, which can echo request bodies, with a fixed one-line
   summary. It is installed on the root logger and on `mcp.shared.session` for the lifetime
   of the app (`mcp_http_server.py:331`).

The activity log and the connection file are both opened or chmod'ed to owner-only on POSIX
(`lifecycle.py:290`, `mcp_http_server.py:255`).

## 3. Dispatch

Dispatch happens twice: once in the SDK, once in Rigout.

### Where the two transports converge

This is the point where stdio and HTTP stop being different. Over HTTP the request first
passes through `StreamableHTTPASGIApp` (`mcp_http_server.py:133`), a thin adapter over the
SDK's `StreamableHTTPSessionManager`, which parses the JSON-RPC envelope, manages the
session and routes the request to the handler registered for `CallToolRequest`. Over stdio
`server.run` (`server.py:384`) reads the same JSON-RPC off the read stream and reaches the
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

`handle_call_tool` (`server.py:366`) is the registered function. It delegates to
`_handle_call_tool_result` (`server.py:321`), which is a literal if/elif chain over the tool
name, one branch per handler, imported from `rigout.tools` (`server.py:17`). There is no
dispatch table and no dynamic lookup. The tool list at `server.py:50` and this chain are two
independently maintained lists of the same 15 names.

An unknown name falls to the `else` at `server.py:354` and returns
`Unknown tool: <name>` with `isError=True`. Verified end to end: the client receives
`{"content": [{"type": "text", "text": "Unknown tool: no_such_tool"}], "isError": true}`.

Any exception escaping a handler is caught at `server.py:359` and turned into
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

`get_tunnel_manager` (`ssh_manager.py:1102`) lazily creates one module-level `TunnelManager`
on the first tool call, reading `mcp-server-config.json` at that moment
(`ssh_manager.py:245`). `auto_failover` (`ssh_manager.py:955`) reuses the active endpoint if
it still tests healthy, otherwise tests all configured endpoints concurrently and takes the
fastest (`ssh_manager.py:564`), and otherwise falls back to the local endpoint
(`ssh_manager.py:968`). The local endpoint is a synthetic `TunnelEndpoint` with
`private_key_path == "__local__"` (`ssh_manager.py:976`). That sentinel string is how every
later branch decides local versus SSH (`ssh_manager.py:991`).

## 4. Validation

`SecurityValidator` (`security_validator.py:18`) is applied inside
`TunnelManager.execute_command` (`ssh_manager.py:675`) and
`_execute_local_command` (`ssh_manager.py:1014`), not in the tool handlers. So it guards
anything that ends up as a shell command, including the commands that `file_operations`,
`docker_operations`, `install_software` and `environment_setup` build for themselves.

Before validation, `execute_command` applies a per-endpoint rate limit of 60 requests per
minute (`ssh_manager.py:665`, `ssh_manager.py:341`). Over the limit returns
`{"success": false, "error": "Rate limit exceeded"}`.

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

And `bypass_security: true` in the tool arguments (`server.py:91`, `tools/command.py:16`)
skips validation entirely. It is logged as a `SECURITY_BYPASS` event
(`ssh_manager.py:692`, or `LOCAL_SECURITY_BYPASS` at `ssh_manager.py:1033`) and nothing else
stops it.

## 5. Execution

Three execution shapes.

### Local, one-shot

`_execute_local_command` (`ssh_manager.py:994`). After validation it resolves the working
directory (`:1038`), merges the environment over `os.environ` (`:1049`), then runs:

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
`{"success": false, "error": "Command execution timeout"}` (`ssh_manager.py:1079`).

### Remote over SSH, one-shot

`execute_command` (`ssh_manager.py:648`). The working directory and environment are applied
by prefixing the remote command with `cd '<dir>' && ` and shell-quoted `KEY='value'`
assignments (`ssh_manager.py:697`, `ssh_manager.py:42`). Connections come from a per-host
pool capped at 5 (`_get_ssh_connection`, `ssh_manager.py:778`); a pooled client is only
reused if its transport is still active. The blocking Paramiko sequence of
`exec_command`, two `read()` calls and `recv_exit_status` is wrapped in `_run_ssh_command`
(`ssh_manager.py:58`) and pushed onto a thread at `ssh_manager.py:714`. The client is
returned to the pool in a `finally` (`ssh_manager.py:774`).

Note `set_missing_host_key_policy(AutoAddPolicy())` at `ssh_manager.py:798`: unknown host
keys are accepted rather than verified.

### Persistent terminal sessions

`create_terminal_session` (`ssh_manager.py:836`) makes either a `TerminalSession` backed by
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

Note that `execute_in_session` does not run the security validator. Validation is in
`execute_command` and `_execute_local_command` only.

## 6. Response

Handlers build `CallToolResult` objects directly from `mcp.types`.

Success is a single `TextContent` block of human-readable text, for example
`tools/command.py:36`. `get_server_activity` is the exception: it returns JSON as text
(`tools/activity.py:41`).

Failure goes through two helpers in `tools/_results.py`:

- `error_result(message)` (`tools/_results.py:7`) returns
  `CallToolResult(content=[TextContent(...)], isError=True)`. This is the single place the
  error flag is set in Rigout's own code.
- `failure_detail(result, fallback)` (`tools/_results.py:12`) builds a non-empty diagnostic
  from a failed command dict, preferring `error`, then `stderr`, then
  `Command exited with status N`, then the fallback. It exists so an error is never an empty
  string.

Sanitization on the way out happens in the manager, not the handler:
`sanitize_command_output` (`security_validator.py:445`) is applied to stdout and stderr for
both the SSH path (`ssh_manager.py:730`) and the local path (`ssh_manager.py:1066`). It
regex-replaces `password=`, `token=`, `key=`, `secret=`, `api_key=` and `auth_token=` values
with `***`, case insensitive. `get_server_activity` additionally runs
`redact_sensitive_text` over every log line before sanitizing (`tools/activity.py:27`).

Then the shape changes once more. `handle_call_tool` (`server.py:366`) is what the SDK
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
(`server.py:376`) is a second public entry point that skips the raise and hands back the
full object, which is what the tests use.

The result then travels back out the transport it came in on: JSON-RPC on stdout for stdio,
or an SSE event or JSON body on the streamable HTTP connection.

## Rough edges

Things in this path that are genuinely hard to follow or worth questioning. Not softened.

1. **No auth by default.** `mcp_url_launcher.py:645` generates a bearer token only when the
   server is public. A default local `rigout start` serves every tool, including
   `execute_command`, unauthenticated on 127.0.0.1:8765. Any other process or user on the
   machine can drive it. That is a deliberate local-convenience choice, but nothing in the
   code says so.
2. **The auth layer is conditional, not a middleware.** `mcp_http_server.py:288` swaps in an
   unprotected app when there is no token. Reading `create_app` top to bottom, it is easy to
   see `BearerAuthASGIApp` and assume the endpoint is always protected.
3. **Dispatch is a 15-branch if/elif chain.** `server.py:321-358`. The tool list at
   `server.py:50` and the chain are separate hand-maintained copies of the same names, so
   adding a tool to one and forgetting the other produces `Unknown tool` at runtime with no
   test or type error.
4. **Errors change shape three times.** Handler returns `isError=True`
   (`tools/_results.py:7`), `handle_call_tool` raises `RuntimeError`
   (`server.py:372`), and the SDK converts the exception back into an error result. The
   round trip also drops any content block that is not `TextContent` (`server.py:371`).
   Tracing an error message back to its origin means knowing all three hops.
5. **Half of `SecurityValidator` is dead code.** `validate_file_path`
   (`security_validator.py:348`), `validate_hostname` (`security_validator.py:241`) and
   `validate_ssh_key` (`security_validator.py:395`) are never called from the request path.
   A grep across `src/` finds only their definitions. In particular, `file_operations`
   shell-quotes its paths but never runs the traversal or sensitive-path checks that
   `validate_file_path` implements.
6. **The allow list does not allow-list.** `security_validator.py:342` logs unknown commands
   and lets them through. The name `ALLOWED_COMMANDS` reads like a gate and is not one.
7. **Validation is opt-out per request.** `bypass_security: true` (`server.py:91`) skips
   `validate_command` entirely (`ssh_manager.py:676`, `ssh_manager.py:1015`). It is
   advertised in the public tool schema as a normal argument.
8. **Terminal sessions skip validation.** `execute_in_session` (`ssh_manager.py:887`) never
   calls the validator, so `create_terminal_session` followed by `execute_in_terminal` runs
   arbitrary commands with no deny-list check at all.
9. **`__local__` as a magic string.** The local endpoint is identified by
   `private_key_path == "__local__"` (`ssh_manager.py:991`), and three tool modules
   re-implement that comparison inline instead of asking the manager
   (`tools/file_ops.py:20`, `tools/file_ops.py:74`, `tools/environment.py:16`).
10. **The config file path is relative and resolved late.** `TunnelManager.__init__`
    defaults to `"mcp-server-config.json"` (`ssh_manager.py:245`) and the manager is a lazily
    created module global (`ssh_manager.py:1102`), so which config gets read depends on the
    working directory of whichever process first handles a tool call. In managed mode that
    directory is set to the state dir at `mcp_url_launcher.py:694`, which is not obvious from
    either file alone.
11. **`session_name` is really `session_id`.** `handle_create_terminal_session`
    (`tools/command.py:52`) passes the schema's `session_name` into the `session_id`
    parameter of `create_terminal_session` (`ssh_manager.py:836`). Reusing a name returns
    `None`, which surfaces to the client as the generic
    `Failed to create terminal session` (`tools/command.py:68`).
12. **SSH host keys are auto-accepted.** `AutoAddPolicy` at `ssh_manager.py:798`,
    `ssh_manager.py:505`, `ssh_manager.py:604` and `ssh_manager.py:858`. Remote endpoints are
    authenticated by key on the client side only; the server is not verified.
13. **Query redaction is log-only.** `RedactSetupTokenQueryMiddleware`
    (`mcp_http_server.py:78`) rewrites the scope so uvicorn's access line is clean. The setup
    token is still transmitted in the URL, so it is visible to the tunnel provider and any
    intermediary. The header form (`X-Setup-Token`) avoids that, but the generated setup URL
    at `mcp_http_server.py:236` uses the query form.
