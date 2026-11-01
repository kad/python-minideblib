# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import shutil
import subprocess
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest


def test_py_typed_marker_present():
    assert files("minideblib").joinpath("py.typed").is_file()


def test_py_typed_in_built_wheel(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(tmp_path)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"cannot build a wheel offline: {proc.stderr.strip()[-200:]}")
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        assert "minideblib/py.typed" in zf.namelist()
