# Contributing

Issues and pull requests are welcome. Keep changes small and in line with `specs/v0.4.0.md`.

    pip install -e ".[dev]"
    pytest
    python production_validation.py
    ruff check . && ruff format --check . && mypy src

Add a line to `CHANGELOG.md` under Unreleased for anything a user would notice.
