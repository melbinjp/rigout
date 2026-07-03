# Release Process

Rigout releases are intentionally boring: merge a reviewed PR, tag a clean version,
and let GitHub Actions publish through PyPI Trusted Publishing.

## Release Gates

Every release candidate must satisfy:

- `main` is protected and up to date.
- All release changes landed through pull requests.
- `CHANGELOG.md` has an entry for the version being released.
- `pyproject.toml` has the exact version being released.
- No local `dist/` artifacts are reused.
- No credentials, MCP bearer tokens, generated connection files, logs, or local config are committed.
- CI is green on the release commit.

## Local Validation

Run from a clean checkout:

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy src --ignore-missing-imports
python -m pytest -q
python production_validation.py
python -m build
python -m twine check dist/rigout-*
```

For CLI packaging, also verify the built wheel in a fresh environment:

```bash
python -m pip install dist/rigout-<version>-py3-none-any.whl
rigout --help
rigout-stdio --help
```

If the `rigout` script is not on PATH, verify the module launcher:

```bash
python -m rigout.mcp_url_launcher --help
```

## Version PR

Prepare the release in a normal pull request:

1. Move the relevant `CHANGELOG.md` entries from `Unreleased` to `## <version> - YYYY-MM-DD`.
2. Update `pyproject.toml` to the same version.
3. Run the local validation commands.
4. Open a PR and wait for required CI checks.
5. Merge only after review approval.

## Tag And Publish

After the version PR is merged:

```bash
git switch main
git pull --ff-only
git tag v<version>
git push origin v<version>
```

The `Release` workflow builds the source distribution and wheel, checks both with
Twine, and publishes to PyPI through the GitHub environment named `pypi`.

No PyPI API token should be stored in GitHub. PyPI Trusted Publishing must stay
configured with:

- PyPI project: `rigout`
- GitHub owner: `melbinjp`
- GitHub repository: `rigout`
- Workflow: `release.yml`
- Environment: `pypi`
