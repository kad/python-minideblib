# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Browse an APT repository: suites, components, architectures and the first packages.

Defaults to Debian stable, component contrib (small); use main for the full index.
Pass --sources FILE to read a sources.list or deb822 .sources file instead. suites()
probes a few well-known suite names and raises FetchError if the repository is unreachable.

Run: uv run python examples/01_browse_repository.py [URL SUITE [COMPONENT]] [--sources FILE]
"""

import argparse
import sys
from pathlib import Path

from minideblib import sources
from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import FetchError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Browse an APT repository: URL SUITE [COMPONENT].")
    ap.add_argument("url", nargs="?", default="http://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("component", nargs="?", default="contrib")
    ap.add_argument("--sources", type=Path, help="use the entries of this sources file")
    args = ap.parse_args(argv)
    try:
        if args.sources:
            client = RepoClient(sources.load(args.sources))
            if not client.entries:
                print(f"error: no enabled entries in {args.sources}", file=sys.stderr)
                return 1
            suite, comp = client.entries[0].suite, None
        else:
            client = RepoClient.from_url(args.url, suites=[args.suite])
            suite, comp = args.suite, args.component
        print("suites:       ", ", ".join(client.suites()))
        print("components:   ", ", ".join(client.components(suite)))
        print("architectures:", ", ".join(client.architectures(suite)))
        for rec in client.packages(suite, comp, "amd64")[:5]:
            print(f"  {rec.name} {rec.version} [{rec.component}/{rec.arch}] {rec.description}")
    except FetchError as exc:  # also raised by suites() for an unreachable repository
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# next: see 02_search_and_versions.py
if __name__ == "__main__":
    raise SystemExit(main())
