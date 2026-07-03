# Changelog

All notable user-facing and operational changes to Rigout are tracked here.

This project uses release entries tied to Git tags. Unreleased changes stay under
`Unreleased` until a version bump and tag publish them to PyPI.

## Unreleased

### Added

- Auto-bootstrap `cloudflared` for `rigout --tunnel cloudflare` when it is not already installed.
- Add `--cloudflared-path` and `--no-cloudflared-download` for managed or offline environments.
- Add repository governance documentation, release process, PR template, and CODEOWNERS.

### Changed

- Document `python -m rigout.mcp_url_launcher` as the fallback when the console script is not on PATH.
- Require future repository changes to go through a branch, pull request, CI, and review before merging to `main`.

## 0.1.0 - 2026-07-01

### Added

- Initial `rigout` package metadata and PyPI release workflow.
- Streamable HTTP MCP server entrypoint through `rigout`.
- Stdio MCP entrypoint through `rigout-stdio`.
- Local fallback device control when no SSH endpoint is configured.
- Safety validation for high-risk shell commands.
- Unit, integration, type-check, lint, production-validation, build, and trusted-publishing workflows.
