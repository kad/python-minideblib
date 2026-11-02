# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Startup checks and configuration of ``minideb mcp``.

:func:`check_platform` and :func:`check_not_root` run before anything is
read from stdin; :func:`build_config` turns the parsed command line into a
:class:`ServerConfig`. Every problem raises :class:`StartupError` with a
one-line message.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minideblib import sources
from minideblib.errors import MinidebError
from minideblib.mcp.safeio import WriteRoot, WriteRootError
from minideblib.repo.fetch import Fetcher, HostNotAllowed, redact_url
from minideblib.sources import SourceEntry, SourcesError

__all__ = [
    "MAX_SECONDS",
    "MIN_RESPONSE_BYTES",
    "RepositorySpec",
    "ServerConfig",
    "StartupError",
    "build_config",
    "check_url",
    "check_not_root",
    "check_platform",
]

_URL_SCHEMES = ("http", "https", "file")
#: the smallest accepted ``--max-response-bytes``: tools budget their results
#: to a third of the cap (protocol 2025-06-18 and later carry a result twice),
#: and a few hundred bytes go to the JSON-RPC envelope and error messages
MIN_RESPONSE_BYTES = 4096
#: the largest accepted time option (seconds): 7 days, far below
#: ``threading.TIMEOUT_MAX`` and still finite for every deadline computation
MAX_SECONDS = 7 * 24 * 3600
#: functions the confined writes call with ``dir_fd``
_DIR_FD_FUNCS = ("open", "mkdir", "link", "unlink", "stat")


class StartupError(MinidebError):
    """The server cannot start: unsupported platform, root, or a bad option."""


def check_platform() -> None:
    """Require POSIX with ``O_NOFOLLOW``, ``O_DIRECTORY`` and ``dir_fd`` support."""
    if os.name != "posix":
        raise StartupError("minideb mcp needs a POSIX system (Linux or macOS)")
    for flag in ("O_NOFOLLOW", "O_DIRECTORY"):
        if not hasattr(os, flag):
            raise StartupError(f"minideb mcp needs os.{flag}, which this platform lacks")
    missing = [n for n in _DIR_FD_FUNCS if getattr(os, n) not in os.supports_dir_fd]
    if missing:
        raise StartupError(
            f"minideb mcp needs dir_fd support for os.{', os.'.join(missing)}, "
            "which this platform lacks"
        )


def check_not_root() -> None:
    """Refuse to run when the real, effective or saved user id is 0.

    Uses ``os.getresuid`` where available, else ``getuid``/``geteuid``; on
    platforms without user ids the check is skipped.
    """
    getresuid = getattr(os, "getresuid", None)
    ids: tuple[int, ...]
    if getresuid is not None:
        ids = tuple(getresuid())
    else:
        ids = tuple(f() for f in (getattr(os, "getuid", None), getattr(os, "geteuid", None)) if f)
    if 0 in ids:
        raise StartupError(
            "refusing to run as root (uid 0, also in rootless containers); "
            "start minideb mcp as a normal user"
        )


@dataclass(frozen=True)
class RepositorySpec:
    """A repository configured at start, usable in tool calls by :attr:`name`.

    ``entries`` holds every enabled ``--sources`` entry with that URI (empty
    for a ``--url`` repository that no sources entry names; suites are then
    probed on demand).
    """

    name: str
    url: str
    entries: tuple[SourceEntry, ...] = ()


@dataclass(frozen=True)
class ServerConfig:
    """Everything the MCP tools need to know from the command line."""

    repositories: tuple[RepositorySpec, ...] = ()
    #: resolved explicit ``--download-dir`` (checked at start), or None for
    #: the current directory (checked when a download is requested)
    download_dir: str | None = None
    #: ``(st_dev, st_ino)`` of :attr:`download_dir` when it was checked at
    #: start; downloads refuse a directory that is no longer the same one
    download_dir_id: tuple[int, int] | None = None
    allow_download: bool = True
    #: ``--allow-host`` entries, or None for any host
    allowed_hosts: tuple[str, ...] | None = None
    allow_link_local: bool = False
    max_response_bytes: int = 200_000
    max_download_bytes: int = 1024**3
    verify: bool = True
    timeout: float = 30.0
    retries: int = 2
    call_timeout: float = 120.0
    download_timeout: float = 900.0


def _opt(args: argparse.Namespace, name: str, default: Any) -> Any:
    value = getattr(args, name, None)
    return default if value is None else value


def _positive(value: float, option: str, *, integer: bool = False, zero: bool = False) -> Any:
    if integer:
        if isinstance(value, bool) or not isinstance(value, int):
            raise StartupError(f"--{option}: expected an integer, got {value!r}")
    elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StartupError(f"--{option}: expected a finite number, got {value!r}")
    if value < 0 or (value == 0 and not zero):
        raise StartupError(f"--{option}: must be {'0 or more' if zero else 'positive'}")
    return value


def _seconds(value: float, option: str) -> float:
    value = _positive(value, option)
    if value > MAX_SECONDS:
        raise StartupError(f"--{option}: must be at most {MAX_SECONDS} seconds (7 days)")
    return value


def _response_cap(value: int) -> int:
    value = _positive(value, "max-response-bytes", integer=True)
    if value < MIN_RESPONSE_BYTES:
        raise StartupError(
            f"--max-response-bytes: must be at least {MIN_RESPONSE_BYTES} "
            "(room for a useful tool result and its JSON-RPC envelope)"
        )
    return value


_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


def check_url(url: str, option: str) -> str:
    """Return *url* if usable as a repository URL, else raise :class:`StartupError`.

    *option* names the source of the URL in messages (``--url``, ...).

    Messages never quote the URL itself (it may carry credentials): only the
    option, the scheme and, once the URL parsed, the host.
    """
    head = url.split(":", 1)[0] if ":" in url else ""
    scheme = head.lower() if _SCHEME_RE.fullmatch(head) else ""
    if scheme not in _URL_SCHEMES:
        shown = f"URL scheme {scheme!r}" if scheme else "URL without a scheme"
        raise StartupError(f"{option}: unsupported {shown} (only http, https and file)")
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        userinfo = parts.username is not None or parts.password is not None
        parts.port  # noqa: B018  # raises ValueError for a bad port
    except ValueError:
        raise StartupError(f"{option}: invalid {scheme} URL (bad host or port)") from None
    if userinfo or "@" in parts.netloc:
        raise StartupError(
            f"{option}: credentials in URLs are not accepted ({scheme}://{host or ''})"
        )
    if scheme == "file":
        if parts.netloc not in ("", "localhost") or not parts.path.startswith("/"):
            raise StartupError(f"{option}: file URLs need an absolute local path (file:///...)")
    elif not host:
        raise StartupError(f"{option}: {scheme} URL without a host name")
    return url


def _check_allowed(url: str, option: str, policy: Fetcher | None) -> None:
    """StartupError if the ``--allow-host`` *policy* refuses the host of *url*."""
    if policy is None:
        return
    try:
        policy.check_host(url)
    except HostNotAllowed:
        host = urllib.parse.urlsplit(url).hostname or ""
        raise StartupError(f"{option}: host {host!r} is not allowed by --allow-host") from None


def _repositories(
    urls: list[str], sources_path: str | None, policy: Fetcher | None = None
) -> tuple[RepositorySpec, ...]:
    # keyed by the URL without trailing slashes; the first spelling is kept
    ordered: dict[str, tuple[str, list[SourceEntry]]] = {}
    for url in urls:
        check_url(url, "--url")
        _check_allowed(url, "--url", policy)
        ordered.setdefault(url.rstrip("/"), (url, []))
    if sources_path is not None:
        try:
            entries = sources.load(Path(sources_path))
        except (OSError, SourcesError) as exc:
            raise StartupError(f"--sources {sources_path}: {redact_url(str(exc))}") from None
        for entry in entries:
            if entry.enabled:
                check_url(entry.uri, f"--sources {sources_path}")
                _check_allowed(entry.uri, f"--sources {sources_path}", policy)
                ordered.setdefault(entry.uri.rstrip("/"), (entry.uri, []))[1].append(entry)
    return tuple(
        RepositorySpec(f"repo{i}", url, tuple(group))
        for i, (url, group) in enumerate(ordered.values(), start=1)
    )


def _download_dir(path: str) -> tuple[str, tuple[int, int]]:
    """The resolved path of *path* and its ``(st_dev, st_ino)``."""
    try:
        root = WriteRoot.open(path)
    except WriteRootError as exc:
        raise StartupError(f"--download-dir {exc.strerror}") from exc
    with root:
        if not root.writable():
            raise StartupError(f"--download-dir {root.path}: not writable by the current user")
        info = os.fstat(root.fileno())
        return root.path, (info.st_dev, info.st_ino)


def build_config(args: argparse.Namespace) -> ServerConfig:
    """Validate the ``mcp`` command line options into a :class:`ServerConfig`.

    Options missing from *args* take their defaults. Repositories are named
    ``repo1``, ``repo2``, ... : ``--url`` entries first (in order, duplicates
    once), then one per further distinct URI of the ``--sources`` file or
    directory (entries for a URI already given with ``--url`` are merged into
    that repository); every URI passes the same checks as ``--url`` and,
    with ``--allow-host``, must name an allowed host (``file:`` URLs are not
    affected); the system sources are never read implicitly. An explicit ``--download-dir``
    (``~`` expanded) must exist, be a directory (not a symlink) and be
    writable by the effective user; its device and inode are recorded
    (:attr:`ServerConfig.download_dir_id`). ``--max-response-bytes`` must be
    at least :data:`MIN_RESPONSE_BYTES`; ``--timeout``, ``--call-timeout`` and
    ``--download-timeout`` at most :data:`MAX_SECONDS`.
    """
    defaults = ServerConfig()
    hosts_opt = getattr(args, "allow_host", None)
    allowed_hosts: tuple[str, ...] | None = None
    policy: Fetcher | None = None
    if hosts_opt is not None:
        allowed_hosts = tuple(hosts_opt)
        try:
            # only the allowlist: link-local hosts are refused (or not) per request
            policy = Fetcher(allowed_hosts=allowed_hosts, block_link_local=False)
        except (TypeError, ValueError) as exc:
            raise StartupError(f"--allow-host: {exc}") from None
    download_dir = getattr(args, "download_dir", None)
    checked: tuple[str, tuple[int, int]] | None = None
    if download_dir is not None:
        download_dir = os.path.expanduser(os.fspath(download_dir))
        if not download_dir:
            raise StartupError("--download-dir: empty path")
        checked = _download_dir(download_dir)
    return ServerConfig(
        repositories=_repositories(
            list(getattr(args, "url", None) or ()), _sources_arg(args), policy
        ),
        download_dir=None if checked is None else checked[0],
        download_dir_id=None if checked is None else checked[1],
        allow_download=not getattr(args, "no_download", False),
        allowed_hosts=allowed_hosts,
        allow_link_local=bool(getattr(args, "allow_link_local", False)),
        max_response_bytes=_response_cap(
            _opt(args, "max_response_bytes", defaults.max_response_bytes)
        ),
        max_download_bytes=_positive(
            _opt(args, "max_download_bytes", defaults.max_download_bytes),
            "max-download-bytes",
            integer=True,
        ),
        verify=bool(getattr(args, "verify", True)),
        timeout=_seconds(_opt(args, "timeout", defaults.timeout), "timeout"),
        retries=_positive(
            _opt(args, "retries", defaults.retries), "retries", integer=True, zero=True
        ),
        call_timeout=_seconds(_opt(args, "call_timeout", defaults.call_timeout), "call-timeout"),
        download_timeout=_seconds(
            _opt(args, "download_timeout", defaults.download_timeout), "download-timeout"
        ),
    )


def _sources_arg(args: argparse.Namespace) -> str | None:
    value = getattr(args, "sources", None)
    return None if value is None else os.fspath(value)
