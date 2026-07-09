# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `.github/jules-review-rules.md`: maintainer-authored review guidance loaded
  from the base branch, telling the reviewer not to flag unfamiliar
  dependency/Action versions as nonexistent from training knowledge alone (#18).
- Grouped Dependabot updates: `github-actions` bumps arrive as a single PR;
  `pip` groups minor/patch only, leaving major bumps isolated for review (#17).
- Unit tests for `scripts/jules_review.py` covering its fail-closed
  guarantees: anchored verdict parsing, trusted-author gating, skip
  conditions, 404-tolerant polling, and the no-`automationMode` session
  payload.

### Fixed
- Jules review skips cleanly on Dependabot PRs instead of failing with 401s -
  GitHub withholds repository secrets from Dependabot-triggered runs (#16).
- Auto-merge workflow: arming auto-merge needs `contents: write`, and now
  retries on every push instead of only on PR open/reopen (#16).
- Jules review verdict parsing anchored to a whole line, so verdict text
  quoted inside a finding can never be read as the real verdict (#18).
- GitHub Actions bumped: `checkout` v7, `setup-python` v6,
  `upload-artifact` v7, `download-artifact` v8 (#17).
- Diff truncation for review prompts now cuts at a line boundary.

## [0.2.0] - 2026-07-09

### Added
- Automated Jules PR review (`scripts/jules_review.py`): posts a code review
  comment on every PR and auto-approves it when no blocking issues are found
  and the PR author is trusted (default: the repo owner), since branch
  protection's "require approval of the most recent push" rule otherwise
  cannot be satisfied by the PR author themselves.
- `.github/workflows/auto-merge.yml`: arms GitHub's native auto-merge on PRs
  the repo owner opens against `main`, so they complete on their own once
  Jules' approval and all required checks land - deliberately independent
  of Jules internally, it only flips the same flag a human's approval would
  unblock. PRs from anyone else never get this flag set.
- Unit tests for the Cloudflare quick-tunnel bootstrap (`start_cloudflare_tunnel`,
  `resolve_public_mcp_url`), covering URL extraction and both failure paths.
- `.github/dependabot.yml` for automated dependency and GitHub Actions updates.

### Fixed
- `LocalTerminalSession` hung indefinitely on Windows: `cmd.exe /q` suppresses
  input echo but still prints the shell prompt, so the completion sentinel
  arrived prefixed (`C:\path>__RIGOUT_DONE_xxx__ 0`) instead of at the start
  of the line, and was discarded as a stale echo instead of being recognized.
- CI: removed a bogus `pip install curl` step in the Agent Connection Audit
  workflow (`curl` is a system binary, not a PyPI package).
- CI: pinned macOS runners to `macos-15` after GitHub's `macos-latest` ->
  `macos-26` migration caused runner-acquisition failures; updated branch
  protection's required status checks to match the renamed jobs.

## [0.1.0] - 2026-07-01

Initial PyPI release.
