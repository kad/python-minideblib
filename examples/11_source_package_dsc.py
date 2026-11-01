# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Source packages: fetch a .dsc with RepoClient.fetch_dsc, summarise it, verify its files.

Takes the first source package of the component. With --verify the files it lists are
downloaded into a temporary directory and checked with Dsc.verify (can be large!).

Run: uv run python examples/11_source_package_dsc.py [URL SUITE [COMPONENT]] [--verify]
"""

import argparse
import posixpath
import sys
import tempfile
from pathlib import Path

from minideblib.dsc import DscError
from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import FetchError
from minideblib.repo.release import HashMismatch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch a .dsc: URL SUITE [COMPONENT].")
    ap.add_argument("url", nargs="?", default="http://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("component", nargs="?", default="contrib")
    ap.add_argument("--verify", action="store_true", help="download the files and verify them")
    args = ap.parse_args(argv)
    client = RepoClient.from_url(args.url, suites=[args.suite])
    try:
        records = client.sources(args.suite, args.component)
        if not records:
            print("error: no source packages found", file=sys.stderr)
            return 1
        dsc = client.fetch_dsc(records[0])  # hash-checked against the Sources index
        print(f"{dsc.source} {dsc.version} ({dsc.format}) maintained by {dsc.maintainer}")
        print(f"  binaries: {', '.join(dsc.binaries)}; native: {dsc.is_native}")
        for f in dsc.files:
            print(f"  file {f.name} ({f.size} bytes) {sorted(f.hashes())}")
        if args.verify:
            with tempfile.TemporaryDirectory() as tmp:
                for url in records[0].file_urls:
                    (Path(tmp) / posixpath.basename(url)).write_bytes(client.fetcher.get(url))
                dsc.verify(tmp)
                print(f"verified {len(dsc.files)} files")
    except (FetchError, HashMismatch, DscError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# next: see README.md for sample output of every example
if __name__ == "__main__":
    raise SystemExit(main())
