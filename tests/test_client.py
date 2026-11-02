# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import gzip
import logging
import random
import re
import threading
import time
from typing import Any, cast

import pytest

from minideblib.control import Paragraph
from minideblib.repo import client as client_mod
from minideblib.repo.client import CANDIDATE_SUITES, PackageRecord, RepoClient, SourceRecord
from minideblib.repo.fetch import Fetcher, FetchError
from minideblib.repo.release import HashMismatch
from minideblib.sources import SourceEntry
from minideblib.version import Version
from tests.fixtures.repo import DSC_NAME, DSC_VERSION, SRC_FILES, make_repo

SUITE = "stable/slash-test/1.0"

PKGS = [
    {
        "Package": "hello",
        "Version": "2.10-3",
        "Filename": "pool/main/h/hello/hello_2.10-3_amd64.deb",
        "Size": "100",
        "SHA256": "a" * 64,
        "Description": "example program\n prints a greeting",
    },
    {
        "Package": "hello-doc",
        "Version": "2.10-3",
        "Filename": "pool/main/h/hello/hello-doc_2.10-3_all.deb",
        "Description": "documentation for hello",
    },
    {
        "Package": "othello",
        "Version": "1.0",
        "Filename": "pool/main/o/othello/othello_1.0_amd64.deb",
        "Description": "board GAME",
    },
    {
        "Package": "greeter",
        "Version": "0.1",
        "Filename": "pool/main/g/greeter/greeter_0.1_amd64.deb",
        "Description": "says Hello world",
    },
]

SRCS = [
    {
        "Package": "hello",
        "Binary": "hello,\n hello-doc",
        "Version": "2.10-3",
        "Directory": "pool/main/h/hello",
        "Files": "\n 0123 10 hello_2.10-3.dsc",
        "Checksums-Sha256": "\n "
        + "b" * 64
        + " 10 hello_2.10-3.dsc\n "
        + "c" * 64
        + " 2000 hello_2.10.orig.tar.gz",
    },
    {
        "Package": "legacy",
        "Version": "1",
        "Directory": "pool/main/l/legacy",
        "Files": "\n " + "d" * 32 + " 5 legacy_1.dsc",
    },
]


class RecordingFetcher(Fetcher):
    def __init__(self) -> None:
        super().__init__(retries=0)
        self.gets: list[str] = []
        self.exists_calls: list[str] = []

    def get(self, url: str, **kw: Any) -> bytes:
        self.gets.append(url)
        return super().get(url, **kw)

    def exists(self, url: str, **kw: Any) -> bool:
        self.exists_calls.append(url)
        return super().exists(url, **kw)


def test_components_and_architectures(tmp_path):
    base = make_repo(tmp_path)
    c = RepoClient.from_url(base, suites=[SUITE])
    assert c.components(SUITE) == ["unified"]
    assert c.architectures(SUITE) == ["amd64", "i386"]
    assert c.release(SUITE).codename == SUITE


def test_packages_all_and_arch_filter(tmp_path):
    base = make_repo(tmp_path)
    c = RepoClient.from_url(base, suites=[SUITE])
    recs = c.packages(SUITE)
    assert sorted(r.arch for r in recs) == ["amd64", "i386"]
    amd = c.packages(SUITE, arch="amd64")
    assert len(amd) == 1
    r = amd[0]
    assert isinstance(r, PackageRecord)
    assert (r.suite, r.component, r.arch) == (SUITE, "unified", "amd64")
    assert r.name == "sample-pkg"
    assert r.version == Version("0.4-1")
    assert r.filename == "pool/main/s/sample-pkg/sample-pkg_0.4-1_amd64.deb"
    assert r.url == base + "/" + r.filename
    assert r.size == 1234
    assert r.sha256 == "0" * 64
    assert c.problems == []


def test_packages_component_filter_unknown_records_problem(tmp_path):
    base = make_repo(tmp_path)
    c = RepoClient.from_url(base, suites=[SUITE])
    assert c.packages(SUITE, component="nope", arch="amd64") == []
    assert len(c.problems) == 1
    assert "nope" in c.problems[0]


def test_records_are_frozen(tmp_path):
    base = make_repo(tmp_path)
    r = RepoClient.from_url(base, suites=[SUITE]).packages(SUITE)[0]
    with pytest.raises(AttributeError):
        r.suite = "x"  # ty: ignore[invalid-assignment]  # frozen record, checking it raises


def test_sources_records(tmp_path):
    base = make_repo(tmp_path, sources=SRCS)
    c = RepoClient.from_url(base, suites=[SUITE])
    recs = c.sources(SUITE)
    assert all(isinstance(r, SourceRecord) for r in recs)
    hello, legacy = recs
    assert (hello.suite, hello.component, hello.arch) == (SUITE, "unified", "source")
    assert hello.name == "hello"
    assert hello.version == Version("2.10-3")
    assert hello.directory == "pool/main/h/hello"
    assert hello.files == [
        ("b" * 64, 10, "hello_2.10-3.dsc"),
        ("c" * 64, 2000, "hello_2.10.orig.tar.gz"),
    ]
    assert hello.binaries == ["hello", "hello-doc"]
    assert legacy.files == [("d" * 32, 5, "legacy_1.dsc")]


def test_empty_sources_index(tmp_path):
    base = make_repo(tmp_path)
    c = RepoClient.from_url(base, suites=[SUITE])
    assert c.sources(SUITE) == []
    assert c.problems == []


def test_search_substring_name_and_description(tmp_path):
    base = make_repo(tmp_path, packages=PKGS, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    names = [r.name for r in c.search(SUITE, "HELLO")]
    # substring on name (hello, hello-doc, othello) and short description (greeter)
    assert names == ["hello", "hello-doc", "othello", "greeter"]
    assert [r.name for r in c.search(SUITE, "game")] == ["othello"]
    # long description is not searched
    assert c.search(SUITE, "greeting") == []


def test_search_glob_matches_name_only(tmp_path):
    base = make_repo(tmp_path, packages=PKGS, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    assert [r.name for r in c.search(SUITE, "hello*")] == ["hello", "hello-doc"]
    assert [r.name for r in c.search(SUITE, "HEL?O")] == ["hello"]
    assert [r.name for r in c.search(SUITE, "*hello")] == ["hello", "othello"]
    # glob does not look at descriptions
    assert c.search(SUITE, "*world*") == []


def test_versions_across_two_suites(tmp_path):
    make_repo(
        tmp_path,
        suite="bookworm",
        comps=["main"],
        arches=["amd64"],
        packages=[{"Package": "hello", "Version": "2.10-3", "Filename": "pool/a.deb"}],
    )
    base = make_repo(
        tmp_path,
        suite="trixie",
        comps=["main", "contrib"],
        arches=["amd64"],
        packages=[
            {"Package": "hello", "Version": "2.10-10", "Filename": "pool/b.deb"},
            {"Package": "hello", "Version": "2.10-9", "Filename": "pool/c.deb"},
            {"Package": "other", "Version": "9", "Filename": "pool/d.deb"},
        ],
    )
    c = RepoClient.from_url(base, suites=["trixie", "bookworm"])
    out = c.versions("hello")
    assert [(r.suite, str(r.version), r.component) for r in out] == [
        ("bookworm", "2.10-3", "main"),
        ("trixie", "2.10-10", "main"),
        ("trixie", "2.10-10", "contrib"),
        ("trixie", "2.10-9", "main"),
        ("trixie", "2.10-9", "contrib"),
    ]


def test_versions_across_two_repositories(tmp_path):
    base1 = make_repo(
        tmp_path / "a",
        suite="noble",
        comps=["main"],
        arches=["amd64"],
        packages=[{"Package": "hello", "Version": "1", "Filename": "pool/a.deb"}],
    )
    base2 = make_repo(
        tmp_path / "b",
        suite="noble",
        comps=["universe"],
        arches=["amd64"],
        packages=[{"Package": "hello", "Version": "2", "Filename": "pool/b.deb"}],
    )
    entries = [
        SourceEntry("deb", base1, "noble", ("main",)),
        SourceEntry("deb", base2, "noble", ("universe",)),
    ]
    c = RepoClient(entries)
    assert c.suites() == ["noble"]
    out = c.versions("hello")
    assert [str(r.version) for r in out] == ["2", "1"]
    assert out[0].url == base2 + "/pool/b.deb"
    assert out[1].url == base1 + "/pool/a.deb"
    assert c.components("noble") == ["main", "universe"]


def test_suites_from_sources_list_dedup_and_deb_only(tmp_path):
    entries = [
        SourceEntry("deb", "http://a", "noble", ("main",)),
        SourceEntry("deb-src", "http://a", "src-only", ("main",)),
        SourceEntry("deb", "http://b", "jammy", ("main",)),
        SourceEntry("deb", "http://c", "noble", ("main",)),
    ]
    assert RepoClient(entries).suites() == ["noble", "jammy"]


def test_unknown_suite_with_sources_list(tmp_path):
    c = RepoClient([SourceEntry("deb", "http://a", "noble", ("main",))])
    with pytest.raises(ValueError, match="jammy"):
        c.release("jammy")


def test_sources_entry_components_and_arches_restrict(tmp_path):
    base = make_repo(tmp_path, comps=["unified", "extra"])
    c = RepoClient([SourceEntry("deb", base, SUITE, ("extra",), architectures=("i386",))])
    recs = c.packages(SUITE)
    assert [(r.component, r.arch) for r in recs] == [("extra", "i386")]


def test_debian_security_updates_prefix(tmp_path):
    base = make_repo(
        tmp_path,
        suite="buster/updates",
        comps=["updates/main", "updates/contrib"],
        arches=["amd64"],
        sources=SRCS,
    )
    f = RecordingFetcher()
    c = RepoClient.from_url(base, suites=["buster/updates"], fetcher=f)
    assert c.components("buster/updates") == ["main", "contrib"]
    recs = c.packages("buster/updates", component="main")
    assert [r.component for r in recs] == ["main"]
    assert c.sources("buster/updates", component="contrib")[0].component == "contrib"
    # the full component name is used for URLs
    assert any("/dists/buster/updates/updates/main/" in u for u in f.gets)
    assert c.problems == []


def test_updates_prefix_kept_for_other_suites(tmp_path):
    base = make_repo(tmp_path, suite="noble", comps=["updates/main"], arches=["amd64"])
    c = RepoClient.from_url(base, suites=["noble"])
    assert c.components("noble") == ["updates/main"]
    assert c.packages("noble")[0].component == "updates/main"


def test_find_files_basename_and_path(tmp_path):
    base = make_repo(tmp_path, contents=True)
    c = RepoClient.from_url(base, suites=[SUITE])
    assert c.find_files(SUITE, "copyright") == [
        ("usr/share/doc/sample-pkg/copyright", "doc/sample-pkg")
    ]
    assert c.find_files(SUITE, "/usr/bin/sample-amd64") == [
        ("usr/bin/sample-amd64", "utils/sample-pkg")
    ]
    assert c.find_files(SUITE, "sample-i386", arch="amd64") == []
    assert c.find_files(SUITE, "sample-i386", arch="i386") == [
        ("usr/bin/sample-i386", "utils/sample-pkg")
    ]
    assert c.find_files(SUITE, "bin/sample-amd64") == []
    assert c.problems == []


def test_find_files_top_level_contents(tmp_path):
    body = b"usr/bin/tool    admin/toolpkg,admin/toolpkg2\n"
    base = make_repo(
        tmp_path, arches=["amd64"], extra_files={"Contents-amd64.gz": gzip.compress(body)}
    )
    c = RepoClient.from_url(base, suites=[SUITE])
    assert c.find_files(SUITE, "tool") == [
        ("usr/bin/tool", "admin/toolpkg"),
        ("usr/bin/tool", "admin/toolpkg2"),
    ]


def test_find_files_without_contents(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    assert c.find_files(SUITE, "anything") == []
    assert len(c.problems) == 1
    assert "Contents" in c.problems[0]


def test_hash_mismatch_propagates(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    pkgs = tmp_path / "dists" / SUITE / "unified" / "binary-amd64" / "Packages.gz"
    pkgs.write_bytes(gzip.compress(b"Package: evil\nVersion: 1\n"))
    c = RepoClient.from_url(base, suites=[SUITE])
    with pytest.raises(HashMismatch):
        c.packages(SUITE)


def test_verify_false_accepts_tampered_index(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    pkgs = tmp_path / "dists" / SUITE / "unified" / "binary-amd64" / "Packages.gz"
    pkgs.write_bytes(gzip.compress(b"Package: evil\nVersion: 1\n"))
    c = RepoClient.from_url(base, suites=[SUITE], verify=False)
    assert [r.name for r in c.packages(SUITE)] == ["evil"]


def test_missing_index_recorded_in_problems(tmp_path, caplog):
    base = make_repo(tmp_path)
    d = tmp_path / "dists" / SUITE / "unified" / "binary-i386"
    for f in d.iterdir():
        f.unlink()
    c = RepoClient.from_url(base, suites=[SUITE])
    with caplog.at_level("WARNING", logger="minideblib.repo.client"):
        recs = c.packages(SUITE)
    assert [r.arch for r in recs] == ["amd64"]
    assert len(c.problems) == 1
    assert "binary-i386/Packages" in c.problems[0]
    assert "binary-i386/Packages" in caplog.text


def test_compressed_missing_falls_back_to_uncompressed(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    (tmp_path / "dists" / SUITE / "unified" / "binary-amd64" / "Packages.gz").unlink()
    c = RepoClient.from_url(base, suites=[SUITE])
    assert len(c.packages(SUITE)) == 1
    assert c.problems == []


def test_flat_repo(tmp_path):
    base = make_repo(tmp_path, flat=True, arches=["amd64"], sources=SRCS)
    c = RepoClient.from_url(base, suites=["./"])
    assert c.components("./") == [""]
    assert c.architectures("./") == ["amd64"]
    recs = c.packages("./")
    assert [(r.name, r.component, r.arch) for r in recs] == [("sample-pkg", "", "amd64")]
    assert recs[0].url == base + "/" + recs[0].filename
    assert [r.name for r in c.sources("./")] == ["hello", "legacy"]
    assert c.packages("./", arch="i386") == []


def test_flat_repo_subdir_url(tmp_path):
    base = make_repo(tmp_path, suite="sub/", flat=True, arches=["amd64"])
    c = RepoClient([SourceEntry("deb", base, "sub/", ())])
    recs = c.packages("sub/")
    assert recs[0].url == base + "/sub/" + recs[0].filename


def test_cache_and_clear_cache(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    f = RecordingFetcher()
    c = RepoClient.from_url(base, suites=[SUITE], fetcher=f)
    c.packages(SUITE)
    c.packages(SUITE)
    c.search(SUITE, "sample")
    c.components(SUITE)
    index_gets = [u for u in f.gets if "Packages" in u]
    release_gets = [u for u in f.gets if u.endswith("Release")]
    assert len(index_gets) == 1
    assert len(release_gets) == 1
    c.clear_cache()
    c.packages(SUITE)
    assert len([u for u in f.gets if "Packages" in u]) == 2
    assert len([u for u in f.gets if u.endswith("Release")]) == 2


def test_from_url_probing_finds_only_existing(tmp_path):
    make_repo(tmp_path, suite=SUITE)
    base = make_repo(tmp_path, suite="noble")
    f = RecordingFetcher()
    c = RepoClient.from_url(base, suites=[SUITE], fetcher=f)
    assert c.suites() == [SUITE, "noble"]
    assert len(f.exists_calls) <= client_mod.MAX_PROBE_REQUESTS <= 150
    # existing suites need one probe (InRelease), missing ones two
    assert len(f.exists_calls) == 2 * (len(CANDIDATE_SUITES) + 1) - 2
    assert all(u.endswith(("/InRelease", "/Release")) for u in f.exists_calls)
    # cached
    c.suites()
    assert len(f.exists_calls) == 2 * (len(CANDIDATE_SUITES) + 1) - 2


def test_from_url_probe_release_only(tmp_path):
    base = make_repo(tmp_path, suite="noble-updates", inrelease=False)
    assert RepoClient.from_url(base).suites() == ["noble-updates"]


def test_probe_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_PROBE_REQUESTS", 5)
    f = RecordingFetcher()
    c = RepoClient.from_url(make_repo(tmp_path, suite="noble"), fetcher=f)
    assert c.suites() == []  # noble is beyond the first few candidates
    # 5 // 2 = 2 suites planned, at most 2 calls each
    assert len(f.exists_calls) == 4
    assert any("probe" in p for p in c.problems)


def test_candidate_suites_contents():
    # intentionally short lists (reviewed 2026-11); other suites need suites=[...]
    assert CANDIDATE_SUITES == (
        "stable", "stable-updates", "stable-backports", "testing",
        "stonking", "stonking-updates", "stonking-security", "stonking-backports",
        "resolute", "resolute-updates", "resolute-security", "resolute-backports",
        "noble", "noble-updates", "noble-security", "noble-backports",
    )  # fmt: skip
    assert 2 * len(CANDIDATE_SUITES) <= client_mod.MAX_PROBE_REQUESTS


def test_from_url_unprobed_suite_usable(tmp_path):
    base = make_repo(tmp_path)
    f = RecordingFetcher()
    c = RepoClient.from_url(base, fetcher=f)
    assert c.components(SUITE) == ["unified"]
    assert f.exists_calls == []


def test_versions_skips_missing_suite(tmp_path):
    base = make_repo(tmp_path, suite="noble", arches=["amd64"])
    c = RepoClient(
        [
            SourceEntry("deb", base, "noble", ()),
            SourceEntry("deb", base, "gone", ()),
        ]
    )
    assert [r.suite for r in c.versions("sample-pkg")] == ["noble"]
    assert any("gone" in p for p in c.problems)


def test_top_level_contents_fetched_once_for_many_components(tmp_path):
    body = b"usr/bin/tool    admin/toolpkg\n"
    base = make_repo(
        tmp_path,
        comps=["a", "b"],
        arches=["amd64"],
        extra_files={"Contents-amd64.gz": gzip.compress(body)},
    )
    f = RecordingFetcher()
    c = RepoClient.from_url(base, suites=[SUITE], fetcher=f)
    assert c.find_files(SUITE, "tool") == [("usr/bin/tool", "admin/toolpkg")]
    assert len([u for u in f.gets if "Contents" in u]) == 1


def test_records_hashable(tmp_path):
    base = make_repo(tmp_path)
    c = RepoClient.from_url(base, suites=[SUITE])
    assert len({*c.packages(SUITE)}) == 2
    assert {*c.sources(SUITE)} == set()


def test_entries_with_same_uri_are_merged(tmp_path):
    base = make_repo(tmp_path, comps=["main", "universe", "multiverse"], arches=["amd64"])
    c = RepoClient(
        [
            SourceEntry("deb", base, SUITE, ("main",)),
            SourceEntry("deb", base, SUITE, ("universe",)),
        ]
    )
    assert [r.component for r in c.packages(SUITE)] == ["main", "universe"]


# -- fix round 1 -----------------------------------------------------------


class ProbeFetcher(Fetcher):
    """Thread-safe fetcher with scripted failures per suite and jitter."""

    def __init__(self, errors: dict[str, FetchError] | None = None) -> None:
        super().__init__(retries=0)
        self.errors = errors or {}
        self.lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def exists(self, url: str, **kw: object) -> bool:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(random.uniform(0, 0.005))
            for suite, err in self.errors.items():
                if f"/dists/{suite}/" in url:
                    raise err
            return super().exists(url)
        finally:
            with self.lock:
                self.active -= 1


def test_probe_parallel_order_preserved(tmp_path):
    for s in ("noble", "stable", "resolute-updates", "sid"):
        base = make_repo(tmp_path, suite=s, arches=["amd64"])
    f = ProbeFetcher()
    c = RepoClient.from_url(base, suites=["sid"], fetcher=f)
    expected = ["sid"] + [
        s for s in CANDIDATE_SUITES if s in ("noble", "stable", "resolute-updates")
    ]
    assert c.suites() == expected
    assert f.calls <= client_mod.MAX_PROBE_REQUESTS
    assert f.max_active > 1  # actually concurrent
    assert c.problems == []


def test_probe_budget_planned_before_submitting(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_PROBE_REQUESTS", 9)
    base = make_repo(tmp_path, suite=SUITE)
    f = ProbeFetcher()
    c = RepoClient.from_url(base, suites=[SUITE], fetcher=f)
    assert c.suites() == [SUITE]
    # explicit suite + first 3 candidates = 9 // 2 suites, at most 2 calls each
    assert f.calls == 1 + 3 * 2
    assert f.calls <= 9
    assert len(c.problems) == 1
    assert "not probed" in c.problems[0]


def test_probe_errors_recorded_without_aborting(tmp_path):
    for s in ("stable", "testing", "stonking", "noble"):
        base = make_repo(tmp_path, suite=s, arches=["amd64"])
    f = ProbeFetcher(
        {
            "stable": FetchError("HTTP 503", "u", 503),
            "testing": FetchError("connection refused", "u", transient=True),
            "stonking": FetchError("HTTP 403", "u", 403),
        }
    )
    c = RepoClient.from_url(base, fetcher=f)
    assert c.suites() == ["noble"]
    assert len(c.problems) == 2
    assert c.problems[0].startswith("probe stable:") and "503" in c.problems[0]
    assert c.problems[1].startswith("probe testing:") and "refused" in c.problems[1]


def test_unsafe_filename_rejected(tmp_path):
    pkgs = [
        {"Package": "ok", "Version": "1", "Filename": "./pool/ok.deb"},
        {"Package": "abs", "Version": "1", "Filename": "http://evil/x.deb"},
        {"Package": "up", "Version": "1", "Filename": "../../x.deb"},
        {"Package": "root", "Version": "1", "Filename": "/etc/x.deb"},
        {"Package": "bs", "Version": "1", "Filename": "pool\\x.deb"},
    ]
    base = make_repo(tmp_path, packages=pkgs, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    recs = c.packages(SUITE)
    assert [r.name for r in recs] == ["ok", "abs"]
    assert recs[0].url == base + "/pool/ok.deb"
    assert recs[1].url == base + "/http://evil/x.deb"  # stays under the base
    assert len(c.problems) == 3
    assert all("unsafe" in p for p in c.problems)
    for para in pkgs[2:]:
        rec = PackageRecord(Paragraph(para.items()), SUITE, "unified", "amd64", base)
        with pytest.raises(ValueError, match="unsafe"):
            rec.url  # noqa: B018
    with pytest.raises(ValueError, match="no Filename"):
        PackageRecord(Paragraph([("Package", "x")]), SUITE, "", "amd64", base).url  # noqa: B018


def test_unsafe_source_paths_rejected(tmp_path):
    srcs = [
        {
            "Package": "ok",
            "Version": "1",
            "Directory": "pool/main/o/ok",
            "Files": "\n " + "d" * 32 + " 5 ok_1.dsc",
        },
        {
            "Package": "dir",
            "Version": "1",
            "Directory": "pool/../../etc",
            "Files": "\n " + "d" * 32 + " 5 dir_1.dsc",
        },
        {
            "Package": "file",
            "Version": "1",
            "Directory": "pool/main/f/file",
            "Checksums-Sha256": "\n " + "e" * 64 + " 5 ../../file_1.dsc",
        },
    ]
    base = make_repo(tmp_path, sources=srcs, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    recs = c.sources(SUITE)
    assert [r.name for r in recs] == ["ok"]
    assert recs[0].base_url == base
    assert recs[0].file_urls == [base + "/pool/main/o/ok/ok_1.dsc"]
    assert len(c.problems) == 2
    for para in srcs[1:]:
        rec = SourceRecord(Paragraph(para.items()), SUITE, "unified", base_url=base)
        with pytest.raises(ValueError, match="unsafe"):
            rec.file_urls  # noqa: B018


def test_missing_release_cached(tmp_path):
    base = make_repo(tmp_path, suite="noble", arches=["amd64"])
    f = RecordingFetcher()
    c = RepoClient(
        [SourceEntry("deb", base, "noble", ()), SourceEntry("deb", base, "gone", ())], fetcher=f
    )
    c.versions("sample-pkg")
    c.versions("sample-pkg")
    assert len([u for u in f.gets if "/gone/" in u]) == 2  # InRelease + Release, once
    assert len([p for p in c.problems if "gone" in p]) == 1


def test_no_components_field_records_problem(tmp_path):
    base = make_repo(tmp_path, suite="noble", arches=["amd64"])
    dist = tmp_path / "dists" / "noble"
    for name in ("InRelease", "Release"):
        p = dist / name
        p.write_text(
            "".join(ln for ln in p.read_text().splitlines(True) if not ln.startswith("Components:"))
        )
    c = RepoClient.from_url(base, suites=["noble"])
    assert c.components("noble") == []
    assert c.packages("noble") == []
    assert len(c.problems) == 1
    assert "Components" in c.problems[0]


def test_index_decompressed_once(tmp_path, monkeypatch):
    from minideblib.repo import index

    calls = []
    real = index.decompress

    def counting(data, name="", **kw):
        calls.append(name)
        return real(data, name, **kw)

    monkeypatch.setattr(index, "decompress", counting)
    base = make_repo(tmp_path, arches=["amd64"])
    RepoClient.from_url(base, suites=[SUITE]).packages(SUITE)
    assert len(calls) == 1


# -- .dsc ---------------------------------------------------------------------


def _dsc_client(tmp_path):
    base = make_repo(tmp_path, comps=("main",), dsc=True)
    return base, RepoClient.from_url(base, suites=[SUITE])


def test_source_record_dsc_url(tmp_path):
    base, c = _dsc_client(tmp_path)
    (rec,) = c.sources(SUITE)
    assert rec.dsc_url == f"{base}/pool/main/s/sample-pkg/{DSC_NAME}"
    assert rec.dsc_url in rec.file_urls


def test_source_record_dsc_url_absent_and_unsafe():
    para = Paragraph(
        [("Package", "p"), ("Directory", "pool/p"), ("Files", "\n " + "0" * 32 + " 1 p.tar.gz")]
    )
    assert SourceRecord(para, "s", "main", base_url="http://x").dsc_url is None
    bad = Paragraph(
        [("Package", "p"), ("Directory", "../x"), ("Files", "\n " + "0" * 32 + " 1 p_1.dsc")]
    )
    with pytest.raises(ValueError):
        SourceRecord(bad, "s", "main", base_url="http://x").dsc_url  # noqa: B018


def test_fetch_dsc(tmp_path):
    _, c = _dsc_client(tmp_path)
    (rec,) = c.sources(SUITE)
    d = c.fetch_dsc(rec)
    assert d.source == "sample-pkg" and str(d.version) == DSC_VERSION
    assert [f.name for f in d.files] == list(SRC_FILES)
    d.verify(tmp_path / "pool" / "main" / "s" / "sample-pkg")


def test_fetch_dsc_tampered_raises(tmp_path):
    _, c = _dsc_client(tmp_path)
    (rec,) = c.sources(SUITE)
    path = tmp_path / "pool" / "main" / "s" / "sample-pkg" / DSC_NAME
    path.write_bytes(path.read_bytes().replace(b"Test", b"Evil"))
    with pytest.raises(HashMismatch, match=DSC_NAME):
        c.fetch_dsc(rec)
    # same length, different content is caught by the hash alone
    path.write_bytes(path.read_bytes().replace(b"Evil", b"Eval"))
    with pytest.raises(HashMismatch):
        c.fetch_dsc(rec)


def test_fetch_dsc_tampered_no_verify(tmp_path):
    base = make_repo(tmp_path, comps=("main",), dsc=True)
    c = RepoClient.from_url(base, suites=[SUITE], verify=False)
    (rec,) = c.sources(SUITE)
    path = tmp_path / "pool" / "main" / "s" / "sample-pkg" / DSC_NAME
    path.write_bytes(path.read_bytes().replace(b"Test", b"Evil"))
    assert c.fetch_dsc(rec).maintainer == "Evil <test@example.com>"


def test_fetch_dsc_uses_strongest_listed_hash(tmp_path):
    _, c = _dsc_client(tmp_path)
    (rec,) = c.sources(SUITE)
    path = tmp_path / "pool" / "main" / "s" / "sample-pkg" / DSC_NAME
    path.write_bytes(path.read_bytes().replace(b"Test", b"Evil"))
    # Checksums-Sha256 decides; the (stale) weaker fields are not consulted
    with pytest.raises(HashMismatch, match="sha256"):
        c.fetch_dsc(rec)


@pytest.mark.parametrize(
    "drop",
    [("Checksums-Sha256",), ("Checksums-Sha256", "Checksums-Sha1")],
)
def test_fetch_dsc_weak_hashes_only_rejected(tmp_path, drop):
    _, c = _dsc_client(tmp_path)
    (rec,) = c.sources(SUITE)
    para = Paragraph((k, v) for k, v in rec.paragraph.items() if k not in drop)
    weaker = SourceRecord(para, rec.suite, rec.component, base_url=rec.base_url)
    with pytest.raises(HashMismatch, match="no SHA256 or stronger hash"):
        c.fetch_dsc(weaker)
    base = rec.base_url
    unverified = RepoClient.from_url(base, suites=[SUITE], verify=False)
    assert unverified.fetch_dsc(weaker).source == "sample-pkg"


def test_fetch_dsc_wrong_listed_hash_fails_closed(tmp_path):
    _, c = _dsc_client(tmp_path)
    (rec,) = c.sources(SUITE)
    para = Paragraph(
        [("Package", "sample-pkg"), ("Version", DSC_VERSION), ("Directory", rec.directory)]
        + [("Files", "\n " + "0" * 32 + f" 1 {DSC_NAME}")]
    )
    with pytest.raises(HashMismatch):
        c.fetch_dsc(SourceRecord(para, rec.suite, rec.component, base_url=rec.base_url))


_HUGE = "1" * 5000


def test_source_files_oversized_size_skipped(caplog):
    files = "\n " + "d" * 32 + " 5 p_1.dsc"
    para = Paragraph(
        [("Package", "p"), ("Directory", "pool/p"), ("Files", files)]
        + [("Checksums-Sha256", "\n " + "e" * 64 + f" {_HUGE} p_1.dsc")]
    )
    rec = SourceRecord(para, "s", "main", base_url="http://x")
    with caplog.at_level(logging.WARNING):
        assert rec.files == [("d" * 32, 5, "p_1.dsc")]
    assert "malformed Checksums-Sha256" in caplog.text
    both_bad = Paragraph([("Package", "p"), ("Files", "\n " + "d" * 32 + f" {_HUGE} p_1.dsc")])
    assert SourceRecord(both_bad, "s", "main", base_url="http://x").files == []


def test_check_listed_hash_oversized_size():
    para = Paragraph([("Checksums-Sha256", "\n " + "e" * 64 + f" {_HUGE} p_1.dsc")])
    with pytest.raises(HashMismatch, match="malformed Checksums-Sha256"):
        client_mod._check_listed_hash(para, "p_1.dsc", b"data")


def test_fetch_dsc_without_dsc_file():
    para = Paragraph(
        [("Package", "p"), ("Directory", "d"), ("Files", "\n " + "0" * 32 + " 1 p.tar.gz")]
    )
    rec = SourceRecord(para, "s", "main", base_url="http://x")
    with pytest.raises(ValueError, match=r"\.dsc"):
        RepoClient([]).fetch_dsc(rec)


def test_fetch_dsc_flat(tmp_path):
    base = make_repo(tmp_path, flat=True, arches=["amd64"], dsc=True)
    c = RepoClient.from_url(base, suites=["./"])
    (rec,) = c.sources("./")
    assert c.fetch_dsc(rec).source == "sample-pkg"


# -- Task 24: probe sets, quiet probing, scoping, unreachable repositories ------


def _dists_suite(url: str) -> str:
    return url.split("/dists/", 1)[1].rsplit("/", 1)[0]


def test_candidate_suites_split():
    deb, ubu = client_mod.DEBIAN_SUITES, client_mod.UBUNTU_SUITES
    assert isinstance(deb, tuple) and isinstance(ubu, tuple)
    assert deb + ubu == CANDIDATE_SUITES
    assert not set(deb) & set(ubu)
    assert deb == ("stable", "stable-updates", "stable-backports", "testing")
    assert len(ubu) == 12 and not {"devel", "questing"} & set(ubu)
    for release in ("stonking", "resolute", "noble"):
        for pocket in ("", "-updates", "-security", "-backports"):
            assert f"{release}{pocket}" in ubu
    assert not any(s.endswith("-proposed") for s in CANDIDATE_SUITES)


def test_probe_debian_only(tmp_path):
    base = make_repo(tmp_path, suite="stable", arches=["amd64"])
    make_repo(tmp_path, suite="noble", arches=["amd64"])
    f = RecordingFetcher()
    c = RepoClient.from_url(base, probe="debian", fetcher=f)
    assert c.suites() == ["stable"]
    probed = [_dists_suite(u) for u in f.exists_calls]
    assert set(probed) <= set(client_mod.DEBIAN_SUITES)
    assert not set(probed) & set(client_mod.UBUNTU_SUITES)
    for s in client_mod.DEBIAN_SUITES:
        assert probed.count(s) <= 2
    assert RepoClient.from_url(base, probe="ubuntu").suites() == ["noble"]


def test_probe_none_only_explicit(tmp_path):
    base = make_repo(tmp_path, suite="stable", arches=["amd64"])
    f = RecordingFetcher()
    assert RepoClient.from_url(base, probe="none", fetcher=f).suites() == []
    assert f.exists_calls == []
    c = RepoClient.from_url(base, suites=["stable", "gone"], probe="none", fetcher=f)
    assert c.suites() == ["stable"]
    assert {_dists_suite(u) for u in f.exists_calls} == {"stable", "gone"}


def test_probe_invalid_value():
    with pytest.raises(ValueError, match="probe"):
        RepoClient.from_url("http://x", probe=cast(Any, "fedora"))


def test_probe_logging_contract(tmp_path, caplog):
    for s in ("stable", "noble"):
        base = make_repo(tmp_path, suite=s, arches=["amd64"])
    caplog.set_level(logging.DEBUG, logger="minideblib")
    assert RepoClient.from_url(base).suites() == ["stable", "noble"]
    n = len(CANDIDATE_SUITES)
    client_msgs = [r for r in caplog.records if r.name == "minideblib.repo.client"]
    info = [r.getMessage() for r in client_msgs if r.levelno == logging.INFO]
    assert info == [
        f"probing {n} candidate suites on {base}",
        "found suite stable",
        "found suite noble",
        f"probed {n} suites: 2 found, {n - 2} absent, 0 errors",
    ]
    debug = [r.getMessage() for r in client_msgs if r.levelno == logging.DEBUG]
    absent = [m for m in debug if m.startswith("absent: ")]
    expected = [s for s in CANDIDATE_SUITES if s not in ("stable", "noble")]
    assert absent == ["absent: " + ", ".join(expected)]
    assert not any(re.search(r"probe \S+: absent", r.getMessage()) for r in caplog.records)


def test_from_url_scoping_fetches_one_index(tmp_path):
    base = make_repo(tmp_path, comps=["main", "contrib"], arches=["amd64", "i386"])
    f = RecordingFetcher()
    c = RepoClient.from_url(
        base, suites=[SUITE], components=("main",), architectures=("amd64",), fetcher=f
    )
    assert c.entries[0].components == ("main",)
    assert c.entries[0].architectures == ("amd64",)
    recs = c.packages(SUITE)
    assert {(r.component, r.arch) for r in recs} == {("main", "amd64")}
    indices = [u for u in f.gets if "/binary-" in u]
    assert len(indices) == 1 and "/main/binary-amd64/" in indices[0]
    # unprobed suites get the same scope
    assert {(r.component, r.arch) for r in c.packages(SUITE)} == {("main", "amd64")}


def test_client_scoping_intersects_sources_entries(tmp_path):
    base = make_repo(tmp_path, comps=["main", "contrib", "non-free"], arches=["amd64", "i386"])
    entry = SourceEntry("deb", base, SUITE, ("main", "contrib"))
    c = RepoClient([entry], components=("contrib", "non-free"), architectures=("i386",))
    assert {(r.component, r.arch) for r in c.packages(SUITE)} == {("contrib", "i386")}
    # an explicit argument wins over the client filter
    assert {r.arch for r in c.packages(SUITE, "contrib", "amd64")} == {"amd64"}
    # empty filters mean no restriction
    c = RepoClient([entry])
    assert {r.component for r in c.packages(SUITE)} == {"main", "contrib"}


def test_versions_scoped(tmp_path):
    for s in ("stable", "trixie"):
        base = make_repo(tmp_path, suite=s, comps=["main", "contrib"], arches=["amd64", "i386"])
    f = RecordingFetcher()
    c = RepoClient.from_url(base, fetcher=f)
    out = c.versions("sample-pkg", suites=["trixie"], components=["contrib"],
                     architectures=["i386"])  # fmt: skip
    assert {(r.suite, r.component, r.arch) for r in out} == {("trixie", "contrib", "i386")}
    assert f.exists_calls == []  # explicit suites: no probing
    indices = [u for u in f.gets if "/binary-" in u]
    assert len(indices) == 1 and "/trixie/contrib/binary-i386/" in indices[0]


def test_find_files_scoped(tmp_path):
    def body(name: str) -> bytes:
        return gzip.compress(f"usr/bin/{name}    utils/{name}\n".encode())

    base = make_repo(
        tmp_path,
        comps=["a", "b", "c"],
        arches=["amd64"],
        extra_files={
            "a/Contents-amd64.gz": body("ta"),
            "c/Contents-amd64.gz": body("tc"),
            "Contents-amd64.gz": body("top"),
        },
    )
    f = RecordingFetcher()
    c = RepoClient.from_url(base, suites=[SUITE], fetcher=f)
    assert c.find_files(SUITE, "ta", components=["a", "b"]) == [("usr/bin/ta", "utils/ta")]
    fetched = sorted(u.split(f"/dists/{SUITE}/", 1)[1] for u in f.gets if "Contents" in u)
    assert fetched == ["Contents-amd64.gz", "a/Contents-amd64.gz"]


def test_suites_dead_file_url_raises(tmp_path):
    c = RepoClient.from_url((tmp_path / "nothing").as_uri())
    with pytest.raises(FetchError, match="unreachable"):
        c.suites()


class AlwaysFails(Fetcher):
    def __init__(self, err: FetchError) -> None:
        super().__init__(retries=0)
        self.err = err
        self.calls = 0

    def exists(self, url: str, **kw: object) -> bool:
        self.calls += 1
        raise self.err


def test_suites_unreachable_network_raises():
    for err in (FetchError("connection refused", "u", transient=True), FetchError("x", "u", 503)):
        c = RepoClient.from_url("http://h", suites=["mine"], fetcher=AlwaysFails(err))
        with pytest.raises(FetchError, match="unreachable") as ei:
            c.suites()
        assert ei.value.status == err.status
        assert c.problems == []  # one error instead of a warning per candidate


def test_suites_404_everywhere_is_empty():
    c = RepoClient.from_url("http://h", fetcher=AlwaysFails(FetchError("HTTP 404", "u", 404)))
    assert c.suites() == []


def test_suites_one_answer_keeps_old_behaviour(tmp_path):
    base = make_repo(tmp_path, suite="noble", arches=["amd64"])
    refused = FetchError("refused", "u", transient=True)
    errors = {s: refused for s in CANDIDATE_SUITES if s != "noble"}
    c = RepoClient.from_url(base, fetcher=ProbeFetcher(errors))
    assert c.suites() == ["noble"]
    assert len(c.problems) == len(CANDIDATE_SUITES) - 1


def test_suites_429_everywhere_is_unreachable():
    c = RepoClient.from_url("http://h", fetcher=AlwaysFails(FetchError("HTTP 429", "u", 429)))
    with pytest.raises(FetchError, match="unreachable") as ei:
        c.suites()
    assert ei.value.status == 429


BAD_VERSIONS = [
    {"Package": "good", "Version": "1.0", "Filename": "pool/good_1.0.deb"},
    {"Package": "bad", "Version": "not a version", "Filename": "pool/bad.deb"},
    {"Package": "epoch", "Version": "9" * 5000 + ":1", "Filename": "pool/epoch.deb"},
    {"Package": "long", "Version": "1." + "9" * 5000, "Filename": "pool/long.deb"},
]


def test_invalid_version_skipped_with_problem(tmp_path):
    base = make_repo(tmp_path, packages=BAD_VERSIONS, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    recs = c.packages(SUITE)
    # a 5000-digit upstream version is valid (no int() conversion)
    assert [r.name for r in recs] == ["good", "long"]
    assert len(c.problems) == 2
    assert all("skipping" in p for p in c.problems)
    assert "bad" in c.problems[0] and "epoch" in c.problems[1]
    assert len(c.problems[1]) < 300  # the huge version is not echoed in full
    assert [r.name for r in c.versions("epoch", suites=[SUITE])] == []
    assert [r.name for r in c.versions("good", suites=[SUITE])] == ["good"]


def test_invalid_source_version_skipped_with_problem(tmp_path):
    srcs = [
        {"Package": "ok", "Version": "1", "Directory": "pool/o",
         "Files": "\n " + "d" * 32 + " 5 ok_1.dsc"},
        {"Package": "bad", "Version": "1:", "Directory": "pool/b",
         "Files": "\n " + "d" * 32 + " 5 bad_1.dsc"},
    ]  # fmt: skip
    base = make_repo(tmp_path, sources=srcs, arches=["amd64"])
    c = RepoClient.from_url(base, suites=[SUITE])
    assert [r.name for r in c.sources(SUITE)] == ["ok"]
    assert len(c.problems) == 1 and "bad" in c.problems[0]


def test_missing_suite_not_recorded_as_problem(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    c = RepoClient.from_url(base)
    with pytest.raises(FetchError) as ei:
        c.components("nosuch")
    assert ei.value.status is None or ei.value.status == 404
    assert c.problems == []  # the raised error is the only report


def test_missing_entry_noted_when_another_serves_suite(tmp_path):
    base = make_repo(tmp_path / "a", arches=["amd64"])
    c = RepoClient(
        [SourceEntry("deb", base, SUITE, ()), SourceEntry("deb", base + "/nope", SUITE, ())]
    )
    assert c.components(SUITE) == ["unified"]
    assert c.components(SUITE) == ["unified"]
    assert len(c.problems) == 1
    assert "Release not found" in c.problems[0] and "/nope" in c.problems[0]


@pytest.mark.parametrize(
    ("kw", "scope", "expected"),
    [
        ({"architectures": ["sparc"]}, "architecture", "architecture sparc not listed in Release"),
        ({"components": ["nope"]}, "component", "no architecture/component left after scoping"),
    ],
)
def test_empty_scope_recorded_as_problem(tmp_path, kw, scope, expected):
    base = make_repo(tmp_path, packages=PKGS, sources=SRCS, contents=True, arches=["amd64"])

    def fresh() -> RepoClient:
        # entries without their own components/architectures, as with several --url
        return RepoClient([SourceEntry("deb", base, SUITE, ())], **kw)

    c = fresh()
    assert c.packages(SUITE) == []
    assert len(c.problems) == 1
    assert c.problems[0].startswith(f"{SUITE}: {expected}")
    if scope == "component":
        assert f"({scope};" in c.problems[0]
    else:
        assert c.problems[0].endswith(f"at {base} (Release lists: amd64)")
    c.packages(SUITE)
    assert len(c.problems) == 1  # once per entry
    c = fresh()
    assert c.find_files(SUITE, "x") == []
    assert len(c.problems) == 1 and expected in c.problems[0]
    c = fresh()
    c.sources(SUITE)
    if scope == "component":
        assert len(c.problems) == 1 and expected in c.problems[0]
    else:  # architectures do not apply to Sources
        assert c.problems == []


class BlockingFetcher(Fetcher):
    """``exists`` blocks until *release* is set; records the worker threads."""

    def __init__(self) -> None:
        super().__init__(retries=0)
        self.release = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.calls = 0
        self.daemon: list[bool] = []
        self.cancelled = 0

    def cancel(self) -> None:
        self.cancelled += 1
        super().cancel()

    def exists(self, url: str, **kw: object) -> bool:
        with self.lock:
            self.calls += 1
            self.daemon.append(threading.current_thread().daemon)
        self.started.set()
        self.release.wait(10)
        return False


def test_probe_interrupt_is_prompt_and_workers_are_daemon():
    import _thread

    f = BlockingFetcher()
    c = RepoClient.from_url("http://h", fetcher=f)

    def interrupt() -> None:
        f.started.wait(5)
        time.sleep(0.05)
        _thread.interrupt_main()

    threading.Thread(target=interrupt, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            c.suites()
        assert time.monotonic() - started < 3  # in-flight probes are not waited for
        # the probes' retries are stopped by a token of the run, not by
        # Fetcher.cancel(), which would also stop requests of other threads
        assert f.cancelled == 0
        assert f.daemon and all(f.daemon)  # interpreter exit does not wait for them
        assert f.calls <= client_mod.PROBE_WORKERS  # queued probes are dropped
    finally:
        f.release.set()
    time.sleep(0.1)
    assert f.calls <= client_mod.PROBE_WORKERS


def test_probe_worker_exception_propagates():
    class Broken(Fetcher):
        def exists(self, url: str, **kw: object) -> bool:
            raise RuntimeError("bug in fetcher")

    c = RepoClient.from_url("http://h", fetcher=Broken(retries=0))
    with pytest.raises(RuntimeError, match="bug in fetcher"):
        c.suites()


def test_architecture_scoped_away_by_entry_is_a_scoping_problem(tmp_path):
    base = make_repo(tmp_path, packages=PKGS)  # Release lists amd64 and i386
    c = RepoClient([SourceEntry("deb", base, SUITE, (), architectures=("amd64",))],
                   architectures=["i386"])  # fmt: skip
    assert c.packages(SUITE) == []
    assert len(c.problems) == 1
    assert c.problems[0].startswith(
        f"{SUITE}: no architecture/component left after scoping (architecture; "
        "scope architectures: i386)"
    )


def test_versions_parsed_once_per_record(tmp_path, monkeypatch):
    base = make_repo(tmp_path, packages=PKGS, arches=["amd64"])
    made: list[str] = []

    class Counting(Version):
        def __init__(self, s: str) -> None:
            made.append(s)
            super().__init__(s)

    monkeypatch.setattr(client_mod, "Version", Counting)
    c = RepoClient.from_url(base, suites=[SUITE])
    recs = c.packages(SUITE)
    assert len(recs) == len(PKGS)
    assert sorted(r.version for r in recs) and [str(r.version) for r in recs]
    assert len(made) == len(PKGS)  # _keep validated each once; later uses are cached
    made.clear()
    assert [r.name for r in c.versions("hello", suites=[SUITE])] == ["hello"]
    assert len(made) == len(PKGS)
    srcs = RepoClient.from_url(make_repo(tmp_path / "s", sources=SRCS), suites=[SUITE])
    made.clear()
    assert [str(r.version) for r in srcs.sources(SUITE)] == ["2.10-3", "1"]
    assert len(made) == len(SRCS)


def test_external_cancel_during_probing_propagates_and_caches_nothing(tmp_path):
    from minideblib.repo.fetch import FetchCancelled

    base = make_repo(tmp_path, suite="stable", arches=["amd64"])

    class Stuck(Fetcher):
        """Probes fail transiently and back off for 30 s until *fail* is cleared."""

        def __init__(self) -> None:
            super().__init__(retries=2, retry_delay=30)
            self.fail = True

        def _open(self, url: str, method: str, attempt: int = 1) -> Any:
            if self.fail:
                raise FetchError("connection reset", url, transient=True)
            return super()._open(url, method, attempt)

    f = Stuck()
    c = RepoClient.from_url(base, fetcher=f)
    threading.Timer(0.2, f.cancel).start()
    started = time.monotonic()
    with pytest.raises(FetchCancelled):
        c.suites()
    assert time.monotonic() - started < 5
    assert c.problems == []  # not recorded as probe errors or "unreachable"
    f.fail = False
    assert c.suites() == ["stable"]  # nothing partial was cached


_TIERS = [
    ("Checksums-Sha512", "sha512"),
    ("Checksums-Sha256", "sha256"),
    ("Checksums-Sha1", "sha1"),
    ("Files", "md5"),
]
_DSC_DATA = b"dsc data\n"


def _hash_field(algo: str, data: bytes, name: str) -> str:
    import hashlib

    return f"\n {hashlib.new(algo, data).hexdigest()} {len(data)} {name}"


@pytest.mark.parametrize("strongest", range(len(_TIERS)))
def test_source_files_prefer_strongest_field(strongest):
    import hashlib

    fields = [(f, _hash_field(a, _DSC_DATA, "p_1.dsc")) for f, a in _TIERS[strongest:]]
    # field order in the stanza does not matter
    para = Paragraph([("Package", "p"), *reversed(fields)])
    rec = SourceRecord(para, "s", "main", base_url="http://x")
    algo = _TIERS[strongest][1]
    assert rec.files == [(hashlib.new(algo, _DSC_DATA).hexdigest(), len(_DSC_DATA), "p_1.dsc")]
    if algo in ("sha512", "sha256"):
        client_mod._check_listed_hash(para, "p_1.dsc", _DSC_DATA)  # same row is verified
    else:
        with pytest.raises(HashMismatch, match="SHA256 or stronger"):
            client_mod._check_listed_hash(para, "p_1.dsc", _DSC_DATA)


def test_source_files_merged_by_name_strongest_first():
    import hashlib

    para = Paragraph(
        [
            ("Package", "p"),
            ("Files", _hash_field("md5", b"a", "a.dsc") + _hash_field("md5", b"b", "b.tar.xz")),
            ("Checksums-Sha1", _hash_field("sha1", b"b", "b.tar.xz")),
            ("Checksums-Sha512", _hash_field("sha512", b"a", "a.dsc")),
        ]
    )
    rec = SourceRecord(para, "s", "main", base_url="http://x")
    assert rec.files == [
        (hashlib.sha512(b"a").hexdigest(), 1, "a.dsc"),
        (hashlib.sha1(b"b").hexdigest(), 1, "b.tar.xz"),
    ]


@pytest.mark.parametrize(
    "filename",
    ["pool/%2e%2e/%2e%2e/x.deb", "%2E%2E/x.deb", "pool%2fx.deb", "pool/%252e%252e/x.deb"],
)
def test_encoded_traversal_filename_rejected(filename):
    rec = PackageRecord(
        Paragraph([("Package", "p"), ("Filename", filename)]), SUITE, "main", "amd64", "http://x"
    )
    with pytest.raises(ValueError, match="unsafe"):
        rec.url  # noqa: B018


def test_encoded_traversal_directory_rejected():
    para = Paragraph(
        [
            ("Package", "p"),
            ("Directory", "pool/%2e%2e/%2e%2e"),
            ("Files", "\n " + "0" * 32 + " 1 p.dsc"),
        ]
    )
    with pytest.raises(ValueError, match="unsafe"):
        SourceRecord(para, "s", "main", base_url="http://x").file_urls  # noqa: B018


def test_legit_escapes_in_filename_kept():
    rec = PackageRecord(
        Paragraph([("Package", "p"), ("Filename", "pool/libstdc%2B%2B6_1%3a1_amd64.deb")]),
        SUITE,
        "main",
        "amd64",
        "http://x",
    )
    assert rec.url == "http://x/pool/libstdc%2B%2B6_1%3a1_amd64.deb"


def test_unsafe_suite_never_requested(tmp_path):
    base = make_repo(tmp_path, arches=["amd64"])
    f = RecordingFetcher()
    c = RepoClient([SourceEntry("deb", base, "../../outside", ())], fetcher=f)
    with pytest.raises(Exception, match="unsafe suite"):
        c.packages("../../outside")
    assert f.gets == [] and f.exists_calls == []


def _merged(*entries: SourceEntry) -> list[SourceEntry]:
    return RepoClient(list(entries))._entries("s", ("deb",))


@pytest.mark.parametrize("unrestricted_first", [True, False])
def test_merge_unrestricted_components_win(unrestricted_first):
    free = SourceEntry("deb", "http://x", "s", ())
    main = SourceEntry("deb", "http://x", "s", ("main",))
    pair = (free, main) if unrestricted_first else (main, free)
    (e,) = _merged(*pair)
    assert e.components == ()


@pytest.mark.parametrize("unrestricted_first", [True, False])
def test_merge_unrestricted_architectures_win(unrestricted_first):
    free = SourceEntry("deb", "http://x", "s", ("main",))
    amd = SourceEntry("deb", "http://x", "s", ("main",), architectures=("amd64",))
    pair = (free, amd) if unrestricted_first else (amd, free)
    (e,) = _merged(*pair)
    assert e.architectures == ()


def test_merge_restricted_union_keeps_order():
    (e,) = _merged(
        SourceEntry("deb", "http://x", "s", ("main", "contrib"), architectures=("i386",)),
        SourceEntry("deb", "http://x", "s", ("non-free", "main"), architectures=("amd64", "i386")),
    )
    assert e.components == ("main", "contrib", "non-free")
    assert e.architectures == ("i386", "amd64")


def test_merge_keeps_first_entry_options_and_types_separate():
    a = SourceEntry("deb", "http://x", "s", ("main",), signed_by="/k1.gpg", options={"x": "1"})
    b = SourceEntry("deb", "http://x", "s", ("contrib",), signed_by="/k2.gpg")
    src = SourceEntry("deb-src", "http://x", "s", ("non-free",))
    c = RepoClient([a, b, src])
    (e,) = c._entries("s", ("deb",))
    assert (e.signed_by, e.options, e.components) == ("/k1.gpg", {"x": "1"}, ("main", "contrib"))
    (s,) = c._entries("s", ("deb-src",))
    assert (s.type, s.components) == ("deb-src", ("non-free",))


def test_merged_unrestricted_entry_queries_every_component(tmp_path):
    base = make_repo(tmp_path, comps=["main", "universe"], arches=["amd64"])
    c = RepoClient(
        [SourceEntry("deb", base, SUITE, ("main",)), SourceEntry("deb", base, SUITE, ())]
    )
    assert sorted({r.component for r in c.packages(SUITE)}) == ["main", "universe"]
