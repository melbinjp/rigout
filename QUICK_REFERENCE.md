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
rigout --tunnel cloudflare --cloudflared-path /path/to/cloudflared
rigout --tunnel cloudflare --no-cloudflared-download
rigout --tunnel cloudflare --no-agent-setup-url
python -m rigout.mcp_url_launcher --tunnel cloudflare
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
- User-local Rigout cache: stores auto-downloaded `cloudflared` when needed.

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
