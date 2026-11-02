# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The download tool of the MCP server (``download_package``).

The only tool that writes. Files go into one of two write roots: the base
directory (``--download-dir``, or the working directory) or the private
per-session temporary directory (:func:`~minideblib.mcp.safeio.session_temp_dir`),
optionally below a relative ``subdirectory``. Every write goes through
:class:`~minideblib.mcp.safeio.WriteRoot`: the root is opened once as a
directory descriptor (a configured base directory must still be the
directory checked at start: same device and inode), checked with a real
write probe, subdirectories are opened component by component without
following symlinks, files are written under hidden temporary names and
published with a hard link that never replaces anything.

Before anything is written, every file needs a size and a SHA256 or
stronger hash from the repository index (``ToolError`` naming the file
otherwise), the sum of the sizes must fit ``max_download_bytes`` and the
free space of the target. Verification of downloads does not depend on
``--no-verify``. File names come from the index (``Filename`` basename or
the ``Sources`` file list) and must pass
:func:`~minideblib.mcp.safeio.validate_file_name`; they are never joined
into paths. An existing file with the same name is hashed without
following symlinks: equal content is ``already_present``, anything else is
an error and is left untouched.

Binary packages: one file per architecture, each reported with its own
status (``downloaded``, ``already_present`` or ``error``). Source packages
are all-or-none: every missing file is staged and verified, the ``.dsc``
is parsed and cross-checked against the ``Sources`` stanza, and only then
are the files published; if publishing fails midway the files published by
this call are removed again.

Stale temporary files (older than :data:`STALE_AGE`) of crashed runs are
removed from the target directory at the start of each download.

Reported paths (``root``, ``directory``, ``absolute_path``) are the paths
the directories had when they were opened; the files are written through
the open descriptors, so if a directory is moved during the call the
reported path is the original one. The call
has its own deadline (``download_timeout``) and honours cancellation;
partial files are always removed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import stat
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import BinaryIO, cast

from minideblib.checksums import ChecksumError, is_weak
from minideblib.dsc import Dsc
from minideblib.errors import MinidebError
from minideblib.mcp.protocol import Tool, ToolCall, ToolError
from minideblib.mcp.repos import RepoManager, _json_size, clean_text, clean_value, error_text
from minideblib.mcp.safeio import (
    NewFile,
    UnsafeNameError,
    WriteRoot,
    WriteRootError,
    session_temp_dir,
    validate_file_name,
)
from minideblib.mcp.toolkit import (
    FIELD_LIMIT,
    SHORT_LIMIT,
    opt_str,
    req_str,
    schema,
    string,
    strings,
)
from minideblib.mcp.tools_repo import UNTRUSTED
from minideblib.repo.client import DownloadPlan, FileSpec, RepoClient
from minideblib.repo.fetch import FetchCancelled, FetchTimeout, redact_url
from minideblib.version import Version

__all__ = ["STALE_AGE", "download_tools"]

#: age (seconds) from which leftover temporary files are removed; never small,
#: another server may be writing into the same directory
STALE_AGE = 3600.0
#: largest ``.dsc`` parsed for the cross-check
MAX_DSC_SIZE = 8 * 1024**2
#: most architectures one call may ask for
MAX_ARCHES = 32
#: most components one call may name
MAX_COMPONENTS = 32
#: most per-file errors quoted in a ToolError
_MAX_QUOTED = 5
_PROBLEM_LIMIT = 500
_MAX_PROBLEMS = 50
_TEMP_NOTE = (
    "the private per-session temporary directory (mode 0700) is not removed when the "
    "server exits: move or delete the files when done"
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _shown(text: str) -> str:
    return clean_text(text, SHORT_LIMIT)


@dataclass
class _Item:
    """One file of the call and what happened to it."""

    spec: FileSpec
    plan: DownloadPlan
    #: the hash the file is checked against (SHA256 or stronger)
    algorithm: str = ""
    digest: str = ""
    size: int = 0
    status: str = "pending"
    message: str | None = None
    hashes: dict[str, str] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.status = "error"
        self.message = message


def _opt_list(args: dict[str, object], name: str, most: int) -> list[str] | None:
    value = args.get(name)
    if not isinstance(value, list) or not value:
        return None
    if len(value) > most:
        raise ToolError(f'"{name}": at most {most} items')
    return list(dict.fromkeys(str(v) for v in value))


def _subdirectory(text: str | None) -> list[str]:
    """The validated components of the ``subdirectory`` argument."""
    if not text:
        return []
    if text.startswith("/"):
        raise ToolError('"subdirectory": must be relative to the download directory')
    parts = text.split("/")
    for part in parts:
        try:
            validate_file_name(part)
        except UnsafeNameError as exc:
            msg = str(exc).replace("file name", "directory name", 1)
            raise ToolError(f'"subdirectory": {clean_text(msg, _PROBLEM_LIMIT)}') from None
    return parts


class _Tee:
    """A writer copying everything into a second, in-memory buffer (the ``.dsc``)."""

    def __init__(self, out: BinaryIO) -> None:
        self.out = out
        self.copy = io.BytesIO()

    def write(self, data: bytes) -> int:
        n = self.out.write(data)
        written = len(data) if n is None else n
        self.copy.write(data[:written])
        return written


class DownloadTools:
    """The ``download_package`` handler over one :class:`RepoManager`."""

    def __init__(self, manager: RepoManager) -> None:
        self.manager = manager
        self.config = manager.config
        self.budget = max(1, self.config.max_response_bytes // 3)
        self.base_id = self.config.download_dir_id
        if self.base_id is None and self.config.download_dir is not None:
            # a configuration built without build_config: the directory seen now
            try:
                with WriteRoot.open(self.config.download_dir) as root:
                    info = os.fstat(root.fileno())
                    self.base_id = (info.st_dev, info.st_ino)
            except WriteRootError:
                self.base_id = (-1, -1)  # never matches: downloads are refused

    # -- plumbing ---------------------------------------------------------------------

    def wrap(self, fn: Callable[[ToolCall], dict[str, object]]) -> Callable[[ToolCall], object]:
        """Run *fn* with the download deadline, mapping library errors to :class:`ToolError`."""

        def handler(call: ToolCall) -> object:
            deadline = time.monotonic() + self.config.download_timeout
            if call.deadline is not None:
                deadline = min(deadline, call.deadline)
            scoped = ToolCall(call.arguments, call.token, deadline)
            try:
                with self.manager.fetcher.scope(call.token, deadline):
                    result = fn(scoped)
                return self._bounded(cast(dict[str, object], clean_value(result, FIELD_LIMIT)))
            except ToolError:
                raise
            except FetchCancelled:
                raise ToolError("cancelled") from None
            except FetchTimeout:
                raise ToolError("timed out: the call deadline passed") from None
            except (MinidebError, OSError, ValueError) as exc:
                raise ToolError(error_text(exc)) from None
            finally:
                self.manager.trim()

        return handler

    def _bounded(self, result: dict[str, object]) -> dict[str, object]:
        """*result* within the response budget: compact file entries, then fewer."""
        if _json_size(result) <= self.budget:
            return result
        files = result.get("files")
        if not isinstance(files, list):
            return result
        keep = ("name", "status", "path", "size", "message")
        compact = [{k: v for k, v in f.items() if k in keep} for f in files if isinstance(f, dict)]
        for f in compact:
            if isinstance(f.get("message"), str):
                f["message"] = clean_text(f["message"], 120)
        out = {**result, "files": compact, "compact": True}
        while _json_size(out) > self.budget and compact:
            compact.pop()
            out["files_omitted"] = len(files) - len(compact)
        return out

    @staticmethod
    def _checkpoint(call: ToolCall) -> None:
        if call.token.is_cancelled:
            raise ToolError("cancelled")
        if call.deadline is not None and time.monotonic() >= call.deadline:
            raise ToolError("timed out: the call deadline passed")

    # -- write roots ------------------------------------------------------------------

    def _open_base(self) -> WriteRoot:
        path = self.config.download_dir
        if path is None:
            cwd = os.getcwd()
            root = WriteRoot.open(cwd)
            expected = os.stat(".")
            want: tuple[int, int] | None = (expected.st_dev, expected.st_ino)
        else:
            root = WriteRoot.open(path)
            want = self.base_id
        info = os.fstat(root.fileno())
        if want is not None and (info.st_dev, info.st_ino) != want:
            root.close()
            raise ToolError(
                f"the download directory {clean_text(root.path, FIELD_LIMIT)} is no longer the "
                "directory checked at start (moved or replaced); refusing to write there"
            )
        return root

    def _open_root(self, temp: bool) -> WriteRoot:
        """The write root, open and probed writable (before any network request)."""
        try:
            root = session_temp_dir() if temp else self._open_base()
        except WriteRootError as exc:
            raise ToolError(error_text(exc)) from None
        try:
            writable = root.writable()
        except BaseException:
            root.close()
            raise
        if not writable:
            root.close()
            raise ToolError(
                f"{clean_text(root.path, FIELD_LIMIT)}: not writable by the current user"
            )
        return root

    @contextlib.contextmanager
    def _target(self, root: WriteRoot, parts: list[str]) -> Generator[tuple[WriteRoot, int]]:
        """``(target directory, stale files removed)`` below *root*, open."""
        try:
            target = root.subdir(parts)
        except UnsafeNameError as exc:  # already checked; kept as a backstop
            raise ToolError(f'"subdirectory": {error_text(exc)}') from None
        except WriteRootError as exc:
            raise ToolError(f'"subdirectory": {error_text(exc)}') from None
        with target:
            if parts and not target.writable():
                raise ToolError(
                    f"{clean_text(target.path, FIELD_LIMIT)}: not writable by the current user"
                )
            removed = target.cleanup_stale(STALE_AGE)
            yield target, removed

    # -- planning ---------------------------------------------------------------------

    def _check_item(self, item: _Item, kind: str) -> None:
        """Hash and size requirements of one file.

        No SHA256-or-stronger hash: ``ToolError`` naming the file. No valid
        (non-negative) size: a per-file error for a binary package, a
        ``ToolError`` for a source set.
        """
        name = _shown(item.spec.name)
        strong = {}
        for alg, digest in item.spec.hashes.items():
            try:
                if not is_weak(alg):
                    strong[alg] = digest
            except ChecksumError:
                continue
        if not strong:
            raise ToolError(
                f"{name}: no SHA256 or stronger hash listed in the repository index; "
                "refusing to download it"
            )
        item.algorithm, item.digest = next(iter(strong.items()))
        size = item.spec.size
        if size is None or size < 0:
            msg = f"{name}: no valid size listed in the repository index; not downloaded"
            if kind == "source":
                raise ToolError(f"{msg}; nothing of the source package was downloaded")
            item.fail(msg)
            return
        item.size = size

    def _binary_plans(
        self,
        client: RepoClient,
        args: dict[str, object],
        suite: str,
        name: str,
        version: str | None,
    ) -> tuple[list[DownloadPlan], list[str] | None, list[str]]:
        """``(plans, requested architectures, architectures without a candidate)``."""
        arches = _opt_list(args, "architectures", MAX_ARCHES)
        comps = _opt_list(args, "components", MAX_COMPONENTS)
        plans = client.plan_binary(
            suite, name, version=version, architectures=arches, components=comps
        )
        what = f"{_shown(suite)}: binary package {_shown(name)}"
        if version is not None:
            what += f" version {_shown(version)}"
        if not plans:
            if arches:
                what += f" for {', '.join(_shown(a) for a in arches)}"
            raise ToolError(f"{what} not found")
        if arches is None and len(plans) > 1:
            listed = list(dict.fromkeys(p.architecture or "?" for p in plans))
            comps_seen = list(dict.fromkeys(p.component for p in plans))
            extra = ""
            if len(comps_seen) > 1:
                extra = f" in components {', '.join(_shown(c) for c in comps_seen)}"
            raise ToolError(
                f"{what}: several candidates for architectures "
                f"{', '.join(_shown(a) for a in listed)}{extra}; pass 'architectures' "
                "(one or several of them) to choose"
            )
        missing: list[str] = []
        if arches is not None:
            have = {p.architecture for p in plans}
            missing = [a for a in arches if a not in have and (a == "all" or "all" not in have)]
        return plans, arches, missing

    # -- one file ---------------------------------------------------------------------

    def _existing(self, call: ToolCall, target: WriteRoot, item: _Item) -> None:
        """Mark *item* ``already_present`` or ``error`` when its name exists in *target*.

        Nothing is followed: a symlink (or anything but a regular file) is an
        error. A file of another size is different without being hashed;
        hashing stops when the call is cancelled or past its deadline.
        """
        name = item.spec.name
        shown = _shown(name)
        try:
            info = os.stat(name, dir_fd=target.fileno(), follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            item.fail(f"{shown} exists and is not a regular file (a symlink?); left untouched")
            return
        if info.st_size != item.size:
            item.fail(f"a different file {shown} exists already; left untouched")
            return
        try:
            actual = target.hash_existing(
                name, item.algorithm, check=lambda: self._checkpoint(call)
            )
        except WriteRootError:
            item.fail(f"{shown} exists and is not a regular file (a symlink?); left untouched")
            return
        except OSError as exc:
            item.fail(f"{shown} exists and cannot be read ({error_text(exc)}); left untouched")
            return
        if actual == item.digest.lower():
            item.status = "already_present"
            item.hashes = {item.algorithm: actual}
        else:
            item.fail(f"a different file {shown} exists already; left untouched")

    def _fetch(
        self, call: ToolCall, target: WriteRoot, item: _Item, keep: bool = False
    ) -> tuple[NewFile, bytes | None]:
        """Download *item* under a temporary name, verified; the new file (not published)."""
        self._checkpoint(call)
        new = target.create_file(item.spec.name, size_hint=item.size)
        try:
            tee = _Tee(new.file) if keep else None
            out = cast(BinaryIO, tee) if tee is not None else new.file
            result = self.manager.fetcher.download(
                item.spec.url,
                out,
                max_bytes=item.size,
                expected_size=item.size,
                expected_hashes={item.algorithm: item.digest},
                token=call.token,
                deadline=call.deadline,
            )
        except BaseException:
            new.abort()
            raise
        item.hashes = dict(result.digests)
        return new, None if tee is None else tee.copy.getvalue()

    def _appeared(self, call: ToolCall, target: WriteRoot, item: _Item) -> None:
        """The final name was taken while downloading: identical is ``already_present``."""
        item.status = "pending"
        self._existing(call, target, item)
        if item.status == "pending":  # gone again meanwhile
            item.fail(f"{_shown(item.spec.name)} appeared while downloading; left untouched")

    def _publish_one(self, call: ToolCall, target: WriteRoot, item: _Item, new: NewFile) -> None:
        """Publish a verified binary package file (never replacing anything).

        Failures are per-file errors (the temporary file is removed by ``commit``).
        """
        try:
            new.commit()
        except FileExistsError:
            self._appeared(call, target, item)
            return
        except OSError as exc:
            item.fail(f"{_shown(item.spec.name)}: cannot publish: {error_text(exc)}")
            return
        item.status = "downloaded"

    # -- the tool ---------------------------------------------------------------------

    def download(self, call: ToolCall) -> dict[str, object]:
        args = call.arguments
        self._checkpoint(call)
        req_str(args, "suite")
        req_str(args, "name")
        version = opt_str(args, "version")
        if version is not None:
            try:
                Version(version)
            except (ValueError, MinidebError) as exc:
                raise ToolError(f'"version": {error_text(exc)}') from None
        kind = opt_str(args, "kind") or "binary"
        temp = (opt_str(args, "destination") or "base") == "temp"
        sub_text = opt_str(args, "subdirectory")
        parts = _subdirectory(sub_text)
        if kind == "source" and args.get("architectures"):
            raise ToolError('"architectures" applies to kind binary only')
        # the write root first: an unusable one costs no repository request
        with self._open_root(temp) as root:
            return self._download(call, root, kind, temp, parts)

    def _download(
        self, call: ToolCall, root: WriteRoot, kind: str, temp: bool, parts: list[str]
    ) -> dict[str, object]:
        args = call.arguments
        suite, name = req_str(args, "suite"), req_str(args, "name")
        version = opt_str(args, "version")
        repo = self.manager.resolve(opt_str(args, "repository"))
        client = repo.client
        mark = client.problems_mark()
        requested: list[str] | None = None
        missing: list[str] = []
        if kind == "source":
            comps = _opt_list(args, "components", MAX_COMPONENTS)
            plan = client.plan_source(suite, name, version=version, components=comps)
            plans = [plan]
        else:
            plans, requested, missing = self._binary_plans(client, args, suite, name, version)
        items = [_Item(spec, p) for p in plans for spec in p.files]
        for item in items:
            try:
                validate_file_name(item.spec.name)
            except UnsafeNameError:
                msg = (
                    f"skipped: unsafe file name {_shown(item.spec.name)!r} in the repository "
                    "index (not written)"
                )
                if kind == "source":
                    raise ToolError(
                        f"{_shown(name)}: {msg}; nothing of the source package was downloaded"
                    ) from None
                item.fail(msg)
        for item in [i for i in items if i.status == "pending"]:
            self._check_item(item, kind)
        usable = [i for i in items if i.status == "pending"]
        total = sum(i.size for i in usable)
        if total > self.config.max_download_bytes:
            raise ToolError(
                f"the files to download total {total} bytes, over the download limit of "
                f"{self.config.max_download_bytes} bytes"
            )
        self._checkpoint(call)
        with self._target(root, parts) as (target, removed):
            for item in usable:
                self._existing(call, target, item)
            todo = [i for i in usable if i.status == "pending"]
            if kind == "source":
                self._source_set(call, target, plans[0], items, todo)
            else:
                self._binaries(call, target, todo)
            files = [self._render(root, target, parts, i) for i in items]
            out: dict[str, object] = {
                "repository": repo.label,
                "suite": _shown(suite),
                "name": _shown(name),
                "kind": kind,
                "destination": "temp" if temp else "base",
                "root": clean_text(root.path, FIELD_LIMIT),
                "directory": clean_text(target.path, FIELD_LIMIT),
                "subdirectory": None if not parts else clean_text("/".join(parts), FIELD_LIMIT),
                "temp_directory_used": temp,
            }
        if kind == "source":
            out["version"] = _shown(plans[0].version)
        if temp:
            out["note"] = _TEMP_NOTE
        if requested is not None:
            out["requested_architectures"] = [_shown(a) for a in requested]
            out["not_found_architectures"] = [_shown(a) for a in missing]
        errors = [i for i in items if i.status == "error"]
        if errors and len(errors) == len(items):
            quoted = "; ".join(i.message or "" for i in errors[:_MAX_QUOTED])
            more = f" (and {len(errors) - _MAX_QUOTED} more)" if len(errors) > _MAX_QUOTED else ""
            raise ToolError(f"{quoted}{more}")
        out["complete"] = not errors and not missing
        out["files"] = files
        out["stale_temp_files_removed"] = removed
        found = client.problems_since(mark)
        problems = [clean_text(redact_url(p), _PROBLEM_LIMIT) for p in found[:_MAX_PROBLEMS]]
        if len(found) > _MAX_PROBLEMS:
            problems.append(f"…[{len(found) - _MAX_PROBLEMS} more problems]")
        out["problems"] = problems
        return out

    def _render(
        self, root: WriteRoot, target: WriteRoot, parts: list[str], item: _Item
    ) -> dict[str, object]:
        plan = item.plan
        out: dict[str, object] = {
            "name": _shown(item.spec.name),
            "status": item.status,
        }
        if plan.kind == "binary":
            out["architecture"] = _shown(plan.architecture or "")
            out["version"] = _shown(plan.version)
        out["component"] = _shown(plan.component)
        if item.status != "error":
            rel = "/".join([*parts, item.spec.name])
            out["path"] = clean_text(rel, FIELD_LIMIT)
            out["absolute_path"] = clean_text(
                os.path.join(target.path, item.spec.name), FIELD_LIMIT
            )
            out["size"] = item.size
            out["hashes"] = {k: clean_text(v, 200) for k, v in sorted(item.hashes.items())}
        else:
            out["message"] = clean_text(item.message or "error", _PROBLEM_LIMIT)
        try:
            out["url"] = clean_text(redact_url(item.spec.url), FIELD_LIMIT)
        except ValueError:
            out["url"] = None
        return out

    # -- binaries: each file on its own ----------------------------------------------------

    def _binaries(self, call: ToolCall, target: WriteRoot, todo: list[_Item]) -> None:
        self._space(target, todo)
        for item in todo:
            try:
                new, _ = self._fetch(call, target, item)
            except (FetchCancelled, FetchTimeout, ToolError):
                raise
            except (MinidebError, OSError, ValueError) as exc:
                item.fail(error_text(exc))
                continue
            self._publish_one(call, target, item, new)

    @staticmethod
    def _space(target: WriteRoot, todo: list[_Item]) -> None:
        need = sum(i.size for i in todo)
        free = target.free_bytes()
        if need > free:
            raise ToolError(
                f"{clean_text(target.path, FIELD_LIMIT)}: not enough free space "
                f"({need} bytes needed, {free} available)"
            )

    # -- sources: all or none ------------------------------------------------------------

    def _source_set(
        self,
        call: ToolCall,
        target: WriteRoot,
        plan: DownloadPlan,
        items: list[_Item],
        todo: list[_Item],
    ) -> None:
        conflicts = [i for i in items if i.status == "error"]
        if conflicts:
            raise ToolError(
                "; ".join(i.message or "" for i in conflicts[:_MAX_QUOTED])
                + "; nothing of the source package was downloaded"
            )
        dsc_item = items[0]
        if dsc_item.size > MAX_DSC_SIZE:
            raise ToolError(f"{_shown(dsc_item.spec.name)}: larger than {MAX_DSC_SIZE} bytes")
        self._space(target, todo)
        staged: list[tuple[_Item, NewFile]] = []
        published: list[tuple[str, tuple[int, int]]] = []
        try:
            dsc_data: bytes | None = None
            for item in todo:
                new, copy = self._fetch(call, target, item, keep=item is dsc_item)
                staged.append((item, new))
                if copy is not None:
                    dsc_data = copy
            if dsc_data is None:  # already present: read the verified file back
                dsc_data = _read_small(target, dsc_item.spec.name, MAX_DSC_SIZE)
                if dsc_data is None or _digest(dsc_item.algorithm, dsc_data) != (
                    dsc_item.digest.lower()
                ):
                    raise ToolError(f"{_shown(dsc_item.spec.name)} changed while downloading")
            _cross_check(plan, items, dsc_data)
            self._checkpoint(call)
            for item, new in staged:
                try:
                    new.commit()
                except FileExistsError:
                    # identical content is fine (and is never removed on rollback)
                    self._appeared(call, target, item)
                    if item.status == "already_present":
                        continue
                    raise ToolError(
                        f"{item.message}; nothing of the source package was published "
                        "(files published by this call were removed)"
                    ) from None
                except OSError as exc:
                    raise ToolError(
                        f"{_shown(item.spec.name)}: cannot publish: {error_text(exc)}; nothing "
                        "of the source package was published (files published by this call "
                        "were removed)"
                    ) from None
                published.append((item.spec.name, new.identity))
                item.status = "downloaded"
        except BaseException:
            for _, new in staged:
                with contextlib.suppress(OSError):
                    new.abort()
            _unpublish(target, published)
            raise


def _digest(algorithm: str, data: bytes) -> str:
    return hashlib.new(algorithm, data, usedforsecurity=False).hexdigest()


def _read_small(target: WriteRoot, name: str, limit: int) -> bytes | None:
    """The regular file *name* of *target* (not following symlinks), None if unusable."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=target.fileno())
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        os.set_blocking(fd, True)
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, 1 << 20):
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _unpublish(target: WriteRoot, published: list[tuple[str, tuple[int, int]]]) -> None:
    """Remove the names this call published, if they are still the files it published."""
    fd = target.fileno()
    for name, ident in reversed(published):
        with contextlib.suppress(OSError):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == ident:
                os.unlink(name, dir_fd=fd)


#: digests that do not count as a confirmation of a ``.dsc`` entry
_WEAK_HASHES = frozenset({"md5", "sha1"})


def _cross_check(plan: DownloadPlan, items: list[_Item], data: bytes) -> None:
    """The ``.dsc`` must list exactly the other files of the set, with the same sizes and hashes."""
    dsc_name = _shown(items[0].spec.name)
    try:
        dsc = Dsc.parse(data)
    except (MinidebError, ValueError):
        raise ToolError(f"{dsc_name}: not a valid .dsc; nothing was published") from None
    fail = f"{dsc_name} does not match the Sources index"
    if dsc.source != plan.name:
        raise ToolError(f"{fail} (Source field); nothing was published")
    try:
        same_version = Version(str(dsc.version)) == Version(plan.version)
    except (ValueError, MinidebError):
        same_version = False
    if not same_version:
        raise ToolError(f"{fail} (Version field); nothing was published")
    listed = {f.name: f for f in dsc.files}
    others = {i.spec.name: i for i in items[1:]}
    if set(listed) != set(others):
        raise ToolError(f"{fail} (the .dsc lists other files); nothing was published")
    for fname, entry in listed.items():
        item = others[fname]
        if entry.size != item.size:
            raise ToolError(f"{fail} (size of {_shown(fname)}); nothing was published")
        mine = {item.algorithm: item.digest.lower(), **item.hashes}
        strong_match = False
        for alg, digest in entry.hashes().items():
            if alg not in mine:
                continue
            if mine[alg] != digest.lower():
                raise ToolError(f"{fail} ({alg} of {_shown(fname)}); nothing was published")
            strong_match = strong_match or alg not in _WEAK_HASHES
        if not strong_match:
            # a .dsc whose digests we cannot compare (md5/sha1 only, or an algorithm the
            # index did not list) proves nothing about its claims
            raise ToolError(
                f"{fail} (no SHA256 or stronger hash of {_shown(fname)} to compare); "
                "nothing was published"
            )


def download_tools(manager: RepoManager) -> list[Tool]:
    """The download tool over *manager* (empty when downloads are disabled)."""
    if not manager.config.allow_download:
        return []
    tools = DownloadTools(manager)
    return [
        Tool(
            name="download_package",
            description=(
                "Download a binary package (.deb) or a source package (the .dsc and every file "
                "it lists) into the base download directory (destination 'base') or the "
                "private per-session temporary directory ('temp', mode 0700, kept after the "
                "server exits), optionally below a relative subdirectory. Every file needs a "
                "size and a SHA256 or stronger hash in the repository index and is verified "
                "before it appears under its final name. Nothing is overwritten: an identical "
                "existing file is reported already_present, a different one is an error and "
                "left untouched. Binary: when several architectures offer the package, pass "
                "architectures (each one is downloaded and reported separately); a single "
                "candidate (such as an Architecture: all package) needs none. Source packages "
                "are published all-or-none. The total size is capped by the server's download "
                f"limit. Returns the paths (relative and absolute), sizes and hashes. {UNTRUSTED}"
            ),
            input_schema=schema(
                ("suite", "name"),
                repository=string(
                    "A configured repository name (repo1, ...) or an http://, https:// or "
                    "file:/// URL of an APT repository. Optional when exactly one repository "
                    "is configured.",
                    2048,
                ),
                suite=string("Suite or codename, e.g. stable, trixie, noble-updates."),
                name=string("Exact package name (binary, or source for kind source)."),
                version=string("Exact version (default: the highest).", 200),
                kind={
                    "type": "string",
                    "enum": ["binary", "source"],
                    "default": "binary",
                    "description": "A binary package (.deb) or a source package (.dsc set).",
                },
                architectures=strings(
                    f"Binary only: download for each of these architectures (at most "
                    f"{MAX_ARCHES}, e.g. amd64, arm64); Architecture: all packages cover every "
                    "architecture. Required when several architectures offer the package."
                ),
                components=strings("Only look in these components (e.g. main)."),
                destination={
                    "type": "string",
                    "enum": ["base", "temp"],
                    "default": "base",
                    "description": "'base': the download directory of the server; 'temp': "
                    "the private per-session temporary directory.",
                },
                subdirectory={
                    **string(
                        "Relative subdirectory of the destination (created as needed), e.g. "
                        "debs/trixie; no '..', no leading '/' or '.', no symbolic links. "
                        "Empty: the destination itself.",
                        1024,
                    ),
                    "minLength": 0,
                },
            ),
            handler=tools.wrap(tools.download),
            read_only=False,
            open_world=True,
            idempotent=True,
            destructive=False,
            timeout=manager.config.download_timeout,
        )
    ]
