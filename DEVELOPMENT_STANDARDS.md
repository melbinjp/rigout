# Development Standards

Rigout follows a Google Engineering Practices-inspired workflow: small changes, clear ownership of behavior, readable code, tests near the changed surface, and no hidden release risk.

This file is the source of truth for future contributors and agents.

## Change Control Standard

- Do not commit or push directly to `main`.
- Start every change from an up-to-date `main` branch and create a short-lived branch such as `codex/<scope>` or `feature/<scope>`.
- Open a pull request for every repository change, including docs-only changes.
- Keep pull requests small enough to review in one pass.
- Require CI to pass before merge.
- Require at least one approving review before merge, even for maintainer-authored changes.
- Resolve review comments and conversations before merge.
- Prefer squash merge for a readable main history.
- Delete merged branches.
- Emergency fixes may be small, but they still go through a pull request.

## Core Rules

- Keep changes small and scoped to the behavior being changed.
- Prefer simple, explicit code over clever abstractions.
- Do not preserve compatibility for unused legacy names, flags, scripts, or docs.
- Treat MCP URLs, bearer tokens, generated connection files, logs, keys, and local config as sensitive.
- Keep docs, package metadata, CLI names, tests, and examples aligned with `rigout`.
- Do not add a new dependency unless it removes real complexity or is required by the MCP/device-control domain.

## Python Style

- Use `ruff` for linting and formatting.
- Use `mypy` for source type checks.
- Use Google-style docstrings for public modules, public classes, public functions, and non-obvious tool handlers.
- Prefer typed dataclasses or explicit dictionaries with documented shapes for structured data.
- Catch specific exceptions where possible; avoid bare `except`.
- Keep side effects out of import time unless required by the MCP server framework.

## Testing Standard

Every behavior change should include one of:

- A focused unit test when logic is local.
- An integration test when MCP transport, auth, packaging, or tool dispatch changes.
- A documented manual validation note only when automation is impractical.

Required local checks:

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy src --ignore-missing-imports
python -m pytest -q
python production_validation.py
python -m build
python -m twine check dist/rigout-*
```

## Release Standard

- Build from a clean checkout.
- Never upload old local `dist/` artifacts.
- Confirm `pip install rigout` and `rigout --help` work from the built wheel.
- Rotate any token that was pasted into chat, logs, commits, or issue trackers.
- Keep `CHANGELOG.md` accurate for every user-facing behavior, CLI, packaging, security, or release-process change.
- Prepare version bumps through a release pull request before tagging.
- Publish only through the tagged GitHub Actions release workflow.
- Use PyPI Trusted Publishing with the GitHub environment named `pypi`; do not store long-lived PyPI credentials in GitHub secrets.
- Keep the PyPI trusted publisher fields aligned with the current repository: owner `melbinjp`, repository `rigout`, workflow `release.yml`, environment `pypi`, project `rigout`.
- Follow [RELEASE.md](RELEASE.md) for release gates, validation, version PRs, and tagging.

## Documentation Standard

- The README must explain the shortest working path first.
- `AGENTS.md` must describe how an agent should use Rigout safely.
- `SECURITY.md` must stay explicit about the risk of exposing device control.
- Remove stale docs instead of leaving contradictory instructions.
