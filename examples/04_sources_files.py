# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Load APT source definitions: a deb822 .sources file and a one-line sources.list.

Writes both formats to a temporary directory (or reads the files you pass), loads
them with minideblib.sources and lists the resulting entries.

Run: uv run python examples/04_sources_files.py [FILE ...]
"""

import sys
import tempfile
from pathlib import Path

from minideblib import sources

DEB822 = """\
Types: deb deb-src
URIs: http://deb.debian.org/debian
Suites: stable stable-updates
Components: main contrib
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
ONE_LINE = """\
# a comment
deb [arch=amd64 signed-by=/etc/apt/keyrings/k.gpg] http://archive.ubuntu.com/ubuntu noble multiverse
deb http://example.com/flat/ ./
"""


def show(label: str, entries: list[sources.SourceEntry]) -> None:
    print(f"{label}: {len(entries)} entries")
    for e in entries:
        kind = "flat" if e.flat else "/".join(e.components)
        print(f"  {e.type:8} {e.uri} {e.suite} {kind} arch={','.join(e.architectures) or 'any'}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        for name in args:
            show(name, sources.load(Path(name)))
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "debian.sources").write_text(DEB822)
        (Path(tmp) / "ubuntu.list").write_text(ONE_LINE)
        show("deb822 debian.sources", sources.load(Path(tmp) / "debian.sources"))
        show("one-line ubuntu.list", sources.load(Path(tmp) / "ubuntu.list"))
    return 0


# next: see 05_inspect_deb.py
if __name__ == "__main__":
    raise SystemExit(main())
