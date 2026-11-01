# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Inspect a .deb like dpkg-deb -I / -c: control fields, file list and md5sums.

Without an argument a tiny sample package is built in a temporary directory.

Run: uv run python examples/05_inspect_deb.py [PATH.deb]
"""

import sys
import tempfile
from pathlib import Path

from _sample import build_deb  # the script's directory is first on sys.path

from minideblib.deb import DebError, DebPackage


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(args[0]) if args else Path(tmp) / "sample.deb"
        if not args:
            path.write_bytes(build_deb())
        try:
            deb = DebPackage.open(path)
            files = deb.files()
        except (DebError, OSError) as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 1
    print(f"format {deb.format_version}, control members: {sorted(deb.control_files)}")
    for name in deb.control.fields_in_order():
        print(f"{name}: {deb.control.get_folded(name)}")
    for f in files:  # DebFile: name, size, mode, type ("-" file, "d" dir, ...)
        print(f"{f.type} {f.mode:04o} {f.size:8d} {f.path}")
    for name, digest in deb.md5sums.items():
        print(f"md5 {digest} {name}")
    return 0


# next: see 06_extract_deb_safely.py
if __name__ == "__main__":
    raise SystemExit(main())
