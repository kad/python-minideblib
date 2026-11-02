# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Download plans, package groups, search filters and bounded state of RepoClient."""

import hashlib
import io
from pathlib import Path
from typing import Any, cast

import pytest

from minideblib.repo.client import (
    DownloadPlan,
    FileSpec,
    PackageGroup,
    PackageRecord,
    RepoClient,
    RepoError,
    SourceRecord,
)
from minideblib.repo.fetch import Fetcher
from tests.fixtures.repo import DSC_NAME, make_repo, multiarch_deb, sample_source_files

SUITE = "stable"
ARCHES = ("amd64", "arm64")


class CountingFetcher(Fetcher):
    def __init__(self) -> None:
        super().__init__(retries=0)
        self.gets: list[str] = []

    def get(self, url: str, **kw: Any) -> bytes:
        self.gets.append(url)
        return super().get(url, **kw)


def _repo(tmp_path: Path, **kw: Any) -> str:
    kw.setdefault("comps", ["main"])
    kw.setdefault("arches", ARCHES)
    return make_repo(tmp_path, SUITE, multiarch=True, dsc=True, **kw)


def _client(base: str, **kw: Any) -> RepoClient:
    return RepoClient.from_url(base, suites=[SUITE], **kw)


def test_arch_all_package_planned_once(tmp_path: Path) -> None:
    base = _repo(tmp_path)
    plans = _client(base).plan_binary(SUITE, "sample-data")
    assert len(plans) == 1
    plan = plans[0]
    assert isinstance(plan, DownloadPlan)
    data = multiarch_deb("sample-data", "1.1-1", "all")
    assert (plan.kind, plan.name, plan.version, plan.suite, plan.component, plan.architecture) == (
        "binary",
        "sample-data",
        "1.1-1",
        SUITE,
        "main",
        "all",
    )
    (spec,) = plan.files
    assert isinstance(spec, FileSpec)
    assert spec.name == "sample-data_1.1-1_all.deb"
    assert spec.url == f"{base}/pool/main/s/sample-data/sample-data_1.1-1_all.deb"
    assert spec.size == len(data)
    # the strongest hash listed (SHA512 beats SHA256)
    assert dict(spec.hashes) == {"sha512": hashlib.sha512(data).hexdigest()}


def test_plan_file_downloads_and_verifies(tmp_path: Path) -> None:
    base = _repo(tmp_path)
    (plan,) = _client(base).plan_binary(SUITE, "libsample1", architectures=["arm64"])
    (spec,) = plan.files
    out = io.BytesIO()
    res = Fetcher().download(
        spec.url,
        out,
        max_bytes=10**6,
        expected_size=spec.size,
        expected_hashes=spec.hashes,
    )
    assert out.getvalue() == multiarch_deb("libsample1", "1.1-1", "arm64")
    assert res.size == spec.size


def test_arch_specific_package_one_plan_per_architecture(tmp_path: Path) -> None:
    base = _repo(tmp_path)
    plans = _client(base).plan_binary(SUITE, "libsample1")
    assert [p.architecture for p in plans] == ["amd64", "arm64"]
    sizes = {p.files[0].size for p in plans}
    hashes = {p.files[0].hashes["sha512"] for p in plans}
    assert len(sizes) == 2 and len(hashes) == 2
    for p in plans:
        data = multiarch_deb("libsample1", "1.1-1", p.architecture or "")
        assert p.files[0].size == len(data)
        assert p.files[0].hashes["sha512"] == hashlib.sha512(data).hexdigest()


def test_plan_architecture_filter(tmp_path: Path) -> None:
    c = _client(_repo(tmp_path))
    assert [
        p.architecture for p in c.plan_binary(SUITE, "libsample1", architectures=["arm64"])
    ] == ["arm64"]
    assert [p.architecture for p in c.plan_binary(SUITE, "sample-data", architectures=["all"])] == [
        "all"
    ]
    # Architecture: all packages are included for any architecture, reported as "all";
    # "all" alone selects only them
    (data,) = c.plan_binary(SUITE, "sample-data", architectures=["amd64"])
    assert data.architecture == "all"
    assert c.plan_binary(SUITE, "libsample1", architectures=["all"]) == []
    assert c.plan_binary(SUITE, "no-such-package") == []
    # an empty filter means no filter
    assert len(c.plan_binary(SUITE, "libsample1", architectures=[])) == 2


def test_plan_version_selection(tmp_path: Path) -> None:
    c = _client(_repo(tmp_path))
    assert {p.version for p in c.plan_binary(SUITE, "sample-tool")} == {"1.1-1"}
    older = c.plan_binary(SUITE, "sample-tool", version="1.0-1")
    assert [(p.architecture, p.version) for p in older] == [("amd64", "1.0-1"), ("arm64", "1.0-1")]
    # Debian version equality: an explicit zero epoch is the same version
    assert len(c.plan_binary(SUITE, "sample-tool", version="0:1.0-1")) == 2
    assert c.plan_binary(SUITE, "sample-tool", version="9") == []


def test_plan_components_filter(tmp_path: Path) -> None:
    c = _client(_repo(tmp_path, comps=["main", "extra"]))
    plans = c.plan_binary(SUITE, "sample-data")
    # the same package in two components has two different pool files
    assert sorted(p.component for p in plans) == ["extra", "main"]
    only = c.plan_binary(SUITE, "sample-data", components=["extra"])
    assert [p.component for p in only] == ["extra"]


def test_plan_no_network_besides_indices(tmp_path: Path) -> None:
    f = CountingFetcher()
    c = _client(_repo(tmp_path), fetcher=f)
    c.plan_binary(SUITE, "libsample1")
    c.plan_source(SUITE, "sample-pkg")
    assert not [u for u in f.gets if "/pool/" in u]
    before = len(f.gets)
    c.plan_binary(SUITE, "libsample1")
    assert len(f.gets) == before  # indices come from the cache


def test_unsafe_filename_skipped_with_problem(tmp_path: Path) -> None:
    pkgs = [
        {"Package": "evil", "Version": "1", "Filename": "../../etc/evil.deb", "SHA256": "a" * 64},
        {"Package": "evil", "Version": "0.9", "Filename": "pool/e/evil_0.9.deb", "Size": "5"},
        {"Package": "nofile", "Version": "1"},
    ]
    base = make_repo(tmp_path, SUITE, comps=["main"], arches=["amd64"], packages=pkgs)
    c = _client(base)
    plans = c.plan_binary(SUITE, "evil")
    assert [(p.version, p.files[0].name) for p in plans] == [("0.9", "evil_0.9.deb")]
    assert dict(plans[0].files[0].hashes) == {}  # none listed
    assert any("evil" in p and "unsafe" in p for p in c.problems)
    assert c.plan_binary(SUITE, "nofile") == []
    assert any("nofile" in p and "Filename" in p for p in c.problems)


def test_source_plan(tmp_path: Path) -> None:
    base = _repo(tmp_path)
    plan = _client(base).plan_source(SUITE, "sample-pkg")
    files = sample_source_files()
    assert (plan.kind, plan.name, plan.version, plan.architecture, plan.component) == (
        "source",
        "sample-pkg",
        "1.0-1",
        None,
        "main",
    )
    assert plan.files[0].name == DSC_NAME
    assert {s.name for s in plan.files} == set(files)
    for spec in plan.files:
        assert spec.url == f"{base}/pool/main/s/sample-pkg/{spec.name}"
        assert spec.size == len(files[spec.name])
        assert dict(spec.hashes) == {"sha256": hashlib.sha256(files[spec.name]).hexdigest()}


def test_source_plan_merges_strongest_hashes(tmp_path: Path) -> None:
    dsc, orig = b"dsc", b"orig tarball"

    def row(algo: str, data: bytes, name: str) -> str:
        return f"\n {hashlib.new(algo, data).hexdigest()} {len(data)} {name}"

    stanza = {
        "Package": "multi",
        "Version": "2-1",
        "Directory": "pool/main/m/multi",
        # the tarball first, the .dsc only in weaker fields than SHA512
        "Checksums-Sha512": row("sha512", orig, "multi_2.orig.tar.gz"),
        "Checksums-Sha256": row("sha256", orig, "multi_2.orig.tar.gz")
        + row("sha256", dsc, "multi_2-1.dsc"),
        "Files": row("md5", orig, "multi_2.orig.tar.gz") + row("md5", dsc, "multi_2-1.dsc"),
    }
    older = dict(stanza, Version="1-1")
    base = make_repo(tmp_path, SUITE, comps=["main"], arches=["amd64"], sources=[older, stanza])
    c = _client(base)
    plan = c.plan_source(SUITE, "multi")
    assert plan.version == "2-1"
    assert [s.name for s in plan.files] == ["multi_2-1.dsc", "multi_2.orig.tar.gz"]
    assert dict(plan.files[0].hashes) == {"sha256": hashlib.sha256(dsc).hexdigest()}
    assert dict(plan.files[1].hashes) == {"sha512": hashlib.sha512(orig).hexdigest()}
    assert c.plan_source(SUITE, "multi", version="1-1").version == "1-1"
    with pytest.raises(RepoError, match="multi"):
        c.plan_source(SUITE, "multi", version="3")
    with pytest.raises(RepoError, match="absent"):
        c.plan_source(SUITE, "absent")
    with pytest.raises(RepoError):
        c.plan_source(SUITE, "multi", components=["contrib"])


def test_group_packages(tmp_path: Path) -> None:
    c = _client(_repo(tmp_path))
    groups = c.group_packages(c.packages(SUITE))
    assert all(isinstance(g, PackageGroup) for g in groups)
    assert [(g.name, g.version) for g in groups] == [
        ("libsample1", "1.1-1"),
        ("sample-data", "1.1-1"),
        ("sample-tool", "1.1-1"),
        ("sample-tool", "1.0-1"),
    ]
    lib, data, tool, _ = groups
    assert lib.architectures == ("amd64", "arm64")
    assert lib.multi_arch == "same"
    assert [v.arch for v in lib.variants] == ["amd64", "arm64"]
    assert len({v.size for v in lib.variants}) == 2
    # Architecture: all listed in both indexes: one variant
    assert data.architectures == ("all",)
    assert len(data.variants) == 1 and isinstance(data.variants[0], PackageRecord)
    assert data.multi_arch == "foreign"
    assert (tool.suite, tool.component) == (SUITE, "main")
    assert RepoClient.group_packages([]) == []


def test_group_packages_version_ordering_is_debian() -> None:
    from minideblib.control import Paragraph

    def rec(version: str) -> PackageRecord:
        para = Paragraph([("Package", "p"), ("Version", version), ("Architecture", "amd64")])
        return PackageRecord(para, SUITE, "main", "amd64")

    groups = RepoClient.group_packages([rec("1.0~rc1"), rec("1:0.1"), rec("1.0"), rec("1.10")])
    assert [g.version for g in groups] == ["1:0.1", "1.10", "1.0", "1.0~rc1"]


def test_search_filters(tmp_path: Path) -> None:
    c = _client(_repo(tmp_path, comps=["main", "extra"]))
    everything = c.search(SUITE, "sample")
    assert {r.component for r in everything} == {"main", "extra"}
    # the old positional signature still works
    assert c.search(SUITE, "libsample*") == c.search(SUITE, "libsample*", kind="binary")
    arm = c.search(SUITE, "sample", architectures=["arm64"])
    assert arm and {r.arch for r in arm} == {"arm64"}
    alls = c.search(SUITE, "sample", architectures=["all"])
    assert alls and {r.paragraph["Architecture"] for r in alls} == {"all"}
    extra = c.search(SUITE, "sample", components=["extra"])
    assert extra and {r.component for r in extra} == {"extra"}
    sources = c.search(SUITE, "sample-p*", kind="source")
    assert sources and all(isinstance(r, SourceRecord) for r in sources)
    assert {r.name for r in sources} == {"sample-pkg"}
    assert c.search(SUITE, "pkg-doc", kind="source")  # matches a binary name
    only_main = c.search(SUITE, "sample", kind="source", components=["main"])
    assert {r.component for r in only_main} == {"main"}
    with pytest.raises(ValueError, match="kind"):
        c.search(SUITE, "x", kind=cast(Any, "other"))


def test_problems_bounded(tmp_path: Path) -> None:
    c = _client(_repo(tmp_path))
    assert c.problems_mark() == 0 and c.problems_dropped == 0
    for i in range(50):
        c._problem(f"p{i}")
    mark = c.problems_mark()
    assert mark == 50
    for i in range(50, 1100):
        c._problem(f"p{i}")
    assert len(c.problems) == 1000
    assert c.problems[0] == "p100" and c.problems[-1] == "p1099"
    assert c.problems_dropped == 100
    assert c.problems_mark() == 1100
    since = c.problems_since(mark)
    assert since[0] == "p100" and len(since) == 1000  # older ones were dropped
    m2 = c.problems_mark()
    c._problem("new")
    assert c.problems_since(m2) == ["new"]
    assert c.problems_since(c.problems_mark()) == []


def test_cache_budget_evicts_least_recently_used(tmp_path: Path) -> None:
    f = CountingFetcher()
    c = _client(_repo(tmp_path), fetcher=f, cache_budget_bytes=1)
    first = c.packages(SUITE, arch="amd64")
    assert c.cache_bytes > 0
    c.packages(SUITE, arch="arm64")
    # a budget of one byte keeps only the index loaded last
    assert len(c._parsed) == 1
    n = len(f.gets)
    again = c.packages(SUITE, arch="amd64")  # reloaded
    assert len(f.gets) > n
    assert [r.paragraph for r in again] == [r.paragraph for r in first]
    c.clear_cache()
    assert c.cache_bytes == 0


def test_cache_budget_default_keeps_everything(tmp_path: Path) -> None:
    f = CountingFetcher()
    c = _client(_repo(tmp_path), fetcher=f)
    c.packages(SUITE)
    n = len(f.gets)
    c.packages(SUITE)
    assert len(f.gets) == n
    assert len(c._parsed) == 2


def test_cache_budget_recent_use_protects_entry(tmp_path: Path) -> None:
    base = _repo(tmp_path, arches=("amd64", "arm64", "i386"))
    probe = _client(base)
    probe.packages(SUITE, arch="amd64")
    one = probe.cache_bytes
    # room for two indices
    c = _client(base, cache_budget_bytes=2 * one + one // 2)
    c.packages(SUITE, arch="amd64")
    c.packages(SUITE, arch="arm64")
    c.packages(SUITE, arch="amd64")  # touch: arm64 is now least recently used
    c.packages(SUITE, arch="i386")
    kept = {path for _, _, path in c._parsed}
    assert any("binary-amd64" in p for p in kept)
    assert any("binary-i386" in p for p in kept)
    assert not any("binary-arm64" in p for p in kept)


def _pkg(name: str, version: str, arch: str) -> dict[str, str]:
    return {
        "Package": name,
        "Version": version,
        "Architecture": arch,
        "Filename": f"pool/main/f/{name}/{name}_{version}_{arch}.deb",
        "Size": "10",
        "SHA256": hashlib.sha256(f"{name}{version}{arch}".encode()).hexdigest(),
    }


def test_plan_package_moved_to_arch_all(tmp_path: Path) -> None:
    by_arch = {
        "amd64": [_pkg("foo", "1.0", "amd64"), _pkg("foo", "2.0", "all")],
        "arm64": [_pkg("foo", "2.0", "all"), _pkg("foo", "1.0", "arm64")],
    }
    c = _client(make_repo(tmp_path, SUITE, comps=["main"], arches=ARCHES, packages_by_arch=by_arch))
    plans = c.plan_binary(SUITE, "foo")
    assert [(p.architecture, p.version) for p in plans] == [("all", "2.0")]
    plans = c.plan_binary(SUITE, "foo", architectures=["arm64"])
    assert [(p.architecture, p.version) for p in plans] == [("all", "2.0")]


def test_plan_package_moved_from_arch_all(tmp_path: Path) -> None:
    # amd64 already has the arch-specific 2.0, arm64 still only the old all 1.0
    by_arch = {
        "amd64": [_pkg("foo", "1.0", "all"), _pkg("foo", "2.0", "amd64")],
        "arm64": [_pkg("foo", "1.0", "all")],
    }
    c = _client(make_repo(tmp_path, SUITE, comps=["main"], arches=ARCHES, packages_by_arch=by_arch))
    plans = c.plan_binary(SUITE, "foo", architectures=["amd64"])
    assert [(p.architecture, p.version) for p in plans] == [("amd64", "2.0")]
    plans = c.plan_binary(SUITE, "foo")
    assert [(p.architecture, p.version) for p in plans] == [("all", "1.0"), ("amd64", "2.0")]


def _binary_all_repo(tmp_path: Path, **kw: Any) -> str:
    by_arch = {
        "all": [_pkg("data", "1", "all")],
        "amd64": [_pkg("tool", "1", "amd64")],
        "arm64": [_pkg("tool", "1", "arm64")],
    }
    return make_repo(
        tmp_path,
        SUITE,
        comps=["main"],
        arches=("amd64", "arm64", "all"),
        packages_by_arch=by_arch,
        **kw,
    )


def test_plan_reads_binary_all_index(tmp_path: Path) -> None:
    f = CountingFetcher()
    c = _client(_binary_all_repo(tmp_path), fetcher=f)
    (plan,) = c.plan_binary(SUITE, "data", architectures=["amd64"])
    assert plan.architecture == "all"
    assert not [u for u in f.gets if "binary-arm64" in u]
    assert c.search(SUITE, "data", architectures=["amd64"])
    f.gets.clear()
    # "all" alone: the binary-all index plus one concrete architecture, not all of them
    (plan,) = _client(_binary_all_repo(tmp_path / "b"), fetcher=f).plan_binary(
        SUITE, "data", architectures=["all"]
    )
    assert plan.architecture == "all"
    read = [u for u in f.gets if "/binary-" in u]
    assert any("binary-all" in u for u in read)
    assert len({u.split("/binary-")[1].split("/")[0] for u in read}) == 2


def test_plan_no_binary_all_with_no_support_field(tmp_path: Path) -> None:
    f = CountingFetcher()
    base = _binary_all_repo(tmp_path, extra_fields={"No-Support-for-Architecture-all": "Packages"})
    c = _client(base, fetcher=f)
    assert c.plan_binary(SUITE, "tool", architectures=["amd64"])
    assert not [u for u in f.gets if "binary-all" in u]


def test_group_packages_normalises_versions() -> None:
    from minideblib.control import Paragraph

    def rec(version: str, arch: str) -> PackageRecord:
        para = Paragraph([("Package", "p"), ("Version", version), ("Architecture", arch)])
        return PackageRecord(para, SUITE, "main", arch)

    groups = RepoClient.group_packages([rec("1.0", "amd64"), rec("0:1.0", "arm64")])
    assert len(groups) == 1 and groups[0].architectures == ("amd64", "arm64")


def test_skip_problems_recorded_once(tmp_path: Path) -> None:
    pkgs = [
        {"Package": "evil", "Version": "1", "Filename": "../evil.deb"},
        {"Package": "nofile", "Version": "1"},
    ]
    c = _client(make_repo(tmp_path, SUITE, comps=["main"], arches=["amd64"], packages=pkgs))
    for _ in range(3):
        c.plan_binary(SUITE, "evil")
        c.plan_binary(SUITE, "nofile")
        c.packages(SUITE)
    assert len([p for p in c.problems if "evil" in p]) == 1
    assert len([p for p in c.problems if "nofile" in p]) == 1


def test_concurrent_use_is_safe(tmp_path: Path) -> None:
    import threading
    import time

    c = _client(_repo(tmp_path, arches=("amd64", "arm64", "i386")), cache_budget_bytes=1)
    errors: list[BaseException] = []
    stop = time.monotonic() + 1.0

    def work(i: int) -> None:
        try:
            n = 0
            while time.monotonic() < stop:
                arch = ARCHES[(i + n) % 2] if i % 3 else "i386"
                assert c.packages(SUITE, arch=arch)
                c._problem(f"t{i}-{n}")
                c.problems_since(c.problems_mark() - 5)
                _ = c.cache_bytes
                n += 1
        except BaseException as e:  # reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert c.problems_mark() == len(c.problems) + c.problems_dropped
    assert len(c.problems) <= 1000
