# Versioning

The version in `pyproject.toml` is the only source. A release is tagged `v` plus that version, and
needs a dated section in `CHANGELOG.md`.

Before 1.0.0, a release with a breaking change bumps the minor version (0.3.1 to 0.4.0), and any
other release bumps the patch version. `scripts/check_release.py` checks this when a tag is pushed.
