# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import pytest

from minideblib import ar
from tests.fixtures.debs import make_ar


def test_members_and_odd_padding():
    data = make_ar([("a", b"123"), ("bb", b"4567")])
    assert [(m.name, m.data) for m in ar.iter_members(data)] == [("a", b"123"), ("bb", b"4567")]


def test_bad_magic():
    with pytest.raises(ar.ArError):
        list(ar.iter_members(b"not an archive"))


def test_truncated():
    with pytest.raises(ar.ArError):
        list(ar.iter_members(make_ar([("a", b"12345")])[:-3]))


def test_bad_header_terminator():
    data = bytearray(make_ar([("a", b"12")]))
    data[8 + 58] = ord("x")
    with pytest.raises(ar.ArError):
        list(ar.iter_members(bytes(data)))


def test_gnu_long_names():
    table = b"averyveryverylongmembername.txt/\n"

    def hdr(name, size):
        return f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{size:<10}`\n".encode()

    data = b"!<arch>\n" + hdr("//", len(table)) + table + b"\n" + hdr("/0", 2) + b"hi"
    (m,) = ar.iter_members(data)
    assert m.name == "averyveryverylongmembername.txt"


@pytest.mark.parametrize("size", [b"-60", b"+4", b"1_0", b"", b"\xd9\xa4"])
def test_bad_size_field_rejected_promptly(size):
    hdr = b"foo/".ljust(16) + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
    hdr += b"100644".ljust(8) + size.ljust(10) + b"`\n"
    assert len(hdr) == 60
    with pytest.raises(ar.ArError):
        # islice bounds the iteration so a regression cannot hang the suite
        import itertools

        list(itertools.islice(ar.iter_members(b"!<arch>\n" + hdr + b"x" * 100), 1000))


def test_odd_last_member_requires_padding():
    data = make_ar([("a", b"123")])
    assert data.endswith(b"123\n")
    with pytest.raises(ar.ArError, match="padding"):
        list(ar.iter_members(data[:-1]))


@pytest.mark.parametrize(
    "members", [[("a", b"123")], [("a", b"1234")], [("a", b"12"), ("b", b"3")]]
)
def test_padding_present_or_not_needed(members):
    data = make_ar(members)
    assert [(m.name, m.data) for m in ar.iter_members(data)] == members


def test_even_last_member_without_trailing_byte():
    data = make_ar([("a", b"1234")])
    assert data.endswith(b"1234")
    assert [m.data for m in ar.iter_members(data)] == [b"1234"]
