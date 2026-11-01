# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""scripts/check_release.py against tmp_path copies of the version files."""

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release.py"
CHANGELOG = "python-minideblib ({ver}) unstable; urgency=low\n\n  * Something.\n"


def make_root(
    tmp_path: Path, ver: str = "1.2.3", init: str | None = None, deb: str | None = None
) -> Path:
    (tmp_path / "src" / "minideblib").mkdir(parents=True)
    (tmp_path / "debian").mkdir()
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{ver}"\n')
    (tmp_path / "src" / "minideblib" / "__init__.py").write_text(f'__version__ = "{init or ver}"\n')
    (tmp_path / "debian" / "changelog").write_text(CHANGELOG.format(ver=deb or ver))
    return tmp_path


def check(root: Path, tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), tag], capture_output=True, text=True
    )


def test_match(tmp_path: Path) -> None:
    res = check(make_root(tmp_path), "v1.2.3")
    assert res.returncode == 0
    assert "release 1.2.3 OK" in res.stdout


def test_debian_revision_stripped(tmp_path: Path) -> None:
    assert check(make_root(tmp_path, deb="1.2.3-1"), "v1.2.3").returncode == 0


def test_tag_mismatch(tmp_path: Path) -> None:
    res = check(make_root(tmp_path), "v1.2.4")
    assert res.returncode == 1
    assert "pyproject.toml" in res.stderr + res.stdout


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "vfoo", "release-1.2.3"])
def test_bad_tag_format(tmp_path: Path, tag: str) -> None:
    res = check(make_root(tmp_path), tag)
    assert res.returncode == 1
    assert "tag" in res.stderr + res.stdout


def test_init_mismatch(tmp_path: Path) -> None:
    res = check(make_root(tmp_path, init="1.2.0"), "v1.2.3")
    assert res.returncode == 1
    assert "__version__" in res.stderr + res.stdout


def test_debian_changelog_mismatch(tmp_path: Path) -> None:
    res = check(make_root(tmp_path, deb="0.6.21.34"), "v1.2.3")
    assert res.returncode == 1
    out = res.stderr + res.stdout
    assert "debian/changelog" in out
    assert "0.6.21.34" in out


def test_all_mismatches_reported(tmp_path: Path) -> None:
    res = check(make_root(tmp_path, init="9", deb="8"), "v1.2.4")
    assert (res.stderr + res.stdout).count("\n") >= 3


def _module():
    spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("tag", ["v0.7.0", "v0.7.0-rc1", "v1.10.2", "v1.0.0-alpha3", "v2.0.0-b2"])
def test_tag_grammar_accepts(tag: str) -> None:
    assert _module().expected_versions(tag) is not None


@pytest.mark.parametrize(
    "tag",
    ["0.7.0", "v0.7", "v01.2.3", "v1.2.3.4.5foo", "v1.2.3-", "v1.2.3-rc", "", "v1.2.3-x1"],
)
def test_tag_grammar_rejects(tag: str) -> None:
    assert _module().expected_versions(tag) is None


def test_tag_regex_is_linear() -> None:
    mod = _module()
    start = time.perf_counter()
    assert mod.expected_versions("v1.2.3" + "a" * 28 + "!") is None
    assert mod.expected_versions("v1.2.3-rc" + "1" * 5000 + "!") is None
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    ("tag", "pep", "deb"),
    [
        ("v1.2.3-rc1", "1.2.3rc1", "1.2.3~rc1"),
        ("v1.2.3-alpha2", "1.2.3a2", "1.2.3~alpha2"),
        ("v1.2.3-beta4", "1.2.3b4", "1.2.3~beta4"),
        ("v1.2.3-a1", "1.2.3a1", "1.2.3~a1"),
        ("v1.2.3-b1", "1.2.3b1", "1.2.3~b1"),
        ("v1.2.3", "1.2.3", "1.2.3"),
    ],
)
def test_normalisation(tag: str, pep: str, deb: str) -> None:
    assert _module().expected_versions(tag) == (pep, deb)


def test_prerelease_matches(tmp_path: Path) -> None:
    root = make_root(tmp_path, ver="1.2.3rc1", deb="1.2.3~rc1-1")
    assert check(root, "v1.2.3-rc1").returncode == 0


def test_prerelease_mismatch_messages_show_expected(tmp_path: Path) -> None:
    root = make_root(tmp_path, ver="1.2.3-rc1", init="1.2.3", deb="1.2.3")
    res = check(root, "v1.2.3-rc1")
    assert res.returncode == 1
    assert "expects 1.2.3rc1" in res.stderr
    assert res.stderr.count("expects 1.2.3rc1") == 2
    assert "expects 1.2.3~rc1" in res.stderr


def test_final_release_rejects_prerelease_versions(tmp_path: Path) -> None:
    res = check(make_root(tmp_path, ver="1.2.3rc1", init="1.2.3rc1", deb="1.2.3~rc1"), "v1.2.3")
    assert res.returncode == 1
    assert res.stderr.count("error:") == 3


def test_leading_blank_lines_in_changelog(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "debian" / "changelog").write_text("\n\n" + CHANGELOG.format(ver="1.2.3"))
    assert check(root, "v1.2.3").returncode == 0


def test_garbage_changelog_shows_text(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "debian" / "changelog").write_text("not a changelog\n")
    res = check(root, "v1.2.3")
    assert res.returncode == 1
    assert "not a changelog" in res.stderr


def test_undecodable_changelog(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "debian" / "changelog").write_bytes(b"\xff\xfe\x00bad")
    res = check(root, "v1.2.3")
    assert res.returncode == 1
    assert "Traceback" not in res.stderr
