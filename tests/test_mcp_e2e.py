# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""``minideb mcp`` end to end over real pipes and a repository served over HTTP.

The server runs as a subprocess (clean environment, hard timeouts, killed in
``finally``: the ``spawn`` fixture of ``conftest.py``); the multi-arch
fixture repository is served by a thread (``mcp_http``, a
:class:`~tests.mcp_client.RepoServer`).
Covered: the whole workflow (overview, search, package info, binary,
multi-arch and source downloads, inspection of the downloaded files),
cancelling a slow download, two sessions in one directory and a proxy given
by ``http_proxy``/``no_proxy`` in the child's environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from minideblib.mcp.safeio import TEMP_PREFIX
from tests.fixtures.repo import SRC_FILES, multiarch_deb, sample_source_files
from tests.mcp_client import (
    WAIT,
    ProxyServer,
    RepoServer,
    Spawner,
    error_text,
    structured,
)

SUITE = "stable"
ARCHES = ("amd64", "arm64", "all")


def _temps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob(f"{TEMP_PREFIX}*"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wait_for(predicate: Any, timeout: float = WAIT) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "condition not reached in time"
        time.sleep(0.01)


# -- the whole workflow ---------------------------------------------------------------------


def test_workflow(spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path) -> None:
    url = f"{mcp_http.url}/debian"
    child = spawn("--url", url, "mcp", "--download-dir", str(mcp_downloads))
    client = child.client
    client.initialize()

    overview = structured(client.call("repository_overview", {"suites": [SUITE]}))
    assert overview["repositories"] == [{"name": "repo1", "url": url, "suites": []}]
    [suite] = overview["suites"]
    assert suite["suite"] == SUITE
    assert suite["architectures"] == list(ARCHES)

    found = structured(client.call("search_packages", {"suite": SUITE, "pattern": "*sample*"}))
    rows = {(p["name"], p["version"]): p for p in found["packages"]}
    assert set(rows) >= {
        ("sample-tool", "1.1-1"),
        ("sample-tool", "1.0-1"),
        ("libsample1", "1.1-1"),
        ("sample-data", "1.1-1"),
    }
    lib = rows[("libsample1", "1.1-1")]
    assert lib["multi_arch"] == "same"
    assert {"amd64", "arm64"} <= set(lib["architectures"])
    data_row = rows[("sample-data", "1.1-1")]
    assert data_row["architectures"] == ["all"]  # one entry, though in every index

    info = structured(
        client.call(
            "package_info",
            {"suite": SUITE, "name": "libsample1", "architectures": ["amd64", "arm64"]},
        )
    )
    [group] = [g for g in info["packages"] if set(g["architectures"]) >= {"amd64", "arm64"}]
    variants = {v["architecture"]: v for v in group["variants"]}
    for arch in ("amd64", "arm64"):
        deb = multiarch_deb("libsample1", "1.1-1", arch)
        assert variants[arch]["sha256"] == _sha256(deb)
        assert variants[arch]["size"] == len(deb)
    assert variants["amd64"]["sha256"] != variants["arm64"]["sha256"]

    # binary: the Architecture: all package is one candidate
    got = structured(client.call("download_package", {"suite": SUITE, "name": "sample-data"}))
    [entry] = got["files"]
    assert (entry["status"], entry["architecture"]) == ("downloaded", "all")
    deb_all = multiarch_deb("sample-data", "1.1-1", "all")
    assert (mcp_downloads / entry["name"]).read_bytes() == deb_all
    assert entry["hashes"]["sha256"] == _sha256(deb_all)

    # multi-arch: several candidates need a choice; each architecture is reported
    many = client.call("download_package", {"suite": SUITE, "name": "libsample1"})
    assert "pass 'architectures'" in error_text(many)
    got = structured(
        client.call(
            "download_package",
            {
                "suite": SUITE,
                "name": "libsample1",
                "architectures": ["amd64", "arm64"],
                "subdirectory": "multi/arch",
            },
        )
    )
    by_arch = {f["architecture"]: f for f in got["files"]}
    assert {"amd64", "arm64"} <= set(by_arch)
    for arch in ("amd64", "arm64"):
        f = by_arch[arch]
        assert f["status"] == "downloaded"
        assert f["path"] == f"multi/arch/{f['name']}"
        path = Path(f["absolute_path"])
        assert path.parent == mcp_downloads / "multi" / "arch"
        assert path.read_bytes() == multiarch_deb("libsample1", "1.1-1", arch)
    assert got["complete"] is True

    # source: the .dsc first, then every file it lists
    got = structured(
        client.call(
            "download_package",
            {"suite": SUITE, "name": "sample-pkg", "kind": "source", "subdirectory": "src"},
        )
    )
    names = [f["name"] for f in got["files"]]
    expected = sample_source_files()
    assert names[0].endswith(".dsc")
    assert set(names) == set(expected)
    for f in got["files"]:
        assert f["status"] == "downloaded"
        assert Path(f["absolute_path"]).read_bytes() == expected[f["name"]]

    # inspect what was downloaded
    arm = by_arch["arm64"]
    deb = structured(client.call("inspect_deb", {"path": arm["absolute_path"]}))
    control = dict(deb["control"])
    assert (control["Package"], control["Architecture"]) == ("libsample1", "arm64")
    assert "usr/share/libsample1/arm64.txt" in {f["path"] for f in deb["files"]}
    payload = structured(
        client.call(
            "inspect_deb",
            {"path": arm["absolute_path"], "read_file": "usr/share/libsample1/arm64.txt"},
        )
    )
    assert payload["member"]["content"].startswith("libsample1 1.1-1 arm64\n")

    dsc_path = next(f["absolute_path"] for f in got["files"] if f["name"].endswith(".dsc"))
    dsc = structured(client.call("inspect_dsc", {"path": dsc_path, "verify": True}))
    assert dsc["source"] == "sample-pkg"
    assert {f["name"] for f in dsc["files"]} == set(SRC_FILES)
    verification = dsc["verification"]
    assert verification["ok"] is True
    assert {f["status"] for f in verification["files"]} == {"ok"}

    assert _temps(mcp_downloads) == []
    # every request went to the repository layout
    assert all(p.startswith("/debian/") for p in mcp_http.paths), mcp_http.paths
    child.stop()
    child.assert_pure_stdout()


# -- cancellation -----------------------------------------------------------------------------


def test_cancelled_slow_download_leaves_the_directory_clean(
    spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path
) -> None:
    mcp_http.trickle["sample-data_1.1-1_all.deb"] = 0.02  # about 20 s for the whole file
    child = spawn("--url", f"{mcp_http.url}/debian", "mcp", "--download-dir", str(mcp_downloads))
    client = child.client
    client.initialize()
    rid = client.send_request(
        "tools/call",
        {"name": "download_package", "arguments": {"suite": SUITE, "name": "sample-data"}},
    )
    # the body is arriving and is being written under a temporary name
    _wait_for(lambda: mcp_http.started.is_set() and _temps(mcp_downloads))
    start = time.monotonic()
    client.notify("notifications/cancelled", {"requestId": rid, "reason": "test"})
    # calls run one at a time: this answer comes once the cancelled call has ended
    after = client.request("tools/call", {"name": "repository_overview", "arguments": {}})
    assert time.monotonic() - start < 5.0
    assert "result" in after and not after["result"].get("isError")
    assert client.no_message(0.3)  # never an answer to the cancelled call
    assert _temps(mcp_downloads) == []
    assert list(mcp_downloads.iterdir()) == []
    child.stop()
    child.assert_pure_stdout()
    assert rid not in {json.loads(line).get("id") for line in client.raw}


# -- sessions ---------------------------------------------------------------------------------


def test_two_sessions_in_one_directory(
    spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path
) -> None:
    args = ("--url", f"{mcp_http.url}/debian", "mcp", "--download-dir", str(mcp_downloads))
    request = {"suite": SUITE, "name": "sample-data"}

    first = spawn(*args)
    first.client.initialize()
    [entry] = structured(first.client.call("download_package", request))["files"]
    assert entry["status"] == "downloaded"
    first.stop()
    path = mcp_downloads / entry["name"]
    before = path.stat()

    second = spawn(*args)
    second.client.initialize()
    got = structured(second.client.call("download_package", request))
    [again] = got["files"]
    assert again["status"] == "already_present"
    # checked against the strongest hash of the index (here SHA512)
    [(algorithm, digest)] = again["hashes"].items()
    assert entry["hashes"][algorithm] == digest
    assert got["stale_temp_files_removed"] == 0
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    # the second session downloads what the first did not
    got = structured(
        second.client.call(
            "download_package",
            {"suite": SUITE, "name": "libsample1", "architectures": ["arm64"]},
        )
    )
    assert [f["status"] for f in got["files"] if f["architecture"] == "arm64"] == ["downloaded"]
    second.stop()
    assert _temps(mcp_downloads) == []


# -- proxy ------------------------------------------------------------------------------------

PROXY_USER = "proxyuser"
PROXY_SECRET = "Pr0xy-s3cret-77"
FAKE_HOST = "deb.example.test"


def test_proxy_from_the_environment(
    spawn: Spawner, mcp_site: Path, mcp_http: RepoServer, mcp_downloads: Path
) -> None:
    proxy = ProxyServer({f"http://{FAKE_HOST}": mcp_site})
    try:
        proxy_url = f"http://{PROXY_USER}:{PROXY_SECRET}@127.0.0.1:{proxy.port}"
        env = {"http_proxy": proxy_url, "no_proxy": "localhost,127.0.0.1"}
        repo = f"http://{FAKE_HOST}/debian"
        child = spawn("--url", repo, "mcp", "--download-dir", str(mcp_downloads), env=env)
        client = child.client
        client.initialize()

        overview = structured(client.call("repository_overview", {"suites": [SUITE]}))
        assert overview["proxies"]["http"] == f"http://127.0.0.1:{proxy.port}"
        assert overview["proxies"]["no"] == "localhost,127.0.0.1"
        assert overview["suites"][0]["architectures"] == list(ARCHES)

        found = structured(
            client.call("search_packages", {"suite": SUITE, "pattern": "libsample1"})
        )
        assert [p["name"] for p in found["packages"]] == ["libsample1"]
        got = structured(client.call("download_package", {"suite": SUITE, "name": "sample-data"}))
        [entry] = got["files"]
        assert entry["status"] == "downloaded"
        assert entry["url"].startswith(f"{repo}/pool/")
        deb = multiarch_deb("sample-data", "1.1-1", "all")
        assert (mcp_downloads / entry["name"]).read_bytes() == deb

        # the proxy saw absolute-URI requests for the fake host, with the credentials
        assert proxy.lines, "the proxy saw no request"
        for line in proxy.lines:
            assert line.startswith(f"GET http://{FAKE_HOST}/debian/"), line
        assert any("/pool/main/s/sample-data/" in line for line in proxy.lines)
        token = base64.b64encode(f"{PROXY_USER}:{PROXY_SECRET}".encode()).decode()
        assert set(proxy.auth) == {f"Basic {token}"}

        # no_proxy: a local repository given per call is reached directly
        local = f"{mcp_http.url}/debian"
        seen = len(proxy.lines)
        direct = structured(
            client.call(
                "search_packages", {"repository": local, "suite": SUITE, "pattern": "sample-tool"}
            )
        )
        assert {p["name"] for p in direct["packages"]} == {"sample-tool"}
        assert len(proxy.lines) == seen
        assert any(p.startswith("/debian/dists/") for p in mcp_http.paths)

        child.stop()
        child.assert_pure_stdout()
        out = b"".join(client.raw)
        for secret in (PROXY_SECRET, token):
            assert secret.encode() not in out
            assert secret not in child.stderr
    finally:
        proxy.close()


# -- corrupt downloads --------------------------------------------------------------------------


def _files(directory: Path) -> list[str]:
    """Every entry below *directory* but real directories (relative paths)."""
    return sorted(
        str(p.relative_to(directory))
        for p in directory.rglob("*")
        if p.is_symlink() or not p.is_dir()
    )


def _tampered(data: bytes) -> bytes:
    """*data* with its last byte changed (same size, other content)."""
    return data[:-1] + bytes([data[-1] ^ 0xFF])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_tampered, id="same-size"),
        pytest.param(lambda d: d + b"\0" * 7, id="longer"),
        pytest.param(lambda d: d[:-7], id="shorter"),
    ],
)
def test_corrupt_binary_is_never_published(
    spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path, body: Any
) -> None:
    name = "sample-data_1.1-1_all.deb"
    mcp_http.replace[name] = body(multiarch_deb("sample-data", "1.1-1", "all"))
    child = spawn("--url", f"{mcp_http.url}/debian", "mcp", "--download-dir", str(mcp_downloads))
    child.client.initialize()
    resp = child.client.call("download_package", {"suite": SUITE, "name": "sample-data"})
    text = error_text(resp)
    assert "!= expected" in text or "exceeds" in text, text
    assert any(p.endswith(name) for p in mcp_http.paths)  # it was fetched
    child.stop()
    assert _files(mcp_downloads) == []


def test_corrupt_architecture_is_a_per_file_error(
    spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path
) -> None:
    arm = "libsample1_1.1-1_arm64.deb"
    mcp_http.replace[arm] = _tampered(multiarch_deb("libsample1", "1.1-1", "arm64"))
    child = spawn("--url", f"{mcp_http.url}/debian", "mcp", "--download-dir", str(mcp_downloads))
    child.client.initialize()
    got = structured(
        child.client.call(
            "download_package",
            {"suite": SUITE, "name": "libsample1", "architectures": ["amd64", "arm64"]},
        )
    )
    by_arch = {f["architecture"]: f for f in got["files"]}
    assert by_arch["arm64"]["status"] == "error"
    assert "!= expected" in by_arch["arm64"]["message"]
    assert by_arch["amd64"]["status"] == "downloaded"
    assert got["complete"] is False
    child.stop()
    assert arm not in _files(mcp_downloads)
    amd = "libsample1_1.1-1_amd64.deb"
    assert (mcp_downloads / amd).read_bytes() == multiarch_deb("libsample1", "1.1-1", "amd64")
    assert not [f for f in _files(mcp_downloads) if TEMP_PREFIX in f]


@pytest.mark.parametrize("member", sorted(sample_source_files()))
def test_corrupt_source_member_publishes_nothing(
    spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path, member: str
) -> None:
    mcp_http.replace[member] = _tampered(sample_source_files()[member])
    child = spawn("--url", f"{mcp_http.url}/debian", "mcp", "--download-dir", str(mcp_downloads))
    child.client.initialize()
    resp = child.client.call(
        "download_package", {"suite": SUITE, "name": "sample-pkg", "kind": "source"}
    )
    assert "!= expected" in error_text(resp)
    child.stop()
    assert _files(mcp_downloads) == []  # all or none: not even the intact files


# -- symlinks in the target path ----------------------------------------------------------------


def test_symlinked_subdirectory_is_never_followed(
    spawn: Spawner, mcp_http: RepoServer, mcp_downloads: Path, tmp_path: Path
) -> None:
    # deterministic: the subdirectory exists already as a symlink to another directory;
    # mkdir finds it taken and only an O_NOFOLLOW open refuses to enter it
    outside = tmp_path / "outside"
    outside.mkdir()
    (mcp_downloads / "debs").symlink_to(outside, target_is_directory=True)
    child = spawn("--url", f"{mcp_http.url}/debian", "mcp", "--download-dir", str(mcp_downloads))
    child.client.initialize()
    resp = child.client.call(
        "download_package", {"suite": SUITE, "name": "sample-data", "subdirectory": "debs"}
    )
    assert "symlink, which is refused" in error_text(resp)
    child.stop()
    assert list(outside.iterdir()) == []
    assert _files(mcp_downloads) == ["debs"]  # the symlink itself, untouched
