# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Protocol compatibility of ``minideb mcp``, end to end.

Version negotiation over every supported protocol version (and unsupported
ones) through a subprocess, and, when the official ``mcp`` Python SDK is
installed (it is not a dependency: the test is skipped otherwise), a
session driven by the SDK's own stdio client.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

import minideblib
from minideblib.mcp.protocol import SUPPORTED_VERSIONS
from tests.mcp_client import Spawner, child_env, skip_as_root, structured

SUITE = "stable"
#: oldest SDK release tested: 1.10.0 added protocol 2025-06-18 and structuredContent
#: (StdioServerParameters.cwd and stdio_client(errlog=) are older)
SDK_MINIMUM = (1, 10, 0)


def _release(text: str) -> tuple[int, ...]:
    """The leading numeric release of a version string (``"1.12.3rc1"`` -> ``(1, 12, 3)``)."""
    match = re.match(r"\d+(?:\.\d+)*", text)
    return tuple(int(x) for x in match.group().split(".")) if match else (0,)


def test_the_supported_versions() -> None:
    assert SUPPORTED_VERSIONS == ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_version_negotiation(spawn: Spawner, mcp_site: Path, version: str) -> None:
    child = spawn("--url", (mcp_site / "debian").as_uri(), "mcp")
    init = child.client.initialize(version)
    assert init["protocolVersion"] == version
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"] == {"name": "minideb", "version": minideblib.__version__}

    resp = child.client.call("repository_overview", {"suites": [SUITE]})
    result = resp["result"]
    assert result.get("isError") is not True, result
    [content] = result["content"]
    assert content["type"] == "text"
    data = json.loads(content["text"])
    assert data["suites"][0]["suite"] == SUITE
    if version >= "2025-06-18":
        assert result["structuredContent"] == data
    else:
        assert "structuredContent" not in result

    # batches are refused whatever the version (2025-03-26 allowed them)
    child.client.send([{"jsonrpc": "2.0", "id": 50, "method": "ping"}])
    msg = child.client.recv()
    assert (msg["id"], msg["error"]["code"]) == (None, -32600)
    assert child.client.request("ping")["result"] == {}
    child.stop()
    child.assert_pure_stdout()


@pytest.mark.parametrize("version", ["2099-01-01", "2024-10-07", "latest"])
def test_unsupported_version_gets_the_newest(spawn: Spawner, version: str) -> None:
    child = spawn("mcp")
    init = child.client.initialize(version)
    assert init["protocolVersion"] == SUPPORTED_VERSIONS[0]
    tools = child.client.request("tools/list")["result"]["tools"]
    assert "repository_overview" in {t["name"] for t in tools}
    overview = structured(child.client.call("repository_overview"))
    assert overview["repositories"] == []
    child.stop()


def test_official_sdk_client(tmp_path: Path, mcp_site: Path) -> None:
    """The official ``mcp`` SDK's stdio client: initialize, list tools, call tools."""
    skip_as_root()
    sdk = pytest.importorskip("mcp")
    stdio = pytest.importorskip("mcp.client.stdio")
    installed = _release(importlib.metadata.version("mcp"))
    if installed < SDK_MINIMUM:
        pytest.skip(f"mcp SDK {installed} is older than {SDK_MINIMUM}")
    work = tmp_path / "work"
    work.mkdir()
    repo = (mcp_site / "debian").as_uri()
    params = sdk.StdioServerParameters(
        command=sys.executable,
        args=["-P", "-m", "minideblib", "--url", repo, "mcp", "--no-download"],
        env=child_env(tmp_path),
        cwd=str(work),
    )

    errlog_path = tmp_path / "server-stderr.log"

    async def session() -> dict[str, Any]:
        with errlog_path.open("w") as errlog:
            async with (
                stdio.stdio_client(params, errlog=errlog) as (read, write),
                sdk.ClientSession(read, write) as client,
            ):
                return await _drive(client)

    async def _drive(client: Any) -> dict[str, Any]:
        try:
            init = await client.initialize()
        except RuntimeError as exc:  # the SDK refuses the negotiated version
            if "protocol version" not in str(exc).lower():
                raise
            return {"skip": str(exc)}
        listed = await client.list_tools()
        good = await client.call_tool("repository_overview", {"suites": [SUITE]})
        bad = await client.call_tool("search_packages", {"suite": 5, "pattern": "x"})
        missing = await client.call_tool(
            "package_info", {"suite": SUITE, "name": "no-such-package"}
        )
        return {
            "init": init,
            "tools": [t.name for t in listed.tools],
            "good": good,
            "bad": bad,
            "missing": missing,
        }

    async def bounded() -> dict[str, Any]:
        return await asyncio.wait_for(session(), 60)

    got = asyncio.run(bounded())
    if "skip" in got:
        pytest.skip(f"the installed mcp SDK does not speak our protocol versions: {got['skip']}")
    log = errlog_path.read_text(errors="replace")
    assert "Traceback" not in log
    init = got["init"]
    assert init.serverInfo.name == "minideb"
    assert init.serverInfo.version == minideblib.__version__
    assert init.protocolVersion in SUPPORTED_VERSIONS
    assert "untrusted" in (init.instructions or "")
    assert {"repository_overview", "search_packages", "inspect_deb"} <= set(got["tools"])
    assert "download_package" not in got["tools"]

    good = got["good"]
    assert not good.isError
    [content] = good.content
    data = json.loads(content.text)
    assert data["suites"][0]["suite"] == SUITE
    if getattr(good, "structuredContent", None) is not None:
        assert good.structuredContent == data

    for failed in (got["bad"], got["missing"]):
        assert failed.isError is True
        assert failed.content[0].text


def test_sdk_release_parsing() -> None:
    assert _release("1.10.0") == (1, 10, 0)
    assert _release("1.9.4") < SDK_MINIMUM <= _release("1.12.3rc1")
    assert _release("garbage") == (0,)
