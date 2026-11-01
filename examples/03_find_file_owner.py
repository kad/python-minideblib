# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Find which package ships a file, using the repository's Contents indices.

PATH is an exact Contents path or just a base name; the default exists in Debian
stable (contrib). Contents files are large: Debian has one per component (main is
about 12 MB compressed, contrib well under 1 MB) and Ubuntu a single archive-wide one
(about 50 MB), so restrict components with -c (repeatable; default: all of them).

Run: uv run python examples/03_find_file_owner.py [URL SUITE [PATH]] [-c COMPONENT]
"""

import argparse
import sys

from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import FetchError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find a file's package: URL SUITE [PATH].")
    ap.add_argument("url", nargs="?", default="http://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("path", nargs="?", default="etc/GeoIP.conf")
    ap.add_argument("-c", "--component", action="append", help="only search this component")
    args = ap.parse_args(argv)
    try:
        client = RepoClient.from_url(args.url, suites=[args.suite])
        hits = client.find_files(args.suite, args.path, arch="amd64", components=args.component)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path, owner in hits:  # owner is "section/package"
        print(f"{path} is shipped by {owner}")
    for problem in client.problems:  # e.g. a repository without Contents files
        print(f"note: {problem}")
    if not hits:
        print(f"no package found for {args.path}", file=sys.stderr)
        return 1
    return 0


# next: see 04_sources_files.py
if __name__ == "__main__":
    raise SystemExit(main())
