# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Extract a .deb safely: size limit, and how unsafe members are rejected with DebError.

Extracts PATH (or a generated sample) into a temporary directory, then shows that a
package with a '../' member and an over-limit extraction are refused before writing.

Run: uv run python examples/06_extract_deb_safely.py [PATH.deb]
"""

import sys
import tempfile
from pathlib import Path

from _sample import build_deb  # the script's directory is first on sys.path

from minideblib.deb import DebError, DebPackage


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "out"
        try:
            deb = DebPackage.open(args[0]) if args else DebPackage(build_deb())
            deb.extract(dest, max_size=10 * 1024 * 1024)  # total bytes limit
        except (DebError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"extracted {deb.control['Package']} to a temporary directory:")
        for p in sorted(p for p in dest.rglob("*") if p.is_file()):
            print("  ", p.relative_to(dest))
        evil = DebPackage(build_deb({"../escape.txt": b"nope"}))
        for label, pkg, limit in (("unsafe path", evil, None), ("size limit", deb, 1)):
            try:
                pkg.extract(Path(tmp) / label.replace(" ", "-"), max_size=limit)
            except DebError as exc:
                print(f"rejected ({label}): {exc}")
        print("escape.txt written outside:", (Path(tmp) / "escape.txt").exists())
    return 0


# next: see 07_compare_versions.py
if __name__ == "__main__":
    raise SystemExit(main())
