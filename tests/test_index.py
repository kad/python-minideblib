# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import logging

import pytest

from minideblib.compress import DEFAULT_MAX_SIZE, DecompressionLimitError
from minideblib.repo import index, release
from minideblib.repo.fetch import Fetcher, FetchError
from minideblib.repo.release import HashMismatch
from tests.fixtures.repo import make_repo

SUITE = "stable/slash-test/1.0"
PKG = "unified/binary-amd64/Packages"

LIVE = (
    "Package: a\nVersion: 1\nMulti-Arch: same\nBuild-Profiles: <!nocheck>\n"
    "Description-md5: d41d8cd98f00b204e9800998ecf8427e\n"
    "Depends: " + ", ".join(f"lib{i} (>= 1.{i})" for i in range(2000)) + "\n"
    "Description: short\n long line\n .\n more\n\n"
    "Package: b\nVersion: 2\n"
)


def test_load_packages_fields():
    paras = index.load_packages(LIVE.encode(), "Packages")
    assert [p["Package"] for p in paras] == ["a", "b"]
    assert paras[0]["Multi-Arch"] == "same"
    assert paras[0]["Build-Profiles"] == "<!nocheck>"
    assert paras[0]["Description-md5"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert len(paras[0]["Depends"]) > 20000


def test_load_packages_padded_gz(tmp_path):
    base = make_repo(tmp_path, packages=LIVE, pad_gz=True)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    entry = rel.files[PKG + ".gz"]
    raw = Fetcher().get(release.index_url(rel, entry))
    assert len(raw) % 10240 == 0
    assert [p["Package"] for p in index.load_packages(raw, entry.path)] == ["a", "b"]


def test_load_sources_empty():
    assert index.load_sources(b"", "Sources") == []


def test_load_sources_empty_gz(tmp_path):
    base = make_repo(tmp_path)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    entry = rel.files["unified/source/Sources.gz"]
    raw = Fetcher().get(release.index_url(rel, entry))
    assert index.load_sources(raw, entry.path) == []


def test_load_sources():
    data = b"Package: src\nVersion: 1\nFiles:\n abc 1 x.dsc\n"
    assert index.load_sources(data, "Sources")[0]["Package"] == "src"


def test_load_contents_with_header_and_multi():
    data = (
        b"FILE                    LOCATION\n"
        b"usr/bin/foo             utils/foo\n"
        b"usr/share/x             doc/a,libs/b,doc/c\n"
        b"\n"
        b"opt/with space/file     misc/pkg\n"
    )
    out = index.load_contents(data, "Contents-amd64")
    assert out == {
        "usr/bin/foo": ["utils/foo"],
        "usr/share/x": ["doc/a", "libs/b", "doc/c"],
        "opt/with space/file": ["misc/pkg"],
    }


def test_load_contents_no_header_merges_duplicates():
    out = index.load_contents(b"a  s/p\na  s/q\nlonely\n", "Contents-amd64")
    assert out == {"a": ["s/p", "s/q"]}


def test_load_contents_gz_fixture(tmp_path):
    base = make_repo(tmp_path, contents=True)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    entry = release.select_indices(rel, "unified", "amd64", "contents")[0]
    raw = index.fetch_index(Fetcher(), base_url=base, rel=rel, entry=entry)
    out = index.load_contents(raw, "Contents-amd64")
    assert out["usr/share/doc/sample-pkg/copyright"] == ["doc/sample-pkg"]


def _setup(tmp_path, **kw):
    base = make_repo(tmp_path, **kw)
    return base, release.fetch_release(Fetcher(), base, SUITE)


def test_fetch_index_ok(tmp_path):
    base, rel = _setup(tmp_path)
    data = index.fetch_index(Fetcher(), base, rel, rel.files[PKG + ".gz"])
    assert data.startswith(b"Package: sample-pkg")


def test_fetch_index_tampered(tmp_path):
    base, rel = _setup(tmp_path)
    (tmp_path / "dists" / SUITE / PKG).write_bytes(b"Package: evil\n")
    entry = rel.files[PKG]
    with pytest.raises(HashMismatch):
        index.fetch_index(Fetcher(), base, rel, entry)
    assert index.fetch_index(Fetcher(), base, rel, entry, verify=False) == b"Package: evil\n"


def test_fetch_index_404(tmp_path):
    base, rel = _setup(tmp_path)
    (tmp_path / "dists" / SUITE / PKG).unlink()
    with pytest.raises(FetchError):
        index.fetch_index(Fetcher(), base, rel, rel.files[PKG])


def test_fetch_index_max_size(tmp_path):
    base, rel = _setup(tmp_path)
    assert DEFAULT_MAX_SIZE > 100
    with pytest.raises(DecompressionLimitError):
        index.fetch_index(Fetcher(), base, rel, rel.files[PKG + ".gz"], max_size=10)


class _Counting(Fetcher):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return super().get(url, **kw)


def test_fetch_index_listed_size_over_the_fetcher_limit_refused_before_download(tmp_path):
    base, rel = _setup(tmp_path)
    entry = rel.files[PKG]
    assert entry.size > 10
    fetcher = _Counting(max_bytes=10)
    with pytest.raises(
        FetchError, match=rf"{PKG} is listed at {entry.size} bytes, over the limit of 10"
    ):
        index.fetch_index(fetcher, base, rel, entry)
    assert fetcher.urls == []
    # without verification the Release sizes are not trusted: the fetcher limit applies
    with pytest.raises(FetchError, match="exceeds 10 bytes"):
        index.fetch_index(fetcher, base, rel, entry, verify=False)
    assert len(fetcher.urls) == 1


def test_fetch_index_by_hash(tmp_path):
    base, rel = _setup(tmp_path, by_hash=True)
    assert index.fetch_index(Fetcher(), base, rel, rel.files[PKG + ".gz"])


class Recorder(Fetcher):
    def __init__(self, status=404):
        super().__init__()
        self.urls = []
        self.status = status

    def get(self, url, **kw):
        self.urls.append(url)
        if "/by-hash/SHA512/" in url and self.status != 404:
            raise FetchError("boom", url, self.status)
        return super().get(url, **kw)


def _only_sha256_by_hash(tmp_path):
    base, rel = _setup(tmp_path, by_hash=True)
    import shutil

    for p in (tmp_path / "dists" / SUITE).rglob("by-hash/SHA512"):
        shutil.rmtree(p)
    return base, rel


def test_by_hash_fallback_to_other_algo(tmp_path):
    base, rel = _only_sha256_by_hash(tmp_path)
    f = Recorder()
    data = index.fetch_index(f, base, rel, rel.files[PKG + ".gz"])
    assert data.startswith(b"Package:")
    assert "/by-hash/SHA512/" in f.urls[0]
    assert "/by-hash/SHA256/" in f.urls[1]
    assert len(f.urls) == 2


def test_by_hash_fallback_to_plain(tmp_path):
    import shutil

    base, rel = _setup(tmp_path, by_hash=True)
    for p in (tmp_path / "dists" / SUITE).rglob("by-hash"):
        shutil.rmtree(p)
    f = Recorder()
    assert index.fetch_index(f, base, rel, rel.files[PKG + ".gz"]).startswith(b"Package:")
    assert f.urls[-1].endswith(PKG + ".gz")
    assert len(f.urls) == 5


def test_by_hash_fallback_still_verifies(tmp_path):
    base, rel = _only_sha256_by_hash(tmp_path)
    entry = rel.files[PKG + ".gz"]
    p = tmp_path / "dists" / SUITE / "unified/binary-amd64/by-hash/SHA256" / entry.hashes["SHA256"]
    p.write_bytes(b"tampered")
    with pytest.raises(HashMismatch):
        index.fetch_index(Fetcher(), base, rel, entry)


def test_by_hash_non_404_propagates(tmp_path):
    base, rel = _setup(tmp_path, by_hash=True)
    f = Recorder(status=500)
    with pytest.raises(FetchError) as ei:
        index.fetch_index(f, base, rel, rel.files[PKG + ".gz"])
    assert ei.value.status == 500
    assert len(f.urls) == 1


def test_no_log_noise(tmp_path, caplog):
    base, rel = _setup(tmp_path)
    with caplog.at_level(logging.WARNING):
        index.fetch_index(Fetcher(), base, rel, rel.files[PKG])
    assert not caplog.records


def test_load_contents_splits_on_newline_only(caplog):
    data = "a\x0cb  s/p\nc d  s/q\n".encode()
    out = index.load_contents(data, "Contents-amd64")
    assert out == {"a\x0cb": ["s/p"], "c d": ["s/q"]}


def test_load_contents_warns_without_location(caplog):
    with caplog.at_level("WARNING"):
        out = index.load_contents(b"a  s/p\nlonely\n\n", "Contents-amd64")
    assert out == {"a": ["s/p"]}
    assert "lonely" in caplog.text
    assert caplog.text.count("WARNING") == 1


def test_load_packages_latin1_fallback(caplog):
    with caplog.at_level("WARNING"):
        out = index.load_packages(
            "Package: a\nMaintainer: Andr\xe9\n".encode("latin-1"), "Packages"
        )
    assert out[0]["Maintainer"] == "André"
    assert "latin-1" in caplog.text


@pytest.mark.parametrize("ws", ["\u00a0", "\u2028", "\x0c", "\x0b", "\u3000"])
def test_load_contents_separator_is_space_or_tab_only(ws, caplog):
    data = f"usr/a{ws}b s/p\nusr/c{ws}\ts/q\nusr/d{ws}  s/r\n".encode()
    assert index.load_contents(data, "Contents-amd64") == {
        f"usr/a{ws}b": ["s/p"],
        f"usr/c{ws}": ["s/q"],
        f"usr/d{ws}": ["s/r"],
    }
    with caplog.at_level("WARNING"):
        assert index.load_contents(f"usr/e{ws}s/p\n".encode(), "Contents-amd64") == {}
    assert "without a location" in caplog.text


def test_load_contents_tab_separator():
    assert index.load_contents(b"usr/a b\t\ts/p,t/q \n", "Contents-amd64") == {
        "usr/a b": ["s/p", "t/q"]
    }
