# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Tests for .dsc parsing and verification (offline, inline samples)."""

import hashlib

import pytest

from minideblib.dsc import Dsc, DscError, DscFile, PackageListEntry
from minideblib.version import Version

ORIG = b"orig tarball data"
DEB = b"debian tarball data"
COMP = b"component tarball"


def _h(alg: str, data: bytes) -> str:
    return hashlib.new(alg, data).hexdigest()


def _block(field: str, alg: str, items: dict[str, bytes], blank_first: bool = True) -> str:
    head = f"{field}: \n" if blank_first else f"{field}:\n"
    return head + "".join(f" {_h(alg, d)} {len(d)} {n}\n" for n, d in items.items())


FILES = {
    "sample-pkg_1.0.orig.tar.xz": ORIG,
    "sample-pkg_1.0.orig-docs.tar.gz": COMP,
    "sample-pkg_1.0-1.debian.tar.xz": DEB,
}


def make_text(
    fmt: str | None = "3.0 (quilt)",
    files: dict[str, bytes] | None = None,
    extra: str = "",
    sums: tuple[str, ...] = ("md5", "sha1", "sha256", "sha512"),
) -> str:
    files = FILES if files is None else files
    out = "Source: sample-pkg\n"
    if fmt is not None:
        out = f"Format: {fmt}\n" + out
    out += (
        "Binary: sample-pkg, sample-pkg-doc,\n"
        " sample-pkg-dev\n"
        "Architecture: any all\n"
        "Version: 1:1.0-1\n"
        "Maintainer: Test Maintainer <test@example.com>\n"
        'Uploaders: Alice <a@example.com>, "Bob, Jr." <b@example.com>\n'
        "Homepage: https://www.example.com/sample\n"
        "Standards-Version: 4.7.0\n"
        "Vcs-Browser: https://git.example.com/sample\n"
        "Vcs-Git: https://git.example.com/sample.git\n"
        "Build-Depends: debhelper-compat (= 13),\n"
        " pkg-config\n"
        "Build-Conflicts-Indep: foo\n"
        "Package-List:\n"
        " sample-pkg deb utils optional arch=any\n"
        " sample-pkg-doc deb doc optional arch=all profile=!nodoc\n"
    )
    names = {
        "md5": ("Files", "md5"),
        "sha1": ("Checksums-Sha1", "sha1"),
        "sha256": ("Checksums-Sha256", "sha256"),
        "sha512": ("Checksums-Sha512", "sha512"),
    }
    for key in sums:
        field, alg = names[key]
        out += _block(field, alg, files)
    return out + extra


def signed(text: str) -> str:
    body = "".join(f"- {ln}\n" if ln.startswith("-") else f"{ln}\n" for ln in text.splitlines())
    return (
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        + body
        + "-----BEGIN PGP SIGNATURE-----\n\nabcd\n=xx\n-----END PGP SIGNATURE-----\n"
    )


def test_parse_quilt():
    d = Dsc.parse(make_text())
    assert d.source == "sample-pkg"
    assert d.version == Version("1:1.0-1")
    assert d.format == "3.0 (quilt)"
    assert not d.is_native
    assert d.binaries == ["sample-pkg", "sample-pkg-doc", "sample-pkg-dev"]
    assert d.architectures == ["any", "all"]
    assert d.maintainer == "Test Maintainer <test@example.com>"
    assert d.standards_version == "4.7.0"
    assert d.homepage == "https://www.example.com/sample"
    assert d.vcs == {
        "browser": "https://git.example.com/sample",
        "git": "https://git.example.com/sample.git",
    }
    assert d.build_depends == {
        "Build-Depends": "debhelper-compat (= 13), pkg-config",
        "Build-Conflicts-Indep": "foo",
    }
    assert d.fields["Source"] == "sample-pkg"


def test_optional_fields_absent():
    d = Dsc.parse("Source: s\nVersion: 1\nFiles:\n " + "0" * 32 + " 1 s_1.tar.gz\n")
    assert d.standards_version is None and d.homepage is None
    assert d.vcs == {} and d.build_depends == {} and d.package_list == []
    assert d.uploaders == [] and d.binaries == [] and d.maintainer == ""


def test_uploaders_quoted_comma():
    d = Dsc.parse(make_text())
    assert d.uploaders == ["Alice <a@example.com>", '"Bob, Jr." <b@example.com>']


def test_uploaders_comma_inside_angle_brackets():
    d = Dsc.parse(make_text(extra="Uploaders: A <a,b@example.com>, B <b@example.com>\n"))
    assert d.uploaders == ["A <a,b@example.com>", "B <b@example.com>"]


@pytest.mark.parametrize("sep", ["\u2028", "\x0c", "\x85", "\r"])
def test_package_list_splits_at_lf_only(sep):
    text = make_text().replace(
        " sample-pkg deb utils optional arch=any\n",
        f" sample-pkg deb utils optional arch=any{sep}evil deb x y\n",
    )
    names = [e.name for e in Dsc.parse(text).package_list]
    assert names == ["sample-pkg", "sample-pkg-doc"]


def test_package_list():
    d = Dsc.parse(make_text())
    assert d.package_list == [
        PackageListEntry("sample-pkg", "deb", "utils", "optional", {"arch": "any"}),
        PackageListEntry(
            "sample-pkg-doc", "deb", "doc", "optional", {"arch": "all", "profile": "!nodoc"}
        ),
    ]


def test_files_merge_and_helpers():
    d = Dsc.parse(make_text())
    assert [f.name for f in d.files] == list(FILES)
    f = d.files[0]
    assert f == DscFile(
        "sample-pkg_1.0.orig.tar.xz",
        len(ORIG),
        _h("md5", ORIG),
        _h("sha1", ORIG),
        _h("sha256", ORIG),
        _h("sha512", ORIG),
    )
    assert [x.name for x in d.orig_tarballs()] == [
        "sample-pkg_1.0.orig.tar.xz",
        "sample-pkg_1.0.orig-docs.tar.gz",
    ]
    deb = d.debian_tarball()
    assert deb is not None and deb.name == "sample-pkg_1.0-1.debian.tar.xz"
    assert d.diff() is None


def test_checksums_only_and_md5_none():
    d = Dsc.parse(make_text(sums=("sha256",)))
    assert d.files[0].md5 is None and d.files[0].sha1 is None
    assert d.files[0].sha256 == _h("sha256", ORIG)


def test_checksum_file_not_in_files_is_merged():
    extra = {"extra.tar.gz": b"x"}
    text = make_text(sums=("md5",)) + _block("Checksums-Sha256", "sha256", extra)
    d = Dsc.parse(text)
    extra_file = d.files[-1]
    assert extra_file.name == "extra.tar.gz" and extra_file.md5 is None
    assert extra_file.sha256 == _h("sha256", b"x")


def test_no_trailing_space_variant():
    text = make_text().replace(": \n", ":\n")
    assert Dsc.parse(text).files[0].size == len(ORIG)


def test_clearsigned_and_text_after_signature_ignored():
    text = make_text(extra="Description: -dash line\n -- and more\n")
    d = Dsc.parse(signed(text) + "\nSource: other\nVersion: 9\n")
    assert d.source == "sample-pkg"
    assert d.fields["Description"].startswith("-dash line")
    assert len(d.files) == 3


def test_clearsigned_bytes_and_crlf():
    text = signed(make_text()).replace("\n", "\r\n")
    d = Dsc.parse(text.encode())
    assert d.source == "sample-pkg" and d.binaries[-1] == "sample-pkg-dev"
    assert d.files[0].size == len(ORIG)
    d2 = Dsc.parse(make_text().replace("\n", "\r\n"))
    assert d2.files[0].size == len(ORIG)


def test_format_missing_is_1_0():
    d = Dsc.parse(make_text(fmt=None, files={"s_1.tar.gz": b"a"}))
    assert d.format == "1.0" and d.is_native


def test_is_native_variants():
    assert Dsc.parse(make_text(fmt="3.0 (native)")).is_native
    assert not Dsc.parse(make_text(fmt="3.0 (quilt)")).is_native
    with_diff = {"s_1.orig.tar.gz": b"a", "s_1-1.diff.gz": b"b"}
    d = Dsc.parse(make_text(fmt="1.0", files=with_diff))
    assert not d.is_native
    diff = d.diff()
    assert diff is not None and diff.name == "s_1-1.diff.gz"
    assert Dsc.parse(make_text(fmt="1.0", files={"s_1.tar.gz": b"a"})).is_native


def test_size_disagreement():
    text = make_text(sums=("md5",)).replace(f" {len(ORIG)} sample-pkg_1.0.orig.tar.xz", " 3 x", 1)
    text = text.replace(" 3 x", " 3 sample-pkg_1.0.orig.tar.xz")
    text += _block("Checksums-Sha256", "sha256", FILES)
    with pytest.raises(DscError, match="sample-pkg_1.0.orig.tar.xz"):
        Dsc.parse(text)


@pytest.mark.parametrize("drop", ["Source", "Version"])
def test_missing_required_field(drop):
    lines = [ln for ln in make_text().splitlines(keepends=True) if not ln.startswith(drop + ":")]
    with pytest.raises(DscError, match=drop):
        Dsc.parse("".join(lines))


def test_missing_files():
    with pytest.raises(DscError, match="Files"):
        Dsc.parse("Source: s\nVersion: 1\n")


def test_empty_and_garbage():
    with pytest.raises(DscError):
        Dsc.parse("")
    with pytest.raises(DscError):
        Dsc.parse("not a field\n")
    with pytest.raises(DscError, match="Version"):
        Dsc.parse("Source: s\nVersion: !bad!\nFiles:\n " + "0" * 32 + " 1 a\n")


@pytest.mark.parametrize(
    "line",
    [
        "zz" * 16 + " 1 a.tar.gz",  # not hex
        "A" * 32 + " 1 a.tar.gz",  # upper case
        "0" * 31 + " 1 a.tar.gz",  # wrong length
        "0" * 32 + " x a.tar.gz",  # bad size
        "0" * 32 + " 1",  # missing name
        "0" * 32 + " 1 dir/a.tar.gz",
        "0" * 32 + " 1 .hidden",
    ],
)
def test_bad_file_lines(line):
    with pytest.raises(DscError):
        Dsc.parse(f"Source: s\nVersion: 1\nFiles:\n {line}\n")


def test_duplicate_file_in_field():
    row = f" {'0' * 32} 1 a.tar.gz\n"
    with pytest.raises(DscError, match="a.tar.gz"):
        Dsc.parse(f"Source: s\nVersion: 1\nFiles:\n{row}{row}")


def test_oversized_size_raises_dsc_error():
    row = f" {'0' * 32} {'1' * 5000} a.tar.gz\n"
    with pytest.raises(DscError, match="Files"):
        Dsc.parse(f"Source: s\nVersion: 1\nFiles:\n{row}")


def test_load(tmp_path):
    p = tmp_path / "x.dsc"
    p.write_bytes(signed(make_text()).encode())
    assert Dsc.load(p).source == "sample-pkg"
    assert Dsc.load(str(p)).version == Version("1:1.0-1")
    bad = tmp_path / "bad.dsc"
    bad.write_text("Source: s\n")
    with pytest.raises(DscError, match="bad.dsc"):
        Dsc.load(bad)
    with pytest.raises(OSError):
        Dsc.load(tmp_path / "missing.dsc")


# -- verify -------------------------------------------------------------------


@pytest.fixture
def srcdir(tmp_path):
    for name, data in FILES.items():
        (tmp_path / name).write_bytes(data)
    return tmp_path


def test_verify_ok(srcdir):
    assert Dsc.parse(make_text()).verify(srcdir) is None
    assert Dsc.parse(make_text()).verify(str(srcdir)) is None


@pytest.mark.parametrize("alg", ["md5", "sha1", "sha256", "sha512"])
def test_verify_wrong_hash_each_algorithm(srcdir, alg):
    # same size, different content, so only the one algorithm differs
    text = make_text(sums=(alg,))
    (srcdir / "sample-pkg_1.0.orig.tar.xz").write_bytes(b"X" * len(ORIG))
    with pytest.raises(DscError) as exc:
        Dsc.parse(text).verify(srcdir)
    assert "sample-pkg_1.0.orig.tar.xz" in str(exc.value)
    assert ("md5sum" if alg == "md5" else alg) in str(exc.value)


def test_verify_later_algorithm_detected(srcdir):
    good = make_text(sums=("md5", "sha1"))
    bad_sha512 = _block(
        "Checksums-Sha512", "sha512", {**FILES, "sample-pkg_1.0-1.debian.tar.xz": b"other!"}
    )
    # size differs -> sizes disagree at parse time
    with pytest.raises(DscError):
        Dsc.parse(good + bad_sha512)
    same_len = _block(
        "Checksums-Sha512", "sha512", {**FILES, "sample-pkg_1.0-1.debian.tar.xz": b"Y" * len(DEB)}
    )
    with pytest.raises(DscError, match="sha512"):
        Dsc.parse(good + same_len).verify(srcdir)


def test_verify_wrong_size(srcdir):
    (srcdir / "sample-pkg_1.0-1.debian.tar.xz").write_bytes(DEB + b"!")
    with pytest.raises(DscError, match="sample-pkg_1.0-1.debian.tar.xz"):
        Dsc.parse(make_text()).verify(srcdir)


def test_verify_missing_file(srcdir):
    (srcdir / "sample-pkg_1.0.orig-docs.tar.gz").unlink()
    with pytest.raises(DscError, match="orig-docs"):
        Dsc.parse(make_text()).verify(srcdir)


def test_verify_symlink_rejected(srcdir, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "real"
    other.write_bytes(DEB)
    link = srcdir / "sample-pkg_1.0-1.debian.tar.xz"
    link.unlink()
    link.symlink_to(other)
    with pytest.raises(DscError, match="regular"):
        Dsc.parse(make_text()).verify(srcdir)


def test_verify_single_read_pass(srcdir, monkeypatch):
    import builtins

    opened: list[str] = []
    real = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    Dsc.parse(make_text()).verify(srcdir)
    assert len(opened) == len(FILES)


def test_file_name_with_slash_rejected(srcdir):
    text = make_text(files={"../evil.tar.gz": b"x"})
    with pytest.raises(DscError, match="evil"):
        Dsc.parse(text)


@pytest.mark.parametrize("name", ["..\\evil.tar.gz", "dir\\evil.tar.gz", "evil\\"])
def test_file_name_with_backslash_rejected(name):
    text = make_text(files={name: b"x"})
    with pytest.raises(DscError, match="evil"):
        Dsc.parse(text)


def test_orig_tarballs_regex_is_linear():
    import time

    # quadratic regexes take several seconds here; the linear one a few ms
    names = ["x" + ".orig-" * 12000 + "a", "x" + ".orig-a.tar." * 12000 + ".tar"]
    started = time.monotonic()
    for name in names:
        dsc = Dsc.parse("Source: p\nVersion: 1\nChecksums-Sha256:\n " + "0" * 64 + f" 1 {name}\n")
        assert dsc.orig_tarballs() == []
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    ("name", "orig"),
    [
        ("p_1.orig.tar.gz", True),
        ("p_1.orig.tar.xz", True),
        ("p_1.orig-doc.tar.bz2", True),
        ("p_1.orig-doc.tar.xz.asc", False),
        ("p_1.orig.tar.gz.asc", False),
        ("p_1.debian.tar.xz", False),
        ("p_1.tar.gz", False),
    ],
)
def test_orig_tarballs_names(name, orig):
    dsc = Dsc.parse("Source: p\nVersion: 1\nChecksums-Sha256:\n " + "0" * 64 + f" 1 {name}\n")
    assert bool(dsc.orig_tarballs()) is orig


@pytest.mark.parametrize("files", ["Files:\n", "Files: \n", "Checksums-Sha256:\nFiles:\n"])
def test_no_files_listed_rejected(files):
    with pytest.raises(DscError, match="no files listed"):
        Dsc.parse("Source: p\nVersion: 1\n" + files)
