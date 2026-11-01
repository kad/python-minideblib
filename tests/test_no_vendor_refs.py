# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Guard: tracked files must not mention a specific hardware vendor."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Built at runtime so this file does not match its own search.
WORD = "in" + "tel"


def test_no_vendor_references_in_tracked_files():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("not a git checkout")
    res = subprocess.run(
        ["git", "grep", "-n", "-I", "-i", "-e", WORD, "--", ".", ":!uv.lock"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode in (0, 1), res.stderr
    assert res.stdout == "", f"vendor references found:\n{res.stdout}"
