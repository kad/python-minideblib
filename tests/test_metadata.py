# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Packaging metadata rules."""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# autopkgtest copies only tests/ next to the installed package
if not (ROOT / "pyproject.toml").is_file():
    pytest.skip("needs the source tree", allow_module_level=True)


def test_no_license_classifier_with_license_expression():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"]
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]
