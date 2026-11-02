# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

from datetime import UTC

import pytest

from minideblib import changelog
from minideblib.version import Version

SAMPLE = """\
foo (1:2.0-1) unstable experimental; urgency=low

  * New upstream release. Closes: #123, #456
  * Multi-line change
    continues here. LP: #789

 -- Jane Doe <jane@example.com>  Mon, 01 Jan 2024 10:00:00 +0000

foo (1.0-1) unstable; urgency=high

  * Initial release.

 -- Jane Doe <jane@example.com>  Tue, 02 Jan 2001 11:00:00 +0200
"""


@pytest.mark.parametrize("sep", ["\u2028", "\x0c", "\x85", "\r"])
def test_lines_end_at_lf_only(sep):
    smuggled = f"  * Initial release.{sep}bar (9.9) x; urgency=low\n"
    text = SAMPLE.replace("  * Initial release.\n", smuggled)
    entries = changelog.parse(text)
    assert [str(e.version) for e in entries] == ["1:2.0-1", "1.0-1"]
    assert "bar (9.9)" in entries[1].changes


def test_parse_entries():
    entries = changelog.parse(SAMPLE, keywords=["LP"])
    assert [e.package for e in entries] == ["foo", "foo"]
    first = entries[0]
    assert first.version == Version("1:2.0-1")
    assert first.distributions == ["unstable", "experimental"]
    assert first.urgency == "low"
    assert first.maintainer == "Jane Doe <jane@example.com>"
    assert first.date.year == 2024 and first.date.tzinfo == UTC
    assert first.bugs == ["123", "456"]
    assert first.keywords == {"LP": ["789"]}
    assert "continues here" in first.changes
    assert first.changes.startswith("* New upstream")
    assert entries[1].urgency == "high"
    assert entries[1].version < first.version


def test_empty():
    assert changelog.parse("\n\n") == []


@pytest.mark.parametrize(
    "bad",
    [
        "garbage\n",
        "foo (1.0) unstable; foo=bar\n\n  * x\n\n -- A <a@b>  Mon, 01 Jan 2024 10:00:00 +0000\n",
        "foo (1.0) unstable; urgency=low\n\n  * x\n\n -- A <a@b>  not a date\n",
        "foo (1.0) unstable; urgency=low\n\n  * x\n",
    ],
)
def test_malformed(bad):
    with pytest.raises(ValueError):
        changelog.parse(bad)


BINNMU = """\
foo (1.0-1+b1) unstable; urgency=medium, binary-only=yes

  * Binary-only non-maintainer upload for amd64. LP: #55, notLP: #66
  * Fixes LP: #1, #2 and Debian: #3, #4 (Closes: #9)
  * xLP: #77 not a keyword; X-LP: #88 neither

 -- Builder <b@example.com>  Mon, 01 Jan 2024 10:00:00 +0000
"""


def test_binnmu_attributes():
    (e,) = changelog.parse(BINNMU)
    assert e.urgency == "medium"
    assert e.attributes == {"urgency": "medium", "binary-only": "yes"}


def test_keywords_only_whole_words_and_several():
    (e,) = changelog.parse(BINNMU, keywords=["LP", "Debian"])
    assert e.keywords == {"LP": ["55", "1", "2"], "Debian": ["3", "4"]}
    assert e.bugs == ["9"]


def test_keyword_list_stops_before_next_keyword():
    text = BINNMU.replace(
        "  * xLP: #77 not a keyword; X-LP: #88 neither\n", "  * LP: #5, Debian: #6\n"
    )
    (e,) = changelog.parse(text, keywords=["LP", "Debian"])
    assert e.keywords["LP"] == ["55", "1", "2", "5"]
    assert e.keywords["Debian"] == ["3", "4", "6"]
