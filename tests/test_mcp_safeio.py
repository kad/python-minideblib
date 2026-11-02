# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Confined write helpers of the MCP server: roots, subdirectories, exclusive files."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from minideblib.errors import MinidebError
from minideblib.mcp import safeio
from minideblib.mcp.safeio import (
    TEMP_PREFIX,
    UnsafeNameError,
    WriteRoot,
    WriteRootError,
    session_temp_dir,
    validate_file_name,
)

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_non_root = pytest.mark.skipif(_IS_ROOT, reason="permission checks do not apply to root")


@pytest.fixture
def root(tmp_path: Path) -> Iterator[WriteRoot]:
    with WriteRoot.open(tmp_path) as r:
        yield r


def _temp_names(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir() if p.name.startswith(TEMP_PREFIX))


# -- errors ----------------------------------------------------------------------


def test_errors_are_minideb_errors() -> None:
    assert issubclass(WriteRootError, MinidebError)
    assert issubclass(WriteRootError, OSError)
    assert issubclass(UnsafeNameError, MinidebError)
    assert issubclass(UnsafeNameError, ValueError)


# -- validate_file_name ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["hello_2.10-3_amd64.deb", "a", "x" * 255, "a-dash.deb", "ünï.deb", "a b.deb", "e\u0301.deb"],
)
def test_valid_file_names(name: str) -> None:
    assert validate_file_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        ".hidden",
        ".minideb-x",
        "a/b",
        "/abs",
        "../up",
        "a\\b",
        "nul\0byte",
        "tab\tname",
        "new\nline",
        "del\x7f",
        "c1\x85",
        "x" * 256,
        "ü" * 128,  # 256 bytes in UTF-8
        "bad\udcff",  # lone surrogate (not encodable)
        "-dash.deb",
        "--help",
        " lead.deb",
        "trail.deb ",
        "trail.deb\u00a0",
        "   ",
        "\u3000",
        "rtl\u202egnp.deb",  # Cf: right-to-left override
        "zero\u200bwidth.deb",  # Cf: zero-width space
        "bom\ufeff.deb",  # Cf
        "line\u2028sep",  # Zl
        "para\u2029sep",  # Zp
        "priv\ue000.deb",  # Co
        "unassigned\U000e0fff.deb",  # Cn
    ],
)
def test_invalid_file_names(name: str) -> None:
    with pytest.raises(UnsafeNameError):
        validate_file_name(name)


def test_validate_file_name_rejects_non_str() -> None:
    with pytest.raises(UnsafeNameError):
        validate_file_name(b"abc")  # ty: ignore[invalid-argument-type]


# -- WriteRoot.open ----------------------------------------------------------------


def test_open_reports_resolved_path(tmp_path: Path) -> None:
    with WriteRoot.open(tmp_path) as r:
        assert r.path == os.path.realpath(tmp_path)
        assert r.writable()
        assert not r.closed
    assert r.closed


def test_open_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "d").mkdir()
    with WriteRoot.open("d") as r:
        assert r.path == os.path.realpath(tmp_path / "d")


def test_symlinked_root_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    with pytest.raises(WriteRootError, match="symlink|not a directory"):
        WriteRoot.open(tmp_path / "link")


def test_missing_root_refused(tmp_path: Path) -> None:
    with pytest.raises(WriteRootError, match="does not exist"):
        WriteRoot.open(tmp_path / "missing")


def test_file_root_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"x")
    with pytest.raises(WriteRootError, match="not a directory"):
        WriteRoot.open(tmp_path / "f")


def test_open_create(tmp_path: Path) -> None:
    with WriteRoot.open(tmp_path / "new", create=True) as r:
        assert (tmp_path / "new").is_dir()
        assert r.writable()
    # existing directory is fine with create=True
    WriteRoot.open(tmp_path / "new", create=True).close()


def test_open_create_does_not_follow_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    with pytest.raises(WriteRootError):
        WriteRoot.open(tmp_path / "link", create=True)


def test_private_root_checks_owner_and_mode(tmp_path: Path) -> None:
    d = tmp_path / "priv"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    WriteRoot.open(d, private=True).close()
    os.chmod(d, 0o755)
    with pytest.raises(WriteRootError, match="accessible by other users"):
        WriteRoot.open(d, private=True)


def test_private_root_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "priv"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    euid = os.geteuid()
    monkeypatch.setattr(safeio.os, "geteuid", lambda: euid + 1)
    with pytest.raises(WriteRootError, match="not owned"):
        WriteRoot.open(d, private=True)


def test_closed_root_raises(tmp_path: Path) -> None:
    r = WriteRoot.open(tmp_path)
    r.close()
    r.close()  # idempotent
    with pytest.raises(WriteRootError, match="closed"):
        r.writable()


# -- writable ---------------------------------------------------------------------


def test_probe_file_removed(root: WriteRoot, tmp_path: Path) -> None:
    created: list[str] = []
    real_open = os.open

    def spy(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if isinstance(path, str) and path.startswith(TEMP_PREFIX):
            created.append(path)
            assert flags & os.O_EXCL and flags & os.O_CREAT and flags & os.O_NOFOLLOW
            assert dir_fd is not None
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safeio.os, "open", spy)
        assert root.writable()
    assert created, "no probe file was created"
    assert list(tmp_path.iterdir()) == []


@needs_non_root
def test_unwritable_directory_not_writable(tmp_path: Path) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        with WriteRoot.open(d) as r:
            assert not r.writable()
    finally:
        os.chmod(d, 0o700)


def test_probe_failure_reported_not_writable(
    root: WriteRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # access() may say yes (e.g. read-only mount, ACLs) while creating fails
    real_open = os.open

    def deny(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if isinstance(path, str) and path.startswith(TEMP_PREFIX):
            raise PermissionError(13, "denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safeio.os, "open", deny)
    assert not root.writable()


def test_free_bytes(root: WriteRoot, tmp_path: Path) -> None:
    st = os.statvfs(tmp_path)
    free = root.free_bytes()
    assert isinstance(free, int)
    assert free >= 0
    assert abs(free - st.f_bavail * st.f_frsize) <= 64 * 1024 * 1024


# -- subdir -----------------------------------------------------------------------


def test_subdir_creates_components(root: WriteRoot, tmp_path: Path) -> None:
    with root.subdir(["a", "b", "c"]) as sub:
        assert (tmp_path / "a" / "b" / "c").is_dir()
        assert sub.path == os.path.join(root.path, "a", "b", "c")
        with sub.create_file("f.deb") as nf:
            nf.file.write(b"data")
            nf.commit()
    assert (tmp_path / "a" / "b" / "c" / "f.deb").read_bytes() == b"data"
    # existing components are reused
    root.subdir(["a", "b"]).close()


def test_subdir_empty_is_same_directory(root: WriteRoot, tmp_path: Path) -> None:
    with root.subdir([]) as sub:
        assert sub.path == root.path
        assert sub.exists("nothing") is False


def test_symlinked_subdirectory_component_refused(root: WriteRoot, tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "evil").symlink_to(outside)
    with pytest.raises(WriteRootError, match="symlink|not a directory"):
        root.subdir(["a", "evil", "x"])
    assert list(outside.iterdir()) == []
    # dangling symlink: mkdir must not create its target
    (tmp_path / "dangling").symlink_to(outside / "made")
    with pytest.raises(WriteRootError):
        root.subdir(["dangling"])
    assert not (outside / "made").exists()


def test_subdir_component_is_file(root: WriteRoot, tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"")
    with pytest.raises(WriteRootError, match="not a directory"):
        root.subdir(["f"])


@pytest.mark.parametrize(
    "parts",
    [
        [".."],
        ["."],
        ["a", ".."],
        ["/etc"],
        ["a/b"],
        [""],
        ["nul\0"],
        [".hidden"],
        [".minideb-x"],
        ["x" * 256],
        ["ctl\x01"],
    ],
)
def test_subdir_odd_parts_rejected(root: WriteRoot, tmp_path: Path, parts: list[str]) -> None:
    with pytest.raises(UnsafeNameError):
        root.subdir(parts)
    assert list(tmp_path.iterdir()) == []


def test_subdir_rejects_string(root: WriteRoot) -> None:
    with pytest.raises(TypeError):
        root.subdir("abc")


# -- create_file / commit / abort ---------------------------------------------------


def test_create_file_temp_name_and_mode(root: WriteRoot, tmp_path: Path) -> None:
    with root.create_file("pkg.deb") as nf:
        assert nf.temp_name.startswith(TEMP_PREFIX)
        assert nf.name == "pkg.deb"
        st = os.lstat(tmp_path / nf.temp_name)
        assert stat.S_ISREG(st.st_mode)
        nf.file.write(b"abc")
        nf.commit()
    assert (tmp_path / "pkg.deb").read_bytes() == b"abc"
    assert _temp_names(tmp_path) == []


def test_hard_link_publish_leaves_no_temp(root: WriteRoot, tmp_path: Path) -> None:
    nf = root.create_file("x.deb")
    nf.file.write(b"payload")
    nf.commit("y.deb")
    assert nf.committed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["y.deb"]
    st = os.lstat(tmp_path / "y.deb")
    assert st.st_nlink == 1
    nf.abort()  # no-op after commit
    assert (tmp_path / "y.deb").read_bytes() == b"payload"


def test_commit_uses_link_without_replace(root: WriteRoot, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real_link = os.link

    def spy(src: str, dst: str, **kw: Any) -> None:
        calls.append((src, dst))
        assert kw.get("src_dir_fd") is not None and kw.get("dst_dir_fd") is not None
        real_link(src, dst, **kw)

    def no_rename(*a: object, **kw: object) -> None:
        raise AssertionError("rename must never be used")

    monkeypatch.setattr(safeio.os, "link", spy)
    monkeypatch.setattr(safeio.os, "rename", no_rename)
    monkeypatch.setattr(safeio.os, "replace", no_rename)
    with root.create_file("a.deb") as nf:
        nf.commit()
    assert calls and calls[0][0].startswith(TEMP_PREFIX) and calls[0][1] == "a.deb"


def test_commit_onto_existing_name_raises(root: WriteRoot, tmp_path: Path) -> None:
    (tmp_path / "a.deb").write_bytes(b"original")
    nf = root.create_file("a.deb")
    nf.file.write(b"new content")
    with pytest.raises(FileExistsError):
        nf.commit()
    assert (tmp_path / "a.deb").read_bytes() == b"original"
    assert _temp_names(tmp_path) == []
    assert not nf.committed


def test_commit_onto_existing_symlink_raises(root: WriteRoot, tmp_path: Path) -> None:
    target = tmp_path.parent / (tmp_path.name + "-target")
    target.write_bytes(b"keep")
    (tmp_path / "a.deb").symlink_to(target)
    with root.create_file("a.deb") as nf:
        nf.file.write(b"evil")
        with pytest.raises(FileExistsError):
            nf.commit()
    assert target.read_bytes() == b"keep"
    assert (tmp_path / "a.deb").is_symlink()


def test_symlink_swapped_in_after_temp_creation(root: WriteRoot, tmp_path: Path) -> None:
    target = tmp_path.parent / (tmp_path.name + "-victim")
    target.write_bytes(b"victim")
    nf = root.create_file("a.deb")
    nf.file.write(b"data")
    # an attacker plants a symlink at the final name, and replaces the temp name
    (tmp_path / "a.deb").symlink_to(target)
    with pytest.raises(FileExistsError):
        nf.commit()
    assert target.read_bytes() == b"victim"
    assert _temp_names(tmp_path) == []


def test_temp_name_replaced_by_symlink_does_not_redirect(root: WriteRoot, tmp_path: Path) -> None:
    target = tmp_path.parent / (tmp_path.name + "-victim2")
    target.write_bytes(b"victim")
    nf = root.create_file("a.deb")
    os.unlink(tmp_path / nf.temp_name)
    (tmp_path / nf.temp_name).symlink_to(target)
    nf.file.write(b"data")
    # the write goes to the already opened file, never through the planted link,
    # and the planted link is not published under the final name
    with pytest.raises(WriteRootError, match="replaced"):
        nf.commit()
    assert target.read_bytes() == b"victim"
    assert not (tmp_path / "a.deb").exists()
    assert not (tmp_path / "a.deb").is_symlink()
    nf.abort()


def test_create_file_never_overwrites(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = TEMP_PREFIX + "taken"
    (tmp_path / taken).write_bytes(b"other")
    names = iter(["taken", "fresh"])
    monkeypatch.setattr(safeio, "_random_suffix", lambda: next(names))
    with root.create_file("a.deb") as nf:
        assert nf.temp_name == TEMP_PREFIX + "fresh"
    assert (tmp_path / taken).read_bytes() == b"other"


def test_create_file_gives_up_after_collisions(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / (TEMP_PREFIX + "same")).write_bytes(b"")
    monkeypatch.setattr(safeio, "_random_suffix", lambda: "same")
    with pytest.raises(FileExistsError):
        root.create_file("a.deb")


def test_abort_removes_temp(root: WriteRoot, tmp_path: Path) -> None:
    nf = root.create_file("a.deb")
    nf.file.write(b"partial")
    nf.abort()
    nf.abort()
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(WriteRootError):
        nf.commit()


def test_exception_in_context_leaves_no_temp(root: WriteRoot, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), root.create_file("a.deb") as nf:
        nf.file.write(b"partial")
        raise RuntimeError("boom")
    assert list(tmp_path.iterdir()) == []


def test_context_exit_without_commit_aborts(root: WriteRoot, tmp_path: Path) -> None:
    with root.create_file("a.deb") as nf:
        nf.file.write(b"x")
    assert list(tmp_path.iterdir()) == []


def test_create_file_validates_names(root: WriteRoot, tmp_path: Path) -> None:
    with pytest.raises(UnsafeNameError):
        root.create_file("../a.deb")
    with root.create_file("a.deb") as nf, pytest.raises(UnsafeNameError):
        nf.commit("../escape.deb")
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path.parent / "escape.deb").exists()


def test_create_file_size_hint_checks_space(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WriteRoot, "free_bytes", lambda self: 100)
    with pytest.raises(WriteRootError, match="space"):
        root.create_file("a.deb", size_hint=101)
    assert list(tmp_path.iterdir()) == []
    with root.create_file("a.deb", size_hint=100) as nf:
        nf.commit()


def test_commit_fsyncs_file_and_directory(root: WriteRoot, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(safeio.os, "fsync", spy)
    with root.create_file("a.deb") as nf:
        nf.file.write(b"x")
        nf.commit()
    assert len(synced) >= 2


def test_link_unsupported_is_clear_error(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_link(*a: object, **kw: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(safeio.os, "link", no_link)
    nf = root.create_file("a.deb")
    with pytest.raises(WriteRootError, match="hard link"):
        nf.commit()
    assert list(tmp_path.iterdir()) == []


def test_new_file_survives_root_close(tmp_path: Path) -> None:
    r = WriteRoot.open(tmp_path)
    nf = r.create_file("a.deb")
    r.close()
    nf.file.write(b"z")
    nf.commit()
    assert (tmp_path / "a.deb").read_bytes() == b"z"


# -- exists / hash_existing ---------------------------------------------------------


def test_exists_uses_lstat(root: WriteRoot, tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    assert root.exists("f")
    assert root.exists("dangling")
    assert not root.exists("nowhere")
    with pytest.raises(UnsafeNameError):
        root.exists("../f")


def test_hash_existing(root: WriteRoot, tmp_path: Path) -> None:
    (tmp_path / "f.deb").write_bytes(b"hello")
    assert root.hash_existing("f.deb", "sha256") == hashlib.sha256(b"hello").hexdigest()
    assert root.hash_existing("f.deb", "sha512") == hashlib.sha512(b"hello").hexdigest()
    with pytest.raises(ValueError, match="algorithm"):
        root.hash_existing("f.deb", "crc32")


def test_hash_existing_does_not_follow_symlinks(root: WriteRoot, tmp_path: Path) -> None:
    target = tmp_path.parent / (tmp_path.name + "-secret")
    target.write_bytes(b"secret")
    (tmp_path / "link.deb").symlink_to(target)
    with pytest.raises(WriteRootError, match="not a regular file"):
        root.hash_existing("link.deb", "sha256")


def test_hash_existing_refuses_non_regular(root: WriteRoot, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(WriteRootError, match="not a regular file"):
        root.hash_existing("d", "sha256")
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(WriteRootError, match="not a regular file"):
        root.hash_existing("fifo", "sha256")  # must not block


def test_hash_existing_missing(root: WriteRoot) -> None:
    with pytest.raises(FileNotFoundError):
        root.hash_existing("missing.deb", "sha256")


# -- cleanup_stale ------------------------------------------------------------------


def test_cleanup_stale_only_matching_old_owned_regular(root: WriteRoot, tmp_path: Path) -> None:
    old = time.time() - 7200
    stale = tmp_path / (TEMP_PREFIX + "stale")
    stale.write_bytes(b"")
    os.utime(stale, (old, old))
    fresh = tmp_path / (TEMP_PREFIX + "fresh")
    fresh.write_bytes(b"")
    other = tmp_path / "other.deb"
    other.write_bytes(b"")
    os.utime(other, (old, old))
    victim = tmp_path.parent / (tmp_path.name + "-victim3")
    victim.write_bytes(b"keep")
    os.utime(victim, (old, old))
    decoy = tmp_path / (TEMP_PREFIX + "x")
    decoy.symlink_to(victim)
    os.utime(decoy, (old, old), follow_symlinks=False)
    olddir = tmp_path / (TEMP_PREFIX + "dir")
    olddir.mkdir()
    os.utime(olddir, (old, old))

    assert root.cleanup_stale() == 1
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()
    assert decoy.is_symlink()
    assert victim.read_bytes() == b"keep"
    assert olddir.is_dir()


def test_cleanup_stale_skips_foreign_owner(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = time.time() - 7200
    stale = tmp_path / (TEMP_PREFIX + "stale")
    stale.write_bytes(b"")
    os.utime(stale, (old, old))
    euid = os.geteuid()
    monkeypatch.setattr(safeio.os, "geteuid", lambda: euid + 1)
    assert root.cleanup_stale() == 0
    assert stale.exists()


def test_cleanup_stale_max_age(root: WriteRoot, tmp_path: Path) -> None:
    f = tmp_path / (TEMP_PREFIX + "a")
    f.write_bytes(b"")
    old = time.time() - 10
    os.utime(f, (old, old))
    assert root.cleanup_stale(max_age=3600) == 0
    assert root.cleanup_stale(max_age=5) == 1


# -- session_temp_dir ---------------------------------------------------------------


@pytest.fixture
def fresh_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    monkeypatch.setattr(safeio, "_session", None)
    yield base
    if safeio._session is not None:
        safeio._session.close()


def test_session_temp_dir(fresh_session: Path) -> None:
    r = session_temp_dir()
    assert os.path.dirname(r.path) == os.path.realpath(fresh_session)
    assert os.path.basename(r.path).startswith("minideb-mcp-")
    st = os.stat(r.path)
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert st.st_uid == os.geteuid()
    assert r.writable()
    again = session_temp_dir()
    assert again is not r
    assert again.path == r.path
    assert len(list(fresh_session.iterdir())) == 1
    # each call returns a root the caller owns: closing one breaks nothing
    r.close()
    assert again.writable()
    again.close()
    with session_temp_dir() as third:
        assert third.path == r.path
        assert third.writable()
    assert len(list(fresh_session.iterdir())) == 1


def test_session_temp_dir_mkdtemp_failure(
    fresh_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**kw: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(safeio.tempfile, "mkdtemp", fail)
    with pytest.raises(WriteRootError, match="temporary directory"):
        session_temp_dir()
    assert safeio._session is None


def test_session_temp_dir_created_once_across_threads(fresh_session: Path) -> None:
    got: list[WriteRoot] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        got.append(session_temp_dir())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r.path for r in got}) == 1
    assert len({r.fileno() for r in got}) == 8
    assert len(list(fresh_session.iterdir())) == 1
    for r in got:
        r.close()


def test_write_root_error_message_has_no_errno_prefix(tmp_path: Path) -> None:
    with pytest.raises(WriteRootError) as info:
        WriteRoot.open(tmp_path / "missing")
    assert info.value.errno is not None
    assert str(info.value) == f"{tmp_path / 'missing'}: does not exist"


# -- review fixes ------------------------------------------------------------------


def _umask_mode(mask: int, tmp_path: Path) -> int:
    old = os.umask(mask)
    try:
        with WriteRoot.open(tmp_path) as r, r.create_file(f"m{mask:o}.deb") as nf:
            assert stat.S_IMODE(os.fstat(nf.file.fileno()).st_mode) == 0o666 & ~mask
            nf.commit()
    finally:
        os.umask(old)
    return stat.S_IMODE(os.lstat(tmp_path / f"m{mask:o}.deb").st_mode)


@pytest.mark.parametrize("mask", [0o022, 0o077, 0o002, 0o000])
def test_published_mode_follows_umask(tmp_path: Path, mask: int) -> None:
    assert _umask_mode(mask, tmp_path) == 0o666 & ~mask


def test_world_writable_root_without_sticky_refused(tmp_path: Path) -> None:
    d = tmp_path / "shared"
    d.mkdir()
    try:
        os.chmod(d, 0o777)
        with pytest.raises(WriteRootError, match="writable by all users"):
            WriteRoot.open(d)
        os.chmod(d, 0o1777)  # like /tmp: allowed
        WriteRoot.open(d).close()
        os.chmod(d, 0o775)  # group writable: allowed
        WriteRoot.open(d).close()
    finally:
        os.chmod(d, 0o700)


def test_world_writable_subdir_component_refused(root: WriteRoot, tmp_path: Path) -> None:
    d = tmp_path / "shared"
    d.mkdir()
    try:
        os.chmod(d, 0o777)
        with pytest.raises(WriteRootError, match="writable by all users"):
            root.subdir(["shared", "x"])
        assert not (d / "x").exists()
    finally:
        os.chmod(d, 0o700)


def test_post_link_mismatch_leaves_the_other_writers_file(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nf = root.create_file("a.deb")
    nf.file.write(b"x")
    real_stat = os.stat
    swapped = []

    def fake_stat(path: str, *a: Any, **kw: Any) -> os.stat_result:
        if path == "a.deb" and not swapped:
            # another writer replaces the name right after our link
            swapped.append(True)
            os.unlink(path, dir_fd=kw.get("dir_fd"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kw.get("dir_fd"))
            os.write(fd, b"theirs")
            os.close(fd)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(safeio.os, "stat", fake_stat)
    with pytest.raises(WriteRootError, match="replaced while publishing"):
        nf.commit()
    monkeypatch.undo()
    # their file is untouched, our temporary link is gone
    assert [p.name for p in tmp_path.iterdir()] == ["a.deb"]
    assert (tmp_path / "a.deb").read_bytes() == b"theirs"


@pytest.mark.parametrize("failing", ["unlink", "fsync"])
def test_post_link_failure_leaves_nothing_published(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    nf = root.create_file("a.deb")
    nf.file.write(b"x")
    temp = nf.temp_name
    real_unlink, real_fsync = os.unlink, os.fsync
    fsyncs: list[int] = []
    failed: list[bool] = []

    def fake_unlink(path: str, *a: Any, **kw: Any) -> None:
        if failing == "unlink" and path == temp and not failed:
            failed.append(True)  # fail only the first unlink of the temporary name
            raise OSError(errno.EIO, "boom")
        real_unlink(path, *a, **kw)

    def fake_fsync(fd: int) -> None:
        fsyncs.append(fd)
        # the first fsync is the file's, the second the directory's
        if failing == "fsync" and len(fsyncs) == 2:
            raise OSError(errno.EIO, "boom")
        real_fsync(fd)

    monkeypatch.setattr(safeio.os, "unlink", fake_unlink)
    monkeypatch.setattr(safeio.os, "fsync", fake_fsync)
    with pytest.raises(OSError, match="boom"):
        nf.commit()
    monkeypatch.undo()
    assert not (tmp_path / "a.deb").exists()
    assert list(tmp_path.iterdir()) == []


def test_commit_invalid_name_aborts(root: WriteRoot, tmp_path: Path) -> None:
    nf = root.create_file("a.deb")
    nf.file.write(b"x")
    with pytest.raises(UnsafeNameError):
        nf.commit("../escape.deb")
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path.parent / "escape.deb").exists()
    with pytest.raises(WriteRootError):
        nf.commit()


def test_commit_existing_message_has_no_temp_name(root: WriteRoot, tmp_path: Path) -> None:
    (tmp_path / "a.deb").write_bytes(b"original")
    nf = root.create_file("a.deb")
    temp = nf.temp_name
    with pytest.raises(FileExistsError) as info:
        nf.commit()
    assert info.value.errno == errno.EEXIST
    assert str(info.value).endswith(f"{os.path.join(root.path, 'a.deb')}: already exists")
    assert temp not in str(info.value)
    assert info.value.filename is None


@pytest.mark.parametrize("age", [-1, -0.5, float("nan"), float("inf"), "10", True, None])
def test_cleanup_stale_bad_max_age(root: WriteRoot, age: Any) -> None:
    with pytest.raises(ValueError, match="max_age"):
        root.cleanup_stale(age)


def test_cleanup_stale_zero_removes_all_own(root: WriteRoot, tmp_path: Path) -> None:
    f = tmp_path / (TEMP_PREFIX + "now")
    f.write_bytes(b"")
    past = time.time() - 1
    os.utime(f, (past, past))
    assert root.cleanup_stale(0) == 1


def test_negative_size_hint_rejected(root: WriteRoot, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="size_hint"):
        root.create_file("a.deb", size_hint=-1)
    assert list(tmp_path.iterdir()) == []


def test_hash_existing_open_flags(
    root: WriteRoot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "f.deb").write_bytes(b"x")
    seen: list[int] = []
    real_open = os.open

    def spy(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        seen.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safeio.os, "open", spy)
    root.hash_existing("f.deb", "sha256")
    assert seen
    want = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0)
    assert seen[0] & want == want
