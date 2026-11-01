# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The man page header and the RPM spec carry the package version."""

import re
import tomllib
from pathlib import Path

import pytest

import minideblib

ROOT = Path(__file__).resolve().parent.parent
MAN = ROOT / "debian" / "minideb.1"
SPEC = ROOT / "python-minideblib.spec"

# autopkgtest copies only tests/ next to the installed package
if not (ROOT / "pyproject.toml").is_file():
    pytest.skip("needs the source tree", allow_module_level=True)


def _need(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"needs {path.relative_to(ROOT)} from the source tree")


def test_man_page_header_version():
    _need(MAN)
    first = MAN.read_text().splitlines()[0]
    m = re.match(r'^\.TH MINIDEB 1 "[^"]*" "minideblib ([^"]+)"', first)
    assert m, first
    assert m.group(1) == minideblib.__version__


def test_spec_version_matches_pyproject():
    _need(SPEC)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    # RPM spells a pre-release with a tilde so that it sorts first: 0.8.0rc1 -> 0.8.0~rc1
    rpm = re.sub(r"(a|b|rc)([0-9]+)$", r"~\1\2", version)
    spec = SPEC.read_text()
    assert re.search(rf"^Version:\s+{re.escape(rpm)}$", spec, re.M)
