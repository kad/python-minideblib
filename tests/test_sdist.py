# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The sdist ships the tracked project files only (no local work directories)."""

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

if not (ROOT / "pyproject.toml").is_file():
    pytest.skip("needs the source tree", allow_module_level=True)


@pytest.fixture(scope="module")
def sdist_names(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    out = tmp_path_factory.mktemp("sdist")
    res = subprocess.run(
        [uv, "build", "--offline", "--sdist", "--out-dir", str(out), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"cannot build the sdist offline: {res.stderr.strip()[-300:]}")
    (archive,) = out.glob("*.tar.gz")
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    # drop the "minideblib-X.Y.Z/" prefix
    return [n.split("/", 1)[1] for n in names if "/" in n]


def test_sdist_has_project_files(sdist_names: list[str]) -> None:
    for wanted in (
        "pyproject.toml",
        "README.md",
        "COPYING",
        "CHANGES",
        "src/minideblib/__init__.py",
        "tests/test_sdist.py",
        "debian/control",
        "examples/README.md",
        "scripts/build_pyz.py",
    ):
        assert wanted in sdist_names, wanted


def test_sdist_has_no_local_or_generated_files(sdist_names: list[str]) -> None:
    bad = [
        n
        for n in sdist_names
        if n.startswith((".superpowers", "docs/superpowers", ".venv", "dist/", "build/"))
        or any(
            part in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
            for part in n.split("/")
        )
        or n.endswith(".pyc")
    ]
    assert bad == []
