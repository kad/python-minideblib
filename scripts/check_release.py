#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Check that a release tag agrees with every place the version is recorded.

Usage: check_release.py [--root PATH] vX.Y.Z[-PRE]

Tag grammar: "v" + MAJOR.MINOR.PATCH (no leading zeros) + optional pre-release
"-rcN", "-aN", "-bN", "-alphaN" or "-betaN". Anything else is rejected.

Version rules, for tag vX.Y.Z (final release):
  pyproject.toml, minideblib.__version__   X.Y.Z
  debian/changelog                          X.Y.Z (Debian revision "-N" ignored when present)
For a pre-release tag vX.Y.Z-rcN:
  pyproject.toml, minideblib.__version__   X.Y.ZrcN (PEP 440; alpha -> a, beta -> b)
  debian/changelog                          X.Y.Z~rcN (same letters as the tag; revision ignored)
The Debian revision is the part after the last "-"; a native version has none, so nothing
is stripped from it.
"""

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

NUM = r"(0|[1-9][0-9]*)"
TAG_RE = re.compile(rf"^v{NUM}\.{NUM}\.{NUM}(?:-(rc|alpha|beta|a|b)([0-9]+))?$")
CHANGELOG_RE = re.compile(r"^\S+ \(([^)\s]+)\)\s+\S+;")
INIT_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
PEP440_LETTER = {"rc": "rc", "a": "a", "alpha": "a", "b": "b", "beta": "b"}
TAG_HELP = "vMAJOR.MINOR.PATCH[-rcN|-aN|-bN|-alphaN|-betaN]"


def expected_versions(tag: str) -> tuple[str, str] | None:
    """Return (PEP 440 version, Debian upstream version) for *tag*, or None if malformed."""
    m = TAG_RE.match(tag)
    if not m:
        return None
    base = ".".join(m.group(i) for i in (1, 2, 3))
    if m.group(4) is None:
        return base, base
    return f"{base}{PEP440_LETTER[m.group(4)]}{m.group(5)}", f"{base}~{m.group(4)}{m.group(5)}"


def upstream_version(debian_version: str) -> str:
    """Drop the Debian revision ("-N", after the last hyphen) when there is one."""
    return debian_version.rsplit("-", 1)[0] if "-" in debian_version else debian_version


def check(root: Path, tag: str) -> list[str]:
    want = expected_versions(tag)
    if want is None:
        return [f"error: tag {tag!r} is not {TAG_HELP}"]
    pep, deb = want
    errors: list[str] = []

    def compare(where: str, found: str | None, expected: str, detail: str = "") -> None:
        if found is None:
            errors.append(f"error: cannot read the version from {where}{detail}")
        elif found != expected:
            errors.append(f"error: {where} is {found}, tag {tag} expects {expected}{detail}")

    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())
        compare("pyproject.toml", project.get("project", {}).get("version"), pep)
    except (OSError, ValueError) as exc:  # ValueError: bad TOML or bad UTF-8
        errors.append(f"error: pyproject.toml: {exc}")

    try:
        init = (root / "src" / "minideblib" / "__init__.py").read_text()
        found = INIT_RE.search(init)
        compare("minideblib.__version__", found.group(1) if found else None, pep)
    except (OSError, ValueError) as exc:
        errors.append(f"error: minideblib/__init__.py: {exc}")

    try:
        lines = (root / "debian" / "changelog").read_text().splitlines()
        first = next((ln for ln in lines if ln.strip()), "")
        cm = CHANGELOG_RE.match(first)
        if cm is None:
            errors.append(f"error: debian/changelog: unparsable first line {first!r}")
        else:
            compare(
                "debian/changelog",
                upstream_version(cm.group(1)),
                deb,
                f" (top entry version {cm.group(1)})",
            )
    except (OSError, ValueError) as exc:
        errors.append(f"error: debian/changelog: {exc}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("tag", help="release tag, e.g. v0.7.0")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = p.parse_args(argv)
    errors = check(args.root, args.tag)
    for line in errors:
        print(line, file=sys.stderr)
    if errors:
        return 1
    print(f"release {args.tag.removeprefix('v')} OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
