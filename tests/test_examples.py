# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Run every script in examples/ offline (file:// fixtures) and check its structure."""

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import live_target
from tests.fixtures.debs import make_deb
from tests.fixtures.repo import make_repo

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))
SUITE, COMP = "stable", "contrib"


@pytest.fixture(autouse=True, scope="module")
def examples_on_path() -> Iterator[None]:
    """Put examples/ first on sys.path, as Python does for a script run by path."""
    saved_path, saved_modules = list(sys.path), set(sys.modules)
    sys.path.insert(0, str(EXAMPLES_DIR))
    yield
    sys.path[:] = saved_path
    for name in set(sys.modules) - saved_modules:
        if name == "_sample" or name.startswith("minideb_example_"):
            del sys.modules[name]


def load(name: str) -> ModuleType:
    path = EXAMPLES_DIR / f"{name}.py"
    modname = f"minideb_example_{name}"
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def run(name: str, args: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    assert load(name).main(args) == 0
    return capsys.readouterr().out


@pytest.fixture(scope="module")
def url(tmp_path_factory: pytest.TempPathFactory) -> str:
    pkgs = [
        {"Package": "sample-pkg", "Version": v, "Description": "sample package",
         "Filename": f"pool/{COMP}/s/sample-pkg/sample-pkg_{v}_amd64.deb"}
        for v in ("0.4-1", "1:0.1-1", "0.4~rc1-1")
    ]  # fmt: skip
    return make_repo(
        tmp_path_factory.mktemp("repo"), SUITE, (COMP,), ("amd64",), packages=pkgs,
        contents=True, dep11=True, dsc=True,
    )  # fmt: skip


def test_01_browse(url, capsys):
    out = run("01_browse_repository", [url, SUITE, COMP], capsys)
    assert COMP in out and "amd64" in out and "sample-pkg" in out


def test_01_browse_sources_file(url, tmp_path, capsys):
    f = tmp_path / "x.list"
    f.write_text(f"deb {url} {SUITE} {COMP}\n")
    assert COMP in run("01_browse_repository", ["--sources", str(f)], capsys)


def test_01_errors(tmp_path, capsys):
    empty = tmp_path / "empty.list"
    empty.write_text("# nothing enabled\n")
    assert load("01_browse_repository").main(["--sources", str(empty)]) == 1
    assert "no enabled entries" in capsys.readouterr().err
    dead = (tmp_path / "gone").as_uri()
    assert load("01_browse_repository").main([dead, SUITE, COMP]) == 1
    assert "unreachable" in capsys.readouterr().err


def test_02_search_versions(url, capsys):
    out = run("02_search_and_versions", [url, SUITE, COMP], capsys)
    assert "sample-pkg" in out
    assert "0.4~rc1-1 < 0.4-1 < 1:0.1-1" in out


def test_03_file_owner(url, capsys):
    out = run("03_find_file_owner", [url, SUITE, "usr/bin/sample-amd64", "-c", COMP], capsys)
    assert "utils/sample-pkg" in out


def test_03_nothing_found(url, capsys):
    assert load("03_find_file_owner").main([url, SUITE, "no/such/file"]) == 1
    assert "no package found for no/such/file" in capsys.readouterr().err


def test_04_sources_files(capsys):
    out = run("04_sources_files", [], capsys)
    assert "deb822" in out and "one-line" in out and "http://deb.debian.org/debian" in out


def test_05_inspect_default_and_path(tmp_path, capsys):
    out = run("05_inspect_deb", [], capsys)
    assert "Package: minideb-sample" in out and "usr/bin/minideb-sample" in out
    deb = tmp_path / "x.deb"
    deb.write_bytes(make_deb({"Package": "hello", "Version": "1", "Architecture": "all"},
                             {"usr/bin/hello": b"#!/bin/sh\n"}))  # fmt: skip
    out = run("05_inspect_deb", [str(deb)], capsys)
    assert "Package: hello" in out and "usr/bin/hello" in out


def test_05_bad_deb_exit_code(tmp_path, capsys):
    bad = tmp_path / "bad.deb"
    bad.write_bytes(b"not a deb")
    assert load("05_inspect_deb").main([str(bad)]) != 0
    assert "error" in capsys.readouterr().err.lower()


def test_06_extract_safely(capsys):
    out = run("06_extract_deb_safely", [], capsys)
    assert "usr/bin/minideb-sample" in out
    assert "rejected" in out and "unsafe" in out


def test_07_versions(capsys):
    out = run("07_compare_versions", [], capsys)
    assert "1.0~rc1 < 1.0 < 1.0+b1 < 1:0.9" in out


def test_08_changelog_control(capsys):
    out = run("08_changelog_and_control", [], capsys)
    assert "sample-pkg" in out and "sample-pkg-doc" in out


def test_09_appstream(url, capsys):
    pytest.importorskip("yaml")
    out = run("09_appstream", [url, SUITE, COMP], capsys)
    assert "org.example.Hello" in out


def test_09_appstream_without_yaml(url, monkeypatch, capsys):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "yaml":
            raise ImportError("no yaml")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert load("09_appstream").main([url, SUITE, COMP]) != 0
    assert "PyYAML" in capsys.readouterr().err


def test_10_fetcher_errors(url, capsys):
    out = run("10_custom_fetcher_and_errors", [url, SUITE, COMP], capsys)
    assert "FetchError" in out and "problems" in out


def test_10_unreachable_repo_exit_code(tmp_path, capsys):
    bad = (tmp_path / "nothing").as_uri()
    assert load("10_custom_fetcher_and_errors").main([bad, SUITE, COMP]) != 0
    assert capsys.readouterr().err


def test_11_source_package(url, capsys):
    out = run("11_source_package_dsc", [url, SUITE, COMP, "--verify"], capsys)
    assert "sample-pkg 1.0-1" in out and "verified" in out


@pytest.mark.network
def test_live_defaults_01_02(capsys):
    live_url, suite, comp, _ = live_target()
    assert load("01_browse_repository").main([live_url, suite, comp]) == 0
    assert comp in capsys.readouterr().out
    assert load("02_search_and_versions").main([live_url, suite, comp]) == 0
    assert capsys.readouterr().out


def test_examples_exist():
    assert len(EXAMPLES) >= 11


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_structure(path):
    mod = load(path.stem)
    assert mod.__doc__ and "Run:" in mod.__doc__
    assert callable(mod.main)
    assert 30 <= path.read_text().count("\n") <= 60
    readme = " ".join((EXAMPLES_DIR / "README.md").read_text().split())
    assert path.name in readme
    # the Run: line lists the same options and arguments as the README
    run_line = re.search(r"Run: (.*?)(?:\n\n|\Z)", mod.__doc__, re.S)
    assert run_line and " ".join(run_line[1].split()) in readme
    assert "sys.path.insert" not in path.read_text()
