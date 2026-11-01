# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Live-repository tests; skipped unless MINIDEB_NETWORK=1.

Target: MINIDEB_TEST_URL/SUITE/COMPONENT/ARCH (see tests/conftest.py).
"""

import pytest

from minideblib.repo.client import RepoClient
from tests.conftest import live_target


@pytest.mark.network
def test_live_repo_components_and_packages():
    url, suite, comp, arch = live_target()
    c = RepoClient.from_url(url, suites=[suite])
    assert comp in c.components(suite)
    recs = c.packages(suite, comp, arch)
    assert recs
    base = url.rstrip("/") + "/"
    assert all(r.url.startswith(base) for r in recs)


@pytest.mark.network
def test_live_appstream():
    pytest.importorskip("yaml", reason="PyYAML not installed (extra 'dep11')")
    from minideblib.repo.release import select_indices

    url, suite, comp, arch = live_target()
    c = RepoClient.from_url(url, suites=[suite])
    if not select_indices(c.release(suite), comp, arch, "dep11"):
        pytest.skip(f"{url} {suite} {comp}/{arch} has no DEP-11 metadata in Release")
    docs = c.appstream(suite, comp, arch)
    assert any(d.get("ID") and d.get("Package") for d in docs)
    assert all(d["_suite"] == suite and d["_component"] == comp for d in docs)
