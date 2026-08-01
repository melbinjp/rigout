# Repository Structure

An orientation map, not an inventory: it names the parts you need to find your way
around and leaves out the rest. Nothing is implied by an omission.

```text
.
|-- src/rigout/
|   |-- server.py
|   |-- mcp_http_server.py
|   |-- mcp_url_launcher.py
|   |-- lifecycle.py
|   |-- ssh_manager.py
|   |-- security_validator.py
|   `-- tools/
|-- tests/
|   |-- unit/
|   `-- integration/
|-- docs/
|-- .github/workflows/
|   |-- ci.yml
|   |-- scheduled-ci.yml
|   |-- pr-review.yml
|   |-- review-preview.yml
|   |-- auto-merge.yml
|   |-- agent-audit.yml
|   `-- release.yml
|-- scripts/
|   |-- jules_review.py
|   `-- check_release.py
|-- README.md
|-- AGENTS.md
|-- URL_MCP_SERVER.md
|-- QUICK_REFERENCE.md
|-- TROUBLESHOOTING.md
|-- CONTRIBUTING.md
|-- DEVELOPMENT_STANDARDS.md
|-- SECURITY.md
|-- DEPLOYMENT.md
|-- VERSIONING.md
|-- pyproject.toml
|-- rigout.sh
`-- rigout.ps1
```

The package source is `src/rigout`. Package builds are governed by `pyproject.toml`.
