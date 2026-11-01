# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import os

import pytest


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
