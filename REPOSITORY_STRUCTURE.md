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
|   `-- release.yml
|-- README.md
|-- AGENTS.md
|-- URL_MCP_SERVER.md
|-- QUICK_REFERENCE.md
|-- TROUBLESHOOTING.md
|-- CONTRIBUTING.md
|-- DEVELOPMENT_STANDARDS.md
|-- SECURITY.md
|-- pyproject.toml
|-- rigout.sh
`-- rigout.ps1
```

The package source is `src/rigout`. Package builds are governed by `pyproject.toml`.
