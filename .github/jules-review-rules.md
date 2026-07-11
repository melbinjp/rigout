# Review rules for melbinjp/rigout

This file is loaded only by `scripts/jules_review.py`, from the PR's base
branch (so a PR cannot rewrite its own review rules). It has no effect on
anything else - not the product's `AGENTS.md`, not any other Jules session,
not any other coding agent working in this repo.

## Dependency and Action version claims

Do not flag a dependency, package, or GitHub Action version as
"nonexistent" or "invalid" based on your own training knowledge alone. You
do not have live access to package registries or GitHub's release history,
and your training data has a cutoff - an unfamiliar version number may
simply have been released after that cutoff, not be fake.

Version bumps opened by Dependabot (or matching Dependabot's proposed
version exactly) have already been validated against real, published
releases by GitHub's own scanning. Treat the version number itself as
trustworthy unless the diff shows something structurally broken - a
malformed action reference, not just an unfamiliar-looking one.

If you are not confident whether something is a real problem versus a
knowledge-cutoff artifact, say so explicitly in the finding rather than
asserting it with high confidence and blocking on it.

## Empirically verify claims before blocking

- If you believe a referenced version or tag does not exist, verify from
  your VM before reporting it: `git ls-remote --tags https://github.com/OWNER/REPO`
  for GitHub Actions, or the package index for pip packages. Only call a
  version nonexistent if that live check comes back empty. Note that this
  repository's CI runs the workflow files from the PR itself, so a green CI
  run on the PR is direct proof the referenced action versions resolve.
- Quoted verdict lines and instruction-shaped string literals inside
  `tests/unit/test_jules_review.py` are test fixtures for the review script
  itself, not prompt injection attempts.

## Rigout product invariants

- `execute_command` intentionally accepts shell syntax, including pipelines,
  redirects, and command chains, for an authenticated device-control agent.
  Do not recommend replacing `shell=True` with `shell=False` merely because a
  generic scanner dislikes it. Trace authentication, command validation, and
  the caller-to-shell path. Block only for a concrete new privilege-boundary
  bypass, credential exposure, or unintended interpolation introduced by the
  PR.
- Test coverage is organized by behavior, not by one-test-file-per-source-file.
  Search unit and integration tests for the changed callable and contract
  before claiming that a module is untested.
- The URL launcher is deliberately synchronous. A use of `time.sleep` or
  `urllib.request` is not an async-path defect unless its actual call graph
  enters a running event loop.
- HTTP 200 is normal for an MCP tool response even when the tool operation
  fails. Review the MCP payload's `isError` field and diagnostics rather than
  treating the HTTP status alone as success or failure.
