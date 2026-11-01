# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Use a custom Fetcher (timeout, retries, user agent) and handle errors properly.

Shows FetchError (with .status), HashMismatch, and reading client.problems, the list of
non-fatal issues (for example listed indices missing on the server).

Run: uv run python examples/10_custom_fetcher_and_errors.py [URL SUITE [COMPONENT]]
"""

import argparse
import sys

from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import Fetcher, FetchError
from minideblib.repo.release import HashMismatch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Custom fetcher demo: URL SUITE [COMPONENT].")
    ap.add_argument("url", nargs="?", default="http://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("component", nargs="?", default="contrib")
    args = ap.parse_args(argv)
    fetcher = Fetcher(timeout=10, retries=1, user_agent="my-tool/1.0 (minideblib example)")
    client = RepoClient.from_url(args.url, suites=[args.suite], fetcher=fetcher)
    try:
        recs = client.packages(args.suite, args.component, "amd64")
    except HashMismatch as exc:  # index differs from what Release lists: do not trust it
        print(f"HashMismatch: {exc}", file=sys.stderr)
        return 1
    except FetchError as exc:
        print(f"error: FetchError status={exc.status}: {exc}", file=sys.stderr)
        return 1
    print(f"{len(recs)} packages in {args.suite}/{args.component}")
    try:
        fetcher.get(args.url.rstrip("/") + "/dists/no-such-file")  # deliberate failure
    except FetchError as exc:
        print(f"expected FetchError: status={exc.status} transient={exc.transient}")
    print(f"problems: {client.problems or 'none'}")  # non-fatal issues collected by the client
    if not recs:
        print("error: no packages found; is the suite right?", file=sys.stderr)
        return 1
    return 0


# next: see 11_source_package_dsc.py
if __name__ == "__main__":
    raise SystemExit(main())
