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
