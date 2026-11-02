# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Command line interface: ``minideb`` (also ``python -m minideblib``).

Repository commands query APT repositories given with ``--url`` or
``--sources``; ``deb`` commands inspect ``.deb`` files and ``dsc`` commands ``.dsc`` source
package files (their OpenPGP signature is never verified); ``mcp`` runs an MCP
server for AI agents (:mod:`minideblib.mcp`).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minideblib import MinidebError, __version__, sources
from minideblib.compress import DEFAULT_MAX_SIZE
from minideblib.deb import DebError, DebFile, DebPackage
from minideblib.dsc import Dsc, DscFile
from minideblib.repo import dep11
from minideblib.repo.client import PackageRecord, RepoClient, SourceRecord
from minideblib.repo.fetch import Fetcher, FetchError, redact_url
from minideblib.repo.release import icon_size
from minideblib.sources import SourceEntry
from minideblib.version import Version

_UNAUTH = (
    "The Release/InRelease signature is not verified by this tool; only the file hashes "
    "listed in Release are checked, so repository metadata is unauthenticated."
)

EPILOG = f"""\
examples:
  minideb --url http://deb.debian.org/debian components stable
  minideb --url http://archive.ubuntu.com/ubuntu list noble -c main -a amd64
  minideb --sources /etc/apt/sources.list.d suites
  minideb --url http://deb.debian.org/debian appstream stable -c main -a amd64 --search editor
  minideb --url http://archive.ubuntu.com/ubuntu icons noble -c main
  minideb --url http://deb.debian.org/debian source stable hello
  minideb deb info package.deb
  minideb dsc verify hello_2.10-3.dsc

Global options (-v, -q, -j, --url, --sources, -c, -a, --no-verify, --timeout, --retries)
may be given before or after the command; a value given after the command wins, -v counts
add up, and --url, -c and -a given after the command extend the lists given before it.
-c/--component and -a/--arch scope what is downloaded (repeatable). The own -c/-a
options of list (-c, -a), files (-a), appstream (-c, -a) and icons (-c) are repeatable
too and replace the global ones for that command.
Logs go to stderr only (-v: steps, -vv: requests and details with elapsed time).

exit status: 0 success, 1 nothing found, 2 usage, network or data error.
  1 (nothing on stdout, also with --json) is used by show, files, versions, source and
  suites with an empty result, appstream --id/--package matching nothing, and deb field
  with a missing field (without --json it still prints the fields found);
  list and search exit with 0 on an empty result.

note: {_UNAUTH}
"""

EXIT_OK, EXIT_NOT_FOUND, EXIT_ERROR = 0, 1, 2


class NotFound(Exception):
    """Raised by a command when the asked-for item does not exist."""


class Ctx:
    """Parsed global options plus the clients created for the command."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.clients: list[RepoClient] = []

    def emit(self, doc: Any) -> None:
        """Write the single JSON document to stdout."""
        print(json.dumps(doc, indent=2, ensure_ascii=False))

    def client(
        self,
        hints: Sequence[str] = (),
        components: Sequence[str] | None = None,
        architectures: Sequence[str] | None = None,
    ) -> RepoClient:
        """Client for ``--url`` (one or more) or ``--sources`` / the system lists.

        *hints* are suite names that may be queried even if the sources do not list them.
        *components*/*architectures* (the per-command ``-c``/``-a`` values), when given and
        not empty, replace the global scope for this client.
        """
        a = self.args
        fetcher = Fetcher(timeout=a.timeout, retries=a.retries)
        hints = list(dict.fromkeys(hints))
        kw: dict[str, Any] = {
            "fetcher": fetcher,
            "verify": a.verify,
            "components": list(dict.fromkeys(components or a.components)),
            "architectures": list(dict.fromkeys(architectures or a.architectures)),
        }
        if a.url and len(a.url) == 1:
            probe = getattr(a, "probe", "all")
            client = RepoClient.from_url(a.url[0], suites=hints, probe=probe, **kw)
        elif a.url:
            entries = [SourceEntry("deb", u, s, ()) for u in a.url for s in hints]
            client = RepoClient(entries, **kw)
        else:
            entries = sources.load(Path(a.sources) if a.sources else None)
            client = RepoClient(entries, **kw)
        self.clients.append(client)
        return client


# -- formatting ---------------------------------------------------------------


def _record_dict(rec: PackageRecord | SourceRecord) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": rec.name,
        "version": str(rec.version),
        "arch": rec.arch,
        "component": rec.component,
        "suite": rec.suite,
    }
    if isinstance(rec, PackageRecord):
        d.update(filename=rec.filename or None, size=rec.size, sha256=rec.sha256)
        d["description"] = rec.description
    else:
        d.update(directory=rec.directory, binaries=rec.binaries)
    return d


def _is_all(rec: PackageRecord | SourceRecord) -> bool:
    return isinstance(rec, PackageRecord) and rec.paragraph.get("Architecture") == "all"


def _record_line(rec: PackageRecord | SourceRecord) -> str:
    arch = "all" if _is_all(rec) else rec.arch
    return f"{rec.name} {rec.version} {arch} {rec.component}"


def _print_records(ctx: Ctx, recs: Sequence[PackageRecord | SourceRecord], dedupe: bool) -> None:
    if ctx.args.json:
        ctx.emit([_record_dict(r) for r in recs])
        return
    seen: set[tuple[str, str]] = set()
    for rec in recs:
        key = (rec.name, str(rec.version))
        if dedupe and _is_all(rec):
            if key in seen:
                continue
            seen.add(key)
        print(_record_line(rec))


def _mode_string(f: DebFile) -> str:
    kind = {"d": "d", "l": "l", "h": "h", "f": "-"}.get(f.type, "?")
    bits = ""
    for shift in (6, 3, 0):
        m = (f.mode >> shift) & 7
        bits += ("r" if m & 4 else "-") + ("w" if m & 2 else "-") + ("x" if m & 1 else "-")
    return kind + bits


def _owner(uid: int, gid: int) -> str:
    return f"{'root' if uid == 0 else uid}/{'root' if gid == 0 else gid}"


def format_deb_file(f: DebFile) -> str:
    """One ``dpkg-deb -c`` style line."""
    when = datetime.fromtimestamp(f.mtime, UTC).strftime("%Y-%m-%d %H:%M")
    path = f.path + "/" if f.type == "d" and not f.path.endswith("/") else f.path
    line = f"{_mode_string(f)} {_owner(f.uid, f.gid)} {f.size} {when} {path}"
    if f.type == "l":
        line += f" -> {f.linkname}"
    elif f.type == "h":
        line += f" link to {f.linkname}"
    return line


# -- repository commands ------------------------------------------------------


def cmd_suites(ctx: Ctx) -> int:
    found = ctx.client(ctx.args.suite or ()).suites()
    if not found:
        raise NotFound("no suites found")
    if ctx.args.json:
        ctx.emit(found)
    else:
        print("\n".join(found))
    return EXIT_OK


def _simple_list(ctx: Ctx, items: list[str]) -> int:
    if ctx.args.json:
        ctx.emit(items)
    else:
        print("\n".join(items))
    return EXIT_OK


def cmd_components(ctx: Ctx) -> int:
    return _simple_list(ctx, ctx.client([ctx.args.suite]).components(ctx.args.suite))


def cmd_archs(ctx: Ctx) -> int:
    return _simple_list(ctx, ctx.client([ctx.args.suite]).architectures(ctx.args.suite))


def cmd_list(ctx: Ctx) -> int:
    a = ctx.args
    client = ctx.client([a.suite], a.component, a.arch)
    if a.source:
        recs: list[PackageRecord | SourceRecord] = list(client.sources(a.suite))
    else:
        recs = list(client.packages(a.suite))
    _print_records(ctx, recs, dedupe=len(client.scope_architectures) != 1)
    return EXIT_OK


def cmd_show(ctx: Ctx) -> int:
    a = ctx.args
    recs = [r for r in ctx.client([a.suite]).packages(a.suite) if r.name == a.package]
    if not recs:
        raise NotFound(f"package {a.package!r} not found in {a.suite}")
    if a.json:
        docs = []
        for r in recs:
            doc: dict[str, Any] = dict(r.paragraph.items())
            try:
                url: str | None = r.url
            except ValueError:
                url = None
            doc.update(suite=r.suite, component=r.component, arch=r.arch, url=url)
            docs.append(doc)
        ctx.emit(docs)
    else:
        print("\n".join(r.paragraph.dump() for r in recs), end="")
    return EXIT_OK


def cmd_versions(ctx: Ctx) -> int:
    a = ctx.args
    recs = ctx.client(a.suite or ()).versions(a.package)
    if not recs:
        raise NotFound(f"package {a.package!r} not found in any suite")
    if a.json:
        ctx.emit([_record_dict(r) for r in recs])
    else:
        for r in recs:
            print(f"{r.suite} {_record_line(r)}")
    return EXIT_OK


def cmd_search(ctx: Ctx) -> int:
    a = ctx.args
    _print_records(ctx, ctx.client([a.suite]).search(a.suite, a.pattern), dedupe=True)
    return EXIT_OK


def cmd_files(ctx: Ctx) -> int:
    a = ctx.args
    found = ctx.client([a.suite], architectures=a.arch).find_files(a.suite, a.path)
    if not found:
        raise NotFound(f"{a.path!r} not found in Contents of {a.suite}")
    if a.json:
        ctx.emit([{"path": p, "package": pkg} for p, pkg in found])
    else:
        for p, pkg in found:
            print(f"{p}  {pkg}")
    return EXIT_OK


def _appstream_match(summary: dep11.Dep11Summary, a: argparse.Namespace) -> bool:
    if a.id is not None:
        return summary.id == a.id
    if a.package is not None:
        return summary.package == a.package
    if a.search is not None:
        text = a.search.lower()
        fields = (summary.id, summary.package, summary.name, summary.summary)
        return any(text in f.lower() for f in fields)
    return True


def cmd_appstream(ctx: Ctx) -> int:
    a = ctx.args
    dep11.require_yaml()  # fail fast, before any download
    docs = ctx.client([a.suite], a.component, a.arch).appstream(a.suite)
    found = []
    for doc in docs:
        summary = dep11.summarize(doc, a.locale)
        if _appstream_match(summary, a):
            found.append((doc, summary))
    if not found and (a.id is not None or a.package is not None):
        what = f"id {a.id!r}" if a.id is not None else f"package {a.package!r}"
        raise NotFound(f"no AppStream component with {what} in {a.suite}")
    if a.json:
        ctx.emit([doc for doc, _ in found])
    else:  # a component listed for several architectures is printed once
        lines = (f"{s.id}  {s.package}  {s.name}  {s.summary}" for _, s in found)
        for line in dict.fromkeys(lines):
            print(line)
    return EXIT_OK


def cmd_icons(ctx: Ctx) -> int:
    a = ctx.args
    found = ctx.client([a.suite], a.component).icon_indices(a.suite)
    if a.json:
        ctx.emit(
            [
                {
                    "path": i.path,
                    "component": i.path.split("/dep11/", 1)[0],
                    "icon_size": icon_size(i.path),
                    "bytes": i.size,
                }
                for i in found
            ]
        )
    else:
        for i in found:
            print(f"{i.path}  {icon_size(i.path)}  {i.size}")
    return EXIT_OK


def _newest_source(ctx: Ctx, client: RepoClient) -> SourceRecord:
    a = ctx.args
    recs = [r for r in client.sources(a.suite) if r.name == a.package]
    if a.want_version is not None:
        wanted = Version(a.want_version)
        recs = [r for r in recs if r.version == wanted]
    if not recs:
        which = f" version {a.want_version}" if a.want_version is not None else ""
        raise NotFound(f"source package {a.package!r}{which} not found in {a.suite}")
    return max(recs, key=lambda r: r.version)


def cmd_source(ctx: Ctx) -> int:
    client = ctx.client([ctx.args.suite])  # one client: Release is fetched once
    rec = _newest_source(ctx, client)
    dsc = client.fetch_dsc(rec)
    _print_dsc(ctx, dsc, extra={"suite": rec.suite, "component": rec.component, "url": rec.dsc_url})
    return EXIT_OK


# -- dsc commands -------------------------------------------------------------


def _dsc_file_dict(f: DscFile) -> dict[str, Any]:
    return {
        "name": f.name,
        "size": f.size,
        "md5": f.md5,
        "sha1": f.sha1,
        "sha256": f.sha256,
        "sha512": f.sha512,
    }


def _dsc_dict(dsc: Dsc) -> dict[str, Any]:
    return {
        "source": dsc.source,
        "version": str(dsc.version),
        "format": dsc.format,
        "is_native": dsc.is_native,
        "binaries": dsc.binaries,
        "architectures": dsc.architectures,
        "maintainer": dsc.maintainer,
        "uploaders": dsc.uploaders,
        "standards_version": dsc.standards_version,
        "homepage": dsc.homepage,
        "vcs": dsc.vcs,
        "build_depends": dsc.build_depends,
        "package_list": [
            {"name": p.name, "type": p.type, "section": p.section, "priority": p.priority}
            | {"extra": p.extra}
            for p in dsc.package_list
        ],
        "files": [_dsc_file_dict(f) for f in dsc.files],
    }


def _print_dsc(ctx: Ctx, dsc: Dsc, extra: dict[str, Any] | None = None) -> None:
    if ctx.args.json:
        ctx.emit(_dsc_dict(dsc) | (extra or {}))
        return
    rows: list[tuple[str, str | None]] = [
        ("Source", dsc.source),
        ("Version", str(dsc.version)),
        ("Format", dsc.format + (" [native]" if dsc.is_native else "")),
        ("Binary", ", ".join(dsc.binaries)),
        ("Architecture", " ".join(dsc.architectures)),
        ("Maintainer", dsc.maintainer),
        ("Uploaders", "; ".join(dsc.uploaders)),
        ("Standards-Version", dsc.standards_version),
        ("Homepage", dsc.homepage),
    ]
    rows += [(f"Vcs-{k.capitalize()}", v) for k, v in dsc.vcs.items()]
    rows += list(dsc.build_depends.items())
    for name, value in rows:
        if value:
            print(f"{name}: {value}")
    print("Files:")
    for f in dsc.files:
        print(f"  {f.name} ({f.size} bytes)")


def _print_dsc_files(ctx: Ctx, dsc: Dsc) -> None:
    if ctx.args.json:
        ctx.emit([_dsc_file_dict(f) for f in dsc.files])
        return
    for f in dsc.files:
        hashes = " ".join(f"{alg}:{value}" for alg, value in f.hashes().items())
        print(f"{f.name} {f.size} {hashes}")


def cmd_dsc_info(ctx: Ctx) -> int:
    _print_dsc(ctx, Dsc.load(ctx.args.file))
    return EXIT_OK


def cmd_dsc_files(ctx: Ctx) -> int:
    _print_dsc_files(ctx, Dsc.load(ctx.args.file))
    return EXIT_OK


def cmd_dsc_verify(ctx: Ctx) -> int:
    a = ctx.args
    dsc = Dsc.load(a.file)
    dsc.verify(a.dir if a.dir is not None else Path(a.file).resolve().parent)
    if a.json:
        ctx.emit({"verified": True, "files": len(dsc.files)})
    else:
        print(f"verified {len(dsc.files)} files")
    return EXIT_OK


# -- deb commands -------------------------------------------------------------


def _open_deb(ctx: Ctx) -> DebPackage:
    return DebPackage.open(ctx.args.file, max_size=getattr(ctx.args, "max_size", DEFAULT_MAX_SIZE))


def cmd_deb_info(ctx: Ctx) -> int:
    control = _open_deb(ctx).control
    if ctx.args.json:
        ctx.emit(dict(control.items()))
    else:
        print(control.dump(), end="")
    return EXIT_OK


def cmd_deb_field(ctx: Ctx) -> int:
    control = _open_deb(ctx).control
    wanted = ctx.args.fields
    if not wanted:
        return cmd_deb_info(ctx)
    names = {n.lower(): n for n in control.fields_in_order()}
    found = {names[f.lower()]: control[f] for f in wanted if f.lower() in names}
    if len(found) < len(wanted):
        missing = [f for f in wanted if f.lower() not in names]
        if ctx.args.json:
            raise NotFound(f"no such field: {', '.join(missing)}")  # JSON: nothing on stdout
    if ctx.args.json:
        ctx.emit(found)
    elif len(wanted) == 1:
        if found:
            print(*found.values())
    else:
        for name, value in found.items():
            print(f"{name}: {value}")
    if len(found) < len(wanted):
        missing = [f for f in wanted if f.lower() not in names]
        raise NotFound(f"no such field: {', '.join(missing)}")
    return EXIT_OK


def cmd_deb_ls(ctx: Ctx) -> int:
    files = _open_deb(ctx).files()
    if ctx.args.json:
        ctx.emit(
            [
                {k: getattr(f, k) for k in ("path", "size", "mode", "uid", "gid", "mtime")}
                | {"type": f.type, "linkname": f.linkname or None}
                for f in files
            ]
        )
    else:
        for f in files:
            print(format_deb_file(f))
    return EXIT_OK


def _dest_dir(arg: str) -> Path:
    """The destination directory of ``deb ctrl``/``deb extract``; a symlink is refused.

    Only the final component is checked: the path leading to it is the
    caller's (symlinked ancestors are followed, missing ones created).
    """
    dest = Path(arg)
    if dest.is_symlink():
        raise DebError(f"destination {arg} is a symlink")
    if dest.exists() and not dest.is_dir():
        raise DebError(f"destination {arg} is not a directory")
    return dest


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_DEST_HELP = (
    "destination directory, created with missing parents; DIR itself must not be a "
    "symlink, but symlinks in the directories leading to it are followed"
)


def _open_subdir(parent_fd: int, name: str, shown: str) -> int:
    """Open (creating it if missing) directory *name* below *parent_fd*, never via a symlink."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o777, dir_fd=parent_fd)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as e:
        raise DebError(f"refusing to write below {shown}: a symlink or not a directory") from e


def _write_new_file(dir_fd: int, name: str, data: bytes, shown: str) -> None:
    """Write *name* in *dir_fd* without following a symlink or writing through a hard link.

    A symlink or other non-regular entry is refused; an existing regular file is
    replaced (a new file is renamed over it, so another link to it keeps its data).
    """
    with contextlib.suppress(FileNotFoundError):
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            kind = "a symlink" if stat.S_ISLNK(st.st_mode) else "not a regular file"
            raise DebError(f"refusing to overwrite {shown}: {kind}")
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def cmd_deb_ctrl(ctx: Ctx) -> int:
    pkg = _open_deb(ctx)
    dest = _dest_dir(ctx.args.dir)
    names = []
    for name in pkg.control_files:
        if name.startswith("/") or ".." in Path(name).parts:
            raise DebError(f"unsafe control file name: {name!r}")
        names.append(name)
    dest.mkdir(parents=True, exist_ok=True)
    # every directory below DEST is opened relative to its parent without following symlinks
    try:
        top = os.open(dest, _DIR_FLAGS)  # O_NOFOLLOW: DEST must not have become a symlink
    except OSError as e:
        raise DebError(f"destination {dest} is a symlink or not a directory") from e
    try:
        for name in names:
            *dirs, base = name.split("/")
            fd = os.dup(top)
            try:
                for i, part in enumerate(dirs):
                    sub = _open_subdir(fd, part, str(dest.joinpath(*dirs[: i + 1])))
                    os.close(fd)
                    fd = sub
                _write_new_file(fd, base, pkg.control_files[name], str(dest / name))
            finally:
                os.close(fd)
    finally:
        os.close(top)
    if ctx.args.json:
        ctx.emit(names)
    return EXIT_OK


def cmd_deb_extract(ctx: Ctx) -> int:
    pkg = _open_deb(ctx)
    pkg.extract(
        _dest_dir(ctx.args.dir),
        max_size=ctx.args.max_size,
        skip_absolute_symlinks=ctx.args.skip_absolute_symlinks,
    )
    return EXIT_OK


# -- parser -------------------------------------------------------------------


#: list-valued global options: values given after the command extend those before it
_LIST_OPTIONS = ("url", "components", "architectures")


def _add_global_options(p: argparse.ArgumentParser, top: bool, own: Sequence[str] = ()) -> None:
    """Add the global options to *p*.

    On subcommand parsers (``top`` false) the defaults are suppressed, so an option
    that is not repeated after the command never overrides the value given before it;
    list options are collected under ``<dest>_after`` and appended by :func:`parse_args`.
    *own* names the options (``"-c"``, ``"-a"``) the command defines itself.
    """
    skip: Any = None if top else argparse.SUPPRESS

    def d(value: Any) -> Any:
        return value if top else argparse.SUPPRESS

    def dest(name: str) -> str:
        return name if top else f"{name}_after"

    g = p.add_argument_group("global options" if top else "global options (also valid here)")
    g.add_argument(
        "--url",
        action="append",
        metavar="URL",
        dest=dest("url"),
        default=skip,
        help=f"repository base URL (repeatable). {_UNAUTH}",
    )
    if "-c" not in own:
        g.add_argument(
            "-c",
            "--component",
            action="append",
            dest=dest("components"),
            metavar="COMP",
            default=d([]),
            help="only read this component (repeatable; default: all)",
        )
    if "-a" not in own:
        g.add_argument(
            "-a",
            "--arch",
            action="append",
            dest=dest("architectures"),
            metavar="ARCH",
            default=d([]),
            help="only read this architecture (repeatable; default: all)",
        )
    g.add_argument(
        "--sources",
        metavar="PATH",
        default=skip,
        help="sources.list/.sources file or directory (default: /etc/apt/sources.list and "
        f"sources.list.d). {_UNAUTH}",
    )
    g.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        default=d(True),
        help="skip index hashes (by default SHA256 or stronger is required)",
    )
    g.add_argument(
        "-j",
        "--json",
        action="store_true",
        default=d(False),
        help="write one JSON document to stdout",
    )
    g.add_argument(
        "--timeout",
        type=float,
        default=d(30),
        metavar="SECONDS",
        help="network timeout",
    )
    g.add_argument("--retries", type=int, default=d(2), metavar="N", help="network retries")
    g.add_argument(
        "-v",
        "--verbose",
        action="count",
        dest="verbose" if top else "verbose_after",
        default=d(0),
        help="log steps to stderr; -vv: also requests and details (repeatable)",
    )
    g.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=d(False),
        help="hide warnings (errors are always shown)",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse *argv*; global options work before and after the command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.verbose += getattr(args, "verbose_after", 0)
    for name in _LIST_OPTIONS:
        merged = (getattr(args, name) or []) + getattr(args, f"{name}_after", [])
        setattr(args, name, list(dict.fromkeys(merged)) if merged else getattr(args, name))
    if args.url and args.sources:
        parser.error("--url and --sources are mutually exclusive")
    return args


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="minideb",
        description="Query APT repositories and inspect Debian packages.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_options(p, top=True)
    p.add_argument("--version", action="version", version=f"minideb {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(
        name: str,
        func: Callable[[Ctx], int],
        help_: str,
        parent: Any = sub,
        aliases: Sequence[str] = (),
        own: Sequence[str] = (),
        epilog: str | None = None,
    ) -> Any:
        extra: dict[str, Any] = {}
        if epilog is not None:
            extra = {"epilog": epilog, "formatter_class": argparse.RawDescriptionHelpFormatter}
        sp = parent.add_parser(name, help=help_, description=help_, aliases=list(aliases), **extra)
        _add_global_options(sp, top=False, own=own)
        # report the canonical name even when an alias was typed
        sp.set_defaults(func=func, **{parent.dest: name})
        return sp

    s = add("suites", cmd_suites, "list suites (probed for --url)", aliases=["su"])
    s.add_argument("--suite", action="append", metavar="NAME", help="extra suite to probe")
    _add_probe_options(s)
    add("components", cmd_components, "list components of SUITE", aliases=["comps"]).add_argument(
        "suite"
    )
    add("archs", cmd_archs, "list architectures of SUITE", aliases=["arch"]).add_argument("suite")
    s = add("list", cmd_list, "list packages of SUITE", aliases=["ls"], own=("-c", "-a"))
    s.add_argument("suite")
    _add_own_scope(s, "-c", "-a")
    s.add_argument("--source", action="store_true", help="list source packages")
    s = add("show", cmd_show, "show the control data of a package", aliases=["sh"])
    s.add_argument("suite")
    s.add_argument("package")
    s = add("versions", cmd_versions, "versions of a package in all suites", aliases=["ver"])
    s.add_argument("package")
    s.add_argument("--suite", action="append", metavar="NAME", help="extra suite to probe")
    _add_probe_options(s)
    s = add("search", cmd_search, "search package names and descriptions", aliases=["find"])
    s.add_argument("suite")
    s.add_argument("pattern", help="substring, or glob with * or ? (names only)")
    s = add(
        "files",
        cmd_files,
        "find the package providing a file (Contents; large downloads, scope with -c/-a)",
        aliases=["contents"],
        own=("-a",),
    )
    s.add_argument("suite")
    s.add_argument("path", help="full path, or a base name")
    _add_own_scope(s, "-a")
    s = add(
        "appstream",
        cmd_appstream,
        "list DEP-11/AppStream components (id, package, name, summary, once per "
        "component; --json: raw documents per architecture); needs PyYAML: "
        "pip install 'minideblib[dep11]'. Exit status 1 when --id or --package matches nothing",
        aliases=["as"],
        own=("-c", "-a"),
    )
    s.add_argument("suite")
    _add_own_scope(s, "-c", "-a")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--id", help="component ID, exact match")
    g.add_argument("--package", help="binary package name, exact match")
    g.add_argument("--search", metavar="TEXT", help="case-insensitive substring of id, "
                   "package, name or summary")  # fmt: skip
    s.add_argument("--locale", default="C", help="locale for name and summary (default: C)")
    s = add("icons", cmd_icons, "list DEP-11 icon tarballs (path, icon size, bytes)", own=("-c",))
    s.add_argument("suite")
    _add_own_scope(s, "-c")

    s = add(
        "source",
        cmd_source,
        "show the .dsc of a source package (hash-checked download)",
        aliases=["src"],
    )
    s.add_argument("suite")
    s.add_argument("package")
    s.add_argument("--version", dest="want_version", metavar="V", help="select this version")

    _add_mcp(add)

    dsc = sub.add_parser("dsc", help="inspect .dsc files", description="inspect .dsc files")
    _add_global_options(dsc, top=False)
    csub = dsc.add_subparsers(dest="dsc_command", required=True, metavar="DSC_COMMAND")
    d = add("info", cmd_dsc_info, "print the key fields of a .dsc", csub)
    d.add_argument("file")
    d = add("files", cmd_dsc_files, "list files with size and hashes", csub)
    d.add_argument("file")
    d = add(
        "verify",
        cmd_dsc_verify,
        "check the listed files' size and hashes (exit status 2 on mismatch or missing file)",
        csub,
    )
    d.add_argument("file")
    d.add_argument("--dir", metavar="DIR", help="directory of the files (default: that of FILE)")

    deb = sub.add_parser("deb", help="inspect .deb files", description="inspect .deb files")
    _add_global_options(deb, top=False)
    dsub = deb.add_subparsers(dest="deb_command", required=True, metavar="DEB_COMMAND")
    d = add("info", cmd_deb_info, "print control fields (dpkg-deb -I)", dsub, ["i"])
    d.add_argument("file")
    d = add("ctrl", cmd_deb_ctrl, "extract control files to DIR (dpkg-deb -e)", dsub, ["control"])
    d.add_argument("file")
    d.add_argument("dir", help=_DEST_HELP)
    d = add("ls", cmd_deb_ls, "list contents tar-style (dpkg-deb -c)", dsub, ["list"])
    d.add_argument("file")
    d = add("extract", cmd_deb_extract, "safely extract data to DIR (dpkg-deb -x)", dsub, ["x"])
    d.add_argument("file")
    d.add_argument("dir", help=_DEST_HELP)
    d.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE, metavar="BYTES")
    d.add_argument(
        "--skip-absolute-symlinks",
        action="store_true",
        help="do not create symlinks with an absolute target (warned about); by default "
        "they are refused and nothing is extracted",
    )
    d = add(
        "field",
        cmd_deb_field,
        "print fields (dpkg-deb -f); if a field is missing, found ones are printed and the "
        "exit status is 1 (with --json nothing is printed)",
        dsub,
        ["f"],
    )
    d.add_argument("file")
    d.add_argument("fields", nargs="*", metavar="FIELD")
    return p


MCP_EPILOG = """\
security model:
  - refuses to run as root (real, effective or saved uid 0, also in rootless containers)
  - writes only below the base download directory and a private per-session
    directory (mode 0700) under the temp location; never overwrites, never
    follows symlinks
  - every downloaded file is verified against a SHA256-or-stronger hash from the
    repository index (--no-verify only affects index checks and prints a warning)
  - repository metadata is unauthenticated (Release signatures are not verified)
    and all repository text is untrusted data
  - stdout carries only protocol messages; logs go to stderr with control
    characters escaped and URL credentials redacted

The server speaks MCP (JSON-RPC 2.0, one message per line) on stdin/stdout and exits
with status 0 at EOF, or at SIGTERM or SIGINT once it is running (while it is still
starting a signal gets the default action), 2 when it cannot start. At EOF on stdin, as at
SIGTERM or SIGINT, calls still running or queued are cancelled and get no response.
The server waits for them up to 5 seconds, and exits anyway at most 7 seconds after a
signal; a second signal exits at once. Repositories given with --url or --sources are
named repo1, repo2, ...; without them the client names a repository URL in each call
(the system sources are not read).

example client configuration ("mcpServers" entry):
  {"command": "minideb", "args": ["mcp", "--download-dir", "/home/user/debs"]}
"""


def cmd_mcp(ctx: Ctx) -> int:
    from minideblib.mcp.run import run

    return run(ctx.args)


def _add_mcp(add: Callable[..., Any]) -> None:
    s = add(
        "mcp",
        cmd_mcp,
        "run an MCP server for AI agents on stdin/stdout (Linux and macOS)",
        epilog=MCP_EPILOG,
    )
    # -c/-a/-j do not apply to the server (rejected by minideblib.mcp.run); no system sources
    for action in s._actions:
        if action.dest in ("components_after", "architectures_after", "json"):
            action.help = argparse.SUPPRESS
        elif action.dest == "sources":
            action.help = (
                "sources.list/.sources file or directory whose repositories are offered as "
                "repo1, repo2, ... (default: none; the system sources are not read)"
            )
    g = s.add_argument_group("server options")
    g.add_argument(
        "--download-dir",
        metavar="DIR",
        help="base directory for downloads (default: the directory the server was started "
        "in, checked when a download is requested); an explicit DIR must exist and be "
        "writable, checked at start",
    )
    g.add_argument("--no-download", action="store_true", help="read-only server: no download tool")
    g.add_argument(
        "--allow-host",
        action="append",
        metavar="HOST",
        help="only accept repositories on this host (repeatable; '*.example.org' matches "
        "subdomains; file:// mirrors stay allowed; default: any host)",
    )
    g.add_argument(
        "--allow-link-local",
        action="store_true",
        help="allow link-local addresses and cloud metadata host names (refused by default)",
    )
    g.add_argument(
        "--max-response-bytes",
        type=int,
        metavar="N",
        help="cap of one tool result message (default: 200000; at least 4096; results "
        "carry their data twice for newer protocol versions, so about a third is usable)",
    )
    g.add_argument(
        "--max-download-bytes",
        type=int,
        metavar="N",
        help="cap of the total size of the files of one download_package call (default: 1 GiB)",
    )
    g.add_argument(
        "--call-timeout",
        type=float,
        metavar="SECONDS",
        help="time limit of one tool call (default: 120; at most 604800, 7 days)",
    )
    g.add_argument(
        "--download-timeout",
        type=float,
        metavar="SECONDS",
        help="time limit of one download_package call (default: 900; at most 604800, 7 days)",
    )


def _add_own_scope(p: argparse.ArgumentParser, *flags: str) -> None:
    """Per-command ``-c``/``-a`` (repeatable): they replace the global scope for the command."""
    if "-c" in flags:
        p.add_argument(
            "-c",
            "--component",
            action="append",
            metavar="COMP",
            help="only this component (repeatable; replaces the global -c for this command)",
        )
    if "-a" in flags:
        p.add_argument(
            "-a",
            "--arch",
            action="append",
            metavar="ARCH",
            help="only this architecture (repeatable; replaces the global -a for this command)",
        )


_PROBE_HELP = (
    "candidate suites to probe for --url (default: all): debian (stable, stable-updates, "
    "stable-backports, testing), ubuntu (the latest release and the two latest LTS releases, "
    "each with -updates/-security/-backports), all (both) or none (only --suite names); "
    "other suites can be added with --suite. "
    "Only used with a single --url (ignored with several --url or --sources). "
    "Probing exists because many archives do not allow directory listings; it costs up to "
    "two requests (InRelease, then Release) per candidate"
)


def _add_probe_options(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--probe", choices=("all", "debian", "ubuntu", "none"), help=_PROBE_HELP)
    g.add_argument(
        "--no-probe", dest="probe", action="store_const", const="none", help="same as --probe none"
    )
    p.set_defaults(probe="all")


class _Formatter(logging.Formatter):
    """``minideb: [+0.123s] <level>: <message>``; time only with ``-vv``, level from WARNING up.

    With ``-vv`` a record's traceback (``exc_info``) follows the message.
    """

    def __init__(self, start: float, with_time: bool) -> None:
        super().__init__()
        self.start = start
        self.with_time = with_time

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        if record.levelno >= logging.WARNING:
            text = f"{record.levelname.lower()}: {text}"
        if self.with_time and record.exc_info:  # -vv: with the traceback
            text = f"{text}\n{self.formatException(record.exc_info).rstrip()}"
        stamp = f"[+{record.created - self.start:.3f}s] " if self.with_time else ""
        return f"minideb: {stamp}{text}"


class _LogState:
    """What :func:`_setup_logging` changed, for :func:`_teardown_logging` to undo."""

    def __init__(self, handler: logging.Handler, logger: logging.Logger) -> None:
        self.handler = handler
        self.logger = logger
        self.level = logger.level
        self.propagate = logger.propagate


def _setup_logging(args: argparse.Namespace, start: float) -> _LogState:
    """Send ``minideblib`` logs to stderr; returns the state for :func:`_teardown_logging`."""
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    else:
        level = logging.ERROR if args.quiet else logging.WARNING
    handler = logging.StreamHandler(sys.stderr)  # looked up now, so redirections apply
    handler.setFormatter(_Formatter(start, args.verbose >= 2))
    if args.verbose == 0:  # problems are printed once, after the command (see _run)
        handler.addFilter(lambda record: not getattr(record, "problem", False))
    logger = logging.getLogger("minideblib")
    state = _LogState(handler, logger)  # saved before changing anything
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return state


def _teardown_logging(state: _LogState) -> None:
    """Remove the handler and restore the previous level and propagate value."""
    state.logger.removeHandler(state.handler)
    state.logger.setLevel(state.level)
    state.logger.propagate = state.propagate


def main(argv: list[str] | None = None) -> int:
    start = time.time()
    args = parse_args(argv)
    state = _setup_logging(args, start)
    try:
        return _run(args)
    finally:
        _teardown_logging(state)


def _run(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    try:
        code = args.func(ctx)
    except NotFound as e:
        if str(e):
            _error(e)
        code = EXIT_NOT_FOUND
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK
    except FetchError as e:
        code = _fail(_describe_fetch_error(e, args), args, e)
    except RecursionError:
        code = _fail(ValueError("data nested too deeply"), args)
    except (MinidebError, ValueError, OSError) as e:  # ValueError: e.g. a NUL in a file name
        code = _fail(e, args)
    if args.verbose == 0 and not args.quiet:
        for client in ctx.clients:
            for problem in client.problems:
                print(f"minideb: warning: {problem}", file=sys.stderr)
    return code


def _error(e: BaseException) -> None:
    print(f"minideb: error: {e}", file=sys.stderr)


def _describe_fetch_error(e: FetchError, args: argparse.Namespace) -> BaseException:
    """Friendlier message for a 404 on Release/InRelease of the requested suite."""
    suite = getattr(args, "suite", None)
    cause = getattr(e.__cause__, "reason", e.__cause__)
    missing = e.status == 404 or isinstance(cause, FileNotFoundError)
    if missing and isinstance(suite, str) and e.url.endswith(("/Release", "/InRelease")):
        base = redact_url(e.url.rsplit("/dists/", 1)[0])
        how = "HTTP 404" if e.status == 404 else "no such file"
        return FetchError(f"suite {suite!r} not found at {base} ({how})", e.url, e.status)
    return e


def _silence_stdout() -> None:
    """Point stdout at /dev/null so the interpreter's exit flush cannot fail (`| head`)."""
    with contextlib.suppress(OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def _fail(e: BaseException, args: argparse.Namespace, cause: BaseException | None = None) -> int:
    _error(e)
    if args.verbose >= 2:
        import traceback

        traceback.print_exception(cause or e, file=sys.stderr)
    return EXIT_ERROR
