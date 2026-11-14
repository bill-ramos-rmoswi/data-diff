#!/usr/bin/env python3
"""Print one version's section of CHANGELOG.md, for the GitHub Release body.

    python3 dev/release/notes.py            # the version in pyproject.toml
    python3 dev/release/notes.py 0.15.0     # a given version
    python3 dev/release/notes.py --check    # only check that the section exists

Exits 1 if CHANGELOG.md has no non-empty "## [<version>]" section. pr-validate.yml runs --check, so a
version bump without notes fails the pull request instead of the release.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def package_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^\[tool\.poetry\][^\[]*?^version\s*=\s*"([^"]+)"', text, re.M | re.S)
    if not m:
        sys.exit("No version in [tool.poetry] of pyproject.toml")
    return m.group(1)


def section(version: str) -> str:
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    version = args[0].lstrip("v") if args else package_version()
    body = section(version)
    if not body:
        print(
            f"CHANGELOG.md has no section for {version}: add '## [{version}] - <date>' with its notes", file=sys.stderr
        )
        return 1
    if check:
        print(f"CHANGELOG.md has notes for {version} ({len(body.splitlines())} lines)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
