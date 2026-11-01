# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Tests for the ``minideb`` command line interface (offline, fixture repositories)."""

import argparse
import json
import re
from pathlib import Path

import pytest

from minideblib import __version__, cli
from tests.conftest import live_target
from tests.fixtures.debs import make_deb
from tests.fixtures.repo import DSC_NAME, DSC_VERSION, SRC_FILES, make_repo, sample_source_files

SUITE = "stable/slash-test/1.0"
PKG = [
    {
        "Package": "sample",
        "Version": "0.4-1",
        "Maintainer": "T <t@example.com>",
        "Filename": "pool/main/s/sample/sample_0.4-1_all.deb",
        "Description": "sample package",
    }
]
CONTENTS = "usr/bin/tool-amd64  utils/sample\nusr/bin/tool-i386  utils/sample\n"


def mk(root, **kw):
    kw.setdefault("comps", ("main",))
    kw.setdefault("contents", CONTENTS)
    return make_repo(root, SUITE, **kw)


@pytest.fixture
def repo(tmp_path):
    return mk(tmp_path / "repo", packages=PKG)


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_components_and_archs(repo, capsys):
    assert run(capsys, "--url", repo, "components", SUITE) == (0, "main\n", "")
    assert run(capsys, "--url", repo, "archs", SUITE) == (0, "amd64\ni386\n", "")


def test_json_components(repo, capsys):
    code, out, err = run(capsys, "--url", repo, "--json", "components", SUITE)
    assert (code, json.loads(out), err) == (0, ["main"], "")


def test_list_text_dedupes_all(tmp_path, capsys):
    pk = [
        {"Package": "a", "Version": "1", "Architecture": "all"},
        {"Package": "b", "Version": "2"},
    ]
    url = mk(tmp_path / "r", packages=pk)
    code, out, _ = run(capsys, "--url", url, "list", SUITE)
    assert code == 0
    assert out.splitlines() == ["a 1 all main", "b 2 amd64 main", "b 2 i386 main"]
    code, out, _ = run(capsys, "--url", url, "--json", "list", SUITE, "-c", "main")
    recs = json.loads(out)
    assert [r["arch"] for r in recs if r["name"] == "a"] == ["amd64", "i386"]
    assert {"name", "version", "arch", "component", "suite"} <= set(recs[0])


def test_list_arch_filter(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "list", SUITE, "-c", "main", "-a", "amd64")
    assert (code, out) == (0, "sample 0.4-1 amd64 main\n")


def test_list_source(tmp_path, capsys):
    src = [{"Package": "s", "Version": "3", "Directory": "pool/s"}]
    url = mk(tmp_path / "r", sources=src)
    code, out, _ = run(capsys, "--url", url, "list", SUITE, "--source")
    assert (code, out) == (0, "s 3 source main\n")


def test_show(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "show", SUITE, "sample")
    assert code == 0
    assert out.startswith("Package: sample\nVersion: 0.4-1\n")
    code, out, _ = run(capsys, "--url", repo, "--json", "show", SUITE, "sample")
    docs = json.loads(out)
    assert list(docs[0])[:3] == ["Package", "Version", "Maintainer"]
    assert docs[0]["suite"] == SUITE and docs[0]["component"] == "main"
    assert docs[0]["url"].endswith("sample_0.4-1_all.deb")


def test_show_missing_exit_1(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "show", SUITE, "nope")
    assert (code, out) == (1, "")


def test_versions(repo, capsys):
    # url mode probes candidate suites only; the slash suite is not a candidate,
    # so nothing is found: exit status 1 and nothing on stdout (also with --json)
    code, out, _ = run(capsys, "--url", repo, "versions", "sample")
    assert (code, out) == (1, "")
    code, out, _ = run(capsys, "--url", repo, "--json", "versions", "sample")
    assert (code, out) == (1, "")
    code, out, _ = run(capsys, "--url", repo, "versions", "sample", "--suite", SUITE)
    assert code == 0 and out.startswith(f"{SUITE} sample 0.4-1")


def test_versions_with_sources(repo, tmp_path, capsys):
    sf = tmp_path / "x.list"
    sf.write_text(f"deb [trusted=yes] {repo} {SUITE} main\n")
    code, out, _ = run(capsys, "--sources", sf, "versions", "sample")
    assert code == 0
    assert out.splitlines() == [
        f"{SUITE} sample 0.4-1 amd64 main",
        f"{SUITE} sample 0.4-1 i386 main",
    ]


def test_search(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "search", SUITE, "sample")
    assert code == 0 and "sample 0.4-1 amd64 main" in out
    code, out, _ = run(capsys, "--url", repo, "--json", "search", SUITE, "zzz")
    assert (code, json.loads(out)) == (0, [])


def test_files(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "files", SUITE, "usr/bin/tool-amd64")
    assert code == 0 and out.split() == ["usr/bin/tool-amd64", "utils/sample"]
    code, out, _ = run(capsys, "--url", repo, "--json", "files", SUITE, "tool-i386")
    assert json.loads(out) == [{"path": "usr/bin/tool-i386", "package": "utils/sample"}]
    assert run(capsys, "--url", repo, "files", SUITE, "nothing")[0] == 1


def test_suites_with_sources_file(tmp_path, capsys):
    sf = tmp_path / "ubuntu.sources"
    sf.write_text(
        "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\n"
        "Suites: noble noble-updates noble-backports\nComponents: main\n"
    )
    code, out, _ = run(capsys, "--sources", sf, "suites")
    assert (code, out.split()) == (0, ["noble", "noble-updates", "noble-backports"])
    code, out, _ = run(capsys, "--sources", sf, "--json", "suites")
    assert json.loads(out) == ["noble", "noble-updates", "noble-backports"]


def test_suites_url_probe_with_suite_option(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "suites", "--suite", SUITE)
    assert (code, out) == (0, SUITE + "\n")


def test_multiple_urls(tmp_path, capsys):
    a = mk(tmp_path / "a", comps=("one",))
    b = mk(tmp_path / "b", comps=("two",))
    code, out, _ = run(capsys, "--url", a, "--url", b, "components", SUITE)
    assert (code, out.split()) == (0, ["one", "two"])


def test_hash_mismatch_exit_2_and_no_verify(tmp_path, capsys):
    url = mk(tmp_path / "r", uncompressed=True)
    idx = tmp_path / "r" / "dists" / SUITE / "main" / "binary-amd64" / "Packages"
    idx.write_bytes(idx.read_bytes() + b"\n\nPackage: evil\nVersion: 1\n")
    (idx.parent / "Packages.gz").unlink()
    code, out, err = run(capsys, "--url", url, "list", SUITE, "-a", "amd64")
    assert code == 2 and out == ""
    assert re.fullmatch(r"minideb: error: .+\n", err)
    code, out, _ = run(capsys, "--url", url, "--no-verify", "list", SUITE, "-a", "amd64")
    assert code == 0 and "evil" in out


def test_missing_repository_exit_2(tmp_path, capsys):
    code, out, err = run(capsys, "--url", (tmp_path / "nonexistent").as_uri(), "archs", SUITE)
    assert code == 2 and out == "" and err.startswith("minideb: error:")
    code, _, err = run(capsys, "--url", "gopher://x", "--retries", "0", "archs", SUITE)
    assert code == 2 and err.startswith("minideb: error:")


def test_unknown_suite_exit_2(repo, capsys):
    code, out, err = run(capsys, "--url", repo, "components", "nosuch")
    assert code == 2 and out == ""
    assert err.splitlines()[0].startswith("minideb: error: suite 'nosuch' not found at ")


def test_missing_index_is_warning_not_error(tmp_path, capsys):
    url = mk(tmp_path / "r", packages=PKG, xz=False)
    d = tmp_path / "r" / "dists" / SUITE / "main"
    for f in d.glob("binary-*/Packages*"):
        f.unlink()
    code, _, err = run(capsys, "--url", url, "--json", "list", SUITE)
    assert code == 0  # missing indices are reported as problems, not errors
    assert "minideb: warning:" in err


def test_failing_json_prints_nothing(tmp_path, capsys):
    code, out, _ = run(capsys, "--json", "--url", (tmp_path / "x").as_uri(), "archs", SUITE)
    assert (code, out) == (2, "")


def test_show_several_versions_separated_by_one_blank_line(tmp_path, capsys):
    pk = [
        {"Package": "p", "Version": "1", "Filename": "a.deb"},
        {"Package": "p", "Version": "2", "Filename": "b.deb"},
    ]
    url = mk(tmp_path / "r", packages=pk, arches=("amd64",))
    out = run(capsys, "--url", url, "show", SUITE, "p")[1]
    assert out == "Package: p\nVersion: 1\nFilename: a.deb\nArchitecture: amd64\n\n" + (
        "Package: p\nVersion: 2\nFilename: b.deb\nArchitecture: amd64\n"
    )


def test_keyboard_interrupt_and_broken_pipe(repo, monkeypatch, capsys):
    def interrupt(ctx):
        raise KeyboardInterrupt

    def pipe(ctx):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_silence_stdout", lambda: None)
    for func, want in ((interrupt, 130), (pipe, 0)):
        monkeypatch.setattr(cli, "cmd_components", func)
        parser = cli.build_parser()
        monkeypatch.setattr(cli, "build_parser", lambda p=parser: p)
        parser.set_defaults(func=func)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        sub.choices["components"].set_defaults(func=func)
        assert run(capsys, "--url", repo, "components", SUITE) == (want, "", "")


def test_deb_field_json_missing_prints_nothing(deb, capsys):
    assert run(capsys, "--json", "deb", "field", deb, "Package", "Depends")[:2] == (1, "")


def test_dest_is_file(deb, tmp_path, capsys):
    f = tmp_path / "afile"
    f.write_text("x")
    for sub in ("ctrl", "extract"):
        code, _, err = run(capsys, "deb", sub, deb, f)
        assert code == 2 and err == f"minideb: error: destination {f} is not a directory\n"


def test_unknown_suite_in_sources_exit_2(tmp_path, capsys):
    sf = tmp_path / "a.list"
    sf.write_text("deb http://x.invalid/ noble main\n")
    code, _, err = run(capsys, "--sources", sf, "components", "other")
    assert code == 2 and "not configured" in err


def test_problems_on_stderr(tmp_path, capsys):
    url = mk(tmp_path / "r")
    (tmp_path / "r" / "dists" / SUITE / "main" / "binary-i386" / "Packages").unlink()
    (tmp_path / "r" / "dists" / SUITE / "main" / "binary-i386" / "Packages.gz").unlink()
    code, out, err = run(capsys, "--url", url, "list", SUITE)
    assert code == 0 and "amd64" in out
    assert "minideb: warning:" in err
    code, out, err = run(capsys, "--url", url, "--json", "list", SUITE)
    assert code == 0 and isinstance(json.loads(out), list)
    assert "minideb: warning:" in err


def test_no_args_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_epilog_and_no_vendor_names(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    main_help = capsys.readouterr().out
    assert "signature" in main_help and "not verified" in main_help
    assert "unauthenticated" in main_help
    subs = ["suites", "components", "archs", "list", "show", "versions", "search", "files", "deb"]
    texts = [main_help]
    for sub in subs:
        argv = ["deb", "--help"] if sub == "deb" else [sub, "--help"]
        with pytest.raises(SystemExit):
            cli.main(argv)
        texts.append(capsys.readouterr().out)
    for dsub in ("info", "ctrl", "ls", "extract", "field"):
        with pytest.raises(SystemExit):
            cli.main(["deb", dsub, "--help"])
        texts.append(capsys.readouterr().out)
    vendor = "in" + "tel"
    assert all(vendor not in t.lower() for t in texts)


# -- deb ----------------------------------------------------------------------

CONTROL = {"Package": "hello", "Version": "1.0", "Architecture": "amd64", "Description": "Hi"}


@pytest.fixture
def deb(tmp_path):
    p = tmp_path / "hello.deb"
    p.write_bytes(make_deb(CONTROL, {"usr/bin/hello": b"#!/bin/sh\n", "usr/share/x": b"x"}))
    return p


def test_deb_info(deb, capsys):
    code, out, _ = run(capsys, "deb", "info", deb)
    assert code == 0 and "Package: hello\n" in out and "Version: 1.0\n" in out
    code, out, _ = run(capsys, "--json", "deb", "info", deb)
    assert json.loads(out)["Package"] == "hello"


def test_deb_field_single_and_multiple(deb, capsys):
    assert run(capsys, "deb", "field", deb, "Package") == (0, "hello\n", "")
    assert run(capsys, "deb", "field", deb, "package", "Version")[1] == (
        "Package: hello\nVersion: 1.0\n"
    )
    code, out, _ = run(capsys, "deb", "field", deb, "Depends")
    assert (code, out) == (1, "")
    code, out, _ = run(capsys, "--json", "deb", "field", deb, "Package", "Version")
    assert json.loads(out) == {"Package": "hello", "Version": "1.0"}


def test_deb_ls_shape(deb, capsys):
    code, out, _ = run(capsys, "deb", "ls", deb)
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "drwxr-xr-x root/root 0 1970-01-01 00:00 ./"
    pat = re.compile(
        r"^[-dlh][r-][w-][x-][r-][w-][x-][r-][w-][x-] \S+/\S+ +\d+ \d{4}-\d\d-\d\d \d\d:\d\d \./"
    )
    assert all(pat.match(ln) for ln in lines)
    assert any(
        ln.startswith("-rwxr-xr-x root/root 10 2023-11-14 22:13 ./usr/bin/hello") for ln in lines
    )
    docs = json.loads(run(capsys, "--json", "deb", "ls", deb)[1])
    assert docs[1]["path"] and "size" in docs[1]


def test_deb_ctrl(deb, tmp_path, capsys):
    dest = tmp_path / "ctl"
    assert run(capsys, "deb", "ctrl", deb, dest)[0] == 0
    assert b"Package: hello" in (dest / "control").read_bytes()
    assert (dest / "md5sums").exists()


def test_deb_extract(deb, tmp_path, capsys):
    dest = tmp_path / "out"
    assert run(capsys, "deb", "extract", deb, dest)[0] == 0
    assert (dest / "usr/bin/hello").read_bytes() == b"#!/bin/sh\n"
    code, _, err = run(capsys, "deb", "extract", deb, tmp_path / "o2", "--max-size", "3")
    assert code == 2 and "exceeds" in err


def test_deb_extract_hostile(tmp_path, capsys):
    p = tmp_path / "evil.deb"
    p.write_bytes(make_deb(CONTROL, {"../evil": b"x", "ok": b"y"}))
    dest = tmp_path / "sandbox" / "dest"
    code, _, err = run(capsys, "deb", "extract", p, dest)
    assert code == 2 and err.startswith("minideb: error:")
    assert not (tmp_path / "sandbox" / "evil").exists()
    assert not (dest / "ok").exists()


def test_bad_deb_and_missing_file(tmp_path, capsys):
    bad = tmp_path / "bad.deb"
    bad.write_bytes(b"not a deb")
    code, out, err = run(capsys, "deb", "info", bad)
    assert code == 2 and out == "" and err.startswith("minideb: error:")
    code, _, err = run(capsys, "deb", "info", tmp_path / "missing.deb")
    assert code == 2 and err.startswith("minideb: error:")


def test_traceback_only_with_vv(tmp_path, capsys):
    bad = tmp_path / "bad.deb"
    bad.write_bytes(b"x")
    assert "Traceback" not in run(capsys, "deb", "info", bad)[2]
    assert "Traceback" in run(capsys, "-vv", "deb", "info", bad)[2]


def test_main_module_importable():
    assert Path(cli.__file__).parent.joinpath("__main__.py").exists()


@pytest.mark.network
def test_live_components_and_list(capsys):
    """Target is configurable: MINIDEB_TEST_URL/SUITE/COMPONENT/ARCH."""
    url, suite, comp, arch = live_target()
    code, out, _ = run(capsys, "--json", "--url", url, "components", suite)
    assert code == 0 and comp in json.loads(out)
    code, out, _ = run(capsys, "--url", url, "list", suite, "-c", comp, "-a", arch)
    assert code == 0 and out.strip()


def test_missing_zstd_codec_exit_2(tmp_path, capsys, monkeypatch):
    from minideblib import compress

    zst = {"main/binary-amd64/Packages.zst": b"\x28\xb5\x2f\xfd" + b"\0" * 16}
    url = mk(tmp_path / "r", extra_files=zst)
    monkeypatch.setattr(compress, "_zstd_open", lambda data: compress._no_zstd())
    code, out, err = run(capsys, "--url", url, "list", SUITE, "-a", "amd64")
    assert (code, out) == (2, "")
    assert re.fullmatch(r"minideb: error: .*pip install 'minideblib\[zstd\]'.*\n", err)


# -- dsc / source ---------------------------------------------------------------


@pytest.fixture
def srcdir(tmp_path):
    d = tmp_path / "srcdir"
    d.mkdir()
    for name, data in sample_source_files().items():
        (d / name).write_bytes(data)
    return d


def test_dsc_info(srcdir, capsys):
    code, out, err = run(capsys, "dsc", "info", srcdir / DSC_NAME)
    assert (code, err) == (0, "")
    assert "Source: sample-pkg\n" in out
    assert f"Version: {DSC_VERSION}\n" in out
    assert "Format: 3.0 (quilt)\n" in out
    assert "Binary: sample-pkg, sample-pkg-doc, sample-pkg-dev\n" in out
    code, out, _ = run(capsys, "--json", "dsc", "info", srcdir / DSC_NAME)
    doc = json.loads(out)
    assert code == 0
    assert doc["source"] == "sample-pkg" and doc["version"] == DSC_VERSION
    assert doc["format"] == "3.0 (quilt)" and doc["is_native"] is False
    assert doc["binaries"] == ["sample-pkg", "sample-pkg-doc", "sample-pkg-dev"]
    assert doc["package_list"][0]["name"] == "sample-pkg"
    assert [f["name"] for f in doc["files"]] == list(SRC_FILES)


def test_dsc_files(srcdir, capsys):
    code, out, _ = run(capsys, "dsc", "files", srcdir / DSC_NAME)
    lines = out.splitlines()
    assert code == 0 and len(lines) == len(SRC_FILES)
    first = next(iter(SRC_FILES))
    assert lines[0].split()[0] == first
    assert str(len(SRC_FILES[first])) in lines[0].split()
    assert any(t.startswith("sha256:") for t in lines[0].split())
    code, out, _ = run(capsys, "--json", "dsc", "files", srcdir / DSC_NAME)
    docs = json.loads(out)
    assert docs[0]["name"] == first and docs[0]["size"] == len(SRC_FILES[first])
    assert len(docs[0]["sha256"]) == 64 and len(docs[0]["md5"]) == 32
    assert docs[0]["sha512"] is None


def test_dsc_verify_ok(srcdir, capsys, tmp_path, monkeypatch):
    assert run(capsys, "dsc", "verify", srcdir / DSC_NAME)[0] == 0
    code, out, _ = run(capsys, "--json", "dsc", "verify", srcdir / DSC_NAME)
    assert code == 0 and json.loads(out) == {"verified": True, "files": len(SRC_FILES)}
    other = tmp_path / "elsewhere"
    other.mkdir()
    for name, data in SRC_FILES.items():
        (other / name).write_bytes(data)
    code, _, err = run(capsys, "dsc", "verify", srcdir / DSC_NAME, "--dir", other)
    assert (code, err) == (0, "")


def test_dsc_verify_mismatch_and_missing(srcdir, capsys, tmp_path):
    victim = srcdir / next(iter(SRC_FILES))
    victim.write_bytes(b"X" * len(victim.read_bytes()))
    code, out, err = run(capsys, "--json", "dsc", "verify", srcdir / DSC_NAME)
    assert code == 2 and out == ""
    assert err.startswith("minideb: error:") and victim.name in err and err.count("\n") == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run(capsys, "dsc", "verify", srcdir / DSC_NAME, "--dir", empty)
    assert code == 2 and err.startswith("minideb: error:")


def test_dsc_bad_input(tmp_path, capsys):
    code, _, err = run(capsys, "dsc", "info", tmp_path / "missing.dsc")
    assert code == 2 and err.startswith("minideb: error:")
    bad = tmp_path / "bad.dsc"
    bad.write_text("Source: x\n")
    code, _, err = run(capsys, "dsc", "files", bad)
    assert code == 2 and "Version" in err and err.count("\n") == 1


@pytest.fixture
def srcrepo(tmp_path):
    return mk(tmp_path / "srcrepo", dsc=True)


def test_source(srcrepo, capsys):
    code, out, err = run(capsys, "--url", srcrepo, "source", SUITE, "sample-pkg")
    assert (code, err) == (0, "")
    assert "Source: sample-pkg\n" in out and f"Version: {DSC_VERSION}\n" in out
    code, out, _ = run(capsys, "--url", srcrepo, "--json", "source", SUITE, "sample-pkg")
    doc = json.loads(out)
    assert code == 0 and doc["source"] == "sample-pkg" and doc["version"] == DSC_VERSION
    assert doc["url"].endswith(DSC_NAME) and doc["component"] == "main"
    assert [f["name"] for f in doc["files"]] == list(SRC_FILES)


def test_source_version_and_absent(srcrepo, capsys):
    ok = run(capsys, "--url", srcrepo, "source", SUITE, "sample-pkg", "--version", DSC_VERSION)
    assert ok[0] == 0
    code, out, err = run(capsys, "--url", srcrepo, "source", SUITE, "sample-pkg", "--version", "9")
    assert (code, out) == (1, "") and err.startswith("minideb: error:")
    code, out, err = run(capsys, "--url", srcrepo, "source", SUITE, "nope")
    assert (code, out) == (1, "") and "nope" in err


def test_source_tampered_dsc_exit_2(srcrepo, tmp_path, capsys):
    path = tmp_path / "srcrepo" / "pool" / "main" / "s" / "sample-pkg" / DSC_NAME
    path.write_bytes(path.read_bytes().replace(b"Test", b"Evil"))
    code, out, err = run(capsys, "--url", srcrepo, "source", SUITE, "sample-pkg")
    assert (code, out) == (2, "") and err.startswith("minideb: error:")
    code, out, _ = run(capsys, "--url", srcrepo, "--no-verify", "source", SUITE, "sample-pkg")
    assert code == 0 and "Evil" in out


# -- Task 24: global -c/--component and -a/--arch --------------------------------


@pytest.fixture
def wide(tmp_path):
    return mk(tmp_path / "wide", packages=PKG, comps=("main", "contrib"))


def test_global_component_and_arch(wide, capsys):
    ns = cli.parse_args(["-c", "a", "versions", "x", "-c", "b", "-a", "amd64"])
    assert ns.components == ["a", "b"] and ns.architectures == ["amd64"]
    ns = cli.parse_args(["versions", "x"])
    assert ns.components == [] and ns.architectures == []
    code, out, _ = run(capsys, "--url", wide, "-c", "contrib", "-a", "i386", "list", SUITE)
    assert (code, out) == (0, "sample 0.4-1 i386 contrib\n")
    # the per-command option of list wins for that command
    code, out, _ = run(capsys, "--url", wide, "-c", "contrib", "list", SUITE, "-c", "main",
                       "-a", "amd64")  # fmt: skip
    assert (code, out) == (0, "sample 0.4-1 amd64 main\n")
    code, out, _ = run(capsys, "--url", wide, "search", SUITE, "sample", "-c", "main",
                       "-a", "amd64", "-a", "i386")  # fmt: skip
    assert out.splitlines() == ["sample 0.4-1 amd64 main", "sample 0.4-1 i386 main"]
    code, out, _ = run(capsys, "--url", wide, "-a", "amd64", "-c", "contrib",
                       "versions", "sample", "--suite", SUITE)  # fmt: skip
    assert out.splitlines() == [f"{SUITE} sample 0.4-1 amd64 contrib"]
    code, out, _ = run(capsys, "--url", wide, "files", SUITE, "tool-amd64", "-c", "contrib")
    assert (code, out.split()) == (0, ["usr/bin/tool-amd64", "utils/sample"])
    code, out, _ = run(capsys, "--url", wide, "-c", "main", "suites", "--suite", SUITE,
                       "--no-probe")  # fmt: skip
    assert (code, out) == (0, SUITE + "\n")


def test_global_scope_with_sources_file(wide, tmp_path, capsys):
    sf = tmp_path / "x.list"
    sf.write_text(f"deb [trusted=yes] {wide} {SUITE} main contrib\n")
    code, out, _ = run(capsys, "--sources", sf, "-c", "contrib", "-a", "amd64", "list", SUITE)
    assert (code, out) == (0, "sample 0.4-1 amd64 contrib\n")


def test_suites_unreachable_url_exit_2(tmp_path, capsys):
    code, out, err = run(capsys, "--url", (tmp_path / "gone").as_uri(), "suites")
    assert (code, out) == (2, "")
    assert err.startswith("minideb: error:") and "unreachable" in err


# -- final fix wave: exit codes, scoping and errors ---------------------------------


def test_list_skips_invalid_versions_with_warning(tmp_path, capsys):
    pk = [
        {"Package": "good", "Version": "1.0"},
        {"Package": "bad", "Version": "not a version"},
        {"Package": "epoch", "Version": "9" * 5000 + ":1"},
    ]
    url = mk(tmp_path / "r", packages=pk)
    for json_flag in ([], ["-j"]):
        code, out, err = run(capsys, "--url", url, *json_flag, "list", SUITE, "-a", "amd64")
        assert code == 0
        if json_flag:
            assert [r["name"] for r in json.loads(out)] == ["good"]
        else:
            assert out == "good 1.0 amd64 main\n"
        warnings = [ln for ln in err.splitlines() if ln.startswith("minideb: warning:")]
        assert len(warnings) == 2 and "bad" in warnings[0] and "epoch" in warnings[1]
    code, out, _ = run(capsys, "--url", url, "versions", "good", "--suite", SUITE, "--no-probe",
                       "-a", "amd64")  # fmt: skip
    assert (code, out) == (0, f"{SUITE} good 1.0 amd64 main\n")


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_suites_and_versions_empty_exit_1(repo, capsys, json_flag):
    code, out, err = run(capsys, "--url", repo, *json_flag, "suites", "--no-probe")
    assert (code, out) == (1, "") and err.startswith("minideb: error:")
    code, out, err = run(capsys, "--url", repo, *json_flag, "versions", "nope", "--suite", SUITE,
                         "--no-probe")  # fmt: skip
    assert (code, out) == (1, "") and "nope" in err


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_list_and_search_empty_exit_0(repo, tmp_path, capsys, json_flag):
    code, out, _ = run(capsys, "--url", repo, *json_flag, "search", SUITE, "zzz*")
    assert code == 0 and (json.loads(out) == [] if json_flag else out == "")
    url = mk(tmp_path / "empty", packages=[])
    code, out, _ = run(capsys, "--url", url, *json_flag, "list", SUITE)
    assert code == 0 and (json.loads(out) == [] if json_flag else out == "")


def test_missing_suite_error_printed_once(repo, capsys):
    for cmd in (["components", "nosuch"], ["list", "nosuch"], ["show", "nosuch", "x"]):
        code, out, err = run(capsys, "--url", repo, *cmd)
        assert (code, out) == (2, "")
        assert err.count("\n") == 1 and err.startswith("minideb: error: suite 'nosuch'"), err


def test_source_builds_one_client(srcrepo, capsys, monkeypatch):
    made: list[object] = []
    real = cli.RepoClient.from_url

    def counting(*a, **kw):
        made.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(cli.RepoClient, "from_url", counting)
    code, _, _ = run(capsys, "--url", srcrepo, "source", SUITE, "sample-pkg")
    assert code == 0 and len(made) == 1


def test_per_command_component_and_arch_accumulate(wide, capsys):
    code, out, _ = run(capsys, "--url", wide, "list", SUITE, "-c", "main", "-c", "contrib",
                       "-a", "amd64")  # fmt: skip
    assert (code, out) == (0, "sample 0.4-1 amd64 main\nsample 0.4-1 amd64 contrib\n")
    code, out, _ = run(capsys, "--url", wide, "list", SUITE, "-c", "contrib",
                       "-a", "amd64", "-a", "i386")  # fmt: skip
    assert (code, out) == (0, "sample 0.4-1 amd64 contrib\nsample 0.4-1 i386 contrib\n")
    # per-command values replace the global ones for that command
    code, out, _ = run(capsys, "--url", wide, "-c", "main", "-a", "i386", "list", SUITE,
                       "-c", "contrib", "-a", "amd64")  # fmt: skip
    assert (code, out) == (0, "sample 0.4-1 amd64 contrib\n")
    code, out, _ = run(capsys, "--url", wide, "files", SUITE, "tool-amd64", "-c", "contrib",
                       "-a", "amd64", "-a", "i386")  # fmt: skip
    assert (code, out.split()) == (0, ["usr/bin/tool-amd64", "utils/sample"])
    ns = cli.parse_args(["appstream", "s", "-c", "a", "-c", "b", "-a", "x", "-a", "y"])
    assert (ns.component, ns.arch) == (["a", "b"], ["x", "y"])
    ns = cli.parse_args(["icons", "s", "-c", "a", "-c", "b"])
    assert ns.component == ["a", "b"]


def test_ctrl_c_during_probing_returns_130_promptly(monkeypatch, capsys):
    import signal
    import threading
    import time

    release = threading.Event()
    main = threading.main_thread().ident
    assert main is not None

    class Slow(cli.Fetcher):
        def exists(self, url: str) -> bool:
            if url.endswith("/dists/stable/InRelease"):
                time.sleep(0.2)  # let the main thread start waiting for results
                signal.pthread_kill(main, signal.SIGINT)
            release.wait(15)  # an in-flight probe that would block for a long time
            return False

    monkeypatch.setattr(cli, "Fetcher", Slow)
    started = time.monotonic()
    try:
        code, _, _ = run(capsys, "--url", "http://127.0.0.1:9/x", "suites")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert code == 130
    assert elapsed < 5


def test_deb_extract_skip_absolute_symlinks(tmp_path, capsys):
    import gzip

    from tests.fixtures.debs import make_ar, make_tar
    from tests.test_deb import _links_tar

    p = tmp_path / "links.deb"
    p.write_bytes(
        make_ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", gzip.compress(make_tar({"control": b"Package: a\n"}))),
                ("data.tar.gz", gzip.compress(_links_tar())),
            ]
        )
    )
    code, out, err = run(capsys, "deb", "extract", p, tmp_path / "d1")
    assert code == 2 and "unsafe link target" in err and "--skip-absolute-symlinks" in err
    code, out, err = run(capsys, "deb", "extract", p, tmp_path / "d2", "--skip-absolute-symlinks")
    assert (code, out) == (0, "")
    assert err == (
        "minideb: warning: deb: skipping absolute symlink usr/lib/ssl/certs -> /etc/ssl/certs\n"
    )
    assert (tmp_path / "d2/usr/lib/ssl/openssl.cnf").exists()
    code, _, err = run(capsys, "-q", "deb", "x", p, tmp_path / "d3", "--skip-absolute-symlinks")
    assert (code, err) == (0, "")


@pytest.mark.parametrize("kind", ["link-missing", "mtime=1e400", "mtime=nan"])
def test_deb_hostile_archive_one_line_errors(tmp_path, capsys, kind):
    import gzip

    from tests.fixtures.debs import make_ar, make_tar
    from tests.test_deb import _hostile_tar

    p = tmp_path / "hostile.deb"
    p.write_bytes(
        make_ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", gzip.compress(make_tar({"control": b"Package: a\n"}))),
                ("data.tar.gz", gzip.compress(_hostile_tar(kind))),
            ]
        )
    )
    code, out, err = run(capsys, "deb", "extract", p, tmp_path / "d")
    assert (code, out) == (2, "")
    assert err.startswith("minideb: error: ") and err.count("\n") == 1
    code, out, err = run(capsys, "deb", "ls", p)
    assert (code, err) == (0, "")
    assert ("1970-01-01 00:00 ./f" in out) == kind.startswith("mtime=")
    code, out, err = run(capsys, "--json", "deb", "ls", p)
    assert (code, err) == (0, "")
    docs = json.loads(out, parse_constant=lambda c: pytest.fail(f"invalid JSON constant {c}"))
    assert all(d["mtime"] == 0 for d in docs if kind.startswith("mtime="))


# -- symlinked destinations (deb ctrl / deb extract) ---------------------------


def _nested_ctrl_deb(tmp_path: Path) -> Path:
    import gzip

    from tests.fixtures.debs import make_ar, make_tar

    ctrl = make_tar({"control": b"Package: n\n", "sub/conf": b"c\n"})
    p = tmp_path / "nested.deb"
    p.write_bytes(
        make_ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", gzip.compress(ctrl)),
                ("data.tar.gz", gzip.compress(make_tar({"usr/bin/x": b"x"}))),
            ]
        )
    )
    return p


def _assert_one_line_error(code: int, err: str, word: str) -> None:
    assert code == 2
    assert err.startswith("minideb: error:") and err.count("\n") == 1
    assert word in err


@pytest.mark.parametrize("sub", ["ctrl", "extract"])
def test_deb_symlinked_dest_refused(deb, tmp_path, capsys, sub):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    code, _, err = run(capsys, "deb", sub, deb, link)
    _assert_one_line_error(code, err, "symlink")
    assert list(outside.iterdir()) == []


def test_deb_ctrl_symlinked_control_refused(deb, tmp_path, capsys):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    dest = tmp_path / "ctl"
    dest.mkdir()
    (dest / "control").symlink_to(victim)
    code, _, err = run(capsys, "deb", "ctrl", deb, dest)
    _assert_one_line_error(code, err, "symlink")
    assert victim.read_bytes() == b"keep"
    assert (dest / "control").is_symlink()


def test_deb_ctrl_symlinked_parent_refused(tmp_path, capsys):
    p = _nested_ctrl_deb(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "ctl"
    dest.mkdir()
    (dest / "sub").symlink_to(outside, target_is_directory=True)
    code, _, err = run(capsys, "deb", "ctrl", p, dest)
    _assert_one_line_error(code, err, "symlink")
    assert list(outside.iterdir()) == []


def test_deb_ctrl_hard_link_not_written_through(deb, tmp_path, capsys):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    dest = tmp_path / "ctl"
    dest.mkdir()
    (dest / "control").hardlink_to(victim)
    assert run(capsys, "deb", "ctrl", deb, dest)[0] == 0
    assert victim.read_bytes() == b"keep"
    assert b"Package: hello" in (dest / "control").read_bytes()


def test_deb_ctrl_nested_and_overwrite(tmp_path, capsys):
    p = _nested_ctrl_deb(tmp_path)
    dest = tmp_path / "a" / "ctl"  # missing parents are created
    assert run(capsys, "deb", "ctrl", p, dest)[0] == 0
    (dest / "control").write_bytes(b"old")
    assert run(capsys, "deb", "ctrl", p, dest)[0] == 0  # regular files are replaced
    assert (dest / "control").read_bytes() == b"Package: n\n"
    assert (dest / "sub" / "conf").read_bytes() == b"c\n"


def test_deb_extract_symlinked_subdir_refused(deb, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "usr").symlink_to(outside, target_is_directory=True)
    code, _, err = run(capsys, "deb", "extract", deb, dest)
    assert code == 2 and err.startswith("minideb: error:") and err.count("\n") == 1
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("suite", ["../../outside", "%2e%2e/x", "/abs"])
def test_unsafe_suite_is_an_error(repo, capsys, suite):
    code, out, err = run(capsys, "--url", repo, "list", suite)
    assert (code, out) == (2, "")
    assert err.startswith("minideb: error: unsafe suite name") and err.count("\n") == 1


@pytest.mark.parametrize("sub", ["ctrl", "extract"])
def test_deb_dest_below_symlinked_ancestor_is_followed(deb, tmp_path, capsys, sub):
    # documented contract: only DEST itself is checked; the caller's path is trusted
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "link").symlink_to(real, target_is_directory=True)
    assert run(capsys, "deb", sub, deb, tmp_path / "out" / "link" / "new" / "dir")[0] == 0
    made = real / "new" / "dir"
    assert (made / "control").exists() if sub == "ctrl" else (made / "usr/bin/hello").exists()


def test_deb_dest_help_documents_symlinks(capsys):
    for sub in ("ctrl", "extract"):
        with pytest.raises(SystemExit):
            cli.main(["deb", sub, "--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert "must not be a symlink" in out and "leading to it are followed" in out
