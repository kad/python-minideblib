# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Every project Python file starts with the two-line licence header."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# autopkgtest copies only tests/ next to the installed package
if not (ROOT / "pyproject.toml").is_file():
    pytest.skip("needs the source tree", allow_module_level=True)

SPDX = "# SPDX-License-Identifier: GPL-2.0-only"
YEARS = r"\d{4}(?:-\d{4})?(?:, \d{4}(?:-\d{4})?)*"
COPYRIGHT = re.compile(rf"# Copyright \(C\) {YEARS} Alexander D\. Kanevskiy <kad@bifh\.org>")
ENCODING = re.compile(r"#.*coding[:=]\s*[-\w.]+")


def _files() -> list[Path]:
    found: list[Path] = []
    for pattern in (
        "src/minideblib/**/*.py",
        "scripts/*.py",
        "examples/*.py",
        "tests/*.py",
        "tests/fixtures/*.py",
    ):
        found.extend(ROOT.glob(pattern))
    return sorted(set(found))


FILES = _files()


@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(ROOT)) for p in FILES])
def test_header_form(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    if lines and ENCODING.fullmatch(lines[0]):
        lines = lines[1:]
    assert lines[:1] == [SPDX], f"{path.name}: missing SPDX line"
    assert len(lines) > 1 and COPYRIGHT.fullmatch(lines[1]), f"{path.name}: bad copyright line"


def test_files_found():
    assert FILES
