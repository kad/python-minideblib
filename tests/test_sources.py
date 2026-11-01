# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import logging
from pathlib import Path

import pytest

from minideblib.sources import (
    SourceEntry,
    SourcesError,
    load,
    parse_deb822,
    parse_one_line,
    parse_sources_list,
)

UBUNTU = """\
Types: deb deb-src
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""

SLASH_SOURCE_LINE = (
    "deb [arch=amd64 signed-by=/usr/share/keyrings/example-archive.gpg] "
    "https://repo.example.com/ubuntu stable/slash-test/1.0 unified"
)


def one(line: str) -> SourceEntry:
    """Parse a line that must yield an entry."""
    entry = parse_one_line(line)
    assert entry is not None
    return entry


def test_deb822_cross_product():
    entries = parse_deb822(UBUNTU)
    assert len(entries) == 6
    assert {e.type for e in entries} == {"deb", "deb-src"}
    assert {e.suite for e in entries} == {"noble", "noble-updates", "noble-backports"}
    assert entries[0].components == ("main", "restricted", "universe", "multiverse")
    assert entries[0].signed_by == "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
    assert entries[0].uri == "http://archive.ubuntu.com/ubuntu/"


def test_slash_suite_line():
    e = one(SLASH_SOURCE_LINE)
    assert e.suite == "stable/slash-test/1.0"
    assert e.components == ("unified",)
    assert e.architectures == ("amd64",)
    assert e.signed_by == "/usr/share/keyrings/example-archive.gpg"
    assert not e.flat


def test_flat():
    e = one("deb http://x/ ./")
    assert e.flat
    assert e.components == ()
    assert one("deb http://x/ stable/").flat


def test_comments_and_blank():
    assert parse_one_line("") is None
    assert parse_one_line("   # deb http://x/ a b") is None
    e = one("deb http://x/ a b c  # trailing")
    assert e.components == ("b", "c")
    assert e.suite == "a"


def test_multi_arch_trusted():
    e = one("deb [arch=amd64,i386 trusted=yes] http://x/ a main")
    assert e.architectures == ("amd64", "i386")
    assert e.trusted is True
    e = one("deb [trusted=no] http://x/ a main")
    assert e.trusted is False
    assert one("deb http://x/ a main").trusted is None


def test_option_operators_kept_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        e = one("deb [arch+=i386 lang=en] http://x/ a main")
    assert e.options["arch+"] == "i386"
    assert e.options["lang"] == "en"
    assert e.architectures == ()
    assert caplog.records


@pytest.mark.parametrize(
    "line",
    ["deb", "deb http://x/", "foo http://x/ a main", "deb [arch=amd64 http://x/ a main"],
)
def test_malformed_skipped(line, caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_one_line(line) is None
    assert caplog.records


def test_parse_sources_list():
    text = "# c\ndeb http://x/ a main\n\nbad\ndeb-src http://x/ a main\n"
    entries = parse_sources_list(text)
    assert [e.type for e in entries] == ["deb", "deb-src"]


def test_deb822_enabled_no_and_flat_and_options():
    text = (
        "Types: deb\nURIs: http://a/\nSuites: x\nComponents: main\nEnabled: no\n\n"
        "# c\n\n\nTypes: deb\nURIs: http://b/ http://c/\nSuites: ./\n"
        "Architectures: amd64 arm64\nTrusted: yes\nLanguages: en\n"
    )
    entries = parse_deb822(text)
    assert [e.uri for e in entries] == ["http://b/", "http://c/"]
    assert entries[0].flat and entries[0].components == ()
    assert entries[0].architectures == ("amd64", "arm64")
    assert entries[0].trusted is True
    assert entries[0].options == {"languages": "en"}


def test_deb822_inline_signed_by_verbatim():
    block = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n abc\n -----END PGP PUBLIC KEY BLOCK-----"
    text = f"Types: deb\nURIs: http://a/\nSuites: x\nComponents: main\nSigned-By: {block}\n"
    # continuation lines: first line follows the field, rest verbatim
    e = parse_deb822(text)[0]
    assert e.signed_by is not None
    assert e.signed_by.startswith("-----BEGIN PGP")
    assert e.signed_by == block


def test_deb822_incomplete_stanza_skipped(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_deb822("Types: deb\nURIs: http://a/\n") == []
    assert caplog.records


def test_load_dir_and_file(tmp_path: Path):
    (tmp_path / "b.sources").write_text(UBUNTU)
    (tmp_path / "a.list").write_text("deb http://x/ a main\n")
    (tmp_path / "ignored.txt").write_text("deb http://y/ a main\n")
    entries = load(tmp_path)
    assert len(entries) == 7
    assert entries[0].uri == "http://x/"
    assert len(load(tmp_path / "a.list")) == 1
    assert len(load(tmp_path / "b.sources")) == 6


def test_load_default(tmp_path: Path, monkeypatch):
    import minideblib.sources as s

    (tmp_path / "sources.list").write_text("deb http://x/ a main\n")
    d = tmp_path / "sources.list.d"
    d.mkdir()
    (d / "z.list").write_text("deb http://z/ a main\n")
    monkeypatch.setattr(s, "DEFAULT_SOURCES_LIST", tmp_path / "sources.list")
    monkeypatch.setattr(s, "DEFAULT_SOURCES_DIR", d)
    assert [e.uri for e in load()] == ["http://x/", "http://z/"]
    monkeypatch.setattr(s, "DEFAULT_SOURCES_LIST", tmp_path / "nope")
    monkeypatch.setattr(s, "DEFAULT_SOURCES_DIR", tmp_path / "nope.d")
    assert load() == []


def test_entry_defaults_hashable():
    e = SourceEntry("deb", "http://x/", "a", ("main",))
    assert e.options == {} and e.enabled
    hash(e)


def test_cdrom_uri():
    e = one("deb cdrom:[Debian 12 DVD 1]/ bookworm main")
    assert e.uri == "cdrom:[Debian 12 DVD 1]/"
    assert (e.suite, e.components) == ("bookworm", ("main",))


def test_ipv6_uri():
    e = one("deb http://[::1]/x a main")
    assert (e.uri, e.suite, e.components) == ("http://[::1]/x", "a", ("main",))
    e = one("deb [arch=amd64] http://[::1]/x a main")
    assert e.uri == "http://[::1]/x" and e.architectures == ("amd64",)


def test_options_with_cdrom():
    e = one("deb [ arch=amd64 signed-by=/k.gpg ] cdrom:[Debian 12 DVD 1]/ bookworm main")
    assert e.uri == "cdrom:[Debian 12 DVD 1]/"
    assert e.architectures == ("amd64",) and e.signed_by == "/k.gpg"
    assert e.components == ("main",)


def test_deb822_options_not_shared():
    a = parse_deb822(
        "Types: deb\nURIs: http://a/ http://b/\nSuites: x\nComponents: m\nLanguages: en\n"
    )
    a[0].options["new"] = "1"
    assert "new" not in a[1].options


def test_load_dir_skips_bad_file(tmp_path: Path, caplog):
    (tmp_path / "a.list").write_bytes(b"deb http://x/ a main\n\xff\xfe\n")
    (tmp_path / "b.list").write_text("deb http://y/ a main\n")
    (tmp_path / "c.list").write_bytes(b"\xff\xfe\xfa")
    with caplog.at_level(logging.WARNING):
        entries = load(tmp_path)
    assert [e.uri for e in entries] == ["http://y/"]
    assert any("c.list" in r.getMessage() for r in caplog.records)


def test_load_single_file_error_propagates(tmp_path: Path):
    (tmp_path / "c.list").write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(SourcesError, match=r"c\.list: not valid UTF-8") as ei:
        load(tmp_path / "c.list")
    assert isinstance(ei.value, ValueError) and isinstance(ei.value.__cause__, UnicodeDecodeError)
    with pytest.raises(OSError):
        load(tmp_path / "missing.list")


def test_malformed_sources_file_named_and_skipped_in_dir(tmp_path: Path, caplog):
    (tmp_path / "a.sources").write_text("Types: deb\nnocolon\n")
    (tmp_path / "b.list").write_text("deb http://y/ a main\n")
    with caplog.at_level(logging.WARNING):
        entries = load(tmp_path)
    assert [e.uri for e in entries] == ["http://y/"]
    assert any("a.sources" in r.getMessage() for r in caplog.records)
    with pytest.raises(ValueError, match=r"a\.sources: line 2"):
        load(tmp_path / "a.sources")


@pytest.mark.parametrize(
    "line",
    [
        "deb\thttps://host/repo\tstable\tmain contrib",
        "deb\thttps://host/repo\tstable\tmain\tcontrib",
        "deb \t https://host/repo\t \tstable  main\t contrib",
        "deb\t[arch=amd64]\thttps://host/repo\tstable\tmain\tcontrib",
        "deb [arch=amd64]\thttps://host/repo stable main contrib",
        "deb\t[\tarch=amd64\t]\thttps://host/repo stable main contrib",
    ],
)
def test_tab_separated_entries(line):
    e = one(line)
    assert (e.type, e.uri, e.suite, e.components) == (
        "deb",
        "https://host/repo",
        "stable",
        ("main", "contrib"),
    )
    if "arch=" in line:
        assert e.architectures == ("amd64",)


def test_tab_separated_cdrom():
    e = one("deb\t[arch=amd64]\tcdrom:[Debian 12 DVD 1]/\tbookworm\tmain")
    assert e.uri == "cdrom:[Debian 12 DVD 1]/"
    assert (e.suite, e.components, e.architectures) == ("bookworm", ("main",), ("amd64",))


def test_non_ascii_space_is_not_a_separator(caplog):
    # U+00A0 is not a field separator, so this is one unknown type word
    with caplog.at_level(logging.WARNING):
        assert parse_one_line("deb\u00a0https://host/repo stable main") is None
