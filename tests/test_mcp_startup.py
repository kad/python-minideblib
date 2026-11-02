# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Startup checks of the MCP server: platform, uid, configuration from the CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from minideblib.errors import MinidebError
from minideblib.mcp import startup
from minideblib.mcp.startup import (
    MAX_SECONDS,
    MIN_RESPONSE_BYTES,
    RepositorySpec,
    ServerConfig,
    StartupError,
    build_config,
    check_not_root,
    check_platform,
)

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_startup_error_is_minideb_error() -> None:
    assert issubclass(StartupError, MinidebError)


# -- check_not_root ---------------------------------------------------------------


@pytest.mark.parametrize("ids", [(1000, 1000, 0), (0, 1000, 1000), (1000, 0, 1000), (0, 0, 0)])
def test_root_ids_refused(monkeypatch: pytest.MonkeyPatch, ids: tuple[int, int, int]) -> None:
    monkeypatch.setattr(startup.os, "getresuid", lambda: ids, raising=False)
    with pytest.raises(StartupError, match="root"):
        check_not_root()


def test_normal_ids_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup.os, "getresuid", lambda: (1000, 1001, 1002), raising=False)
    check_not_root()


def test_fallback_without_getresuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(startup.os, "getresuid", raising=False)
    monkeypatch.setattr(startup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(startup.os, "geteuid", lambda: 0)
    with pytest.raises(StartupError, match="root"):
        check_not_root()
    monkeypatch.setattr(startup.os, "getuid", lambda: 0)
    monkeypatch.setattr(startup.os, "geteuid", lambda: 1000)
    with pytest.raises(StartupError, match="root"):
        check_not_root()
    monkeypatch.setattr(startup.os, "getuid", lambda: 1000)
    check_not_root()


@pytest.mark.skipif(_IS_ROOT, reason="the test process runs as root")
def test_current_user_ok() -> None:
    check_not_root()


# -- check_platform ---------------------------------------------------------------


def test_platform_ok() -> None:
    check_platform()


def test_platform_not_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup.os, "name", "nt")
    with pytest.raises(StartupError, match="POSIX"):
        check_platform()


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_platform_missing_flag(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.delattr(startup.os, flag)
    with pytest.raises(StartupError, match=flag):
        check_platform()


@pytest.mark.parametrize("func", ["open", "mkdir", "link", "unlink", "stat"])
def test_platform_missing_dir_fd(monkeypatch: pytest.MonkeyPatch, func: str) -> None:
    missing = getattr(os, func)
    monkeypatch.setattr(
        startup.os, "supports_dir_fd", {f for f in os.supports_dir_fd if f is not missing}
    )
    with pytest.raises(StartupError, match="dir_fd"):
        check_platform()


# -- build_config -----------------------------------------------------------------


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "url": None,
        "sources": None,
        "timeout": 30.0,
        "retries": 2,
        "verify": True,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_defaults() -> None:
    cfg = build_config(_args())
    assert isinstance(cfg, ServerConfig)
    assert cfg.repositories == ()
    assert cfg.download_dir is None
    assert cfg.download_dir_id is None
    assert cfg.allow_download is True
    assert cfg.allowed_hosts is None
    assert cfg.allow_link_local is False
    assert cfg.max_response_bytes == 200_000
    assert cfg.max_download_bytes == 1024**3
    assert cfg.verify is True
    assert cfg.timeout == 30.0
    assert cfg.retries == 2
    assert cfg.call_timeout == 120.0
    assert cfg.download_timeout == 900.0


def test_minimal_namespace() -> None:
    # only the attributes the CLI always sets for every command may be relied on
    cfg = build_config(argparse.Namespace())
    assert cfg.repositories == ()
    assert cfg.verify is True


def test_all_options() -> None:
    cfg = build_config(
        _args(
            no_download=True,
            allow_host=["deb.debian.org", "*.example.org"],
            allow_link_local=True,
            max_response_bytes=4096,
            max_download_bytes=5000,
            call_timeout=5.0,
            download_timeout=60.0,
            verify=False,
            timeout=3.0,
            retries=0,
        )
    )
    assert cfg.allow_download is False
    assert cfg.allowed_hosts == ("deb.debian.org", "*.example.org")
    assert cfg.allow_link_local is True
    assert cfg.max_response_bytes == 4096
    assert cfg.max_download_bytes == 5000
    assert cfg.call_timeout == 5.0
    assert cfg.download_timeout == 60.0
    assert cfg.verify is False
    assert cfg.timeout == 3.0
    assert cfg.retries == 0


def test_config_is_frozen() -> None:
    cfg = build_config(_args())
    with pytest.raises(AttributeError):
        cfg.verify = False  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_response_bytes", 0),
        ("max_response_bytes", -1),
        ("max_response_bytes", 4095),
        ("max_download_bytes", 0),
        ("call_timeout", 0),
        ("call_timeout", float("nan")),
        ("download_timeout", -5),
        ("download_timeout", float("inf")),
        ("timeout", 0),
        ("retries", -1),
    ],
)
def test_bad_numbers(name: str, value: float) -> None:
    with pytest.raises(StartupError, match=name.replace("_", "-")):
        build_config(_args(**{name: value}))


@pytest.mark.parametrize("name", ["call_timeout", "download_timeout", "timeout"])
def test_time_options_bounded(name: str) -> None:
    assert MAX_SECONDS == 7 * 24 * 3600
    assert getattr(build_config(_args(**{name: MAX_SECONDS})), name) == MAX_SECONDS
    for value in (MAX_SECONDS + 1, 1e300):
        option = name.replace("_", "-")
        with pytest.raises(StartupError, match=rf"^--{option}: must be at most 604800 seconds"):
            build_config(_args(**{name: value}))


def test_max_response_bytes_minimum() -> None:
    with pytest.raises(StartupError, match=r"--max-response-bytes: must be at least 4096"):
        build_config(_args(max_response_bytes=100))
    assert MIN_RESPONSE_BYTES == 4096
    assert build_config(_args(max_response_bytes=4096)).max_response_bytes == 4096


@pytest.mark.parametrize("host", ["http://x.org", "x.org:80", "", "a/b"])
def test_bad_allowed_host(host: str) -> None:
    with pytest.raises(StartupError, match="allow-host"):
        build_config(_args(allow_host=[host]))


# -- repositories -------------------------------------------------------------------


def test_url_repositories_named_in_order() -> None:
    cfg = build_config(_args(url=["http://a.example/debian", "https://b.example/ubuntu/"]))
    assert cfg.repositories == (
        RepositorySpec("repo1", "http://a.example/debian"),
        RepositorySpec("repo2", "https://b.example/ubuntu/"),
    )


def test_duplicate_url_kept_once() -> None:
    cfg = build_config(_args(url=["http://a.example/debian", "http://a.example/debian"]))
    assert [r.name for r in cfg.repositories] == ["repo1"]


def test_file_url_allowed(tmp_path: Path) -> None:
    cfg = build_config(_args(url=[tmp_path.as_uri()]))
    assert cfg.repositories[0].url == tmp_path.as_uri()


@pytest.mark.parametrize(
    "url", ["ftp://x.example/debian", "http://user:pw@x.example/debian", "not a url", "x.org"]
)
def test_bad_urls(url: str) -> None:
    with pytest.raises(StartupError, match="--url"):
        build_config(_args(url=[url]))


def test_sources_one_repository_per_uri(tmp_path: Path) -> None:
    src = tmp_path / "s.list"
    src.write_text(
        "deb http://a.example/debian stable main\n"
        "deb-src http://a.example/debian stable main\n"
        "deb http://a.example/debian stable-updates main\n"
        "deb https://c.example/ubuntu noble main universe\n"
    )
    cfg = build_config(_args(url=["http://u.example/x"], sources=str(src)))
    names = [(r.name, r.url) for r in cfg.repositories]
    assert names == [
        ("repo1", "http://u.example/x"),
        ("repo2", "http://a.example/debian"),
        ("repo3", "https://c.example/ubuntu"),
    ]
    assert cfg.repositories[0].entries == ()
    assert [(e.type, e.suite) for e in cfg.repositories[1].entries] == [
        ("deb", "stable"),
        ("deb-src", "stable"),
        ("deb", "stable-updates"),
    ]
    assert cfg.repositories[2].entries[0].components == ("main", "universe")


def test_sources_directory(tmp_path: Path) -> None:
    (tmp_path / "a.list").write_text("deb http://a.example/debian stable main\n")
    (tmp_path / "b.sources").write_text(
        "Types: deb\nURIs: http://b.example/debian\nSuites: bookworm\nComponents: main\n"
    )
    cfg = build_config(_args(sources=str(tmp_path)))
    assert [r.url for r in cfg.repositories] == [
        "http://a.example/debian",
        "http://b.example/debian",
    ]


def test_sources_disabled_entries_skipped(tmp_path: Path) -> None:
    (tmp_path / "b.sources").write_text(
        "Types: deb\nURIs: http://b.example/debian\nSuites: bookworm\nComponents: main\n"
        "Enabled: no\n"
    )
    cfg = build_config(_args(sources=str(tmp_path / "b.sources")))
    assert cfg.repositories == ()


def test_sources_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="sources"):
        build_config(_args(sources=str(tmp_path / "missing.list")))


def test_sources_malformed(tmp_path: Path) -> None:
    src = tmp_path / "bad.list"
    src.write_bytes(b"\xff\xfe not utf-8\n")
    with pytest.raises(StartupError, match="sources"):
        build_config(_args(sources=str(src)))


def test_no_implicit_system_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: object = None) -> list[object]:
        raise AssertionError("system sources must not be read")

    monkeypatch.setattr(startup.sources, "load", boom)
    assert build_config(_args()).repositories == ()


# -- download directory -----------------------------------------------------------


def test_download_dir_valid(tmp_path: Path) -> None:
    cfg = build_config(_args(download_dir=str(tmp_path)))
    assert cfg.download_dir == os.path.realpath(tmp_path)
    assert list(tmp_path.iterdir()) == []
    info = os.stat(tmp_path)
    assert cfg.download_dir_id == (info.st_dev, info.st_ino)


def test_download_dir_missing(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="download-dir.*does not exist"):
        build_config(_args(download_dir=str(tmp_path / "missing")))


def test_download_dir_not_directory(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"")
    with pytest.raises(StartupError, match="download-dir.*not a directory"):
        build_config(_args(download_dir=str(tmp_path / "f")))


def test_download_dir_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    with pytest.raises(StartupError, match="download-dir"):
        build_config(_args(download_dir=str(tmp_path / "link")))


@pytest.mark.skipif(_IS_ROOT, reason="permission checks do not apply to root")
def test_download_dir_unwritable(tmp_path: Path) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        with pytest.raises(StartupError, match="download-dir.*not writable"):
            build_config(_args(download_dir=str(d)))
    finally:
        os.chmod(d, 0o700)


def test_download_dir_kept_when_downloads_disabled(tmp_path: Path) -> None:
    # read-only server: the directory is still validated and reported
    cfg = build_config(_args(download_dir=str(tmp_path), no_download=True))
    assert cfg.allow_download is False
    assert cfg.download_dir == os.path.realpath(tmp_path)


def test_url_and_sources_same_uri_merged(tmp_path: Path) -> None:
    src = tmp_path / "s.list"
    src.write_text(
        "deb http://a.example/debian/ stable main\n"
        "deb http://b.example/debian sid main\n"
        "deb http://a.example/debian/ testing main\n"
    )
    cfg = build_config(_args(url=["http://a.example/debian"], sources=str(src)))
    assert [(r.name, r.url) for r in cfg.repositories] == [
        ("repo1", "http://a.example/debian"),
        ("repo2", "http://b.example/debian"),
    ]
    assert [e.suite for e in cfg.repositories[0].entries] == ["stable", "testing"]


SECRET = "s3cr3tpw"


@pytest.mark.parametrize(
    ("line", "match"),
    [
        (f"deb http://user:{SECRET}@a.example/debian stable main", "credentials"),
        (f"deb https://{SECRET}@a.example/debian stable main", "credentials"),
        (f"deb ftp://user:{SECRET}@a.example/debian stable main", "scheme 'ftp'"),
        (f"deb tor+http://user:{SECRET}@a.example/debian stable main", "scheme 'tor\\+http'"),
        (f"deb cdrom:[{SECRET}]/ stable main", "scheme 'cdrom'"),
        (f"deb http://user:{SECRET}@a.example:99999/debian stable main", "bad host or port"),
        (f"deb http://a.example:x{SECRET}/debian stable main", "bad host or port"),
        (f"deb file:relative/{SECRET} stable main", "absolute"),
        (f"deb file://otherhost/{SECRET} stable main", "absolute"),
        ("deb http:///debian stable main", "host"),
    ],
)
def test_sources_uris_checked_like_url(tmp_path: Path, line: str, match: str) -> None:
    src = tmp_path / "s.list"
    src.write_text(line + "\n")
    with pytest.raises(StartupError, match=match) as info:
        build_config(_args(sources=str(src)))
    assert SECRET not in str(info.value)
    assert "--sources" in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://user:{SECRET}@a.example/debian",
        f"ftp://user:{SECRET}@a.example/debian",
        f"http://user:{SECRET}@a.example:bad/debian",
        f"http://[{SECRET}/debian",
        f"user:{SECRET}@a.example/debian",
        f"file:{SECRET}",
    ],
)
def test_url_errors_never_echo_secrets(url: str) -> None:
    with pytest.raises(StartupError, match="--url") as info:
        build_config(_args(url=[url]))
    assert SECRET not in str(info.value)


def test_disabled_sources_entry_not_checked(tmp_path: Path) -> None:
    (tmp_path / "b.sources").write_text(
        "Types: deb\nURIs: cdrom:/x\nSuites: bookworm\nComponents: main\nEnabled: no\n"
    )
    assert build_config(_args(sources=str(tmp_path / "b.sources"))).repositories == ()


def test_file_url_localhost_ok(tmp_path: Path) -> None:
    url = "file://localhost" + str(tmp_path)
    assert build_config(_args(url=[url])).repositories[0].url == url


def test_download_dir_expanduser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "debs").mkdir()
    cfg = build_config(_args(download_dir="~/debs"))
    assert cfg.download_dir == os.path.realpath(tmp_path / "debs")


def test_download_dir_empty_refused() -> None:
    with pytest.raises(StartupError, match="download-dir.*empty"):
        build_config(_args(download_dir=""))
