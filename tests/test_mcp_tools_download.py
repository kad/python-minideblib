# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The download tool of the MCP server, against file:// and local HTTP repositories."""

from __future__ import annotations

import argparse
import errno
import functools
import hashlib
import http.server
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from minideblib.mcp import safeio
from minideblib.mcp.protocol import Tool, ToolCall, ToolError, validate
from minideblib.mcp.safeio import TEMP_PREFIX, WriteRoot
from minideblib.mcp.startup import RepositorySpec, ServerConfig, build_config
from minideblib.mcp.tools import build_tools
from minideblib.repo.fetch import CancelToken
from tests.fixtures.repo import (
    DSC_NAME,
    SRC_FILES,
    clearsign,
    make_repo,
    multiarch_deb,
    multiarch_packages,
    sample_source_files,
)
from tests.mcp_client import start_in_process

SUITE = "stable"
ARCHES = ("amd64", "arm64", "all")
TOOL = "download_package"
TOOL_TAR = "sample-pkg_1.0-1.debian.tar.xz"
DEB_AMD64 = "sample-tool_1.1-1_amd64.deb"
DEB_ARM64 = "sample-tool_1.1-1_arm64.deb"
DEB_ALL = "sample-data_1.1-1_all.deb"


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: the real environment's proxy settings never reach the tools."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name == "REQUEST_METHOD":
            monkeypatch.delenv(name, raising=False)


# -- fixtures -----------------------------------------------------------------------------


def _hex(algo: str, data: bytes) -> str:
    return hashlib.new(algo, data, usedforsecurity=False).hexdigest()


def _by_arch() -> tuple[dict[str, Any], dict[str, bytes]]:
    by_arch: dict[str, Any] = {}
    pool: dict[str, bytes] = {}
    for arch in ("amd64", "arm64"):
        paras, debs = multiarch_packages(arch, "main")
        by_arch[arch] = paras
        pool.update(debs)
    by_arch["all"] = [p for p in by_arch["amd64"] if p["Architecture"] == "all"]
    return by_arch, pool


def _repo(
    root: Path,
    by_arch: dict[str, Any] | None = None,
    pool: dict[str, bytes] | None = None,
    **kw: Any,
) -> str:
    """A multi-architecture repository with real pool files; its file:// URL."""
    default_by_arch, default_pool = _by_arch()
    kw.setdefault("dsc", True)
    url = make_repo(
        root,
        SUITE,
        comps=["main"],
        arches=ARCHES,
        packages_by_arch=by_arch if by_arch is not None else default_by_arch,
        **kw,
    )
    for rel, data in (pool if pool is not None else default_pool).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return url


def _ident(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_dev, info.st_ino


def _config(url: str, dest: Path, **kw: Any) -> ServerConfig:
    kw.setdefault("download_dir", str(dest))
    kw.setdefault("download_dir_id", _ident(dest))
    return ServerConfig(repositories=(RepositorySpec("repo1", url),), **kw)


def _tool(config: ServerConfig) -> Tool:
    return {t.name: t for t in build_tools(config)}[TOOL]


def _call(tool: Tool, token: CancelToken | None = None, **args: Any) -> dict[str, Any]:
    validate(args, tool.input_schema)
    result = tool.handler(ToolCall(args, token or CancelToken(), time.monotonic() + 30))
    assert isinstance(result, dict)
    json.dumps(result, allow_nan=False)
    return result


def _listing(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, Path]:
    root = tmp_path / "repo"
    return _repo(root), root


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


@pytest.fixture
def tool(repo: tuple[str, Path], dest: Path) -> Tool:
    return _tool(_config(repo[0], dest))


# -- local HTTP server -------------------------------------------------------------------


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the repository; a hook for a path suffix runs halfway through the body."""

    hooks: dict[str, Callable[[], None]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        hook = next((h for s, h in self.hooks.items() if self.path.endswith(s)), None)
        if hook is None:
            super().do_GET()
            return
        data = Path(self.translate_path(self.path)).read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        half = len(data) // 2
        self.wfile.write(data[:half])
        self.wfile.flush()
        hook()
        self.wfile.write(data[half:])
        self.wfile.flush()


@pytest.fixture
def http_repo(repo: tuple[str, Path]) -> Iterator[tuple[str, dict[str, Callable[[], None]]]]:
    hooks: dict[str, Callable[[], None]] = {}
    handler = type("Handler", (_Handler,), {"hooks": hooks})
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(handler, directory=str(repo[1]))
    )
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hooks
    finally:
        srv.shutdown()
        srv.server_close()


# -- registry --------------------------------------------------------------------------------


def test_registered_with_write_annotations() -> None:
    config = ServerConfig(download_timeout=321.0)
    tool = _tool(config)
    annotations = tool.describe()["annotations"]
    assert isinstance(annotations, dict)
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is True
    assert annotations["openWorldHint"] is True
    assert "untrusted data" in tool.description
    assert tool.timeout == 321.0
    assert tool.input_schema["additionalProperties"] is False
    required = tool.input_schema["required"]
    assert isinstance(required, list) and set(required) == {"suite", "name"}


def test_no_download_hides_the_tool() -> None:
    config = ServerConfig(allow_download=False)
    assert TOOL not in {t.name for t in build_tools(config)}
    proc = start_in_process(build_tools(config))
    try:
        proc.client.initialize()
        listed = proc.client.request("tools/list", {})["result"]["tools"]
        names = {t["name"] for t in listed}
        assert TOOL not in names and "search_packages" in names
    finally:
        proc.stop()
    proc = start_in_process(build_tools(ServerConfig()))
    try:
        proc.client.initialize()
        listed = proc.client.request("tools/list", {})["result"]["tools"]
        assert TOOL in {t["name"] for t in listed}
    finally:
        proc.stop()


# -- binary packages ----------------------------------------------------------------------


def test_binary_download_file_url(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64"])
    data = multiarch_deb("sample-tool", "1.1-1", "amd64")
    assert (dest / DEB_AMD64).read_bytes() == data
    assert _listing(dest) == [DEB_AMD64]
    assert r["repository"] == "repo1"
    assert r["destination"] == "base" and r["temp_directory_used"] is False
    assert r["root"] == os.path.realpath(dest)
    assert r["complete"] is True
    (f,) = r["files"]
    assert f["status"] == "downloaded"
    assert f["name"] == DEB_AMD64 and f["path"] == DEB_AMD64
    assert f["absolute_path"] == os.path.join(os.path.realpath(dest), DEB_AMD64)
    assert f["size"] == len(data)
    assert f["hashes"]["sha256"] == _hex("sha256", data)
    assert f["hashes"]["sha512"] == _hex("sha512", data)
    assert f["architecture"] == "amd64" and f["version"] == "1.1-1"
    assert "@" not in f["url"]


def test_binary_download_over_http(
    http_repo: tuple[str, dict[str, Callable[[], None]]], dest: Path
) -> None:
    tool = _tool(_config(http_repo[0], dest))
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["arm64"])
    assert r["files"][0]["status"] == "downloaded"
    assert (dest / DEB_ARM64).read_bytes() == multiarch_deb("sample-tool", "1.1-1", "arm64")
    assert r["files"][0]["url"].startswith("http://127.0.0.1:")


def test_architecture_all_single_file(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-data")
    assert [f["name"] for f in r["files"]] == [DEB_ALL]
    assert r["files"][0]["architecture"] == "all"
    assert _listing(dest) == [DEB_ALL]


def test_several_candidates_need_architectures(tool: Tool, dest: Path) -> None:
    with pytest.raises(ToolError, match="amd64.*arm64") as info:
        _call(tool, suite=SUITE, name="sample-tool")
    assert "architectures" in str(info.value)
    assert _listing(dest) == []


def test_explicit_architectures_each_reported(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64", "s390x"])
    assert [(f["architecture"], f["status"]) for f in r["files"]] == [
        ("amd64", "downloaded"),
        ("arm64", "downloaded"),
    ]
    assert r["not_found_architectures"] == ["s390x"]
    assert _listing(dest) == [DEB_AMD64, DEB_ARM64]


def test_version_selection(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-tool", version="1.0-1", architectures=["amd64"])
    assert r["files"][0]["name"] == "sample-tool_1.0-1_amd64.deb"
    with pytest.raises(ToolError, match="not found"):
        _call(tool, suite=SUITE, name="sample-tool", version="9.9-9", architectures=["amd64"])
    with pytest.raises(ToolError, match="not found"):
        _call(tool, suite=SUITE, name="no-such-package")


def test_already_present_is_idempotent(tool: Tool, dest: Path) -> None:
    _call(tool, suite=SUITE, name="sample-data")
    before = os.stat(dest / DEB_ALL)
    r = _call(tool, suite=SUITE, name="sample-data")
    (f,) = r["files"]
    assert f["status"] == "already_present"
    assert f["hashes"]["sha512"] == _hex("sha512", multiarch_deb("sample-data", "1.1-1", "all"))
    after = os.stat(dest / DEB_ALL)
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert r["complete"] is True


def test_existing_different_file_untouched(tool: Tool, dest: Path) -> None:
    (dest / DEB_ALL).write_bytes(b"mine")
    with pytest.raises(ToolError, match="different"):
        _call(tool, suite=SUITE, name="sample-data")
    assert (dest / DEB_ALL).read_bytes() == b"mine"
    assert _listing(dest) == [DEB_ALL]


def test_existing_different_file_among_several(tool: Tool, dest: Path) -> None:
    (dest / DEB_ARM64).write_bytes(b"mine")
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    status = {f["architecture"]: f for f in r["files"]}
    assert status["amd64"]["status"] == "downloaded"
    assert status["arm64"]["status"] == "error"
    assert "different" in status["arm64"]["message"]
    assert (dest / DEB_ARM64).read_bytes() == b"mine"
    assert r["complete"] is False


def test_hash_mismatch_leaves_nothing(repo: tuple[str, Path], dest: Path) -> None:
    deb = repo[1] / "pool/main/s/sample-tool" / DEB_AMD64
    data = bytearray(deb.read_bytes())
    data[-1] ^= 0xFF
    deb.write_bytes(bytes(data))
    with pytest.raises(ToolError, match="sha512|mismatch|!="):
        _call(
            _tool(_config(repo[0], dest)), suite=SUITE, name="sample-tool", architectures=["amd64"]
        )
    assert _listing(dest) == []


def test_no_verify_does_not_weaken_downloads(repo: tuple[str, Path], dest: Path) -> None:
    deb = repo[1] / "pool/main/s/sample-data" / DEB_ALL
    deb.write_bytes(b"x" * len(deb.read_bytes()))
    tool = _tool(_config(repo[0], dest, verify=False))
    with pytest.raises(ToolError):
        _call(tool, suite=SUITE, name="sample-data")
    assert _listing(dest) == []


def _weak_only(paras: list[dict[str, str]]) -> list[dict[str, str]]:
    out = []
    for p in paras:
        q = {k: v for k, v in p.items() if k not in ("SHA256", "SHA512")}
        q["MD5sum"] = "0" * 32
        out.append(q)
    return out


def test_md5_only_index_refused(tmp_path: Path, dest: Path) -> None:
    by_arch, pool = _by_arch()
    by_arch = {a: _weak_only(p) for a, p in by_arch.items()}
    url = _repo(tmp_path / "weak", by_arch, pool)
    with pytest.raises(ToolError, match=f"{DEB_ALL}.*SHA256"):
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-data")
    assert _listing(dest) == []


def test_missing_size_refused(tmp_path: Path, dest: Path) -> None:
    by_arch, pool = _by_arch()
    for paras in by_arch.values():
        for p in paras:
            p.pop("Size", None)
    url = _repo(tmp_path / "nosize", by_arch, pool)
    with pytest.raises(ToolError, match=f"{DEB_ALL}.*size"):
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-data")


def test_size_cap(repo: tuple[str, Path], dest: Path) -> None:
    tool = _tool(_config(repo[0], dest, max_download_bytes=100))
    with pytest.raises(ToolError, match="100"):
        _call(tool, suite=SUITE, name="sample-data")
    assert _listing(dest) == []


def test_size_cap_is_the_sum_of_the_set(repo: tuple[str, Path], dest: Path) -> None:
    sizes = [len(multiarch_deb("sample-tool", "1.1-1", a)) for a in ("amd64", "arm64")]
    tool = _tool(_config(repo[0], dest, max_download_bytes=max(sizes) + 1))
    with pytest.raises(ToolError, match="limit"):
        _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    assert _listing(dest) == []


def test_disk_space_check(tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WriteRoot, "free_bytes", lambda self: 10)
    with pytest.raises(ToolError, match="free space"):
        _call(tool, suite=SUITE, name="sample-data")
    assert _listing(dest) == []


def test_hostile_filename_skipped(tmp_path: Path, dest: Path) -> None:
    by_arch, pool = _by_arch()
    for p in by_arch["arm64"]:
        if p["Package"] == "sample-tool" and p["Version"] == "1.1-1":
            p["Filename"] = "pool/main/s/sample-tool/.bashrc"
    # the hidden pool file holds the right bytes, so only the name is hostile
    pool["pool/main/s/sample-tool/.bashrc"] = multiarch_deb("sample-tool", "1.1-1", "arm64")
    url = _repo(tmp_path / "hostile", by_arch, pool)
    tool = _tool(_config(url, dest))
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    status = {f["architecture"]: f for f in r["files"]}
    assert status["amd64"]["status"] == "downloaded"
    assert status["arm64"]["status"] == "error"
    assert "unsafe file name" in status["arm64"]["message"]
    assert _listing(dest) == [DEB_AMD64]
    with pytest.raises(ToolError, match="unsafe file name"):
        _call(tool, suite=SUITE, name="sample-tool", architectures=["arm64"])


def test_final_name_symlink_never_followed(tool: Tool, tmp_path: Path, dest: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"precious")
    (dest / DEB_ALL).symlink_to(target)
    with pytest.raises(ToolError, match="not a regular file"):
        _call(tool, suite=SUITE, name="sample-data")
    assert target.read_bytes() == b"precious"
    assert (dest / DEB_ALL).is_symlink()
    assert _listing(dest) == [DEB_ALL]
    # a dangling one is not followed either
    (dest / DEB_ALL).unlink()
    (dest / DEB_ALL).symlink_to(tmp_path / "missing.deb")
    with pytest.raises(ToolError):
        _call(tool, suite=SUITE, name="sample-data")
    assert not (tmp_path / "missing.deb").exists()


# -- subdirectories and write roots ----------------------------------------------------


def test_subdirectory(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-data", subdirectory="debs/all")
    assert (dest / "debs/all" / DEB_ALL).is_file()
    assert r["files"][0]["path"] == f"debs/all/{DEB_ALL}"
    assert r["directory"] == os.path.join(os.path.realpath(dest), "debs", "all")
    assert r["subdirectory"] == "debs/all"


@pytest.mark.parametrize("sub", ["..", "../x", "/tmp", "a/../b", "a//b", ".hidden", "a/./b", "-x"])
def test_subdirectory_traversal_refused(tool: Tool, dest: Path, sub: str) -> None:
    with pytest.raises(ToolError, match="subdirectory"):
        _call(tool, suite=SUITE, name="sample-data", subdirectory=sub)
    assert _listing(dest) == []


def test_subdirectory_symlink_refused(tool: Tool, tmp_path: Path, dest: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (dest / "evil").symlink_to(elsewhere)
    with pytest.raises(ToolError, match="symlink"):
        _call(tool, suite=SUITE, name="sample-data", subdirectory="evil")
    with pytest.raises(ToolError, match="symlink"):
        _call(tool, suite=SUITE, name="sample-data", subdirectory="evil/deeper")
    assert list(elsewhere.iterdir()) == []


def test_download_dir_replaced_after_start(repo: tuple[str, Path], tmp_path: Path) -> None:
    d = tmp_path / "dl"
    d.mkdir()
    config = build_config(argparse.Namespace(url=[repo[0]], download_dir=str(d)))
    tool = _tool(config)
    d.rename(tmp_path / "dl.old")
    d.mkdir()
    with pytest.raises(ToolError, match="no longer"):
        _call(tool, suite=SUITE, name="sample-data")
    assert _listing(d) == [] and _listing(tmp_path / "dl.old") == []


def test_default_base_is_the_working_directory(
    repo: tuple[str, Path], dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(dest)
    tool = _tool(ServerConfig(repositories=(RepositorySpec("repo1", repo[0]),)))
    r = _call(tool, suite=SUITE, name="sample-data")
    assert r["root"] == os.path.realpath(dest)
    assert _listing(dest) == [DEB_ALL]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_base_refused(tool: Tool, dest: Path) -> None:
    dest.chmod(0o500)
    try:
        with pytest.raises(ToolError, match="not writable"):
            _call(tool, suite=SUITE, name="sample-data")
    finally:
        dest.chmod(0o700)
    assert _listing(dest) == []


def test_stale_temp_files_cleaned(tool: Tool, dest: Path) -> None:
    old = dest / f"{TEMP_PREFIX}0123456789abcdef"
    fresh = dest / f"{TEMP_PREFIX}fedcba9876543210"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    r = _call(tool, suite=SUITE, name="sample-data")
    assert not old.exists() and fresh.exists()
    assert r["stale_temp_files_removed"] == 1


@pytest.fixture
def session_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    parent = tmp_path / "tmp"
    parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(parent))
    monkeypatch.setattr(safeio, "_session", None)
    try:
        yield parent
    finally:
        session = safeio._session
        if session is not None:
            session.close()


def test_destination_temp(tool: Tool, dest: Path, session_tmp: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-data", destination="temp")
    (made,) = list(session_tmp.iterdir())
    assert stat.S_IMODE(os.stat(made).st_mode) == 0o700
    assert (made / DEB_ALL).is_file()
    assert r["destination"] == "temp" and r["temp_directory_used"] is True
    assert r["root"] == os.path.realpath(made)
    assert "not removed" in r["note"]
    assert r["files"][0]["absolute_path"] == os.path.join(os.path.realpath(made), DEB_ALL)
    assert _listing(dest) == []


# -- cancellation ------------------------------------------------------------------------


def test_cancelled_before_start(tool: Tool, dest: Path) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(ToolError, match="cancelled"):
        _call(tool, token, suite=SUITE, name="sample-data")
    assert _listing(dest) == []


def test_cancel_mid_download_leaves_nothing(
    http_repo: tuple[str, dict[str, Callable[[], None]]], dest: Path
) -> None:
    url, hooks = http_repo
    token = CancelToken()
    hooks[DEB_ALL] = token.cancel
    with pytest.raises(ToolError, match="cancelled"):
        _call(_tool(_config(url, dest)), token, suite=SUITE, name="sample-data")
    assert _listing(dest) == []


def test_cancel_mid_source_set_leaves_nothing(
    http_repo: tuple[str, dict[str, Callable[[], None]]], dest: Path
) -> None:
    url, hooks = http_repo
    token = CancelToken()
    hooks[TOOL_TAR] = token.cancel
    with pytest.raises(ToolError, match="cancelled"):
        _call(_tool(_config(url, dest)), token, suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def test_download_deadline(repo: tuple[str, Path], dest: Path) -> None:
    tool = _tool(_config(repo[0], dest))
    args: dict[str, object] = {"suite": SUITE, "name": "sample-data"}
    with pytest.raises(ToolError, match="timed out"):
        tool.handler(ToolCall(args, CancelToken(), time.monotonic() - 1))
    assert _listing(dest) == []


# -- source packages ----------------------------------------------------------------------


def test_source_set(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    files = sample_source_files()
    assert [f["name"] for f in r["files"]] == list(files)
    assert all(f["status"] == "downloaded" for f in r["files"])
    for name, data in files.items():
        assert (dest / name).read_bytes() == data
    assert r["kind"] == "source" and r["version"] == "1.0-1"
    assert _listing(dest) == sorted(files)
    again = _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    assert {f["status"] for f in again["files"]} == {"already_present"}


def test_source_set_partly_present(tool: Tool, dest: Path) -> None:
    (dest / DSC_NAME).write_bytes(sample_source_files()[DSC_NAME])
    r = _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    assert [f["status"] for f in r["files"]] == ["already_present", "downloaded", "downloaded"]


def test_source_set_all_or_none(repo: tuple[str, Path], dest: Path) -> None:
    third = repo[1] / "pool/main/s/sample-pkg" / TOOL_TAR
    third.write_bytes(b"X" * len(SRC_FILES[TOOL_TAR]))
    with pytest.raises(ToolError):
        _call(_tool(_config(repo[0], dest)), suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def test_source_conflict_publishes_nothing(tool: Tool, dest: Path) -> None:
    (dest / TOOL_TAR).write_bytes(b"mine")
    with pytest.raises(ToolError, match="different"):
        _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == [TOOL_TAR]
    assert (dest / TOOL_TAR).read_bytes() == b"mine"


def test_source_publish_failure_rolls_back(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = safeio.NewFile.commit
    seen: list[str] = []

    def commit(self: safeio.NewFile, final_name: str | None = None) -> str:
        seen.append(self.name)
        if len(seen) == 3:
            # someone else creates the last name meanwhile: it must survive
            os.close(os.open(os.path.join(dest, self.name), os.O_CREAT | os.O_WRONLY, 0o644))
        return real(self, final_name)

    monkeypatch.setattr(safeio.NewFile, "commit", commit)
    with pytest.raises(ToolError, match="nothing"):
        _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == [TOOL_TAR]
    assert (dest / TOOL_TAR).read_bytes() == b""


def test_source_size_cap_sum(repo: tuple[str, Path], dest: Path) -> None:
    sizes = [len(d) for d in sample_source_files().values()]
    tool = _tool(_config(repo[0], dest, max_download_bytes=max(sizes) + 1))
    with pytest.raises(ToolError, match="limit"):
        _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def _rows(algo: str, files: dict[str, bytes]) -> str:
    return "".join(f"\n {_hex(algo, d)} {len(d)} {n}" for n, d in files.items())


def _source_repo(root: Path, files: dict[str, bytes], fields: tuple[str, ...]) -> str:
    """A repository whose only source is *files* (the .dsc first), hashed with *fields*."""
    directory = "pool/main/s/sample-pkg"
    algos = {"Checksums-Sha256": "sha256", "Checksums-Sha1": "sha1", "Files": "md5"}
    stanza = {
        "Package": "sample-pkg",
        "Binary": "sample-pkg",
        "Version": "1.0-1",
        "Architecture": "any",
        "Directory": directory,
        **{f: _rows(algos[f], files) for f in fields},
    }
    url = _repo(root, dsc=False, sources=[stanza])
    for name, data in files.items():
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / name).write_bytes(data)
    return url


def test_source_md5_only_refused(tmp_path: Path, dest: Path) -> None:
    url = _source_repo(tmp_path / "weak", sample_source_files(), ("Files",))
    with pytest.raises(ToolError, match=f"{DSC_NAME}.*SHA256"):
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def test_source_dsc_cross_checked(tmp_path: Path, dest: Path) -> None:
    files = dict(sample_source_files())
    # a .dsc listing a file the Sources stanza does not know
    dsc = (
        "Format: 3.0 (quilt)\nSource: sample-pkg\nVersion: 1.0-1\n"
        "Checksums-Sha256:" + _rows("sha256", {**SRC_FILES, "extra.tar.xz": b"extra"}) + "\n"
    )
    files[DSC_NAME] = clearsign(dsc).encode()
    url = _source_repo(tmp_path / "x", files, ("Checksums-Sha256",))
    with pytest.raises(ToolError, match="dsc"):
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def test_source_dsc_with_only_weak_hashes_refused(tmp_path: Path, dest: Path) -> None:
    files = dict(sample_source_files())
    # the Sources stanza has SHA256, but the .dsc lists only MD5: nothing comparable
    dsc = "Format: 3.0 (quilt)\nSource: sample-pkg\nVersion: 1.0-1\nFiles:" + _rows(
        "md5", SRC_FILES
    )
    files[DSC_NAME] = clearsign(dsc + "\n").encode()
    url = _source_repo(tmp_path / "w", files, ("Checksums-Sha256",))
    with pytest.raises(ToolError, match="no SHA256 or stronger hash"):
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def test_source_hostile_file_name_refused(tmp_path: Path, dest: Path) -> None:
    files = {DSC_NAME: sample_source_files()[DSC_NAME], "-rf.tar.xz": b"x"}
    url = _source_repo(tmp_path / "x", files, ("Checksums-Sha256",))
    with pytest.raises(ToolError, match="unsafe file name"):
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-pkg", kind="source")
    assert _listing(dest) == []


def test_source_with_architectures_refused(tool: Tool) -> None:
    with pytest.raises(ToolError, match="architectures"):
        _call(tool, suite=SUITE, name="sample-pkg", kind="source", architectures=["amd64"])


# -- results --------------------------------------------------------------------------------


def test_result_bounded_by_the_response_budget(repo: tuple[str, Path], dest: Path) -> None:
    tool = _tool(_config(repo[0], dest, max_response_bytes=3000))
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    size = len(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= 1000
    shown = len(r["files"]) + r.get("files_omitted", 0)
    assert shown == 2
    assert _listing(dest) == [DEB_AMD64, DEB_ARM64]


def test_hostile_text_is_cleaned(tmp_path: Path, dest: Path) -> None:
    by_arch, pool = _by_arch()
    evil = "pool/main/s/sample-data/\x1b[31mevil\u202e.deb"
    for p in by_arch["all"] + by_arch["amd64"] + by_arch["arm64"]:
        if p["Package"] == "sample-data":
            p["Filename"] = evil
    url = _repo(tmp_path / "r", by_arch, pool)
    with pytest.raises(ToolError, match="unsafe file name") as info:
        _call(_tool(_config(url, dest)), suite=SUITE, name="sample-data")
    assert "\x1b" not in str(info.value) and "\u202e" not in str(info.value)
    assert _listing(dest) == []


# -- review fixes ---------------------------------------------------------------------------


def _set_field(by_arch: dict[str, Any], arch: str, name: str, key: str, value: str) -> None:
    for p in by_arch[arch]:
        if p["Package"] == name and p["Version"] == "1.1-1":
            p[key] = value


def test_negative_size_is_a_per_file_error_and_the_cap_holds(tmp_path: Path, dest: Path) -> None:
    by_arch, pool = _by_arch()
    _set_field(by_arch, "amd64", "sample-tool", "Size", "-100000000")
    url = _repo(tmp_path / "neg", by_arch, pool)
    tool = _tool(_config(url, dest, max_download_bytes=1_000_000))
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    status = {f["architecture"]: f for f in r["files"]}
    assert status["amd64"]["status"] == "error"
    assert "no valid size" in status["amd64"]["message"]
    assert status["arm64"]["status"] == "downloaded"
    assert _listing(dest) == [DEB_ARM64]
    # the negative size does not make room under the cap for the other files
    arm64 = len(multiarch_deb("sample-tool", "1.1-1", "arm64"))
    small = _tool(_config(url, dest, max_download_bytes=arm64 - 1))
    (dest / DEB_ARM64).unlink()
    with pytest.raises(ToolError, match="limit"):
        _call(small, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    assert _listing(dest) == []


def test_plan_binary_never_carries_a_negative_size(tmp_path: Path) -> None:
    from minideblib.repo.client import RepoClient

    by_arch, pool = _by_arch()
    _set_field(by_arch, "amd64", "sample-tool", "Size", "-5")
    url = _repo(tmp_path / "neg", by_arch, pool)
    client = RepoClient.from_url(url, suites=[SUITE], probe="none")
    (plan,) = client.plan_binary(SUITE, "sample-tool", architectures=["amd64"])
    assert plan.files[0].size is None


def test_publish_failure_is_a_per_file_error(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.fsync
    calls = [0]

    def fsync(fd: int) -> None:
        calls[0] += 1
        if calls[0] == 3:  # the file of the second commit
            raise OSError(errno.ENOSPC, "No space left on device")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    r = _call(tool, suite=SUITE, name="sample-tool", architectures=["amd64", "arm64"])
    status = {f["architecture"]: f for f in r["files"]}
    assert status["amd64"]["status"] == "downloaded"
    assert status["arm64"]["status"] == "error"
    assert "cannot publish" in status["arm64"]["message"]
    assert _listing(dest) == [DEB_AMD64]


def _identical_before_commit(
    monkeypatch: pytest.MonkeyPatch, dest: Path, name: str, data: bytes
) -> None:
    real = safeio.NewFile.commit

    def commit(self: safeio.NewFile, final_name: str | None = None) -> str:
        if self.name == name:
            (dest / name).write_bytes(data)  # someone else, same content
        return real(self, final_name)

    monkeypatch.setattr(safeio.NewFile, "commit", commit)


def test_identical_file_appearing_at_publish_is_already_present(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identical_before_commit(
        monkeypatch, dest, DEB_ALL, multiarch_deb("sample-data", "1.1-1", "all")
    )
    r = _call(tool, suite=SUITE, name="sample-data")
    assert r["files"][0]["status"] == "already_present"
    assert _listing(dest) == [DEB_ALL]


def test_identical_source_file_appearing_at_publish(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orig = "sample-pkg_1.0.orig.tar.xz"
    _identical_before_commit(monkeypatch, dest, orig, SRC_FILES[orig])
    r = _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    assert [f["status"] for f in r["files"]] == ["downloaded", "already_present", "downloaded"]
    assert _listing(dest) == sorted(sample_source_files())


def test_rollback_keeps_a_file_that_appeared_identical(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orig = "sample-pkg_1.0.orig.tar.xz"
    real = safeio.NewFile.commit

    def commit(self: safeio.NewFile, final_name: str | None = None) -> str:
        if self.name == orig:
            (dest / orig).write_bytes(SRC_FILES[orig])
        if self.name == TOOL_TAR:
            (dest / TOOL_TAR).write_bytes(b"other")
        return real(self, final_name)

    monkeypatch.setattr(safeio.NewFile, "commit", commit)
    with pytest.raises(ToolError, match="nothing"):
        _call(tool, suite=SUITE, name="sample-pkg", kind="source")
    # the .dsc published by this call is removed; files of others stay
    assert _listing(dest) == sorted([orig, TOOL_TAR])
    assert (dest / orig).read_bytes() == SRC_FILES[orig]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_base_makes_no_requests(
    http_repo: tuple[str, dict[str, Callable[[], None]]], dest: Path
) -> None:
    url, hooks = http_repo
    seen: list[int] = []
    hooks[""] = lambda: seen.append(1)  # every GET
    tool = _tool(_config(url, dest))
    dest.chmod(0o555)
    try:
        with pytest.raises(ToolError, match="not writable"):
            _call(tool, suite=SUITE, name="sample-data")
    finally:
        dest.chmod(0o700)
    assert seen == []
    _call(tool, suite=SUITE, name="sample-data")
    assert seen  # the hook does count requests


def test_new_file_identity(dest: Path) -> None:
    with WriteRoot.open(dest) as root:
        new = root.create_file("x.deb")
        info = os.stat(dest / new.temp_name)
        assert new.identity == (info.st_dev, info.st_ino)
        new.file.write(b"data")
        new.commit()
        published = os.stat(dest / "x.deb")
        assert new.identity == (published.st_dev, published.st_ino)


def test_existing_file_of_other_size_is_not_hashed(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (dest / DEB_ALL).write_bytes(b"mine")

    def no_hash(*args: Any, **kw: Any) -> str:
        raise AssertionError("hashed")

    monkeypatch.setattr(WriteRoot, "hash_existing", no_hash)
    with pytest.raises(ToolError, match="different"):
        _call(tool, suite=SUITE, name="sample-data")


def test_hash_existing_honours_the_check(dest: Path) -> None:
    (dest / "big").write_bytes(b"x" * (3 * 1024 * 1024))
    calls = [0]

    def check() -> None:
        calls[0] += 1
        if calls[0] == 2:
            raise ToolError("cancelled")

    with WriteRoot.open(dest) as root, pytest.raises(ToolError, match="cancelled"):
        root.hash_existing("big", "sha256", check=check)
    assert calls[0] == 2


def test_cancel_while_hashing_an_existing_file(
    tool: Tool, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = CancelToken()
    real = WriteRoot.hash_existing

    def hash_existing(self: WriteRoot, name: str, algorithm: str, **kw: Any) -> str:
        token.cancel()
        return real(self, name, algorithm, **kw)

    monkeypatch.setattr(WriteRoot, "hash_existing", hash_existing)
    data = multiarch_deb("sample-data", "1.1-1", "all")
    (dest / DEB_ALL).write_bytes(data)
    with pytest.raises(ToolError, match="cancelled"):
        _call(tool, token, suite=SUITE, name="sample-data")


def test_empty_subdirectory_is_none(tool: Tool, dest: Path) -> None:
    r = _call(tool, suite=SUITE, name="sample-data", subdirectory="")
    assert r["subdirectory"] is None
    assert _listing(dest) == [DEB_ALL]


def test_stale_cleanup_boundary(tool: Tool, dest: Path) -> None:
    young = dest / f"{TEMP_PREFIX}00000000000000aa"
    old = dest / f"{TEMP_PREFIX}00000000000000bb"
    for f, age in ((young, 3600 - 120), (old, 3600 + 120)):
        f.write_bytes(b"x")
        stamp = time.time() - age
        os.utime(f, (stamp, stamp))
    r = _call(tool, suite=SUITE, name="sample-data")
    assert young.exists() and not old.exists()
    assert r["stale_temp_files_removed"] == 1
