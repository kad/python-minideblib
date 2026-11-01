# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Build the zipapp and run it."""

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_pyz.py"


def build(out: Path, *extra: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
    subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(out), *extra],
        check=True,
        capture_output=True,
        env=env,
    )


def run(pyz: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(pyz), *args], capture_output=True, text=True, cwd=pyz.parent
    )


def need_yaml_licence() -> None:
    """Skip when PyYAML or its licence text is not in the installed metadata (distro installs)."""
    pytest.importorskip("yaml")
    try:
        dist = importlib.metadata.distribution("PyYAML")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("PyYAML metadata not found")
    if not any(f.name.upper().startswith(("LICENSE", "COPYING")) for f in dist.files or ()):
        pytest.skip("PyYAML licence text is not in its package metadata")


@pytest.fixture(scope="module")
def pyz(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("pyz") / "minideb.pyz"
    build(out)
    return out


def test_version(pyz: Path) -> None:
    from minideblib import __version__

    res = run(pyz, "--version")
    assert res.returncode == 0
    assert __version__ in res.stdout + res.stderr


def test_deb_help(pyz: Path) -> None:
    res = run(pyz, "deb", "--help")
    assert res.returncode == 0
    assert "usage" in res.stdout.lower()


def test_alias_help(pyz: Path) -> None:
    res = run(pyz, "ls", "--help")
    assert res.returncode == 0
    assert "usage" in res.stdout.lower()


def test_shebang_and_mode(pyz: Path) -> None:
    assert pyz.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    assert os.access(pyz, os.X_OK)


def test_contents(pyz: Path) -> None:
    with zipfile.ZipFile(pyz) as zf:
        names = zf.namelist()
    assert "__main__.py" in names
    assert "minideblib/cli.py" in names
    assert "COPYING" in names  # the GPL text travels with the zipapp
    assert names == sorted(names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert not any(n.startswith(("yaml/", "zstandard/")) for n in names)


def test_reproducible(tmp_path: Path) -> None:
    a, b = tmp_path / "a" / "x.pyz", tmp_path / "b" / "x.pyz"
    build(a)
    build(b)
    assert a.read_bytes() == b.read_bytes()


def load_build_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_pyz", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reproducible_across_hash_seeds(tmp_path: Path) -> None:
    outs = []
    for seed in ("0", "1", "12345"):
        out = tmp_path / seed / "x.pyz"
        env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
        env["PYTHONHASHSEED"] = seed
        subprocess.run(
            [sys.executable, str(SCRIPT), "-o", str(out)],
            check=True,
            capture_output=True,
            env=env,
        )
        outs.append(out.read_bytes())
    assert outs[0] == outs[1] == outs[2]


def test_independent_of_source_creation_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sorted members and identical bytes even if the tree was created in reverse order."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    mod = load_build_module()
    root = tmp_path / "root"
    pkg = root / "src" / "minideblib"
    sources = sorted(p for p in (ROOT / "src" / "minideblib").rglob("*") if p.is_file())
    for path in reversed(sources):
        rel = path.relative_to(ROOT / "src" / "minideblib")
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, pkg / rel)
        os.utime(pkg / rel, (1_000_000_000 + len(rel.parts), 1_000_000_000 + len(rel.parts)))
    shutil.copyfile(ROOT / "COPYING", root / "COPYING")
    reference, reordered = tmp_path / "ref.pyz", tmp_path / "rev.pyz"
    mod.build(reference)
    mod.build(reordered, root=root)
    assert reference.read_bytes() == reordered.read_bytes()
    with zipfile.ZipFile(reordered) as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert {(i.create_system, i.extract_version) for i in zf.infolist()} == {(3, 20)}
        modes = {i.filename: i.external_attr >> 16 for i in zf.infolist()}
    assert modes["COPYING"] == 0o100644
    assert modes["minideblib/"] == 0o40755
    assert os.access(reordered, os.X_OK)


def test_source_date_epoch(tmp_path: Path) -> None:
    out = tmp_path / "x.pyz"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1700000000"}
    subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(out)], check=True, capture_output=True, env=env
    )
    with zipfile.ZipFile(out) as zf:
        assert {i.date_time for i in zf.infolist()} == {(2023, 11, 14, 22, 13, 20)}


def test_include_extras_vendors_pure_python_yaml(tmp_path: Path) -> None:
    need_yaml_licence()
    out = tmp_path / "x.pyz"
    build(out, "--include-extras")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "yaml/__init__.py" in names
    assert not any(n.endswith((".so", ".pyd")) for n in names)
    assert not any(n.startswith("zstandard/") for n in names)
    res = run(out, "--version")
    assert res.returncode == 0


@pytest.mark.parametrize(
    "value", ["abc", "100", "-5", "", "315532799", "4354819199", "5000000000", "99999999999"]
)
def test_invalid_source_date_epoch(tmp_path: Path, value: str) -> None:
    env = {**os.environ, "SOURCE_DATE_EPOCH": value}
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(tmp_path / "x.pyz")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "SOURCE_DATE_EPOCH" in res.stderr
    assert "Traceback" not in res.stderr


@pytest.mark.parametrize(
    ("value", "stamp"),
    [("315532800", (1980, 1, 1, 0, 0, 0)), ("4354819198", (2107, 12, 31, 23, 59, 58))],
)
def test_source_date_epoch_zip_range_bounds(tmp_path: Path, value: str, stamp: tuple) -> None:
    out = tmp_path / "x.pyz"
    env = {**os.environ, "SOURCE_DATE_EPOCH": value}
    subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(out)], check=True, capture_output=True, env=env
    )
    with zipfile.ZipFile(out) as zf:
        assert {i.date_time for i in zf.infolist()} == {stamp}


def test_source_date_epoch_too_late_message(tmp_path: Path) -> None:
    env = {**os.environ, "SOURCE_DATE_EPOCH": "5000000000"}
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(tmp_path / "x.pyz")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert res.stderr.count("\n") == 1
    assert "315532800" in res.stderr and "4354819198" in res.stderr


def test_extras_bundle_yaml_licence(tmp_path: Path) -> None:
    need_yaml_licence()
    out = tmp_path / "x.pyz"
    build(out, "--include-extras")
    with zipfile.ZipFile(out) as zf:
        assert "yaml/LICENSE" in zf.namelist()


def test_build_does_not_leak_tz(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load_build_module()
    monkeypatch.setenv("TZ", "Europe/Helsinki")
    mod.build(tmp_path / "x.pyz")
    assert os.environ["TZ"] == "Europe/Helsinki"
