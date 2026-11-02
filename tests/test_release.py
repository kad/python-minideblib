# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import gzip
import hashlib
import logging
from datetime import UTC, datetime

import pytest

from minideblib.control import Paragraph, parse_paragraphs, strip_clearsign
from minideblib.repo import release
from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import Fetcher, FetchError
from minideblib.repo.release import HashMismatch, IndexFile, Release, ReleaseError
from tests.fixtures.repo import clearsign, make_repo

SUITE = "stable/slash-test/1.0"
PKG_AMD64 = "unified/binary-amd64/Packages"


class RecordingFetcher(Fetcher):
    def __init__(self, missing=(), fail=()):
        super().__init__()
        self.urls = []
        self.missing = missing
        self.fail = fail

    def get(self, url, **kw):
        self.urls.append(url)
        if any(url.endswith(m) for m in self.missing):
            raise FetchError(f"HTTP 404 fetching {url}", url, 404)
        if any(url.endswith(m) for m in self.fail):
            raise FetchError(f"HTTP 500 fetching {url}", url, 500)
        return super().get(url, **kw)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def _get(base, rel, path):
    return Fetcher().get(release.index_url(rel, rel.files[path]))


# --- fixture sanity ---------------------------------------------------------


def test_fixture_layout(tmp_path):
    base = make_repo(tmp_path)
    assert base.startswith("file://")
    d = tmp_path / "dists" / SUITE
    assert (d / "InRelease").is_file() and (d / "Release").is_file()
    assert (d / "unified/source/Sources").read_bytes() == b""
    assert len((d / f"{PKG_AMD64}.gz").read_bytes()) % 10240 == 0
    assert gzip.decompress((d / f"{PKG_AMD64}.gz").read_bytes()) == (d / PKG_AMD64).read_bytes()
    text = (d / "InRelease").read_text()
    assert text.startswith("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n")
    assert strip_clearsign(text) == (d / "Release").read_text()


def test_clearsign_dash_escapes():
    signed = clearsign("A: 1\n-B: 2\n")
    assert "\n- -B: 2\n" in signed
    assert strip_clearsign(signed) == "A: 1\n-B: 2\n"


# --- URLs ----------------------------------------------------------------------


def test_dists_url_variants():
    assert release.dists_url("http://x/ubuntu/", SUITE) == f"http://x/ubuntu/dists/{SUITE}"
    assert release.dists_url("http://x/ubuntu", "bookworm") == "http://x/ubuntu/dists/bookworm"
    assert release.dists_url("http://x/repo/", "./") == "http://x/repo"
    assert release.dists_url("http://x/repo", "sub/") == "http://x/repo/sub"
    assert release.dists_url("http://x/repo", "./sub/dir/") == "http://x/repo/sub/dir"


# --- fetch_release -----------------------------------------------------------


def test_slash_suite_uses_inrelease(repo):
    f = RecordingFetcher()
    rel = release.fetch_release(f, repo, SUITE)
    assert f.urls == [f"{repo}/dists/{SUITE}/InRelease"]
    assert rel.codename == SUITE
    assert rel.suite_field is None
    assert rel.components == ["unified"]
    assert rel.architectures == ["amd64", "i386"]
    assert rel.acquire_by_hash is False
    assert rel.valid_until is None
    assert isinstance(rel.fields, Paragraph)
    assert rel.fields["Origin"] == "Example"


def test_trailing_slash_base_url(repo):
    f = RecordingFetcher()
    release.fetch_release(f, repo + "/", SUITE)
    assert f.urls == [f"{repo}/dists/{SUITE}/InRelease"]


def test_inrelease_404_falls_back_to_release(repo):
    f = RecordingFetcher(missing=("InRelease",))
    rel = release.fetch_release(f, repo, SUITE)
    assert f.urls[-1] == f"{repo}/dists/{SUITE}/Release"
    assert rel.codename == SUITE


def test_inrelease_missing_file_falls_back(tmp_path):
    base = make_repo(tmp_path, inrelease=False)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    assert PKG_AMD64 in rel.files


def test_inrelease_other_error_propagates(repo):
    f = RecordingFetcher(fail=("InRelease",))
    with pytest.raises(FetchError) as ei:
        release.fetch_release(f, repo, SUITE)
    assert ei.value.status == 500
    assert len(f.urls) == 1


def test_both_missing_raises(tmp_path):
    base = make_repo(tmp_path, inrelease=False, release=False)
    with pytest.raises(FetchError):
        release.fetch_release(Fetcher(), base, SUITE)


def test_flat_repo(tmp_path):
    base = make_repo(tmp_path, flat=True)
    f = RecordingFetcher()
    rel = release.fetch_release(f, base, "./")
    assert f.urls == [f"{base}/InRelease"]
    assert rel.components == []
    assert release.index_url(rel, rel.files["Packages.gz"]) == f"{base}/Packages.gz"
    [idx] = release.select_indices(rel, "", "amd64", "binary")[:1]
    assert idx.path == "Packages.gz"
    data = Fetcher().get(release.index_url(rel, idx))
    rel.verify(idx.path, data)


def test_flat_repo_subdir(tmp_path):
    base = make_repo(tmp_path, suite="sub/", flat=True)
    f = RecordingFetcher()
    rel = release.fetch_release(f, base, "sub/")
    assert f.urls == [f"{base}/sub/InRelease"]
    assert "Sources" in rel.files


def test_expired_valid_until_warns(tmp_path, caplog):
    base = make_repo(tmp_path, valid_until="Mon, 01 Jan 2001 00:00:00 UTC")
    with caplog.at_level(logging.WARNING, logger="minideblib.repo.release"):
        rel = release.fetch_release(Fetcher(), base, SUITE)
    assert rel.valid_until == datetime(2001, 1, 1, tzinfo=UTC)
    assert any("expired" in r.getMessage() for r in caplog.records)


def test_expired_valid_until_no_warning_when_disabled(tmp_path, caplog):
    base = make_repo(tmp_path, valid_until="Mon, 01 Jan 2001 00:00:00 UTC")
    with caplog.at_level(logging.WARNING, logger="minideblib.repo.release"):
        release.fetch_release(Fetcher(), base, SUITE, verify_expiry=False)
    assert not caplog.records


def test_future_valid_until_no_warning(tmp_path, caplog):
    base = make_repo(tmp_path, valid_until="Fri, 01 Jan 2100 00:00:00 UTC")
    with caplog.at_level(logging.WARNING, logger="minideblib.repo.release"):
        rel = release.fetch_release(Fetcher(), base, SUITE)
    assert rel.valid_until is not None
    assert rel.valid_until.year == 2100
    assert not caplog.records


def test_bad_valid_until_does_not_raise(tmp_path):
    base = make_repo(tmp_path, valid_until="whenever")
    rel = release.fetch_release(Fetcher(), base, SUITE)
    assert rel.valid_until is None


# --- parsing -------------------------------------------------------------------


def test_minimal_release_missing_optional_fields():
    rel = Release.parse("Origin: x\n")
    assert rel.codename is None
    assert rel.suite_field is None
    assert rel.components == []
    assert rel.architectures == []
    assert rel.acquire_by_hash is False
    assert rel.valid_until is None
    assert rel.date is None
    assert rel.files == {}


def test_parse_clearsigned_text(tmp_path):
    make_repo(tmp_path)
    text = (tmp_path / "dists" / SUITE / "InRelease").read_text()
    rel = Release.parse(text)
    assert rel.codename == SUITE
    assert set(rel.files[PKG_AMD64].hashes) == {"MD5Sum", "SHA1", "SHA256", "SHA512"}


def test_empty_release_raises():
    with pytest.raises(ReleaseError):
        Release.parse("")


def test_suite_field_and_by_hash():
    rel = Release.parse("Suite: stable\nCodename: bookworm\nAcquire-By-Hash: yes\n")
    assert rel.suite_field == "stable"
    assert rel.codename == "bookworm"
    assert rel.acquire_by_hash is True


def test_malformed_hash_line_raises():
    with pytest.raises(ReleaseError):
        Release.parse("SHA256:\n abc notasize main/Packages\n")


def test_oversized_size_raises_release_error():
    with pytest.raises(ReleaseError, match="malformed SHA256"):
        Release.parse("SHA256:\n abc " + "1" * 5000 + " main/Packages\n")


def test_index_files(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    idx = rel.files[PKG_AMD64 + ".gz"]
    assert isinstance(idx, IndexFile)
    assert idx.size % 10240 == 0
    assert set(idx.hashes) == {"MD5Sum", "SHA1", "SHA256", "SHA512"}
    assert "unified/source/Sources" in rel.files
    assert rel.files["unified/source/Sources"].size == 0


# --- hashes --------------------------------------------------------------------


def test_best_hash_prefers_sha512(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    algo, digest = rel.best_hash(PKG_AMD64)
    assert algo == "SHA512"
    assert digest == rel.files[PKG_AMD64].hashes["SHA512"]


@pytest.mark.parametrize(
    ("algos", "best"),
    [(("MD5Sum", "SHA1"), "SHA1"), (("MD5Sum",), "MD5Sum"), (("SHA1", "SHA256"), "SHA256")],
)
def test_best_hash_picks_strongest(tmp_path, algos, best):
    base = make_repo(tmp_path, hashes=algos)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    assert rel.best_hash(PKG_AMD64)[0] == best


@pytest.mark.parametrize("algos", [("MD5Sum", "SHA1"), ("MD5Sum",), ("SHA1",)])
def test_weak_hashes_only_rejected(tmp_path, algos):
    base = make_repo(tmp_path, hashes=algos)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    with pytest.raises(HashMismatch, match="no SHA256 or stronger hash listed in Release"):
        rel.verify(PKG_AMD64, _get(base, rel, PKG_AMD64))


@pytest.mark.parametrize("algos", [("MD5Sum", "SHA256"), ("SHA1", "SHA256"), ("MD5Sum", "SHA512")])
def test_strong_hash_alongside_weak_verifies(tmp_path, algos):
    base = make_repo(tmp_path, hashes=algos)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    rel.verify(PKG_AMD64, _get(base, rel, PKG_AMD64))
    with pytest.raises(HashMismatch):
        rel.verify(PKG_AMD64, b"x" + _get(base, rel, PKG_AMD64)[1:])


def test_weak_hashes_only_bypassed_without_verify(tmp_path):
    base = make_repo(tmp_path, comps=("main",), hashes=("MD5Sum",))
    with pytest.raises(HashMismatch, match="SHA256 or stronger"):
        RepoClient.from_url(base, suites=[SUITE]).packages(SUITE)
    assert RepoClient.from_url(base, suites=[SUITE], verify=False).packages(SUITE)


def test_verify_ok(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    for path in (PKG_AMD64, PKG_AMD64 + ".gz", "unified/source/Sources"):
        rel.verify(path, _get(repo, rel, path))


def test_verify_altered_bytes(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    data = bytearray(_get(repo, rel, PKG_AMD64))
    data[0] ^= 1
    with pytest.raises(HashMismatch) as ei:
        rel.verify(PKG_AMD64, bytes(data))
    assert PKG_AMD64 in str(ei.value)
    assert isinstance(ei.value, ReleaseError)


def test_verify_altered_size(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    with pytest.raises(HashMismatch, match="size"):
        rel.verify(PKG_AMD64, _get(repo, rel, PKG_AMD64) + b"\n")


def test_verify_unknown_path(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    with pytest.raises(ReleaseError) as ei:
        rel.verify("nope/Packages", b"")
    assert isinstance(ei.value, KeyError)
    assert not isinstance(ei.value, HashMismatch)
    with pytest.raises(ReleaseError):
        rel.best_hash("nope/Packages")


def test_verify_md5_only_hashes():
    data = b"hello\n"
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    rel = Release.parse(f"MD5Sum:\n {md5} {len(data)} main/Packages\n")
    with pytest.raises(HashMismatch, match="main/Packages: no SHA256 or stronger"):
        rel.verify("main/Packages", data)


# --- by-hash -------------------------------------------------------------------


def test_index_url_plain(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    assert (
        release.index_url(rel, rel.files[PKG_AMD64 + ".gz"])
        == f"{repo}/dists/{SUITE}/{PKG_AMD64}.gz"
    )
    assert release.index_url(rel, PKG_AMD64) == f"{repo}/dists/{SUITE}/{PKG_AMD64}"


def test_index_url_by_hash(tmp_path):
    base = make_repo(tmp_path, by_hash=True)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    assert rel.acquire_by_hash is True
    idx = rel.files[PKG_AMD64 + ".gz"]
    url = release.index_url(rel, idx)
    assert url == (
        f"{base}/dists/{SUITE}/unified/binary-amd64/by-hash/SHA512/{idx.hashes['SHA512']}"
    )
    rel.verify(idx.path, Fetcher().get(url))


def test_index_url_by_hash_weak_algo_name():
    rel = Release.parse(
        "Acquire-By-Hash: yes\nMD5Sum:\n d41d8cd98f00b204e9800998ecf8427e 0 main/source/Sources\n",
        dists_url="http://x/dists/s",
    )
    assert release.index_url(rel, "main/source/Sources") == (
        "http://x/dists/s/main/source/by-hash/MD5Sum/d41d8cd98f00b204e9800998ecf8427e"
    )


def test_index_url_by_hash_top_level():
    rel = Release.parse(
        "Acquire-By-Hash: yes\nSHA256:\n " + "a" * 64 + " 0 Contents-amd64.gz\n",
        dists_url="http://x/dists/s",
    )
    assert release.index_url(rel, "Contents-amd64.gz") == (
        "http://x/dists/s/by-hash/SHA256/" + "a" * 64
    )


# --- select_indices -------------------------------------------------------------


def _paths(rel, comp, arch, kind):
    return [i.path for i in release.select_indices(rel, comp, arch, kind)]


def test_select_gz_when_only_gz(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    assert _paths(rel, "unified", "amd64", "binary") == [PKG_AMD64 + ".gz", PKG_AMD64]
    assert _paths(rel, "unified", "i386", "source") == [
        "unified/source/Sources.gz",
        "unified/source/Sources",
    ]


def test_select_xz_when_both(tmp_path):
    base = make_repo(tmp_path, xz=True)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    assert _paths(rel, "unified", "amd64", "binary")[0] == PKG_AMD64 + ".xz"


def _synthetic(paths):
    lines = "".join(f" {'0' * 64} 0 {p}\n" for p in paths)
    return Release.parse(f"Components: main\nArchitectures: amd64\nSHA256:\n{lines}")


def test_select_ordering_all_forms():
    base = "main/binary-amd64/Packages"
    rel = _synthetic([base, base + ".bz2", base + ".gz", base + ".zst", base + ".xz"])
    assert _paths(rel, "main", "amd64", "binary") == [
        base + ".zst",
        base + ".xz",
        base + ".gz",
        base + ".bz2",
        base,
    ]


def test_select_unknown_arch_and_component():
    rel = _synthetic(["main/binary-amd64/Packages.gz"])
    assert release.select_indices(rel, "main", "arm64", "binary") == []
    assert release.select_indices(rel, "contrib", "amd64", "binary") == []


def test_select_ignores_unrelated_entries():
    rel = _synthetic(
        [
            "main/binary-amd64/Packages.gz",
            "main/binary-amd64/Packages.diff/Index",
            "main/binary-amd64/Release",
            "main/i18n/Translation-en.xz",
            "main/dep11/Components-amd64.yml.gz",
        ]
    )
    assert _paths(rel, "main", "amd64", "binary") == ["main/binary-amd64/Packages.gz"]


def test_select_contents_component_and_top_level():
    rel = _synthetic(
        [
            "Contents-amd64.gz",
            "main/Contents-amd64.gz",
            "main/Contents-amd64.xz",
            "Contents-i386.gz",
        ]
    )
    assert _paths(rel, "main", "amd64", "contents") == [
        "main/Contents-amd64.xz",
        "main/Contents-amd64.gz",
        "Contents-amd64.gz",
    ]


def test_select_contents_fixture(tmp_path):
    base = make_repo(tmp_path, contents=True)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    [idx] = release.select_indices(rel, "unified", "amd64", "contents")
    data = Fetcher().get(release.index_url(rel, idx))
    rel.verify(idx.path, data)
    assert b"usr/bin/sample-amd64" in gzip.decompress(data)


def test_select_no_contents_by_default(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    assert release.select_indices(rel, "unified", "amd64", "contents") == []


def test_select_unknown_kind():
    rel = _synthetic(["main/binary-amd64/Packages.gz"])
    with pytest.raises(ValueError):
        release.select_indices(
            rel,
            "main",
            "amd64",
            "bogus",  # ty: ignore[invalid-argument-type]  # deliberately invalid kind
        )


def test_extra_files_listed(tmp_path):
    extra = {"unified/i18n/Translation-en": b"Package: x\n"}
    base = make_repo(tmp_path, extra_files=extra)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    rel.verify("unified/i18n/Translation-en", _get(base, rel, "unified/i18n/Translation-en"))


def test_packages_as_dicts_and_text(tmp_path):
    base = make_repo(tmp_path / "a", packages=[{"Package": "foo", "Version": "1"}])
    rel = release.fetch_release(Fetcher(), base, SUITE)
    [para] = parse_paragraphs(_get(base, rel, "unified/binary-i386/Packages"))
    assert para["Package"] == "foo" and para["Architecture"] == "i386"
    base = make_repo(tmp_path / "b", sources="Package: src\nVersion: 2\n")
    rel = release.fetch_release(Fetcher(), base, SUITE)
    [para] = parse_paragraphs(_get(base, rel, "unified/source/Sources"))
    assert para["Package"] == "src"


def test_parse_bytes_with_bom():
    rel = Release.parse("﻿Codename: x\n".encode())
    assert rel.codename == "x"


# --- hardening (task 9 prerequisites) ------------------------------------------


def test_index_url_requires_dists_url():
    rel = Release.parse("SHA256:\n " + "a" * 64 + " 1 main/Packages\n")
    with pytest.raises(ReleaseError):
        release.index_url(rel, "main/Packages")


@pytest.mark.parametrize(
    "bad", ["../etc/passwd", "/abs/Packages", "a\\b/Packages", "a/../b", "a\0b"]
)
def test_unsafe_release_paths_skipped(bad, caplog):
    text = f"SHA256:\n {'a' * 64} 1 main/Packages\n {'b' * 64} 1 {bad}\n"
    with caplog.at_level(logging.WARNING):
        rel = Release.parse(text)
    assert list(rel.files) == ["main/Packages"]
    assert "unsafe" in caplog.text


def test_negative_size_skipped(caplog):
    text = f"SHA256:\n {'a' * 64} -1 main/Packages\n {'b' * 64} 5 main/Sources\n"
    with caplog.at_level(logging.WARNING):
        rel = Release.parse(text)
    assert list(rel.files) == ["main/Sources"]
    assert "negative" in caplog.text


@pytest.mark.parametrize(
    ("comp", "arch"), [("../x", "amd64"), ("/x", "amd64"), ("main", "a/../b"), ("a\\b", "amd64")]
)
def test_select_indices_rejects_unsafe(comp, arch):
    rel = Release.parse("Origin: x\n", dists_url="http://x/dists/s")
    with pytest.raises(ReleaseError):
        release.select_indices(rel, comp, arch, "binary")


def test_index_urls_candidates(tmp_path):
    base = make_repo(tmp_path, by_hash=True)
    rel = release.fetch_release(Fetcher(), base, SUITE)
    urls = release.index_urls(rel, PKG_AMD64 + ".gz")
    assert [u.split("/by-hash/")[1].split("/")[0] for u in urls[:-1]] == [
        "SHA512",
        "SHA256",
        "SHA1",
        "MD5Sum",
    ]
    assert urls[-1] == f"{base}/dists/{SUITE}/{PKG_AMD64}.gz"
    assert urls[0] == release.index_url(rel, PKG_AMD64 + ".gz")


# --- percent-encoded traversal and unsafe suites -------------------------------

ENCODED_UNSAFE = [
    "%2e%2e/escape",
    "%2E%2E/escape",
    "main/%2e%2e/Packages",
    "main/.%2e/Packages",
    "main/%2e./Packages",
    "%252e%252e/escape",  # double-encoded
    "%25252e%25252e/x",  # triple-encoded
    "main%2fPackages",
    "main%2FPackages",
    "main%5cPackages",
    "main%5CPackages",
    "main%00Packages",
    "%2fetc/passwd",
    "main/%252fPackages",
    "%5c..%5cx",
]
LEGIT = [
    "main/binary-amd64/Packages",
    "./pool/ok.deb",
    "pool/main/g/gcc/libstdc%2B%2B6_1_amd64.deb",
    "pool/main/a/apt/apt_1%3a2.0_amd64.deb",
    "pool/a/100%25-done_1.deb",
    "a/./b",
    "...",
    "a/..b/c",
]


@pytest.mark.parametrize("path", ENCODED_UNSAFE)
def test_is_unsafe_path_encoded(path):
    assert release.is_unsafe_path(path)


@pytest.mark.parametrize("path", LEGIT)
def test_is_unsafe_path_legit(path):
    assert not release.is_unsafe_path(path)


@pytest.mark.parametrize("bad", ENCODED_UNSAFE)
def test_encoded_release_paths_skipped(bad, caplog):
    text = f"SHA256:\n {'a' * 64} 1 main/Packages\n {'b' * 64} 1 {bad}\n"
    with caplog.at_level(logging.WARNING):
        rel = Release.parse(text)
    assert list(rel.files) == ["main/Packages"]
    with pytest.raises(ReleaseError):
        release.index_url(Release.parse("Origin: x\n", dists_url="http://x/dists/s"), bad)


@pytest.mark.parametrize(
    "suite",
    [
        "../../outside",
        "/abs",
        "/abs/",
        "a/../../b",
        "../",
        "%2e%2e/%2e%2e/outside",
        "%252e%252e/x",
        "a%2fb",
        "a\\b",
        "a\0b",
        "",
    ],
)
def test_dists_url_rejects_unsafe_suite(suite):
    with pytest.raises(ReleaseError, match="unsafe suite"):
        release.dists_url("http://x/repo", suite)


def test_unsafe_suite_message_truncated():
    with pytest.raises(ReleaseError) as ei:
        release.dists_url("http://x/repo", "../" * 100)
    assert "../" * 20 not in str(ei.value) and "..." in str(ei.value)


@pytest.mark.parametrize(
    ("suite", "tail"),
    [
        ("stable/slash-test/1.0", "/dists/stable/slash-test/1.0"),
        ("bookworm-updates", "/dists/bookworm-updates"),
        ("./", ""),
        ("/", ""),
        ("sub/", "/sub"),
        ("./sub/dir/", "/sub/dir"),
    ],
)
def test_dists_url_accepts_safe_suites(suite, tail):
    assert release.dists_url("http://x/repo", suite) == "http://x/repo" + tail


def test_fetch_release_unsafe_suite_makes_no_request():
    class NoFetch(Fetcher):
        def get(self, url: str, **kw: object) -> bytes:
            raise AssertionError(url)

    with pytest.raises(ReleaseError):
        release.fetch_release(NoFetch(), "http://x/repo", "../../outside")
