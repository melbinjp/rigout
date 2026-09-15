# Contributing

Issues and pull requests are welcome. Keep changes small and in line with `specs/v0.4.0.md`.

    pip install -e ".[dev]"
    pytest
    python production_validation.py
    ruff check . && ruff format --check . && mypy src

Add a line to `CHANGELOG.md` under Unreleased for anything a user would notice.

## How a change lands

- CI runs lint, type checks, and tests on Linux, macOS and Windows for Python 3.10 to 3.14. A
  second job checks that the documentation still matches the code.
- CodeRabbit reviews every pull request. Merging needs the required checks to pass and one
  approval.
- A release is a `v` tag, as described in `VERSIONING.md`. The release workflow runs CI, builds
  the package, installs the built wheel on its own to test it, and publishes it to PyPI.
- Every Monday, a scheduled run repeats CI against newly released dependencies and opens an
  issue if it fails.
- Dependabot opens at most one grouped update a month for Python packages and one for GitHub
  Actions.
