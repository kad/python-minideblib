# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Local inspection tools (inspect_deb, inspect_dsc), called directly with a ToolCall."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from minideblib.mcp import tools_local
from minideblib.mcp.protocol import Tool, ToolCall, ToolError, _encode, validate
from minideblib.mcp.startup import ServerConfig
from minideblib.mcp.tools import build_tools
from minideblib.repo.fetch import CancelToken
from tests.fixtures.debs import make_deb

SECRET = "TOPSECRET-c0ffee-token"
CONTROL = {
    "Package": "demo",
    "Version": "1.0-1",
    "Architecture": "all",
    "Maintainer": "Debian Developer <dev@example.org>",
    "Description": "demo package\n long text",
}
FILES = {
    "usr/bin/demo": b"#!/bin/sh\necho hi\n",
    "usr/share/doc/demo/README": "café readme\n".encode(),
    "usr/share/demo/blob.bin": b"\x00\x01\x02binary",
    "usr/share/demo/latin1.txt": b"caf\xe9\n",
}


def _tools() -> dict[str, Tool]:
    return {t.name: t for t in build_tools(ServerConfig())}


def _call(name: str, token: CancelToken | None = None, **args: Any) -> dict[str, Any]:
    tool = _tools()[name]
    validate(args, tool.input_schema)
    result = tool.handler(ToolCall(args, token or CancelToken(), time.monotonic() + 30))
    assert isinstance(result, dict)
    json.dumps(result, allow_nan=False)
    return result


def _err(name: str, **args: Any) -> str:
    with pytest.raises(ToolError) as info:
        _call(name, **args)
    return str(info.value)


@pytest.fixture
def deb_path(tmp_path: Path) -> Path:
    p = tmp_path / "demo_1.0-1_all.deb"
    p.write_bytes(make_deb(CONTROL, FILES))
    return p


def _dsc_text(files: dict[str, bytes], source: str = "demo") -> str:
    sha = "".join(f" {hashlib.sha256(c).hexdigest()} {len(c)} {n}\n" for n, c in files.items())
    md5 = "".join(f" {hashlib.md5(c).hexdigest()} {len(c)} {n}\n" for n, c in files.items())
    return (
        "Format: 3.0 (quilt)\n"
        f"Source: {source}\n"
        "Binary: demo\n"
        "Architecture: any\n"
        "Version: 1.0-1\n"
        "Maintainer: Debian Developer <dev@example.org>\n"
        "Build-Depends: debhelper-compat (= 13)\n"
        f"Checksums-Sha256:\n{sha}"
        f"Files:\n{md5}"
    )


SRC = {"demo_1.0.orig.tar.gz": b"orig-data", "demo_1.0-1.debian.tar.xz": b"debian-data"}


@pytest.fixture
def dsc_path(tmp_path: Path) -> Path:
    for name, content in SRC.items():
        (tmp_path / name).write_bytes(content)
    p = tmp_path / "demo_1.0-1.dsc"
    p.write_text(_dsc_text(SRC))
    return p


# -- registration -------------------------------------------------------------


def test_registered_read_only_local() -> None:
    tools = _tools()
    for name in ("inspect_deb", "inspect_dsc"):
        info = tools[name].describe()
        ann: Any = info["annotations"]
        assert ann["readOnlyHint"] is True
        assert ann["openWorldHint"] is False
        assert ann["destructiveHint"] is False


# -- inspect_deb ----------------------------------------------------------------


def test_inspect_deb_entry_limit(deb_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_local, "MAX_ENTRIES", 2)
    with pytest.raises(ToolError) as info:
        _call("inspect_deb", path=str(deb_path))
    assert str(info.value) == (
        "the data archive has more than 2 entries; call again with list_files false "
        "(control fields and read_file still work)"
    )
    out = _call("inspect_deb", path=str(deb_path), list_files=False)
    assert out["control"]


def test_deb_control_and_files(deb_path: Path) -> None:
    out = _call("inspect_deb", path=str(deb_path))
    assert [k for k, _ in out["control"]][:3] == ["Package", "Version", "Architecture"]
    assert ["Package", "demo"] in out["control"]
    assert out["format_version"] == "2.0"
    assert out["size"] == deb_path.stat().st_size
    files = {f["path"]: f for f in out["files"]}
    assert files["usr/bin/demo"]["type"] == "f"
    assert files["usr/bin/demo"]["size"] == len(FILES["usr/bin/demo"])
    assert files["usr/bin/demo"]["md5sum"] == hashlib.md5(FILES["usr/bin/demo"]).hexdigest()
    assert out["md5sums_count"] == len(FILES)
    assert "md5sums" in out["control_files"]
    assert out["total"] == out["returned"] == len(out["files"])
    assert out["truncated"] is False


def test_deb_file_paging(deb_path: Path) -> None:
    first = _call("inspect_deb", path=str(deb_path), limit=2)
    assert first["returned"] == 2
    assert first["next_offset"] == 2
    second = _call("inspect_deb", path=str(deb_path), limit=500, offset=first["next_offset"])
    assert second["next_offset"] is None
    names = [f["path"] for f in first["files"] + second["files"]]
    assert len(names) == first["total"] == len(set(names))


def test_deb_no_file_list(deb_path: Path) -> None:
    out = _call("inspect_deb", path=str(deb_path), list_files=False)
    assert "files" not in out
    assert out["control"]


def test_deb_read_text_member(deb_path: Path) -> None:
    out = _call("inspect_deb", path=str(deb_path), read_file="./usr/share/doc/demo/README")
    assert out["member"]["path"] == "usr/share/doc/demo/README"
    assert out["member"]["content"] == "café readme\n"
    assert out["member"]["truncated"] is False


def test_deb_read_truncates_at_max_bytes(deb_path: Path) -> None:
    out = _call("inspect_deb", path=str(deb_path), read_file="usr/bin/demo", max_bytes=5)
    assert out["member"]["content"] == "#!/bi"
    assert out["member"]["truncated"] is True
    # never splits a multi-byte character
    out = _call(
        "inspect_deb", path=str(deb_path), read_file="usr/share/doc/demo/README", max_bytes=4
    )
    assert out["member"]["content"] == "caf"
    assert len(out["member"]["content"].encode()) <= 4


def test_deb_read_cleans_content(tmp_path: Path) -> None:
    p = tmp_path / "x.deb"
    p.write_bytes(make_deb(CONTROL, {"etc/motd": b"a\x1b[31mred\x1b[0m\x07b\n"}))
    out = _call("inspect_deb", path=str(p), read_file="etc/motd")
    assert out["member"]["content"] == "aredb\n"


@pytest.mark.parametrize("member", ["usr/share/demo/blob.bin", "usr/share/demo/latin1.txt"])
def test_deb_read_binary_refused(deb_path: Path, member: str) -> None:
    assert _err("inspect_deb", path=str(deb_path), read_file=member) == "binary or non-UTF-8 member"


def test_deb_read_missing_member(deb_path: Path) -> None:
    assert "no regular file" in _err("inspect_deb", path=str(deb_path), read_file="nope")


def test_deb_read_oversize_member_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_local, "MAX_MEMBER", 100)
    p = tmp_path / "big.deb"
    p.write_bytes(make_deb(CONTROL, {"usr/share/big.txt": b"x" * 101}))
    assert "too large" in _err("inspect_deb", path=str(p), read_file="usr/share/big.txt")


def test_deb_unpack_bomb_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_local, "MAX_UNPACKED", 4096)
    p = tmp_path / "bomb.deb"
    p.write_bytes(make_deb(CONTROL, {"usr/share/zeros": b"\0" * 100_000}))
    msg = _err("inspect_deb", path=str(p))
    assert "unpacked size" in msg


def test_deb_read_nonregular_member_refused(tmp_path: Path) -> None:
    import io
    import tarfile

    from tests.fixtures.debs import make_ar, make_tar

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("./usr/lnk")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "target"
        tf.addfile(ti)
    ctrl = make_tar({"control": b"Package: p\nVersion: 1\n"})
    p = tmp_path / "l.deb"
    p.write_bytes(
        make_ar([("debian-binary", b"2.0\n"), ("control.tar", ctrl), ("data.tar", buf.getvalue())])
    )
    out = _call("inspect_deb", path=str(p))
    assert any(f["type"] == "l" and f["link"] == "target" for f in out["files"])
    assert "no regular file" in _err("inspect_deb", path=str(p), read_file="usr/lnk")


def test_deb_max_bytes_bounds_validated(deb_path: Path) -> None:
    tool = _tools()["inspect_deb"]
    for bad in (0, 2**31):
        with pytest.raises(ToolError):
            validate({"path": str(deb_path), "max_bytes": bad}, tool.input_schema)


def test_deb_relative_path(deb_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(deb_path.parent)
    out = _call("inspect_deb", path=deb_path.name)
    assert ["Package", "demo"] in out["control"]


def test_deb_control_text_cleaned(tmp_path: Path) -> None:
    p = tmp_path / "x.deb"
    p.write_bytes(
        make_deb({"Package": "p", "Version": "1", "Description": "a‮b\x1b[31mc"}, {"f": b"x"})
    )
    out = _call("inspect_deb", path=str(p))
    assert ["Description", "abc"] in out["control"]


# -- file access rules -------------------------------------------------------------


def test_nonexistent_vs_unreadable(deb_path: Path, tmp_path: Path) -> None:
    missing = _err("inspect_deb", path=str(tmp_path / "nope.deb"))
    assert "does not exist" in missing
    if os.geteuid() == 0:
        pytest.skip("chmod 000 does not restrict root")
    deb_path.chmod(0)
    try:
        denied = _err("inspect_deb", path=str(deb_path))
    finally:
        deb_path.chmod(0o644)
    assert "permission denied" in denied
    assert denied != missing


def test_symlink_refused(deb_path: Path, tmp_path: Path) -> None:
    link = tmp_path / "link.deb"
    link.symlink_to(deb_path)
    assert "symbolic link" in _err("inspect_deb", path=str(link))
    dlink = tmp_path / "link.dsc"
    dlink.symlink_to(deb_path)
    assert "symbolic link" in _err("inspect_dsc", path=str(dlink))


def test_directory_refused(tmp_path: Path) -> None:
    assert "not a regular file" in _err("inspect_deb", path=str(tmp_path))


def test_fifo_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.deb"
    os.mkfifo(fifo)
    seen: list[str] = []

    def run() -> None:
        seen.append(_err("inspect_deb", path=str(fifo)))
        seen.append(_err("inspect_dsc", path=str(fifo)))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert all("not a regular file" in m for m in seen) and len(seen) == 2


def test_component_not_a_directory(deb_path: Path) -> None:
    msg = _err("inspect_deb", path=str(deb_path / "inner.deb"))
    assert msg == "invalid path: a component is not a directory"


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="no /dev/null")
def test_device_refused() -> None:
    assert "not a regular file" in _err("inspect_deb", path="/dev/null")


def test_size_cap_uses_fstat_of_opened_file(
    deb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools_local, "MAX_FILE_SIZE", 100)
    assert "larger than" in _err("inspect_deb", path=str(deb_path))


def test_open_regular_file_returns_fd(deb_path: Path) -> None:
    fd = tools_local.open_regular_file(str(deb_path), 1 << 20)
    try:
        assert os.read(fd, 8) == b"!<arch>\n"
    finally:
        os.close(fd)
    with pytest.raises(ToolError):
        tools_local.open_regular_file(str(deb_path), 10)
    with pytest.raises(ToolError):
        tools_local.open_regular_file("a\0b", 10)


def test_cancelled_before_read(deb_path: Path) -> None:
    token = CancelToken()
    token.cancel()
    assert _err_token("inspect_deb", token, path=str(deb_path)) == "cancelled"


def _err_token(name: str, token: CancelToken, **args: Any) -> str:
    with pytest.raises(ToolError) as info:
        _call(name, token, **args)
    return str(info.value)


def test_deadline_passed(deb_path: Path) -> None:
    tool = _tools()["inspect_deb"]
    with pytest.raises(ToolError, match="timed out"):
        tool.handler(ToolCall({"path": str(deb_path)}, CancelToken(), time.monotonic() - 1))


# -- generic parse errors ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tool", "message"),
    [
        ("junk.deb", "inspect_deb", "not a valid .deb"),
        ("junk.dsc", "inspect_dsc", "not a valid .dsc"),
    ],
)
def test_garbage_gives_generic_error(tmp_path: Path, name: str, tool: str, message: str) -> None:
    p = tmp_path / name
    p.write_text(f"{SECRET}\nSource: {SECRET}\nVersion: {SECRET}\n!<arch>\n{SECRET}\n")
    msg = _err(tool, path=str(p))
    assert message in msg
    assert SECRET not in msg


def test_truncated_deb_does_not_echo(tmp_path: Path) -> None:
    data = make_deb({"Package": SECRET, "Version": "1"}, {"f": b"x" * 1000})
    p = tmp_path / "t.deb"
    p.write_bytes(data[: len(data) // 2])
    msg = _err("inspect_deb", path=str(p))
    assert SECRET not in msg
    assert "not a valid .deb" in msg


def test_dsc_bad_field_does_not_echo(tmp_path: Path) -> None:
    p = tmp_path / "x.dsc"
    p.write_text(f"Source: s\nVersion: {SECRET}!\nFiles:\n {SECRET}\n")
    msg = _err("inspect_dsc", path=str(p))
    assert SECRET not in msg
    assert "not a valid .dsc" in msg


def test_unexpected_exception_text_not_returned(
    deb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> object:
        raise ValueError(SECRET)

    monkeypatch.setattr(tools_local.DebPackage, "files", boom)
    msg = _err("inspect_deb", path=str(deb_path))
    assert SECRET not in msg


# -- inspect_dsc -----------------------------------------------------------------------


def test_dsc_fields(dsc_path: Path) -> None:
    out = _call("inspect_dsc", path=str(dsc_path))
    assert out["source"] == "demo"
    assert out["version"] == "1.0-1"
    assert out["format"] == "3.0 (quilt)"
    assert out["binaries"] == ["demo"]
    assert out["architectures"] == ["any"]
    assert out["build_depends"] == {"Build-Depends": "debhelper-compat (= 13)"}
    assert out["signed"] is False
    assert out["verification"] is None
    names = {f["name"]: f for f in out["files"]}
    assert set(names) == set(SRC)
    assert names["demo_1.0.orig.tar.gz"]["size"] == len(SRC["demo_1.0.orig.tar.gz"])
    assert (
        names["demo_1.0.orig.tar.gz"]["hashes"]["sha256"]
        == hashlib.sha256(SRC["demo_1.0.orig.tar.gz"]).hexdigest()
    )
    assert ["Source", "demo"] in out["fields"]


def test_dsc_clearsigned_flag(tmp_path: Path) -> None:
    p = tmp_path / "s.dsc"
    p.write_text(
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        + _dsc_text(SRC)
        + "\n-----BEGIN PGP SIGNATURE-----\n\nAAAA\n-----END PGP SIGNATURE-----\n"
    )
    out = _call("inspect_dsc", path=str(p))
    assert out["signed"] is True
    assert out["source"] == "demo"


def test_dsc_text_cleaned(tmp_path: Path) -> None:
    p = tmp_path / "c.dsc"
    p.write_text(_dsc_text(SRC, source="de‮mo"))
    assert _call("inspect_dsc", path=str(p))["source"] == "demo"


def test_dsc_verify_good(dsc_path: Path) -> None:
    out = _call("inspect_dsc", path=str(dsc_path), verify=True)
    ver = out["verification"]
    assert ver["ok"] is True
    assert {f["name"]: f["status"] for f in ver["files"]} == {n: "ok" for n in SRC}


def test_dsc_verify_bad(dsc_path: Path) -> None:
    d = dsc_path.parent
    (d / "demo_1.0.orig.tar.gz").write_bytes(b"orig-dat!")  # same size, other content
    (d / "demo_1.0-1.debian.tar.xz").unlink()
    ver = _call("inspect_dsc", path=str(dsc_path), verify=True)["verification"]
    status = {f["name"]: f for f in ver["files"]}
    assert ver["ok"] is False
    assert status["demo_1.0.orig.tar.gz"]["status"] == "mismatch"
    assert status["demo_1.0-1.debian.tar.xz"]["status"] == "missing"


def test_dsc_verify_symlink_not_followed(dsc_path: Path) -> None:
    d = dsc_path.parent
    real = d / "real"
    real.write_bytes(SRC["demo_1.0.orig.tar.gz"])
    (d / "demo_1.0.orig.tar.gz").unlink()
    (d / "demo_1.0.orig.tar.gz").symlink_to(real)
    ver = _call("inspect_dsc", path=str(dsc_path), verify=True)["verification"]
    assert {f["name"]: f["status"] for f in ver["files"]}["demo_1.0.orig.tar.gz"] == "mismatch"


def test_dsc_verify_checks_cancel_between_files(
    dsc_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = CancelToken()
    tool = _tools()["inspect_dsc"]
    calls: list[str] = []
    real = tools_local.verify_file

    def counting(*a: Any, **k: Any) -> None:
        calls.append(str(a[0]))
        token.cancel()
        real(*a, **k)

    monkeypatch.setattr(tools_local, "verify_file", counting)
    with pytest.raises(ToolError, match="cancelled"):
        tool.handler(
            ToolCall({"path": str(dsc_path), "verify": True}, token, time.monotonic() + 30)
        )
    assert len(calls) == 1


def test_dsc_verify_not_ok_when_the_file_list_is_truncated(tmp_path: Path) -> None:
    files = {f"demo_{i}.tar.gz": b"x" for i in range(tools_local.MAX_ITEMS + 5)}
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    dsc = tmp_path / "demo_1.0-1.dsc"
    dsc.write_text(_dsc_text(files))
    ver = _call("inspect_dsc", path=str(dsc), verify=True)["verification"]
    assert ver["files_checked"] == tools_local.MAX_ITEMS
    assert ver["files_total"] == tools_local.MAX_ITEMS + 5
    assert ver["ok"] is False


def test_dsc_verify_stops_inside_a_large_file_on_cancel(
    dsc_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = CancelToken()
    tool = _tools()["inspect_dsc"]
    reads: list[int] = []
    real = tools_local.verify_file

    def spying(*a: Any, progress: Any = None, **k: Any) -> None:
        def tick() -> None:
            reads.append(1)
            token.cancel()
            progress()

        real(*a, progress=tick, **k)

    monkeypatch.setattr(tools_local, "verify_file", spying)
    with pytest.raises(ToolError, match="cancelled"):
        tool.handler(
            ToolCall({"path": str(dsc_path), "verify": True}, token, time.monotonic() + 30)
        )
    assert len(reads) == 1


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/b", "..", ".", "", "a\\b", "a\0b"])
def test_plain_name_rejects_paths(name: str) -> None:
    assert tools_local._plain_name(name) is False


def test_dsc_verify_never_reads_outside_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"x")
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    class F:
        size = 1

        def __init__(self, name: str) -> None:
            self.name = name

        def hashes(self) -> dict[str, str]:
            return {"sha256": hashlib.sha256(b"x").hexdigest()}

    class D:
        files = [F("../secret"), F(str(secret))]

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("verify_file must not be called for unsafe names")

    monkeypatch.setattr(tools_local, "verify_file", boom)
    ver = tools_local.LocalTools(ServerConfig())._verify(
        ToolCall({}, CancelToken(), time.monotonic() + 30),
        cast(Any, D()),
        str(pkg),  # type: ignore[arg-type]
    )
    assert [f["status"] for f in cast(list[dict[str, object]], ver["files"])] == [
        "mismatch",
        "mismatch",
    ]
    assert ver["ok"] is False


def test_dsc_relative_path(dsc_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(dsc_path.parent)
    out = _call("inspect_dsc", path=dsc_path.name, verify=True)
    assert out["verification"]["ok"] is True


# -- response budget ---------------------------------------------------------------


def _wire_size(value: dict[str, Any]) -> int:
    """Bytes of the tools/call response line as the protocol layer encodes it."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    result = {"content": [{"type": "text", "text": text}], "structuredContent": value}
    return len(_encode({"jsonrpc": "2.0", "id": 1, "result": result}))


LIMIT_BYTES = ServerConfig().max_response_bytes


def test_budget_newline_heavy_member(tmp_path: Path) -> None:
    p = tmp_path / "n.deb"
    p.write_bytes(make_deb(CONTROL, {"etc/nl": b"\n" * 70000, **FILES}))
    out = _call("inspect_deb", path=str(p), read_file="etc/nl", max_bytes=65536, limit=500)
    assert _wire_size(out) < LIMIT_BYTES
    assert out["member"]["truncated"] is True
    assert set(out["member"]["content"]) == {"\n"}


def test_budget_cjk_member(tmp_path: Path) -> None:
    p = tmp_path / "c.deb"
    p.write_bytes(make_deb(CONTROL, {"etc/cjk": ("漢字" * 40000).encode(), **FILES}))
    out = _call("inspect_deb", path=str(p), read_file="etc/cjk", max_bytes=65536, limit=500)
    assert _wire_size(out) < LIMIT_BYTES
    content = out["member"]["content"]
    assert content and set(content) <= {"漢", "字"}
    assert out["member"]["truncated"] is True


def test_budget_big_control_fields(tmp_path: Path) -> None:
    control = {"Package": "p", "Version": "1"}
    control.update({f"X-F{i}": 'q"' * 1900 for i in range(60)})
    files = {f"usr/share/f{i}": b"x" for i in range(600)}
    files["etc/nl"] = b"\n" * 70000
    p = tmp_path / "f.deb"
    p.write_bytes(make_deb(control, files))
    out = _call("inspect_deb", path=str(p), read_file="etc/nl", limit=500)
    assert _wire_size(out) < LIMIT_BYTES
    assert out["control"][-1][0] == "…"
    assert out["returned"] >= 1


def test_budget_dsc(tmp_path: Path) -> None:
    names = {f"demo_{i}.tar.gz": b"x" for i in range(500)}
    long_value = 'q"' * 1900
    text = _dsc_text(names) + "".join(f"X-Long{i}: {long_value}\n" for i in range(60))
    p = tmp_path / "b.dsc"
    p.write_text(text)
    out = _call("inspect_dsc", path=str(p))
    assert _wire_size(out) < LIMIT_BYTES


# -- markers survive the final sweep ---------------------------------------------


def test_deb_control_marker_count(tmp_path: Path) -> None:
    control = {"Package": "p", "Version": "1"}
    control.update({f"F{i}": "v" for i in range(99_998)})
    p = tmp_path / "m.deb"
    p.write_bytes(make_deb(control, {"f": b"x"}))
    out = _call("inspect_deb", path=str(p))
    assert len(out["control"]) == 501
    assert out["control"][-1] == ["…", "[99500 more fields]"]


def test_dsc_fields_marker_count(tmp_path: Path) -> None:
    text = _dsc_text(SRC) + "".join(f"F{i}: v\n" for i in range(99_000))
    p = tmp_path / "m.dsc"
    p.write_text(text)
    out = _call("inspect_dsc", path=str(p))
    last = out["fields"][-1]
    assert last[0] == "…"
    assert last[1].startswith("[") and last[1].endswith(" more fields]")
    shown = len(out["fields"]) - 1
    assert int(last[1][1:].split()[0]) == out["fields_total"] - shown


# -- details ---------------------------------------------------------------------------


def test_max_bytes_schema_matches_effective_limit() -> None:
    tool = _tools()["inspect_deb"]
    props: Any = tool.input_schema["properties"]
    assert props["max_bytes"]["maximum"] == ServerConfig().max_response_bytes // 3
    assert str(props["max_bytes"]["maximum"]) in props["max_bytes"]["description"]
    assert "skipped" in _tools()["inspect_dsc"].description
    path: Any = props["path"]
    assert "itself must not be a symbolic link" in path["description"]


def test_verify_detail_has_no_digest(dsc_path: Path) -> None:
    (dsc_path.parent / "demo_1.0.orig.tar.gz").write_bytes(b"orig-dat!")
    ver = _call("inspect_dsc", path=str(dsc_path), verify=True)["verification"]
    detail = {f["name"]: f.get("detail") for f in ver["files"]}["demo_1.0.orig.tar.gz"]
    assert detail == "md5 differs"
    assert hashlib.sha256(b"orig-dat!").hexdigest() not in json.dumps(ver)
    (dsc_path.parent / "demo_1.0.orig.tar.gz").write_bytes(b"short")
    ver = _call("inspect_dsc", path=str(dsc_path), verify=True)["verification"]
    assert {f["name"]: f.get("detail") for f in ver["files"]}["demo_1.0.orig.tar.gz"] == (
        "size differs"
    )


def test_unexpected_failure_is_logged(
    deb_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*a: object, **k: object) -> object:
        raise ValueError(SECRET)

    monkeypatch.setattr(tools_local.DebPackage, "files", boom)
    with caplog.at_level(logging.WARNING, logger="minideblib.mcp.tools_local"):
        msg = _err("inspect_deb", path=str(deb_path))
    assert SECRET not in msg
    assert any(r.levelno == logging.WARNING and r.exc_info for r in caplog.records)


def test_read_file_without_listing(deb_path: Path) -> None:
    out = _call("inspect_deb", path=str(deb_path), list_files=False, read_file="usr/bin/demo")
    assert "files" not in out
    assert out["member"]["content"].startswith("#!/bin/sh")
    assert "no regular file" in _err(
        "inspect_deb", path=str(deb_path), list_files=False, read_file="nope"
    )


def test_too_many_entries(deb_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_local, "MAX_ENTRIES", 2)
    assert "more than 2 entries; call again with list_files false" in _err(
        "inspect_deb", path=str(deb_path)
    )
