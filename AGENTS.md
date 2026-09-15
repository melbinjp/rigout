# Working on rigout

Rigout is small on purpose. Read `specs/v0.4.0.md` before changing behaviour.

- `src/rigout/tools.py` is the eight tools, `server.py` the MCP wiring, `http.py` the transport and
  token check, `cli.py` the command.
- Tests: `pytest`, then `python production_validation.py`, which starts the real command.
- Checks: `ruff check .`, `ruff format --check .`, `mypy src`.
- Do not add a tool for something `run` can already do.
- Every error message says what to call next.
- Anything a user would notice gets a line in `CHANGELOG.md`.
- How pull requests are reviewed, merged and released: `CONTRIBUTING.md`.
