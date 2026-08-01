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
