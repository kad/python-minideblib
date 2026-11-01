# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""List AppStream (DEP-11) components of a repository.

Needs PyYAML (the dep11 extra: pip install 'minideblib[dep11]'); without it a clear
message is printed. Debian contrib is the default; Ubuntu works too, for example
http://archive.ubuntu.com/ubuntu noble multiverse.

Run: uv run python examples/09_appstream.py [URL SUITE [COMPONENT]]
"""

import argparse
import sys

from minideblib.repo.client import RepoClient
from minideblib.repo.dep11 import Dep11Error, MissingYamlError, summarize
from minideblib.repo.fetch import FetchError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List AppStream components: URL SUITE [COMPONENT].")
    ap.add_argument("url", nargs="?", default="http://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("component", nargs="?", default="contrib")
    args = ap.parse_args(argv)
    client = RepoClient.from_url(args.url, suites=[args.suite])
    try:
        docs = client.appstream(args.suite, args.component, "amd64")
    except MissingYamlError as exc:  # the message says how to install PyYAML
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (FetchError, Dep11Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{len(docs)} AppStream components in {args.suite}/{args.component}")
    for doc in docs[:10]:
        s = summarize(doc)  # locale-aware Name/Summary, defaults to "C"
        print(f"  {s.id} ({s.type}) package={s.package}: {s.name} - {s.summary}")
    return 0


# next: see 10_custom_fetcher_and_errors.py
if __name__ == "__main__":
    raise SystemExit(main())
