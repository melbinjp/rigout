"""Regression coverage for the repository readiness decision."""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "production_validation.py"
_SPEC = importlib.util.spec_from_file_location("production_validation", _SCRIPT)
production_validation = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(production_validation)


def test_one_failed_category_can_never_be_declared_production_ready(monkeypatch):
    validator_names = [
        "validate_dependencies",
        "validate_file_structure",
        "validate_security_features",
        "validate_configuration_management",
        "validate_server_architecture",
        "validate_error_handling",
        "validate_logging_system",
        "run_basic_tests",
        "validate_required_quality_gates",
        "validate_runtime_contracts",
        "validate_package_build",
    ]
    for name in validator_names:
        monkeypatch.setattr(production_validation, name, lambda: [])
    monkeypatch.setattr(
        production_validation,
        "validate_runtime_contracts",
        lambda: ["simulated contract failure"],
    )

    report = production_validation.generate_production_report()

    assert report["success_rate"] > 90
    assert report["total_issues"] == 1
    assert report["production_ready"] is False
