"""Security invariants for workflows that receive repository secrets."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_jules_reviewer_executes_from_trusted_base_commit():
    workflow = (_ROOT / ".github" / "workflows" / "pr-review.yml").read_text(encoding="utf-8")

    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "persist-credentials: false" in workflow
