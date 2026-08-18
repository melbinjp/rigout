"""Check that a version number, its tag, and its changelog entry agree.

Nothing enforced the couplings between them before. A tag could name one version while
the package built another, a release could ship with no changelog entry, and a release
containing breaking changes could go out as a patch bump. Each of those is silent: the
artifact builds, the workflow is green, and the mismatch is only visible to whoever
installs it.

Run with no arguments to check the working tree (`--tag` adds the tag agreement check):

    python scripts/check_release.py
    python scripts/check_release.py --tag v0.3.0

Exits non-zero and prints every problem it found, not just the first, so one run says
everything that has to change.

Versioning policy, which this enforces and `VERSIONING.md` explains:
  * The version in `pyproject.toml` is the single source of truth. The tag is `v` plus
    that string, exactly.
  * Below 1.0.0, a release containing a breaking change bumps MINOR; everything else
    bumps PATCH. From 1.0.0 onward, strict SemVer: breaking bumps MAJOR.
  * A release has a changelog section with a date, and it is not empty.
  * When a tag is being released, that date is the day it actually goes out. A date
    that exists is not the same as a date that is true, and 0.3.1 shipped fifteen
    days after the one it carried.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
CHANGELOG_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?:\s+-\s+(\d{4}-\d{2}-\d{2}))?\s*$", re.MULTILINE)

# Wording that marks a release as breaking. "### Removed" is Keep a Changelog's own
# section for it; the prose forms catch a breaking change described inside Changed,
# which is where they usually live because the section is about what altered.
BREAKING_MARKERS = (
    re.compile(r"^### Removed\s*$", re.MULTILINE),
    re.compile(r"\bbreaking\b", re.IGNORECASE),
)


def read_project_version(pyproject: Path) -> str | None:
    """Read `version` from the [project] table, not from any other table that has one."""
    content = pyproject.read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", content)
    if not project:
        return None
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', project.group(1), re.MULTILINE)
    return match.group(1) if match else None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = SEMVER.match(text.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def changelog_sections(changelog: str) -> list[tuple[str, str | None, int]]:
    """Return (version, date, start offset) for each released section, newest first."""
    return [(m.group(1), m.group(2), m.end()) for m in CHANGELOG_HEADING.finditer(changelog)]


def section_body(changelog: str, start: int) -> str:
    """The text of one changelog section, up to the next section heading."""
    following = re.search(r"^## ", changelog[start:], re.MULTILINE)
    return changelog[start : start + following.start()] if following else changelog[start:]


def describes_breaking_change(body: str) -> bool:
    return any(marker.search(body) for marker in BREAKING_MARKERS)


def required_bump(previous: tuple[int, int, int], breaking: bool) -> str:
    """Which component must increase, given the policy and the previous version."""
    if not breaking:
        return "patch"
    return "minor" if previous[0] == 0 else "major"


def bump_kind(previous: tuple[int, int, int], current: tuple[int, int, int]) -> str | None:
    """Classify the step from `previous` to `current`, or None if it is not an increase."""
    if current[0] > previous[0]:
        return "major"
    if current[0] == previous[0] and current[1] > previous[1]:
        return "minor"
    if current[:2] == previous[:2] and current[2] > previous[2]:
        return "patch"
    return None


def bump_is_sufficient(actual: str, required: str) -> bool:
    """A larger bump than required is always acceptable; a smaller one is not."""
    order = {"patch": 0, "minor": 1, "major": 2}
    return order[actual] >= order[required]


def released_tags() -> list[tuple[int, int, int]]:
    """Version tags already in this repository, oldest first. Empty if git is unusable."""
    try:
        output = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "tag", "--list", "v*.*.*"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    # removeprefix, not lstrip: lstrip takes a character set, so it would turn a
    # malformed "vv1.0.0" into a valid-looking 1.0.0 instead of rejecting it.
    versions = [parse_version(line.strip().removeprefix("v")) for line in output.splitlines() if line.strip()]
    return sorted(v for v in versions if v is not None)


CHANGELOG_DATE_TOLERANCE_DAYS = 1


def stale_date_problem(
    version: str,
    entry_date: date,
    today: date,
    tolerance_days: int = CHANGELOG_DATE_TOLERANCE_DAYS,
) -> str | None:
    """Report a changelog date that is not the day the release actually happens.

    `check` already required the date to EXIST. It never asked whether it was TRUE, and
    that gap shipped: rigout 0.3.1 carried `## [0.3.1] - 2026-08-02` while PyPI recorded
    the upload on 2026-08-17. The entry was written when the work was done and never
    touched again when it went out fifteen days later. Nothing failed, because nothing
    looked. A changelog is read for the one thing a git log does not answer at a glance -
    when a version reached users - and it was wrong by two weeks.

    Only applied when a tag is being released. On the working tree the date is legitimately
    the day the entry was drafted, and failing a pull request for that would be noise.

    One day of tolerance, because the release runs on a UTC runner and the entry is written
    in whoever's local timezone, so an honest entry can name the adjacent day.
    """
    drift = (today - entry_date).days
    if abs(drift) <= tolerance_days:
        return None
    if drift < 0:
        return (
            f"the `## [{version}]` heading is dated {entry_date.isoformat()}, which is "
            f"{-drift} days in the future; it should be the day the release goes out ({today.isoformat()})"
        )
    return (
        f"the `## [{version}]` heading is dated {entry_date.isoformat()} but this release is "
        f"going out on {today.isoformat()}, {drift} days later. The date says when a version "
        f"reached users, so an entry drafted early and never updated makes the changelog wrong "
        f"about the one thing it is read for."
    )


def check(tag: str | None, today: date | None = None) -> list[str]:
    problems: list[str] = []

    version_text = read_project_version(REPO_ROOT / "pyproject.toml")
    if version_text is None:
        return ["pyproject.toml has no [project] version"]
    current = parse_version(version_text)
    if current is None:
        return [f"version {version_text!r} in pyproject.toml is not MAJOR.MINOR.PATCH"]

    if tag is not None and tag != f"v{version_text}":
        problems.append(
            f"tag {tag} does not match the packaged version: pyproject.toml says {version_text}, "
            f"so the tag must be v{version_text}. Whichever is wrong, they cannot both ship."
        )

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = changelog_sections(changelog)
    versions_in_changelog = {version for version, _, _ in sections}

    if version_text not in versions_in_changelog:
        problems.append(f"CHANGELOG.md has no `## [{version_text}]` section, so this release documents nothing")
        return problems

    entry = next(item for item in sections if item[0] == version_text)
    # Named entry_date rather than date: the module imports `date` from datetime, and the
    # obvious local name shadows it exactly where the comparison below needs the type.
    _, entry_date, start = entry
    body = section_body(changelog, start)

    if entry_date is None:
        problems.append(
            f"the `## [{version_text}]` heading has no date; it should read `## [{version_text}] - YYYY-MM-DD`"
        )
    elif tag is not None:
        stale = stale_date_problem(
            version_text, date.fromisoformat(entry_date), today if today is not None else date.today()
        )
        if stale is not None:
            problems.append(stale)
    if not body.strip():
        problems.append(f"the `## [{version_text}]` section is empty")

    previous_released = [v for v in released_tags() if v != current]
    if previous_released:
        previous = previous_released[-1]
        actual = bump_kind(previous, current)
        previous_text = ".".join(str(part) for part in previous)
        if actual is None:
            problems.append(f"version {version_text} does not increase on the last released tag v{previous_text}")
        else:
            breaking = describes_breaking_change(body)
            needed = required_bump(previous, breaking)
            if not bump_is_sufficient(actual, needed):
                reason = "describes a breaking change" if breaking else "is a compatible change"
                problems.append(
                    f"version {version_text} is a {actual} bump on v{previous_text}, but the changelog "
                    f"section {reason}, which requires at least a {needed} bump"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check version, tag and changelog agreement")
    parser.add_argument("--tag", help="Release tag to check the packaged version against, e.g. v0.3.0")
    args = parser.parse_args(argv)

    problems = check(args.tag)
    if problems:
        print("Release checks failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    version = read_project_version(REPO_ROOT / "pyproject.toml")
    print(f"Release checks passed for {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
