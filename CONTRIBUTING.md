# Contributing

Rigout is a Python MCP server. Keep changes scoped, tested, and explicit about safety.

Read [DEVELOPMENT_STANDARDS.md](DEVELOPMENT_STANDARDS.md) before making non-trivial changes. It defines the project rules for style, tests, docs, and releases.

## Contribution Flow

Do not push directly to `main`. Use this flow for every change:

1. Update local `main`.
2. Create a short-lived branch.
3. Make a focused change.
4. Update tests and docs near the changed behavior.
5. Update `CHANGELOG.md` for user-facing behavior, CLI, packaging, security, or release-process changes.
6. Run the required checks.
7. Open a pull request.
8. Merge only after CI passes and review approval is recorded.

## Development Setup

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

On macOS/Linux:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

Run before opening a release or pull request:

```bash
ruff check .
ruff format --check .
mypy src/ --ignore-missing-imports
python -m pytest -q
python production_validation.py
python -m build
python -m twine check dist/rigout-*
```

## Rules

- Do not commit generated connection files, logs, keys, tokens, or local config.
- Keep public docs aligned with `rigout`, not older project names.
- Treat public MCP URLs and bearer tokens as credentials.
- Add tests when changing tool behavior, auth, transport, packaging, or local fallback behavior.
- Use [RELEASE.md](RELEASE.md) for version bumps, release notes, tags, and PyPI publishing.
