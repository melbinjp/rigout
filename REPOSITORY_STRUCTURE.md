# Repository Structure

```text
.
|-- src/rigout/
|   |-- server.py
|   |-- mcp_http_server.py
|   |-- mcp_url_launcher.py
|   |-- ssh_manager.py
|   `-- tools/
|-- tests/
|   |-- unit/
|   `-- integration/
|-- .github/workflows/
|   |-- ci.yml
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
