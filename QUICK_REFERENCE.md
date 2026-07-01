# Quick Reference

## Install

```bash
pip install rigout
```

## Run

```bash
rigout
rigout --tunnel cloudflare
rigout --port 9000
rigout --public-url https://rigout.example.com
rigout --auth-token "$RIGOUT_TOKEN"
```

## Source Checkout

```bash
python -m pip install -e .
rigout --tunnel cloudflare
```

## Stdio MCP

```bash
rigout-stdio
```

## Files

- `ai_agent_connection.json`: generated MCP client configuration.
- `mcp-hardware-server.log`: audit and runtime log.
- `pyproject.toml`: package metadata and build configuration.
- `src/rigout/`: package source.
- `tests/`: pytest coverage.

## Verification

```bash
python -m pytest -q
python production_validation.py
python -m build
python -m twine check dist/rigout-*
```

## PyPI Release

Rigout publishes from the GitHub Actions tag workflow through PyPI Trusted Publishing. No PyPI API token is required.

Trusted publisher fields:

- PyPI project: `rigout`
- GitHub owner: `melbinjp`
- GitHub repository: `rigout`
- Workflow filename: `release.yml`
- GitHub environment: `pypi`

Create and push a version tag only after the publisher is configured:

```bash
git tag v0.1.0
git push origin v0.1.0
```

## Cleanup Before Publishing

```bash
git status --short
```

Do not publish old local artifacts from `dist/`. GitHub Actions builds from a clean checkout on tags.
