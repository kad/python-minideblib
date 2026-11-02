# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""MCP client (stdlib only): start `minideb mcp`, list the tools, call two, end it with EOF.

Run: uv run python examples/12_mcp_client.py [URL SUITE [COMPONENT]] [--pattern TEXT]
"""

import argparse
import json
import subprocess
import sys
from typing import Any


def rpc(proc: subprocess.Popen[str], n: int | None, method: str, params: dict[str, Any]) -> Any:
    assert proc.stdin and proc.stdout
    msg = {"jsonrpc": "2.0", "method": method, "params": params, **({} if n is None else {"id": n})}
    print(json.dumps(msg), file=proc.stdin, flush=True)  # one message per line
    for line in proc.stdout if n is not None else ():  # n None: a notification, no reply
        reply = json.loads(line)
        if reply.get("id") == n:
            if "error" in reply:
                raise RuntimeError(reply["error"]["message"])
            return reply["result"]
    if n is not None:
        raise RuntimeError("the server closed its output")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MCP client: URL SUITE [COMPONENT].")
    ap.add_argument("url", nargs="?", default="https://deb.debian.org/debian")
    ap.add_argument("suite", nargs="?", default="stable")
    ap.add_argument("component", nargs="?", default="contrib")
    ap.add_argument("--pattern", default="game", help="substring or glob of package names")
    args = ap.parse_args(argv)
    cmd = [sys.executable, "-m", "minideblib", "mcp", "--no-download"]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
        try:
            who = {"name": "example", "version": "1"}
            rpc(proc, 1, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": who})
            rpc(proc, None, "notifications/initialized", {})  # then the session is ready
            print("tools:", ", ".join(t["name"] for t in rpc(proc, 2, "tools/list", {})["tools"]))
            find = {"suite": args.suite, "pattern": args.pattern, "components": [args.component]}
            calls = [("repository_overview", {"suites": [args.suite]}), ("search_packages", find)]
            for n, (name, extra) in enumerate(calls, 3):
                call = {"name": name, "arguments": {"repository": args.url, **extra}}
                res = rpc(proc, n, "tools/call", call)
                print(f"{name}:", res["content"][0]["text"][:400])
                if res.get("isError"):
                    return 1
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


# next: see README.md for sample output of every example
if __name__ == "__main__":
    raise SystemExit(main())
