# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Compare and sort Debian version strings (epochs, tildes, revisions).

Pass your own versions as arguments; the defaults show the classic corner cases.

Run: uv run python examples/07_compare_versions.py [VERSION ...]
"""

import sys

from minideblib.version import Version, VersionError, compare

DEFAULT = ["1:0.9", "1.0+b1", "1.0", "1.0~rc1"]


def main(argv: list[str] | None = None) -> int:
    args = (sys.argv[1:] if argv is None else argv) or DEFAULT
    try:
        versions = [Version(v) for v in args]
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("sorted:", " < ".join(str(v) for v in sorted(versions)))
    newest = max(versions)
    print(f"newest: {newest}")
    print(f"  epoch={newest.epoch} upstream={newest.upstream!r} revision={newest.revision!r}")
    for a, b in zip(args, args[1:], strict=False):
        sign = {-1: "<", 0: "==", 1: ">"}[compare(a, b)]  # compare() works on plain strings
        print(f"{a} {sign} {b}")
    print("1.0 == 0:1.0 ?", Version("1.0") == Version("0:1.0"))
    return 0


# next: see 08_changelog_and_control.py
if __name__ == "__main__":
    raise SystemExit(main())
