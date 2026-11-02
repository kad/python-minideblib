# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import shutil
import subprocess

import pytest

from minideblib import deb
from minideblib.control import Paragraph
from tests.fixtures.debs import have_zstd, make_ar, make_deb, make_tar

CONTROL = {"Package": "demo", "Version": "1.0-1", "Architecture": "all", "Description": "d"}
FILES = {"usr/bin/x": b"#!/bin/sh\n", "usr/share/doc/demo/README": b"readme\n"}

COMPS = [
    "gz",
    "xz",
    "bz2",
    "none",
    pytest.param("zst", marks=pytest.mark.skipif(not have_zstd(), reason="no zstd codec")),
]


def write(tmp_path, data, name="t.deb"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("data_comp", COMPS)
@pytest.mark.parametrize("control_comp", COMPS)
def test_roundtrip(tmp_path, data_comp, control_comp):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES, data_comp, control_comp)))
    assert isinstance(pkg.control, Paragraph)
    assert pkg.control["Package"] == "demo"
    assert pkg.format_version == "2.0"
    assert set(pkg.control_files) == {"control", "md5sums"}
    assert len(pkg.md5sums["usr/bin/x"]) == 32
    names = [f.path for f in pkg.files()]
    assert "./usr/bin/x" in names
    f = next(f for f in pkg.files() if f.path == "./usr/bin/x")
    assert (f.size, f.type, f.mode & 0o777) == (10, "f", 0o755)
    assert pkg.read("usr/bin/x") == FILES["usr/bin/x"]
    assert pkg.read("./usr/bin/x") == FILES["usr/bin/x"]


@pytest.mark.parametrize("sep", ["\x0c", "\u2028", "\x1c"])
def test_md5sums_split_at_lf_only(tmp_path, sep):
    name = f"usr/share/a{sep}b"
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, {name: b"x"})))
    assert name in pkg.md5sums
    assert "b" not in pkg.md5sums


def test_read_missing(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    with pytest.raises(KeyError):
        pkg.read("nope")


def test_read_max_size(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    assert pkg.read("usr/bin/x", max_size=len(FILES["usr/bin/x"])) == FILES["usr/bin/x"]
    with pytest.raises(deb.DebMemberTooLargeError):
        pkg.read("usr/bin/x", max_size=len(FILES["usr/bin/x"]) - 1)
    assert issubclass(deb.DebMemberTooLargeError, deb.DebSizeLimitError)
    with pytest.raises(deb.MissingMemberError):
        pkg.read("nope", max_size=10)


def test_entry_limit_error_is_a_size_limit_error_and_exported():
    assert issubclass(deb.DebEntryLimitError, deb.DebSizeLimitError)
    assert "DebEntryLimitError" in deb.__all__
    assert deb.__all__ == sorted(deb.__all__, key=lambda n: (not n[0].isupper(), n))


def test_files_max_entries(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    total = len(pkg.files())
    assert len(pkg.files(max_entries=total)) == total
    with pytest.raises(deb.DebEntryLimitError, match=f"more than {total - 1} entries"):
        pkg.files(max_entries=total - 1)


def test_extract_all_and_subset(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    d1 = tmp_path / "all"
    d1.mkdir()
    pkg.extract(d1)
    assert (d1 / "usr/bin/x").read_bytes() == FILES["usr/bin/x"]
    d2 = tmp_path / "some"
    d2.mkdir()
    pkg.extract(d2, ["usr/bin/x"])
    assert (d2 / "usr/bin/x").exists()
    assert not (d2 / "usr/share").exists()


@pytest.mark.parametrize("evil", ["../evil", "usr/../../evil", "/etc/evil"])
def test_extract_rejects_traversal(tmp_path, evil):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, {evil: b"x"})))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError):
        pkg.extract(dest)
    assert not (tmp_path / "evil").exists()
    assert list(dest.iterdir()) == []


def test_extract_rejects_symlink_escape(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("./link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc"
        tf.addfile(ti)
        ti = tarfile.TarInfo("./link/pwn")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    import gzip

    raw = make_ar(
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", gzip.compress(make_tar({"control": b"Package: a\n"}))),
            ("data.tar.gz", gzip.compress(buf.getvalue())),
        ]
    )
    pkg = deb.DebPackage.open(write(tmp_path, raw))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError):
        pkg.extract(dest)


def test_extract_size_limit(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, {"big": b"0" * 5000})))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError):
        pkg.extract(dest, max_size=1000)


def test_missing_members(tmp_path):
    with pytest.raises(deb.DebError):
        deb.DebPackage.open(write(tmp_path, make_ar([("debian-binary", b"2.0\n")])))


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb not available")
@pytest.mark.parametrize("comp", ["gzip", "xz", "none"])
def test_real_dpkg_deb(tmp_path, comp):
    root = tmp_path / "root"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/x").write_text("#!/bin/sh\n")
    (root / "usr/bin/x").chmod(0o755)
    (root / "DEBIAN/control").write_text(
        "Package: real\nVersion: 2.0\nArchitecture: all\nMaintainer: A <a@b.c>\nDescription: d\n"
    )
    out = tmp_path / "real.deb"
    subprocess.run(
        ["dpkg-deb", f"-Z{comp}", "--root-owner-group", "--build", str(root), str(out)],
        check=True,
        capture_output=True,
    )
    pkg = deb.DebPackage.open(out)
    assert pkg.control["Package"] == "real"
    assert any(f.path.endswith("usr/bin/x") for f in pkg.files())
    assert pkg.read("usr/bin/x") == b"#!/bin/sh\n"


def _deb_with_data_tar(tmp_path, tar_bytes, comp=None):
    import gzip

    return deb.DebPackage.open(
        write(
            tmp_path,
            make_ar(
                [
                    ("debian-binary", b"2.0\n"),
                    ("control.tar.gz", gzip.compress(make_tar({"control": b"Package: a\n"}))),
                    ("data.tar.gz", gzip.compress(tar_bytes)),
                ]
            ),
        )
    )


def test_truncated_data_tar_raises_deberror(tmp_path):
    tar = make_tar({"usr/bin/x": b"y" * 5000})
    with pytest.raises(deb.DebError):
        _deb_with_data_tar(tmp_path, tar[:3000]).files()
    pkg = _deb_with_data_tar(tmp_path, tar[:3000])
    dest = tmp_path / "d"
    dest.mkdir()
    with pytest.raises(deb.DebError):
        pkg.extract(dest)


def test_corrupt_gzip_data_raises_deberror(tmp_path):
    raw = make_deb(CONTROL, FILES)
    bad = raw.replace(b"\x1f\x8b", b"\x1f\x8b", 1)
    pkg = deb.DebPackage.open(write(tmp_path, bad))
    pkg._data_raw = pkg._data_raw[:20]
    pkg._data_tar = None
    with pytest.raises(deb.DebError):
        pkg.files()


def test_corrupt_control_gzip_raises_deberror(tmp_path):
    raw = make_deb(CONTROL, FILES)
    i = raw.index(b"control.tar.gz") + 60
    bad = raw[: i + 20] + b"\xff" * 20 + raw[i + 40 :]
    with pytest.raises(deb.DebError):
        deb.DebPackage.open(write(tmp_path, bad))


def test_size_limit_is_distinguishable(tmp_path):
    from minideblib.compress import DecompressionLimitError

    path = write(tmp_path, make_deb(CONTROL, {"big": b"0" * 100000}))
    with pytest.raises(deb.DebError) as ei:
        deb.DebPackage.open(path, max_size=2000).files()
    assert isinstance(ei.value, DecompressionLimitError)


def test_extract_rejects_relative_symlink_escape(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("./link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "../.."
        tf.addfile(ti)
        ti = tarfile.TarInfo("./link/escape.txt")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    pkg = _deb_with_data_tar(tmp_path, buf.getvalue())
    base = tmp_path / "base"
    dest = base / "a" / "dest"
    dest.mkdir(parents=True)
    with pytest.raises(deb.DebError):
        pkg.extract(dest)
    assert not (base / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not any(p.name == "escape.txt" for p in tmp_path.rglob("*"))


@pytest.mark.parametrize("everything", [".", "./", ""])
def test_extract_dot_means_everything(tmp_path, everything):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    dest = tmp_path / "dest"
    dest.mkdir()
    pkg.extract(dest, [everything])
    assert (dest / "usr/bin/x").read_bytes() == FILES["usr/bin/x"]
    assert (dest / "usr/share/doc/demo/README").exists()


def test_extract_needs_tarfile_filters(tmp_path, monkeypatch):
    import tarfile

    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    monkeypatch.delattr(tarfile, "data_filter")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError, match=r"Python 3\.11\.4\+"):
        pkg.extract(dest)
    assert list(dest.iterdir()) == []


def _links_tar(extra=()):
    """A data tar like real packages: an absolute symlink across top-level dirs."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in ("./", "./usr/", "./usr/lib/", "./usr/lib/ssl/"):
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            tf.addfile(ti)
        ti = tarfile.TarInfo("./usr/lib/ssl/openssl.cnf")
        ti.size = 3
        tf.addfile(ti, io.BytesIO(b"cnf"))
        ti = tarfile.TarInfo("./usr/lib/ssl/certs")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/ssl/certs"
        tf.addfile(ti)
        ti = tarfile.TarInfo("./usr/lib/ssl/rel")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "openssl.cnf"
        tf.addfile(ti)
        for name, link in extra:
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = link
            tf.addfile(ti)
    return buf.getvalue()


def test_extract_absolute_symlink_rejected_by_default(tmp_path):
    pkg = _deb_with_data_tar(tmp_path, _links_tar())
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError, match="unsafe link target"):
        pkg.extract(dest)
    assert list(dest.iterdir()) == []


def test_extract_skip_absolute_symlinks(tmp_path, caplog):
    import logging

    pkg = _deb_with_data_tar(tmp_path, _links_tar())
    dest = tmp_path / "dest"
    dest.mkdir()
    with caplog.at_level(logging.WARNING, logger="minideblib"):
        skipped = pkg.extract(dest, skip_absolute_symlinks=True)
    assert skipped == [("usr/lib/ssl/certs", "/etc/ssl/certs")]
    assert (dest / "usr/lib/ssl/openssl.cnf").read_bytes() == b"cnf"
    assert (dest / "usr/lib/ssl/rel").is_symlink()
    assert not (dest / "usr/lib/ssl/certs").exists()
    assert not (dest / "usr/lib/ssl/certs").is_symlink()
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "deb: skipping absolute symlink usr/lib/ssl/certs -> /etc/ssl/certs"
    ]
    plain = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES), "plain.deb"))
    (tmp_path / "plain").mkdir()
    assert plain.extract(tmp_path / "plain") == []


@pytest.mark.parametrize(
    "extra",
    [
        [("./usr/lib/ssl/up", "../../../../outside")],  # relative link escaping dest
        [("./../evil", "x")],  # member path traversal
    ],
)
def test_extract_skip_flag_still_rejects_other_unsafe(tmp_path, extra):
    pkg = _deb_with_data_tar(tmp_path, _links_tar(extra))
    base = tmp_path / "base"
    dest = base / "dest"
    dest.mkdir(parents=True)
    with pytest.raises(deb.DebError):
        pkg.extract(dest, skip_absolute_symlinks=True)
    assert not (base / "outside").exists() and not (base / "evil").exists()


def test_extract_skip_flag_rejects_hostile_traversal(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, {"../x": b"x", "ok": b"y"})))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError, match="unsafe member path"):
        pkg.extract(dest, skip_absolute_symlinks=True)
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize(
    "target", ["./usr/lib/ssl/certs", "usr/lib/ssl/certs", "usr//lib/ssl/./certs"]
)
def test_extract_hardlink_to_skipped_symlink_rejected(tmp_path, target):
    import io
    import tarfile

    buf = io.BytesIO(_links_tar())
    with tarfile.open(fileobj=buf, mode="a") as tf:
        ti = tarfile.TarInfo("./usr/lib/ssl/hard")
        ti.type = tarfile.LNKTYPE
        ti.linkname = target
        tf.addfile(ti)
    pkg = _deb_with_data_tar(tmp_path, buf.getvalue())
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError, match="hard link to skipped symlink: 'usr/lib/ssl/hard'"):
        pkg.extract(dest, skip_absolute_symlinks=True)
    assert list(dest.iterdir()) == []


# -- hostile archives: only DebError escapes ------------------------------------


def _hostile_tar(kind: str) -> bytes:
    """A data tar reproducing one hostile case (see ``HOSTILE``)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        if kind.startswith("mtime="):
            ti = tarfile.TarInfo("./f")
            ti.size = 1
            ti.pax_headers = {"mtime": kind.split("=", 1)[1]}
            tf.addfile(ti, io.BytesIO(b"x"))
            return _close(tf, buf)
        if kind in ("link-through-skipped", "link-to-symlink-unselected"):
            ti = tarfile.TarInfo("./etc")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc" if kind == "link-through-skipped" else "usr"
            tf.addfile(ti)
        ti = tarfile.TarInfo("./hard")
        ti.type = tarfile.LNKTYPE
        ti.linkname = {
            "link-missing": "nothere",
            "link-through-skipped": "etc/passwd",
            "link-to-symlink-unselected": "etc",
            "link-forward": "later",
        }[kind]
        tf.addfile(ti)
        if kind == "link-forward":
            ti = tarfile.TarInfo("./later")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
    return buf.getvalue()


def _close(tf, buf) -> bytes:
    tf.close()
    return buf.getvalue()


HOSTILE = [
    ("link-missing", "hard link to a missing member"),
    ("link-forward", "hard link to a missing member"),
    ("link-through-skipped", "hard link to a missing member"),
    ("link-to-symlink-unselected", "hard link to symlink"),
    ("mtime=1e400", "invalid mtime"),
    ("mtime=-1e30", "invalid mtime"),
    ("mtime=nan", "invalid mtime"),
]


@pytest.mark.parametrize(("kind", "msg"), HOSTILE)
def test_extract_hostile_archive_raises_deb_error(tmp_path, kind, msg):
    pkg = _deb_with_data_tar(tmp_path, _hostile_tar(kind))
    dest = tmp_path / "dest"
    dest.mkdir()
    members = ["hard"] if kind == "link-to-symlink-unselected" else None
    with pytest.raises(deb.DebError, match=msg):
        pkg.extract(dest, members, skip_absolute_symlinks=True)
    assert list(dest.iterdir()) == []
    with pytest.raises(deb.MissingMemberError):
        pkg.read("hard")  # a hard link is not a regular file
    files = pkg.files()  # listing never fails on these values
    assert all(0 <= f.mtime < 2**37 for f in files)


def test_files_invalid_mtime_listed_as_zero(tmp_path):
    for value in ("1e400", "-1e30", "nan", "-1"):
        pkg = _deb_with_data_tar(tmp_path, _hostile_tar(f"mtime={value}"))
        assert [f.mtime for f in pkg.files()] == [0]


def test_extract_backstop_wraps_tarfile_errors(tmp_path, monkeypatch):
    import tarfile

    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    for exc in (KeyError("x"), ValueError("x"), OverflowError("x"), tarfile.ExtractError("x")):

        def boom(*a, exc=exc, **kw):
            raise exc

        monkeypatch.setattr(tarfile.TarFile, "extractall", boom)
        (tmp_path / "d").mkdir(exist_ok=True)
        with pytest.raises(deb.DebError, match="unsafe or invalid member"):
            pkg.extract(tmp_path / "d")


def _deb_with_format(tmp_path, content):
    import gzip

    return deb.DebPackage.open(
        write(
            tmp_path,
            make_ar(
                [
                    ("debian-binary", content),
                    ("control.tar.gz", gzip.compress(make_tar({"control": b"Package: a\n"}))),
                    ("data.tar.gz", gzip.compress(make_tar({"x": b"x"}))),
                ]
            ),
        )
    )


@pytest.mark.parametrize(
    ("content", "version"), [(b"2.0\n", "2.0"), (b"2.1\n", "2.1"), (b"2.10\n", "2.10")]
)
def test_debian_binary_format_2x_accepted(tmp_path, content, version):
    assert _deb_with_format(tmp_path, content).format_version == version


@pytest.mark.parametrize(
    "content",
    [b"1.0\n", b"3.0\n", b"0.939000\n", b"2\n", b"2.\n", b"2.x\n", b"", b"garbage\n", b"\xff\xfe"],
)
def test_debian_binary_other_format_rejected(tmp_path, content):
    with pytest.raises(deb.DebError, match="debian-binary"):
        _deb_with_format(tmp_path, content)


def test_debian_binary_message_is_truncated(tmp_path):
    with pytest.raises(deb.DebError) as ei:
        _deb_with_format(tmp_path, b"9" * 500 + b"\n")
    assert "9" * 41 not in str(ei.value)
    assert "..." in str(ei.value)


def test_missing_debian_binary(tmp_path):
    import gzip

    raw = make_ar(
        [
            ("control.tar.gz", gzip.compress(make_tar({"control": b"Package: a\n"}))),
            ("data.tar.gz", gzip.compress(make_tar({"x": b"x"}))),
        ]
    )
    with pytest.raises(deb.DebError, match="missing ar members"):
        deb.DebPackage.open(write(tmp_path, raw))


# -- relative symlink targets are checked before anything is written -----------


def _tar_members(members):
    """Data tar from ``(name, kind, payload)``; kind ``f`` file, ``d`` dir, ``l`` symlink."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, payload in members:
            ti = tarfile.TarInfo(name)
            if kind == "d":
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(ti)
            elif kind == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, payload
                tf.addfile(ti)
            else:
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
    return buf.getvalue()


_HEAD = [("./usr/", "d", ""), ("./usr/lib/", "d", ""), ("./usr/lib/a", "f", b"a")]


@pytest.mark.parametrize(
    "link",
    [
        ("./usr/lib/x", "../../../outside"),
        ("./x", "../outside"),
        ("./usr/x", "lib/../../.."),
        ("./usr/lib/up", "../.."),  # the root itself is fine ...
    ],
)
def test_escaping_relative_symlink_last_writes_nothing(tmp_path, link):
    members = [*_HEAD, (link[0], "l", link[1])]
    if link[1] == "../..":  # ... but going one level further through it is not
        members.append(("./usr/lib/up2", "l", "up/.."))
    pkg = _deb_with_data_tar(tmp_path, _tar_members(members))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError, match="unsafe link target"):
        pkg.extract(dest)
    assert list(dest.iterdir()) == []


def test_symlink_through_shallower_symlink_escape_detected(tmp_path):
    # deep/a -> .. is the root; deep/a/y -> ../z leaves it although deep/a/../z looks inside
    members = [
        ("./deep/", "d", ""),
        ("./deep/f", "f", b"f"),
        ("./deep/a", "l", ".."),
        ("./deep/a/y", "l", "../z"),
    ]
    pkg = _deb_with_data_tar(tmp_path, _tar_members(members))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(deb.DebError, match="unsafe link target"):
        pkg.extract(dest)
    assert list(dest.iterdir()) == []


def test_relative_symlinks_inside_still_extracted(tmp_path):
    members = [
        *_HEAD,
        ("./usr/bin/", "d", ""),
        ("./usr/bin/x", "l", "../lib/a"),
        ("./usr/lib/b", "l", "a"),
        ("./usr/lib/self", "l", "."),
        ("./usr/top", "l", ".."),
        ("./usr/lib/via", "l", "../../usr/lib/a"),
    ]
    pkg = _deb_with_data_tar(tmp_path, _tar_members(members))
    dest = tmp_path / "dest"
    dest.mkdir()
    pkg.extract(dest)
    assert (dest / "usr/bin/x").read_bytes() == b"a"
    assert (dest / "usr/lib/b").read_bytes() == b"a"
    assert (dest / "usr/lib/via").read_bytes() == b"a"
    assert (dest / "usr/top").is_symlink()


# -- the destination itself must not be a symlink (library) --------------------


def test_extract_to_symlinked_dest_refused(tmp_path):
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(deb.DebError, match="symlink"):
        pkg.extract(link)
    with pytest.raises(deb.DebError, match="symlink"):
        pkg.extract(str(link) + "/")  # a trailing slash does not hide it
    assert list(real.iterdir()) == []


def test_extract_below_symlinked_ancestor_allowed(tmp_path):
    # documented: only the final component is checked; ancestors belong to the caller
    pkg = deb.DebPackage.open(write(tmp_path, make_deb(CONTROL, FILES)))
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "home").symlink_to(real, target_is_directory=True)
    pkg.extract(tmp_path / "home" / "dest")
    assert (real / "dest" / "usr/bin/x").read_bytes() == FILES["usr/bin/x"]
