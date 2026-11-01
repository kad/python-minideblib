# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Search packages by substring and glob, and list every version in Debian version order.

The search term defaults to the first package found in the component; --also adds suites
(for example --also oldstable) so that several versions can be compared.

Run: uv run python examples/02_search_and_versions.py [URL SUITE [COMPONENT]] [--name NAME]
     [--also SUITE]
"""

import argparse
import sys

from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import FetchError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Search packages: URL SUITE [COMPONENT].")
    ap.add_argument("url", nargs="?", default="http://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("component", nargs="?", default="contrib")
    ap.add_argument("--name", help="package to look for (default: first in the component)")
    ap.add_argument("--also", action="append", default=[], help="another suite to include")
    args = ap.parse_args(argv)
    try:
        # Scoping to one component and architecture keeps the downloads small.
        suites = [args.suite, *args.also]
        client = RepoClient.from_url(
            args.url, suites=suites, components=[args.component], architectures=["amd64"]
        )
        recs = client.packages(args.suite)
        name = args.name or (recs[0].name if recs else "")
        if not name:
            print(f"error: no packages in {args.suite}/{args.component}", file=sys.stderr)
            return 1
        for pattern in (name[:4], name[:3] + "*"):  # substring, then glob (name only)
            hits = client.search(args.suite, pattern)
            print(f"search {pattern!r}: {len(hits)} hits, e.g. {[r.name for r in hits[:3]]}")
        found = client.versions(name, suites=suites)  # newest version first within a suite
        print(f"{name}:")
        for rec in found:
            print(f"  {rec.suite:16} {rec.version}  {rec.component}/{rec.arch}")
        print("ordered:", " < ".join(map(str, sorted({r.version for r in found}))))
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# next: see 03_find_file_owner.py
if __name__ == "__main__":
    raise SystemExit(main())
