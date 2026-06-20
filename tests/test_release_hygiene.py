"""Release hygiene — guards that make release mistakes loud.

Born from a release where the CHANGELOG-sectioning step failed
but the tag was pushed anyway: the published sdist carried the release's
entries under [Unreleased]. This test makes that impossible to repeat —
bumping __version__ without sectioning the CHANGELOG turns CI red.
"""

from __future__ import annotations

from pathlib import Path

from lynx import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_changelog_has_a_section_for_the_current_version() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text, (
        f"__version__ is {__version__} but CHANGELOG.md has no "
        f"'## [{__version__}]' section — section it before tagging"
    )


def test_changelog_unreleased_section_exists() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
