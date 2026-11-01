# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The owner's name and address use the current spelling everywhere."""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# autopkgtest copies only tests/ next to the installed package
if not (ROOT / "pyproject.toml").is_file():
    pytest.skip("needs the source tree", allow_module_level=True)

# Built at runtime so this file does not match itself.
OLD_NAME = "alexan" + "dr"
OLD_MAIL = "packages" + "@bifh.org"

SKIP_DIRS = {
    ".git",
    ".superpowers",  # untracked local workflow notes
    "superpowers",  # docs/superpowers, untracked local workflow notes
    "dist",
    "build",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}
SKIP_FILES = {"uv.lock"}

# Paths (relative, posix) that may keep the old spelling, with the reason.
ALLOWED = {
    "debian/changelog": "historical signature lines of past releases",
}


def _text_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            path = Path(dirpath) / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data:
                continue
            try:
                yield path, data.decode("utf-8")
            except UnicodeDecodeError:
                continue


def test_no_old_spelling():
    bad = []
    for path, text in _text_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        low = text.lower()
        if OLD_NAME in low or OLD_MAIL in low:
            bad.append(rel)
    assert not bad, f"old name/address found in: {bad}"


def test_allow_list_is_only_history():
    assert set(ALLOWED) == {"debian/changelog"}
