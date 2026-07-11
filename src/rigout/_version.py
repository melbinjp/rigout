"""Resolve Rigout's public package version from one authoritative source."""

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path


def _source_checkout_version() -> str | None:
    """Read the project version when running directly from an unpackaged checkout."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        content = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None

    project_section = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", content)
    if project_section:
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', project_section.group(1), re.MULTILINE)
        if match:
            return match.group(1)
    return None


def resolve_version() -> str:
    """Resolve source metadata first, then installed distribution metadata."""
    source_version = _source_checkout_version()
    if source_version:
        return source_version
    try:
        return distribution_version("rigout")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = resolve_version()
