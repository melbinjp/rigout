# Versioning and release policy

What Rigout's version number promises, and what enforces it. `scripts/check_release.py`
checks every rule on this page; the release workflow runs it before anything is built,
so a release that breaks one of them fails before it can reach PyPI.

## The number

Rigout follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) and
records changes in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

`pyproject.toml` holds the version, and it is the only place that does.
`rigout.__version__` resolves from it, `rigout --version` prints it, and
`connection.json` and `/health` report it. Nothing else stores a copy to fall out of
step.

## What a bump means

**Before 1.0.0** — the version reads `0.MINOR.PATCH`:

| Change | Bump | Example |
| --- | --- | --- |
| Anything a working setup would notice: a removed argument, a changed default, output a caller parsed | MINOR | 0.2.0 → 0.3.0 |
| Fixes, additions, documentation, anything an existing caller cannot tell apart except by it working better | PATCH | 0.3.0 → 0.3.1 |

SemVer says 0.x may change anything at any time. Rigout does not use that latitude:
below 1.0.0 a breaking change still costs a MINOR bump, so `0.3.x` is a line an
existing setup can stay on.

**From 1.0.0 onward** — strict SemVer. Breaking changes bump MAJOR, additions bump
MINOR, fixes bump PATCH.

A "breaking change" is anything that changes what an existing caller observes, whether
or not a signature changed. A default that now bounds output, an error path that now
returns different text, a tool that now refuses input it used to accept: all breaking.
The test is what a user notices, not what the code does.

## Recording it

Every release has a `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`, and it is not
empty. Breaking changes go under `### Removed`, or are described in `### Changed` with
the word "breaking" and a sentence naming who is affected and what to do instead.

That wording is load-bearing rather than decorative: `check_release.py` reads the
section to decide which bump the release needed. A breaking change described without
either marker and shipped as a patch is the one mistake this cannot catch, which is why
the sentence naming the affected caller matters more than the label.

## Dependency caps, and why Rigout is not always on the newest

Every runtime dependency has an upper bound:

```toml
mcp>=1.0.0,<2      starlette>=0.37.0,<2
uvicorn>=0.29.0,<1  paramiko>=3.0.0,<6
```

0.2.0 required `mcp>=1.0.0` with no upper bound. When `mcp` 2.0.0 was published, a
plain `pip install rigout` resolved it and every install broke on startup with
`AttributeError: 'Server' object has no attribute 'list_tools'` - the decorators that
register tools were removed in that major. Nothing in the release had changed; the
world moved and the install followed it.

A cap means Rigout is deliberately behind. `mcp` 1.x speaks MCP protocol `2025-11-25`
where 2.x speaks `2026-07-28`, so an installation on the capped line negotiates the
older revision and does not offer whatever the newer one adds. That is the cost, and it
is smaller than the alternative: a caller whose install breaks without them changing
anything.

Majors are adopted deliberately, in a release of their own, never as a side effect of
someone else publishing. The migration is checked, run, and released on its own, because
a transport change is exactly the kind that passes a test suite and fails in use.

Two things watch this so it does not become permanent:

- `.github/workflows/scheduled-ci.yml` builds a wheel weekly and installs it into a
  clean environment that resolves from PyPI, catching a breaking release *within* a cap
  that the pinned development environment would not see.
- The same workflow reports which dependencies are held back and by how far, so an
  overdue major is visible rather than forgotten.

## What Rigout adopts from a new MCP, and what it declines

Being behind on a major does not mean ignoring what the current line offers. Two
capabilities were assessed against mcp 1.29 in August 2026, and they went opposite ways.

**Tool annotations: adopted.** `Tool.title` and `ToolAnnotations` - `readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint` - let a client tell a question apart
from an action before it runs one, which matters more here than in most servers: reading
a CPU count and running an arbitrary command as root are both tools Rigout offers. The
fields are in the specification, present in 1.x and 2.x alike, and additive to clients
that ignore them.

**Tasks: declined, and this is worth stating so it is not rediscovered.** `Tool.execution`
with `taskSupport`, the `Task` type, `tasks/get` and its siblings, and
`mcp.server.experimental.task_support` together describe long-running work a client polls
rather than waits for. That addresses Rigout's oldest limitation directly - a command
that outlives its timeout fails, and builds, installs and downloads all can.

It is not adopted, because the API says of itself:

> The experimental tasks API is deprecated and will be removed in mcp 2.0: tasks
> (SEP-1686) were removed from the MCP specification and are expected to return as a
> separate MCP extension.

Checked rather than taken on the warning's word: `mcp.server.experimental.task_support`
raises `ModuleNotFoundError` on 2.0.0. The types survive there, which makes the feature
look available to anyone reading `mcp.types`, but the server half is gone.

Building on it would mean shipping a feature on an interface that is deprecated in the
version Rigout pins and absent from the version it must move to next. That is the same
trade as the unbounded `mcp>=1.0.0` in 0.2.0: it works until someone else's release day.
When tasks return as an extension, this is worth revisiting, and the reason to revisit is
recorded above.

## The mcp 2.x migration, mapped

Rigout pins `mcp>=1.0.0,<2` and speaks MCP protocol `2025-11-25` where 2.x speaks
`2026-07-28`. Moving is a release of its own. What it involves is recorded here because
the discovery is most of the work and is easy to redo badly.

What does **not** change, checked against 2.0.0 rather than assumed:

- `Tool(name=..., description=..., inputSchema=...)` constructs unchanged. 2.x renamed the
  fields to `input_schema` and friends but keeps the camelCase spellings as aliases, so
  every tool definition ports as written.
- `mcp.server.stdio`, `mcp.server.streamable_http_manager`, `mcp.server.models` and
  `mcp.types` all still import.
- `Server`, `Server.run` and `create_initialization_options` all survive.

What does change, and is the whole of the migration:

- `@server.list_tools()` and `@server.call_tool()` are gone. Registration is now
  `server.add_request_handler(method, params_type, handler)`, with
  `"tools/list"` taking `PaginatedRequestParams` and `"tools/call"` taking
  `CallToolRequestParams`.
- The handlers therefore return results directly rather than the bare list and content
  the decorators wrapped, which also removes the wrinkle where an error result has to be
  raised as `RuntimeError` for the SDK to rebuild it.

Two decisions to make before starting, neither obvious:

- **Whether to support both majors or move.** The incompatibility is only the
  registration, so a single `hasattr(Server, "list_tools")` branch would let the cap
  widen to `<3` and not force anybody onto a major that is days old. The cost is a fork
  in the code that has to be tested twice, on both lines.
- **Whether it is time at all.** Nothing Rigout needs is exclusive to 2.x - tasks, which
  looked like the reason to move, are gone from both. The protocol revision is the only
  gain, and the caps mean nobody is broken meanwhile.

## Releasing

```bash
python scripts/check_release.py           # version, changelog, and bump size agree
python scripts/check_release.py --tag v0.3.0   # plus: the tag names this version
```

Then, once the change is merged to `main`:

```bash
git tag v0.3.0 && git push origin v0.3.0
```

The tag triggers `.github/workflows/release.yml`, which re-runs these checks, confirms
the tagged commit is on `main`, runs the full CI pipeline, builds, and publishes to
PyPI through Trusted Publishing. The tag is what publishes; merging alone does not.

## What is checked mechanically

- The version parses as `MAJOR.MINOR.PATCH`.
- The tag is exactly `v` plus the packaged version.
- The tagged commit is an ancestor of `main`, so nothing publishes from a branch that
  was never merged.
- `CHANGELOG.md` has a dated, non-empty section for the version.
- The version increases on the last released tag.
- The increase is large enough for what the changelog section describes.
