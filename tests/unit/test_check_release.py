"""Tests for scripts/check_release.py.

Nothing tied the version, the tag and the changelog together before. Each mismatch this
covers is silent in the ordinary sense: the package builds, the workflow is green, and
the disagreement only becomes visible to whoever installs the result.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_release.py"
_spec = importlib.util.spec_from_file_location("check_release", _SCRIPT)
check_release = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(check_release)


@pytest.mark.unit
class TestVersionParsing:
    @pytest.mark.parametrize("text", ["0.3.0", "1.0.0", "10.20.30"])
    def test_valid_versions(self, text):
        assert check_release.parse_version(text) is not None

    @pytest.mark.parametrize("text", ["0.3", "v0.3.0", "0.3.0a1", "", "0.3.0.1"])
    def test_invalid_versions(self, text):
        assert check_release.parse_version(text) is None

    def test_the_project_version_is_read_from_the_project_table(self, tmp_path):
        """Other tables carry versions too; reading the wrong one would be undetectable."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[build-system]\nrequires = ["hatchling"]\nversion = "9.9.9"\n\n'
            '[project]\nname = "rigout"\nversion = "0.3.0"\n',
            encoding="utf-8",
        )

        assert check_release.read_project_version(pyproject) == "0.3.0"


@pytest.mark.unit
class TestBumpPolicy:
    """Below 1.0.0 a breaking change costs a MINOR; from 1.0.0 it costs a MAJOR."""

    def test_a_breaking_change_below_one_needs_a_minor(self):
        assert check_release.required_bump((0, 2, 0), breaking=True) == "minor"

    def test_a_breaking_change_at_or_above_one_needs_a_major(self):
        assert check_release.required_bump((1, 4, 0), breaking=True) == "major"

    def test_a_compatible_change_needs_only_a_patch(self):
        assert check_release.required_bump((0, 2, 0), breaking=False) == "patch"
        assert check_release.required_bump((2, 1, 0), breaking=False) == "patch"

    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            ((0, 2, 0), (0, 3, 0), "minor"),
            ((0, 2, 0), (0, 2, 1), "patch"),
            ((0, 2, 0), (1, 0, 0), "major"),
            ((0, 2, 0), (0, 2, 0), None),
            ((0, 3, 0), (0, 2, 9), None),
        ],
    )
    def test_bump_classification(self, previous, current, expected):
        assert check_release.bump_kind(previous, current) == expected

    def test_a_larger_bump_than_required_is_acceptable(self):
        assert check_release.bump_is_sufficient("major", "minor") is True
        assert check_release.bump_is_sufficient("minor", "minor") is True

    def test_a_smaller_bump_than_required_is_not(self):
        assert check_release.bump_is_sufficient("patch", "minor") is False
        assert check_release.bump_is_sufficient("minor", "major") is False


@pytest.mark.unit
class TestBreakingChangeDetection:
    def test_a_removed_section_marks_a_release_breaking(self):
        assert check_release.describes_breaking_change("### Removed\n- the old flag\n") is True

    def test_the_word_breaking_in_prose_marks_it_too(self):
        """Breaking changes usually live inside Changed, described rather than labelled."""
        body = "### Changed\n- reads are now bounded. Breaking for a caller that read binary.\n"

        assert check_release.describes_breaking_change(body) is True

    def test_an_ordinary_release_is_not_marked_breaking(self):
        body = "### Fixed\n- a crash on startup\n\n### Added\n- a new flag\n"

        assert check_release.describes_breaking_change(body) is False


@pytest.mark.unit
class TestChangelogParsing:
    CHANGELOG = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "## [0.3.0] - 2026-08-01\n### Added\n- a thing\n\n"
        "## [0.2.0] - 2026-07-09\n### Fixed\n- another\n"
    )

    def test_released_sections_are_found_with_their_dates(self):
        sections = check_release.changelog_sections(self.CHANGELOG)

        assert [version for version, _, _ in sections] == ["0.3.0", "0.2.0"]
        assert sections[0][1] == "2026-08-01"

    def test_unreleased_is_not_treated_as_a_release(self):
        assert "Unreleased" not in [version for version, _, _ in check_release.changelog_sections(self.CHANGELOG)]

    def test_a_section_body_stops_at_the_next_version(self):
        sections = check_release.changelog_sections(self.CHANGELOG)
        body = check_release.section_body(self.CHANGELOG, sections[0][2])

        assert "a thing" in body
        assert "another" not in body


@pytest.mark.unit
class TestThisRepositoryPassesItsOwnPolicy:
    """The rules have to hold for the release actually being prepared, not only in theory."""

    def test_the_working_tree_passes(self):
        assert check_release.check(tag=None) == []

    def test_the_matching_tag_passes(self):
        version = check_release.read_project_version(check_release.REPO_ROOT / "pyproject.toml")

        assert check_release.check(tag=f"v{version}") == []

    def test_a_tag_naming_another_version_is_refused(self):
        problems = check_release.check(tag="v9.9.9")

        assert problems
        assert "does not match the packaged version" in problems[0]
