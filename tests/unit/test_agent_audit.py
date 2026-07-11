"""Regression tests for the agent lifecycle audit helper."""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent_audit.py"
_SPEC = importlib.util.spec_from_file_location("agent_audit", _SCRIPT)
agent_audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(agent_audit)


def test_redact_setup_tokens_hides_value_and_preserves_other_query_fields():
    line = "GET /connection.json?setup_token=secret-value&mode=agent HTTP/1.1"

    assert agent_audit.redact_setup_tokens(line) == ("GET /connection.json?setup_token=***&mode=agent HTTP/1.1")


def test_redact_setup_tokens_leaves_safe_output_unchanged():
    line = "GET /health HTTP/1.1 200 OK"

    assert agent_audit.redact_setup_tokens(line) == line
