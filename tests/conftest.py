# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.repo import make_repo
from tests.mcp_client import RepoServer, Spawner


def pytest_collection_modifyitems(config, items):
    if os.environ.get("MINIDEB_NETWORK") == "1":
        return
    skip = pytest.mark.skip(reason="network test; set MINIDEB_NETWORK=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def live_target() -> tuple[str, str, str, str]:
    """(url, suite, component, arch) for live tests, overridable by env vars."""
    return (
        os.environ.get("MINIDEB_TEST_URL", "http://deb.debian.org/debian"),
        os.environ.get("MINIDEB_TEST_SUITE", "stable"),
        os.environ.get("MINIDEB_TEST_COMPONENT", "contrib"),
        os.environ.get("MINIDEB_TEST_ARCH", "amd64"),
    )


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Spawner]:
    """Start ``minideb`` server subprocesses (clean environment, cwd ``tmp_path/work``).

    Every child is killed and its pipes closed when the test ends.
    """
    spawner = Spawner(tmp_path)
    try:
        yield spawner
    finally:
        spawner.close()


#: suite and architectures of the ``mcp_site`` repository
MCP_SUITE = "stable"
MCP_ARCHES = ("amd64", "arm64", "all")


@pytest.fixture
def mcp_site(tmp_path: Path) -> Path:
    """A directory whose ``debian/`` is the multi-arch fixture repository (with sources)."""
    root = tmp_path / "site"
    make_repo(
        root / "debian", MCP_SUITE, comps=["main"], arches=MCP_ARCHES, multiarch=True, dsc=True
    )
    return root


@pytest.fixture
def mcp_http(mcp_site: Path) -> Iterator[RepoServer]:
    """``mcp_site`` served over HTTP by a thread (the repository: ``{url}/debian``)."""
    server = RepoServer(mcp_site)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def mcp_downloads(tmp_path: Path) -> Path:
    """An empty download directory."""
    path = tmp_path / "downloads"
    path.mkdir()
    return path
