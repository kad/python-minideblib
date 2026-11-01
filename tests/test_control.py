# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import dataclasses
import io
import logging
import time

import pytest

from minideblib import control
from minideblib.control import (
    ControlError,
    Paragraph,
    decode_text,
    parse_file,
    parse_paragraphs,
    strip_clearsign,
)
from minideblib.errors import MinidebError

SAMPLE = """\
Package: foo
Version: 1.0
X-Foo: bar
Multi-Arch: same
Rules-Requires-Root: no
Description: short
 long line one
 .
 long line two

Package: bar
Version: 2.0
"""


def test_roundtrip_preserves_order_case_and_unknown():
    paras = list(parse_paragraphs(SAMPLE))
    assert len(paras) == 2
    p = paras[0]
    assert p.fields_in_order() == [
        "Package", "Version", "X-Foo", "Multi-Arch",
        "Rules-Requires-Root", "Description",
    ]  # fmt: skip
    assert p["x-foo"] == "bar"
    assert p["MULTI-ARCH"] == "same"
    assert p["Rules-Requires-Root"] == "no"
    assert p.dump() + "\n" + paras[1].dump() == SAMPLE


def test_continuation_lines():
    p = next(parse_paragraphs(SAMPLE))
    assert p["Description"] == "short\n long line one\n .\n long line two"


def test_case_insensitive_mapping():
    p = Paragraph()
    p["Package"] = "x"
    assert "PACKAGE" in p
    assert p.get("package") == "x"
    assert p.get("missing") is None
    p["PACKAGE"] = "y"
    assert p.fields_in_order() == ["Package"]
    assert p["package"] == "y"
    assert len(p) == 1
    del p["package"]
    assert "Package" not in p
    with pytest.raises(KeyError):
        p["nope"]


def test_comments_multiple_blanks_crlf_no_trailing_newline():
    text = (
        "# c\r\nTypes: deb\r\nURIs: http://x\r\n# mid\r\nSuites: a\r\n"
        "\r\n\r\n\r\nTypes: deb-src\r\nURIs: y"
    )
    paras = list(parse_paragraphs(text))
    assert [p["Types"] for p in paras] == ["deb", "deb-src"]
    assert paras[0]["Suites"] == "a"
    assert paras[1]["URIs"] == "y"


def test_bytes_and_file_object():
    assert len(list(parse_paragraphs(SAMPLE.encode()))) == 2
    assert len(list(parse_paragraphs(io.StringIO(SAMPLE)))) == 2
    assert len(list(parse_paragraphs(io.BytesIO(SAMPLE.encode())))) == 2


def test_empty():
    assert list(parse_paragraphs("")) == []
    assert list(parse_paragraphs("\n\n# x\n")) == []


def test_parse_file(tmp_path):
    f = tmp_path / "Packages"
    f.write_text(SAMPLE)
    assert [p["Package"] for p in parse_file(f)] == ["foo", "bar"]
    f.write_bytes(SAMPLE.encode())
    assert len(parse_file(str(f))) == 2


def test_parse_file_inrelease(tmp_path):
    f = tmp_path / "InRelease"
    f.write_text(CLEARSIGNED)
    paras = parse_file(f)
    assert len(paras) == 1
    assert paras[0]["Suite"] == "stable"


CLEARSIGNED = """\
-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

Suite: stable
Notes: x
 - bar
- -Foo: v
-----BEGIN PGP SIGNATURE-----

iQ...
-----END PGP SIGNATURE-----
"""


def test_strip_clearsign():
    out = strip_clearsign(CLEARSIGNED)
    assert out == "Suite: stable\nNotes: x\n - bar\n-Foo: v\n"
    assert "PGP" not in out


def test_strip_clearsign_plain_passthrough():
    assert strip_clearsign(SAMPLE) == SAMPLE


def test_strip_clearsign_crlf():
    out = strip_clearsign(CLEARSIGNED.replace("\n", "\r\n"))
    assert out.replace("\r\n", "\n") == "Suite: stable\nNotes: x\n - bar\n-Foo: v\n"


@pytest.mark.parametrize("sep", ["\x0c", " ", "\x85", "\x0b", "\x1c", " "])
def test_unicode_line_separators_do_not_split(sep):
    text = f"Package: foo\nDescription: a{sep}b\n more{sep}text\n\nPackage: bar\n"
    expected = f"a{sep}b\n more{sep}text"
    for source in (text, text.encode(), io.StringIO(text), io.BytesIO(text.encode())):
        paras = list(parse_paragraphs(source))
        assert [p["Package"] for p in paras] == ["foo", "bar"]
        assert paras[0]["Description"] == expected


def test_strip_clearsign_formfeed_in_body():
    text = CLEARSIGNED.replace("Notes: x", "Notes: x\x0cy")
    out = strip_clearsign(text)
    assert "Notes: x\x0cy\n" in out
    assert "PGP" not in out


def test_issue1_crlf_debian_control():
    text = (
        "Source: foo\nSection: utils\nBuild-Depends: a,\n b\n\n"
        "Package: foo\nArchitecture: any\nDescription: short\n long\n"
    )
    lf = [list(p.items()) for p in parse_paragraphs(text)]
    crlf = [list(p.items()) for p in parse_paragraphs(text.replace("\n", "\r\n"))]
    assert len(lf) == 2
    assert crlf == lf


def test_issue2_last_char_not_truncated():
    assert list(parse_paragraphs("Package: foo\nVersion: 1.0"))[0]["Version"] == "1.0"
    assert list(parse_paragraphs("Package: foo\r\nVersion: 1.0\r\n"))[0]["Version"] == "1.0"
    assert list(parse_paragraphs("Package: foo\r\nVersion: 1.0"))[0]["Version"] == "1.0"
    assert list(parse_paragraphs("A: b\n c"))[0]["A"] == "b\n c"


NAMES = ["core-autotest-fsfuzzer"] + [f"core-pkg{i:02d}" for i in range(1, 38)]

DSC_ISSUE3 = (
    "Format: 1.0\nSource: core\nBinary: "
    + ", ".join(NAMES[:1])
    + ",\n "
    + ", ".join(NAMES[1:])
    + "\nArchitecture: any\nVersion: 1.0-1\n"
    "Checksums-Sha1: \n 0123456789abcdef0123456789abcdef01234567 6 core_1.0.orig.tar.gz\n"
    "Files: \n d41d8cd98f00b204e9800998ecf8427e 6 core_1.0.orig.tar.gz\n"
)


def test_issue3_multiline_binary_field():
    p = list(parse_paragraphs(DSC_ISSUE3))[0]
    assert p.get_list("Binary") == NAMES
    assert len(NAMES) == 38
    assert not any(c.isspace() for n in p.get_list("Binary") for c in n)
    assert p["Files"] == "\n d41d8cd98f00b204e9800998ecf8427e 6 core_1.0.orig.tar.gz"
    assert p["Checksums-Sha1"].startswith("\n ")
    assert p.dump() == DSC_ISSUE3.replace(": \n", ":\n")


def test_get_folded_and_get_list_edges():
    p = Paragraph([("A", "x,\n y,\n z"), ("B", "a b\n  c"), ("C", ""), ("D", "a,, b ,")])
    assert p.get_folded("A") == "x, y, z"
    assert p.get_folded("B") == "a b c"
    assert p.get_folded("Missing") == ""
    assert p.get_folded("Missing", "d") == "d"
    assert p.get_list("a") == ["x", "y", "z"]
    assert p.get_list("C") == []
    assert p.get_list("D") == ["a", "b"]
    assert p.get_list("Missing") == []
    assert Paragraph([("L", "a; b\n ;c")]).get_list("L", sep=";") == ["a", "b", "c"]


def test_get_list_whitespace_separated():
    p = Paragraph([("Architectures", "amd64\tarm64  i386\n  riscv64")])
    assert p.get_list("Architectures", sep=None) == ["amd64", "arm64", "i386", "riscv64"]


def test_get_folded_dot_marker_is_literal():
    assert Paragraph([("D", "a\n .\n b")]).get_folded("D") == "a . b"


def test_latin1_fallback_logs_warning(tmp_path, caplog):
    path = tmp_path / "c"
    path.write_bytes("Maintainer: André\n".encode("latin-1"))
    with caplog.at_level("WARNING"):
        (para,) = parse_file(path)
    assert para["Maintainer"] == "André"
    assert "latin-1" in caplog.text


def test_utf8_decoded_strictly_without_warning(tmp_path, caplog):
    path = tmp_path / "c"
    path.write_bytes("Maintainer: André\n".encode())
    with caplog.at_level("WARNING"):
        (para,) = parse_file(path)
    assert para["Maintainer"] == "André"
    assert not caplog.text


def test_latin1_warns_once_per_stream(caplog):
    stream = io.BytesIO("A: é\nB: é\nC: é\n".encode("latin-1"))
    stream.name = "mystream"
    with caplog.at_level("WARNING"):
        (para,) = parse_paragraphs(stream)
    assert para["C"] == "é"
    assert caplog.text.count("latin-1") == 1
    assert "mystream" in caplog.text


# --- contract pinned for the clean reimplementation (spec section 7) ---------

BEGIN = "-----BEGIN PGP SIGNED MESSAGE-----"
SIG = "-----BEGIN PGP SIGNATURE-----"


def test_public_api():
    assert control.__all__ == [
        "ControlError",
        "Paragraph",
        "decode_text",
        "parse_file",
        "parse_paragraphs",
        "strip_clearsign",
    ]
    assert not hasattr(control, "parse_multiline_hashes")


# 1. ControlError: bases, message prefix, lineno


def test_control_error_bases():
    assert issubclass(ControlError, MinidebError)
    assert issubclass(ControlError, ValueError)


@pytest.mark.parametrize(
    ("text", "lineno"),
    [
        (" x\n", 1),  # continuation at start of input
        ("#c\n x\n", 2),  # continuation after only comments
        ("A: 1\n\n x\n", 3),  # continuation right after a blank line
        ("A: b\n  \n c\n", 3),  # whitespace-only line is blank
        ("Foo\n", 1),  # missing colon
        ("Types: deb\nnocolon\n", 2),
        (": v\n", 1),  # empty name
        ("A: 1\n\x0c: v\n", 2),  # name made only of whitespace
        ("A: 1\r\n\r\nbad\r\n", 3),  # CRLF counted as one terminator
        ("A: 1\r\rbad", 3),  # lone CR ends a line
        ("# c\n\n\nA: 1\n#x\nbad", 6),  # comments and blanks are counted
    ],
)
def test_control_error_line_numbers(text, lineno):
    for source in (text, text.encode(), io.StringIO(text), io.BytesIO(text.encode())):
        with pytest.raises(ControlError) as info:
            list(parse_paragraphs(source))
        assert str(info.value).startswith(f"line {lineno}:")
        assert info.value.lineno == lineno


def test_control_error_is_value_error_for_callers():
    with pytest.raises(ValueError, match=r"^line 1:"):
        list(parse_paragraphs("nocolon"))


def test_control_error_quote_is_bounded():
    with pytest.raises(ControlError) as info:
        list(parse_paragraphs("x" * 100_000 + "\n"))
    assert len(str(info.value)) < 300


def test_control_error_without_line():
    exc = ControlError("something odd")
    assert exc.lineno is None
    assert str(exc) == "something odd"
    exc = ControlError("bad", lineno=7)
    assert exc.lineno == 7
    assert str(exc).startswith("line 7:")


# 2. missing keys and non-str membership


def test_missing_key_raises_key_error_not_control_error():
    p = Paragraph([("A", "1")])
    with pytest.raises(KeyError) as info:
        p["B"]
    assert not isinstance(info.value, ControlError)
    with pytest.raises(KeyError):
        del p["B"]
    assert p.get("B") is None
    assert p.get("B", "d") == "d"
    assert 1 not in p
    assert None not in p
    assert ("A",) not in p


def test_mutable_mapping_protocol():
    p = Paragraph([("A", "1"), ("B", "2")])
    assert p.pop("a") == "1"
    assert p.pop("a", "gone") == "gone"
    assert p.setdefault("C", "3") == "3"
    assert p.setdefault("c", "x") == "3"
    p.update([("D", "4")], E="5")
    assert list(p.keys()) == ["B", "C", "D", "E"]
    assert list(p.values()) == ["2", "3", "4", "5"]
    p.clear()
    assert len(p) == 0
    assert p.dump() == ""


def test_delete_and_readd_moves_to_end_with_new_spelling():
    p = Paragraph([("Package", "x"), ("Version", "1")])
    del p["PACKAGE"]
    p["package"] = "y"
    assert p.fields_in_order() == ["Version", "package"]
    assert list(p) == p.fields_in_order()


def test_constructor_accepts_iterables_and_later_duplicate_wins():
    src = Paragraph([("A", "1"), ("B", "2")])
    assert Paragraph(src.items()) == src
    assert Paragraph((k, v) for k, v in [("A", "1"), ("a", "2")]).items() == {"A": "2"}.items()
    assert Paragraph().fields_in_order() == []


def test_values_stored_as_given():
    p = Paragraph()
    p["X"] = "  padded \n weird"
    assert p["x"] == "  padded \n weird"


def test_repr_round_trips():
    p = Paragraph([("A", "1"), ("B", "x\n y")])
    assert eval(repr(p), {"Paragraph": Paragraph}) == p


# 3. duplicate names in parsed input


def test_duplicate_fields_last_value_wins_first_position_kept():
    (p,) = parse_paragraphs("Package: a\nVersion: 1\npackage: b\n")
    assert p.fields_in_order() == ["Package", "Version"]
    assert p["Package"] == "b"


# 4. laziness


def test_parse_paragraphs_is_lazy():
    it = parse_paragraphs("A: 1\n\nbad\n")
    first = next(it)
    assert first["A"] == "1"
    with pytest.raises(ControlError):
        next(it)


def test_parse_paragraphs_lazy_on_streams():
    lines = iter(["A: 1\n", "\n"])

    def gen():
        yield from lines
        raise AssertionError("read past the first paragraph")

    assert next(parse_paragraphs(gen()))["A"] == "1"


# 5. comments


def test_comment_inside_continuation_run():
    (p,) = parse_paragraphs("A: 1\n#c\n b\n")
    assert p["A"] == "1\n b"
    (p,) = parse_paragraphs("A: 1\n #x\n")
    assert p["A"] == "1\n #x"


def test_comment_does_not_end_paragraph():
    (p,) = parse_paragraphs("A: 1\n# c\nB: 2\n")
    assert p.fields_in_order() == ["A", "B"]


# 6. whitespace-only separators


@pytest.mark.parametrize("blank", ["", " ", "\t", " \t ", "\x0c", "\u3000", "\x85"])
def test_whitespace_only_line_separates(blank):
    paras = list(parse_paragraphs(f"A: 1\n{blank}\nB: 2\n"))
    assert [p.fields_in_order() for p in paras] == [["A"], ["B"]]
    with pytest.raises(ControlError, match=r"^line 3:"):
        list(parse_paragraphs(f"A: 1\n{blank}\n c\n"))


def test_many_blank_lines_and_leading_trailing_blanks():
    paras = list(parse_paragraphs("\n\n  \nA: 1\n\n\n\nB: 2\n\n\n"))
    assert [p.fields_in_order() for p in paras] == [["A"], ["B"]]


# 7. value whitespace


def test_value_whitespace_rules():
    (p,) = parse_paragraphs("A:  v  \nB:\nC:x\nD:  first  \n  cont  \n\tt\t\nE:\n x\n")
    assert p["A"] == "v"
    assert p["B"] == ""
    assert p["C"] == "x"
    assert p["D"] == "first\n  cont  \n\tt\t"
    assert p["E"] == "\n x"


def test_field_name_may_contain_inner_spaces_and_is_stripped():
    (p,) = parse_paragraphs("My Field : a: b\n")
    assert p.fields_in_order() == ["My Field"]
    assert p["my field"] == "a: b"


# 8. equality and hashing


def test_paragraph_equality_and_hashing():
    a = Paragraph([("A", "1"), ("B", "2")])
    b = Paragraph([("B", "2"), ("A", "1")])
    assert a == b
    assert a == {"A": "1", "B": "2"}
    assert Paragraph([("A", "1")]) != Paragraph([("a", "1")])
    assert a != Paragraph([("A", "1")])
    with pytest.raises(TypeError):
        hash(a)


def test_paragraph_in_dataclass_with_hash_false():
    @dataclasses.dataclass(frozen=True)
    class Rec:
        name: str
        para: Paragraph = dataclasses.field(hash=False, compare=False)

    r = Rec("x", Paragraph([("A", "1")]))
    assert hash(r) == hash(Rec("x", Paragraph()))


# 9. subclassing


def test_subclass_with_extra_attributes():
    class Sub(Paragraph):
        def __init__(self, items=()):
            super().__init__(items)
            self.extra = 42

    s = Sub([("A", "1")])
    assert s.extra == 42
    assert s["a"] == "1"
    assert s == {"A": "1"}


# 10. strip_clearsign


def test_strip_clearsign_headers_without_blank_give_empty():
    assert strip_clearsign(f"{BEGIN}\nHash: SHA256\nA: 1\n") == ""
    assert strip_clearsign(f"{BEGIN}\n") == ""
    assert strip_clearsign(BEGIN) == ""


def test_strip_clearsign_headers_dropped_whatever_they_contain():
    assert strip_clearsign(f"{BEGIN}\nHash: SHA512\nX: y\n\nA: 1\n{SIG}\n") == "A: 1\n"


def test_strip_clearsign_missing_signature_keeps_body():
    assert strip_clearsign(f"{BEGIN}\nHash: SHA256\n\nA: 1\nB: 2") == "A: 1\nB: 2\n"


def test_strip_clearsign_empty_body():
    assert strip_clearsign(f"{BEGIN}\n\n{SIG}\nxx\n") == ""


def test_strip_clearsign_escaped_marker_does_not_end_body():
    text = f"{BEGIN}\n\nA: 1\n- {SIG}\nB: 2\n{SIG}\n-----END PGP SIGNATURE-----\n"
    assert strip_clearsign(text) == f"A: 1\n{SIG}\nB: 2\n"


def test_strip_clearsign_text_after_signature_discarded():
    text = f"{BEGIN}\n\nA: 1\n{SIG}\nsig\n-----END PGP SIGNATURE-----\n\nEvil: 1\n"
    assert strip_clearsign(text) == "A: 1\n"


def test_strip_clearsign_leading_whitespace_means_unsigned():
    for text in (f" {BEGIN}\n\nA: 1\n", f"\n{BEGIN}\n\nA: 1\n", f"\t{BEGIN}\n\nA: 1\n"):
        assert strip_clearsign(text) == text


def test_strip_clearsign_ignores_one_leading_bom():
    assert strip_clearsign(f"\ufeff{BEGIN}\n\nA: 1\n{SIG}\n") == "A: 1\n"
    double = f"\ufeff\ufeff{BEGIN}\n\nA: 1\n{SIG}\n"
    assert strip_clearsign(double) == double


def test_strip_clearsign_tolerates_trailing_whitespace_on_markers():
    text = f"{BEGIN} \t\r\nHash: SHA256\r\n\r\nA: 1\r\n{SIG}  \r\nsig\r\n"
    assert strip_clearsign(text) == "A: 1\n"


def test_strip_clearsign_lone_cr_and_unsigned_identity():
    assert strip_clearsign(f"{BEGIN}\r\rA: 1\r{SIG}\r") == "A: 1\n"
    plain = "A: 1\r\nB: 2\r"
    assert strip_clearsign(plain) == plain


def test_strip_clearsign_dash_escape_only_dash_space():
    text = f"{BEGIN}\n\n- A: 1\n-B: 2\n--C\n- \n{SIG}\n"
    assert strip_clearsign(text) == "A: 1\n-B: 2\n--C\n\n"


# 11. decode_text


def test_decode_text_utf8_keeps_bom_and_is_silent(caplog):
    with caplog.at_level(logging.WARNING):
        assert decode_text("\ufeffA: é\n".encode(), "x") == "\ufeffA: é\n"
    assert caplog.text == ""


def test_decode_text_latin1_warning_names_source(caplog):
    with caplog.at_level(logging.WARNING, logger="minideblib.control"):
        assert decode_text(b"A: \xe9\n", "Packages.xz") == "A: é\n"
    records = [r for r in caplog.records if r.name == "minideblib.control"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "latin-1" in records[0].getMessage()
    assert "Packages.xz" in records[0].getMessage()


def test_decode_text_generic_label_without_source(caplog):
    with caplog.at_level(logging.WARNING):
        assert decode_text(b"\xff\xfe") == "\xff\xfe"
    assert caplog.text.count("latin-1") == 1


def test_decode_text_never_raises_for_any_byte():
    data = bytes(range(256))
    assert decode_text(data) == data.decode("latin-1")


# 12. BOM handling in parse_paragraphs (decision 2: one leading BOM is ignored)


def test_parse_paragraphs_ignores_one_leading_bom():
    text = "\ufeffPackage: foo\n"
    for source in (text, text.encode(), io.StringIO(text), io.BytesIO(text.encode())):
        (p,) = parse_paragraphs(source)
        assert p.fields_in_order() == ["Package"]
    (p,) = parse_paragraphs("\ufeff\ufeffA: 1\n")
    assert p.fields_in_order() == ["\ufeffA"]
    (p,) = parse_paragraphs("A: x\ufeff\n")
    assert p["A"] == "x\ufeff"


def test_parse_file_bom_prefixed_inrelease_recognised(tmp_path):
    f = tmp_path / "InRelease"
    f.write_bytes(("\ufeff" + CLEARSIGNED).encode())
    (p,) = parse_file(f)
    assert p["Suite"] == "stable"


# 13. whole-buffer latin-1 fallback


def test_bytes_invalid_utf8_decoded_as_latin1_whole(caplog):
    data = "A: é\n".encode() + b"B: \xe9\n"
    with caplog.at_level(logging.WARNING):
        (p,) = parse_paragraphs(data)
    assert p["A"] == "Ã©"
    assert p["B"] == "é"
    assert caplog.text.count("latin-1") == 1


def test_byte_stream_decoded_line_by_line(caplog):
    stream = io.BytesIO("A: é\n".encode() + b"B: \xe9\n")
    with caplog.at_level(logging.WARNING):
        (p,) = parse_paragraphs(stream)
    assert p["A"] == "é"
    assert p["B"] == "é"
    assert caplog.text.count("latin-1") == 1


def test_text_stream_decode_error_propagates(tmp_path):
    f = tmp_path / "bad"
    f.write_bytes(b"A: \xff\n")
    with f.open(encoding="utf-8") as fh, pytest.raises(UnicodeDecodeError):
        list(parse_paragraphs(fh))


# 14. NUL bytes and large inputs


def test_nul_and_control_characters_kept():
    text = "A: x\x00y\x01\n B\x00\nC\x00D: 1\n"
    for source in (text, text.encode(), io.StringIO(text), io.BytesIO(text.encode())):
        (p,) = parse_paragraphs(source)
        assert p["A"] == "x\x00y\x01\n B\x00"
        assert p["C\x00D"] == "1"


def test_many_continuation_lines_linear():
    n = 200_000
    text = "Files:\n" + " d41d8cd98f00b204e9800998ecf8427e 6 f\n" * n
    start = time.perf_counter()
    (p,) = parse_paragraphs(text)
    value = p["Files"]
    folded = p.get_folded("Files")
    dumped = p.dump()
    elapsed = time.perf_counter() - start
    assert value.count("\n") == n
    assert len(folded) > n
    assert dumped == text
    assert elapsed < 10


def test_huge_single_line_linear():
    text = "A: " + "x" * 20_000_000
    start = time.perf_counter()
    (p,) = parse_paragraphs(text)
    assert len(p["A"]) == 20_000_000
    assert time.perf_counter() - start < 10


def test_many_paragraphs():
    text = "".join(f"Package: p{i}\nVersion: 1\n\n" for i in range(50_000))
    paras = list(parse_paragraphs(text))
    assert len(paras) == 50_000
    assert paras[-1]["Package"] == "p49999"


# 15. get_folded with a present empty value


def test_get_folded_present_empty_returns_empty_not_default():
    p = Paragraph([("A", ""), ("B", "\n \n\t")])
    assert p.get_folded("A", "dflt") == ""
    assert p.get_folded("B", "dflt") == ""
    assert p.get_folded("Z", "dflt") == "dflt"


def test_get_folded_splits_only_on_cr_lf():
    p = Paragraph([("A", "a  b\r\n c\rd\x0ce\u2028f")])
    assert p.get_folded("A") == "a  b c d\x0ce\u2028f"


# 16. parse_file errors and warnings


def test_parse_file_missing_raises_oserror(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "nope")


def test_parse_file_latin1_warning_names_path(tmp_path, caplog):
    f = tmp_path / "Packages"
    f.write_bytes(b"A: \xe9\n")
    with caplog.at_level(logging.WARNING):
        (p,) = parse_file(f)
    assert p["A"] == "é"
    assert str(f) in caplog.text


def test_parse_file_error_line_after_armor(tmp_path):
    f = tmp_path / "InRelease"
    f.write_text(f"{BEGIN}\nHash: SHA256\n\nA: 1\nbad\n{SIG}\n")
    with pytest.raises(ControlError, match=r"^line 2:"):
        parse_file(f)


# decision 1: consistent CR / LF / CRLF splitting for every input kind


@pytest.mark.parametrize(
    "text",
    [
        "A: 1\r\r\nB: 2\n",
        "A: 1\rB: 2\r c\r\rC: 3",
        "A: 1\r\n c\r\n\r\nB: 2\r\n",
        "A: 1\n\rB: 2\n",
    ],
)
def test_line_splitting_consistent_for_all_inputs(text):
    expected = [list(p.items()) for p in parse_paragraphs(text)]
    for source in (text.encode(), io.StringIO(text), io.BytesIO(text.encode())):
        assert [list(p.items()) for p in parse_paragraphs(source)] == expected


def test_lone_cr_splits_lines():
    paras = list(parse_paragraphs("A: 1\r\r\nB: 2\n"))
    assert [p.fields_in_order() for p in paras] == [["A"], ["B"]]
    (p,) = parse_paragraphs("A: 1\r c\rB: 2")
    assert p["A"] == "1\n c"
    assert p["B"] == "2"


def test_iterable_of_lines_each_item_is_a_line():
    paras = list(parse_paragraphs(["A: 1", "B: 2\r", "\n", "\n", "C: 3"]))
    assert [p.fields_in_order() for p in paras] == [["A", "B"], ["C"]]
    (p,) = parse_paragraphs([b"A: 1\r\n", b" x\r\n"])
    assert p["A"] == "1\n x"
    for items in (["A: 1", "", "B: 2"], [b"A: 1", b"", b"B: 2"], ["A: 1\r", "\n", "B: 2"]):
        paras = list(parse_paragraphs(items))
        assert [p.fields_in_order() for p in paras] == [["A"], ["B"]], items


def test_dump_forms():
    p = Paragraph([("A", ""), ("B", "\n x"), ("C", "v"), ("D", "f\n c")])
    assert p.dump() == "A:\nB:\n x\nC: v\nD: f\n c\n"
    again = list(parse_paragraphs(p.dump()))
    assert again == [p]


def test_noncanonical_input_dumps_canonically():
    (p,) = parse_paragraphs("A :  1  \r\nB: \r\n x\r\n")
    assert p.dump() == "A: 1\nB:\n x\n"
