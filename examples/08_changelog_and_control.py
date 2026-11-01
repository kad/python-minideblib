# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Parse a debian/changelog and a debian/control file, including the folded Binary list.

Reads the files you pass (CHANGELOG [CONTROL]) or built-in samples.

Run: uv run python examples/08_changelog_and_control.py [CHANGELOG [CONTROL]]
"""

import sys
from pathlib import Path

from minideblib import changelog, control

CHANGELOG = """\
sample-pkg (1.0-2) unstable; urgency=medium

  * Fix crash on start. Closes: #123456, #654321
  * Update docs. (LP: #42)

 -- Jane Doe <jane@example.com>  Sat, 01 Nov 2026 10:00:00 +0000

sample-pkg (1.0-1) unstable; urgency=low

  * Initial release.

 -- Jane Doe <jane@example.com>  Fri, 31 Oct 2026 10:00:00 +0000
"""
SOURCE_STANZA = """\
Source: sample-pkg
Binary: sample-pkg, sample-pkg-doc,
 sample-pkg-dev
Build-Depends: debhelper-compat (= 13), cmake
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        log_text = Path(args[0]).read_text() if args else CHANGELOG
        ctl_text = Path(args[1]).read_text() if len(args) > 1 else SOURCE_STANZA
        entries = changelog.parse(log_text, keywords=["LP"])
        paragraphs = list(control.parse_paragraphs(ctl_text))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for e in entries:
        print(f"{e.package} {e.version} {e.date:%Y-%m-%d} by {e.maintainer}")
        print(f"  bugs closed: {e.bugs}  LP: {e.keywords.get('LP', [])}")
    for para in paragraphs:
        print(f"paragraph with fields {para.fields_in_order()}")
        print("  Binary (folded list):", para.get_list("Binary"))
        print("  Build-Depends:", para.get_list("Build-Depends"))
    return 0


# next: see 09_appstream.py
if __name__ == "__main__":
    raise SystemExit(main())
