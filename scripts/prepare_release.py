#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Prepare a release: write the version of a tag into every file that records it.

Usage: prepare_release.py [--root PATH] [--dry-run] [--commit] [--tag [--sign]]
                          [--allow-dirty] [--force] vX.Y.Z[-PRE]

The tag grammar and the version normalisation are those of check_release.py
(PEP 440 X.Y.ZrcN for Python, Debian X.Y.Z~rcN). RPM uses X.Y.Z~rcN with the
PEP 440 letters (a, b, rc), so a pre-release sorts before its final release.

Files updated (all new contents are computed first; a file that does not match
the expected layout stops the run before anything is written; writes are atomic
and every file is restored if one write or the final check fails):
  pyproject.toml              [project] version
  src/minideblib/__init__.py  __version__
  debian/changelog            new top entry, distribution UNRELEASED (set the
                              distribution, e.g. with "dch -r", when uploading)
  debian/minideb.1            .TH date and "minideblib VERSION"
  python-minideblib.spec      Version:, Release: 1%{?dist}, new %changelog entry
  CHANGES                     the "Unreleased" section becomes "minideblib VERSION
                              (YYYY-MM-DD)" and a fresh empty "Unreleased" goes above
  uv.lock                     the version of the minideblib package entry

The new debian/changelog entry lists the commit subjects since the previous v*
tag (at most 20, merges and "release:" commits skipped, conventional-commit
prefixes stripped). The signatures come from "git config user.name/user.email";
dates use the local time zone, or SOURCE_DATE_EPOCH when it is set.

Versions are compared as (major, minor, patch, stage, number) tuples with
alpha < beta < rc < final, which is the PEP 440 and the Debian "~" order for
this grammar. The tag must be greater than the current pyproject.toml version;
an equal version is a no-op when every file already agrees. --force skips the
comparison. Nothing is ever pushed and nothing touches the network. An interrupt
(Ctrl-C) while the files are being written is handled on a best-effort basis only:
"git checkout -- <files>" recovers a partly written tree.
"""

import argparse
import difflib
import email.utils
import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import check_release

NUM = check_release.NUM
# ranks of the PEP 440 letters; tag letters map to them through check_release.PEP440_LETTER
STAGE_RANK = {"a": 0, "b": 1, "rc": 2}
FINAL_RANK = 3
PEP440_RE = re.compile(rf"^{NUM}\.{NUM}\.{NUM}(?:({'|'.join(STAGE_RANK)})([0-9]+))?$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_SUBJECTS = 20
CONVENTIONAL_RE = re.compile(
    r"^(?:feat|fix|docs|style|refactor|perf|test|tests|build|ci|chore|revert)"
    r"(?:\([^)]*\))?!?:\s*",
    re.I,
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNRELEASED = "Unreleased\n==========\n"

Key = tuple[int, int, int, int, int]


class PrepareError(Exception):
    """A refusal or a failure; the message is printed after "error: "."""


class LayoutError(Exception):
    """A file does not match the expected pattern (the message describes the pattern)."""


@dataclass(frozen=True)
class Release:
    tag: str
    pep: str  # PEP 440: pyproject.toml, __version__, man page, CHANGES, uv.lock
    deb: str  # Debian upstream version
    rpm: str  # RPM Version:
    key: Key


def _key(groups: Sequence[str | None]) -> Key:
    major, minor, patch, stage, number = groups
    rank = FINAL_RANK if stage is None else STAGE_RANK[check_release.PEP440_LETTER[stage]]
    return int(major or 0), int(minor or 0), int(patch or 0), rank, int(number or 0)


def parse_tag(tag: str) -> Release | None:
    m = check_release.TAG_RE.match(tag)
    versions = check_release.expected_versions(tag)
    if m is None or versions is None:
        return None
    pep, deb = versions
    base = ".".join(m.group(i) for i in (1, 2, 3))
    rpm = base if m.group(4) is None else f"{base}~{pep.removeprefix(base)}"
    return Release(tag, pep, deb, rpm, _key(m.groups()))


def pep440_key(version: str) -> Key | None:
    m = PEP440_RE.match(version)
    return _key(m.groups()) if m else None


def _show(key: Key) -> str:
    stage = {v: k for k, v in STAGE_RANK.items()}
    base = f"{key[0]}.{key[1]}.{key[2]}"
    return base if key[3] == FINAL_RANK else f"{base}{stage[key[3]]}{key[4]}"


# ---- git ---------------------------------------------------------------------------------


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if exe is None:
        raise PrepareError("git not found")
    res = subprocess.run([exe, "-C", str(root), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise PrepareError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res


def is_git_root(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    res = git(root, "rev-parse", "--show-toplevel", check=False)
    return res.returncode == 0 and Path(res.stdout.strip()).resolve() == root


def is_dirty(root: Path) -> bool:
    return bool(git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip())


def tag_exists(root: Path, tag: str) -> bool:
    return git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0


def commit_subjects(root: Path, release: Release) -> list[str]:
    """Subjects since the newest v* tag below *release* that HEAD contains."""
    if git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0:
        return []
    tags = git(root, "tag", "-l", "v*", "--merged", "HEAD").stdout.split()
    older = [(rel.key, t) for t in tags if (rel := parse_tag(t)) and rel.key < release.key]
    if not older:
        return []
    previous = max(older)[1]
    log = git(root, "log", "--no-merges", "--topo-order", "--format=%s", f"{previous}..HEAD")
    subjects: list[str] = []
    for line in log.stdout.splitlines():
        line = " ".join(CONTROL_RE.sub(" ", line).split())  # no ESC, tabs or NULs
        if not line or line.lower().startswith("release:"):
            continue
        subjects.append(CONVENTIONAL_RE.sub("", line, count=1) or line)
    return subjects[:MAX_SUBJECTS]


# ---- new file contents -------------------------------------------------------------------


class Context:
    def __init__(self, root: Path, release: Release, repo: bool, now: datetime) -> None:
        self.root = root
        self.release = release
        self.repo = repo
        self.now = now
        self.warnings: list[str] = []

    @functools.cached_property
    def identity(self) -> tuple[str, str]:
        if shutil.which("git") is None:
            raise PrepareError("git is needed to read user.name and user.email")
        name = git(self.root, "config", "user.name", check=False).stdout.strip()
        mail = git(self.root, "config", "user.email", check=False).stdout.strip()
        if not name or not mail:
            raise PrepareError(
                "set git config user.name and user.email: they sign the new "
                "debian/changelog and python-minideblib.spec entries"
            )
        return name, mail

    @functools.cached_property
    def subjects(self) -> list[str]:
        return commit_subjects(self.root, self.release) if self.repo else []


def _replace(text: str, m: re.Match[str], group: int, new: str) -> str:
    return text[: m.start(group)] + new + text[m.end(group) :]


def update_pyproject(text: str, ctx: Context) -> str:
    section = re.search(r"^\[project\][ \t]*\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    m = re.compile(r'^version[ \t]*=[ \t]*"([^"]*)"', re.M).search(
        text, section.start(1) if section else 0, section.end(1) if section else 0
    )
    if section is None or m is None:
        raise LayoutError('a [project] table with version = "..."')
    return _replace(text, m, 1, ctx.release.pep)


def current_version(pyproject: str) -> str | None:
    section = re.search(r"^\[project\][ \t]*\n(.*?)(?=^\[|\Z)", pyproject, re.M | re.S)
    m = re.search(r'^version[ \t]*=[ \t]*"([^"]*)"', section.group(1), re.M) if section else None
    return m.group(1) if m else None


def update_init(text: str, ctx: Context) -> str:
    m = check_release.INIT_RE.search(text)
    if m is None:
        raise LayoutError('a line __version__ = "..."')
    return _replace(text, m, 1, ctx.release.pep)


def update_changelog(text: str, ctx: Context) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    m = check_release.CHANGELOG_RE.match(first)
    if m is None:
        raise LayoutError("a first line 'PACKAGE (VERSION) DISTRIBUTION; urgency=...'")
    rel = ctx.release
    if check_release.upstream_version(m.group(1)) == rel.deb:
        return text
    version = f"{rel.deb}-1" if "-" in m.group(1) else rel.deb  # keep native or not
    package = first.split(" ", 1)[0]
    bullets = [f"New upstream release {rel.deb}.", *ctx.subjects]
    body = "".join(
        textwrap.fill(
            item,
            width=78,
            initial_indent="  * ",
            subsequent_indent="    ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        + "\n"
        for item in bullets
    )
    name, mail = ctx.identity
    stamp = email.utils.format_datetime(ctx.now)
    head = f"{package} ({version}) UNRELEASED; urgency=medium\n\n"
    return f"{head}{body}\n -- {name} <{mail}>  {stamp}\n\n" + text.lstrip("\n")


MAN_RE = re.compile(r'\A(\.TH \S+ \S+ ")([^"]*)(" "minideblib )([^"]*)(")')


def update_man(text: str, ctx: Context) -> str:
    m = MAN_RE.match(text)
    if m is None:
        raise LayoutError('a first line .TH NAME SECTION "DATE" "minideblib VERSION"')
    if m.group(4) == ctx.release.pep:
        return text
    head = f"{m.group(1)}{ctx.now:%Y-%m-%d}{m.group(3)}{ctx.release.pep}{m.group(5)}"
    return head + text[m.end() :]


def update_spec(text: str, ctx: Context) -> str:
    version = re.search(r"^Version:[ \t]+(\S+)[ \t]*$", text, re.M)
    release = re.search(r"^Release:[ \t]+(\S+)[ \t]*$", text, re.M)
    changelog = re.search(r"^%changelog[ \t]*\n", text, re.M)
    if version is None or release is None or changelog is None:
        raise LayoutError("the lines Version: X, Release: N and %changelog")
    rpm = ctx.release.rpm
    if version.group(1) == rpm:
        return text
    name, mail = ctx.identity
    now = ctx.now
    day = f"{WEEKDAYS[now.weekday()]} {MONTHS[now.month - 1]} {now.day:02d} {now.year}"
    entry = f"* {day} {name} <{mail}> - {rpm}-1\n- New upstream release {rpm}.\n\n"
    edits = [
        (version.start(1), version.end(1), rpm),
        (release.start(1), release.end(1), "1%{?dist}"),
        (changelog.end(), changelog.end(), entry),
    ]
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def update_changes(text: str, ctx: Context) -> str:
    pep = ctx.release.pep
    if re.search(rf"^minideblib {re.escape(pep)} \(", text, re.M):
        return text
    m = re.search(r"^Unreleased[ \t]*\n=+[ \t]*\n", text, re.M)
    if m is None:
        raise LayoutError("an 'Unreleased' heading underlined with '=' (see README)")
    following = re.compile(r"^[^\n]+\n=+[ \t]*$", re.M).search(text, m.end())
    if not text[m.end() : following.start() if following else len(text)].strip():
        ctx.warnings.append("CHANGES: the Unreleased section is empty; add the release notes")
    title = f"minideblib {pep} ({ctx.now:%Y-%m-%d})"
    return text[: m.start()] + f"{UNRELEASED}\n{title}\n{'=' * len(title)}\n" + text[m.end() :]


def update_uv_lock(text: str, ctx: Context) -> str:
    m = re.search(r'^\[\[package\]\]\nname = "minideblib"\nversion = "([^"]*)"$', text, re.M)
    if m is None:
        raise LayoutError('a [[package]] entry name = "minideblib" followed by version = "..."')
    return _replace(text, m, 1, ctx.release.pep)


UPDATERS: list[tuple[str, Callable[[str, Context], str]]] = [
    ("pyproject.toml", update_pyproject),
    ("src/minideblib/__init__.py", update_init),
    ("debian/changelog", update_changelog),
    ("debian/minideb.1", update_man),
    ("python-minideblib.spec", update_spec),
    ("CHANGES", update_changes),
    ("uv.lock", update_uv_lock),
]


# ---- checking and writing ----------------------------------------------------------------


def verify(root: Path, release: Release) -> list[str]:
    """check_release.check plus the tests/test_man_version.py assertions."""
    errors = check_release.check(root, release.tag)
    man = (root / "debian" / "minideb.1").read_text()
    m = MAN_RE.match(man)
    if m is None or m.group(4) != release.pep:
        errors.append(f"error: debian/minideb.1 header does not name minideblib {release.pep}")
    spec = (root / "python-minideblib.spec").read_text()
    if not re.search(rf"^Version:\s+{re.escape(release.rpm)}$", spec, re.M):
        errors.append(f"error: python-minideblib.spec Version: is not {release.rpm}")
    return errors


def atomic_write(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_all(root: Path, plan: dict[str, str], originals: dict[str, bytes], rel: Release) -> None:
    written: list[str] = []

    def restore(reason: str) -> PrepareError:
        failed = []
        for name in reversed(written):
            try:
                atomic_write(root / name, originals[name])
            except OSError:
                failed.append(name)
        if failed:
            return PrepareError(f"{reason}; could NOT restore {', '.join(failed)}")
        return PrepareError(f"{reason}; restored the {len(written)} file(s) already written")

    for name, text in plan.items():
        try:
            atomic_write(root / name, text.encode())
        except OSError as exc:
            raise restore(f"cannot write {name}: {exc}") from exc
        written.append(name)
    errors = verify(root, rel)
    if errors:
        raise restore("the updated files do not pass the release check:\n" + "\n".join(errors))


def dry_run_check(plan: dict[str, str], originals: dict[str, bytes], rel: Release) -> list[str]:
    """verify() on a temporary copy holding the planned contents."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, data in originals.items():
            path = Path(tmp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plan[name].encode() if name in plan else data)
        return verify(Path(tmp), rel)


def release_now() -> datetime:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return datetime.fromtimestamp(int(epoch), tz=UTC).astimezone()
        except (ValueError, OverflowError, OSError) as exc:
            raise PrepareError(f"SOURCE_DATE_EPOCH={epoch!r} is not a timestamp") from exc
    return datetime.now().astimezone()


# ---- main --------------------------------------------------------------------------------


def create_tag(root: Path, rel: Release, sign: bool, committed: bool = False) -> None:
    """Tag HEAD; without a release commit just made, the tree must be clean."""
    if not committed and is_dirty(root):
        raise PrepareError(f"the working tree has uncommitted changes; not tagging {rel.tag}")
    if tag_exists(root, rel.tag):
        raise PrepareError(f"tag {rel.tag} already exists")
    git(root, "tag", "-a", *(["-s"] if sign else []), "-m", f"minideblib {rel.pep}", rel.tag)
    print(f"created the annotated tag {rel.tag}" + (" (signed)" if sign else ""))


def next_steps(rel: Release, committed: bool, tagged: bool) -> None:
    steps = [
        "review CHANGES and debian/changelog (the Debian distribution stays UNRELEASED: "
        'set it, e.g. with "dch -r", when uploading)',
        f"make release-check TAG={rel.tag}",
    ]
    if not committed:
        steps.append(f'git commit -m "release: {rel.tag}" -- <the files above>')
    if not tagged:
        steps.append(f'git tag -a {rel.tag} -m "minideblib {rel.pep}"')
    steps.append(f"git push origin master {rel.tag}   (the release workflow does the rest)")
    print("next steps:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")


def prepare(args: argparse.Namespace) -> int:
    if args.sign and not args.tag:
        raise PrepareError("--sign needs --tag")
    rel = parse_tag(args.version_tag)
    if rel is None:
        raise PrepareError(f"tag {args.version_tag!r} is not {check_release.TAG_HELP}")
    root: Path = args.root.resolve()
    repo = is_git_root(root)
    if (args.commit or args.tag) and not repo:
        raise PrepareError(f"{root} is not a git work tree: --commit and --tag need one")

    originals: dict[str, bytes] = {}
    for name, _ in UPDATERS:
        try:
            originals[name] = (root / name).read_bytes()
        except OSError as exc:
            raise PrepareError(f"{name}: cannot read: {exc.strerror or exc}") from exc
    texts = {name: data.decode("utf-8") for name, data in originals.items()}

    current = current_version(texts["pyproject.toml"])
    current_key = pep440_key(current) if current else None
    if current_key is None and not args.force:
        raise PrepareError(
            f"pyproject.toml: [project] version {current!r} is not the expected "
            "MAJOR.MINOR.PATCH[aN|bN|rcN] (use --force to skip the comparison)"
        )
    if current_key is not None and rel.key < current_key and not args.force:
        raise PrepareError(
            f"{rel.pep} is not greater than the current version {_show(current_key)} "
            "(use --force to override)"
        )

    ctx = Context(root, rel, repo, release_now())
    plan: dict[str, str] = {}
    for name, update in UPDATERS:
        try:
            new = update(texts[name], ctx)
        except LayoutError as exc:
            raise PrepareError(f"{name}: unexpected layout, expected {exc}") from exc
        if new != texts[name]:
            plan[name] = new

    if current_key == rel.key and not args.force:
        if plan:
            raise PrepareError(
                f"already at {rel.pep} but these files disagree: {', '.join(plan)} "
                "(use --force to update them)"
            )
        print(f"already at {rel.pep}: nothing to change")
        if args.tag and not args.dry_run:
            create_tag(root, rel, args.sign)
        return 0
    if not plan:
        print(f"already at {rel.pep}: nothing to change")
        return 0

    local_edits: list[str] = []
    if repo:
        diff = git(root, "diff", "--name-only", "HEAD", "--", *plan, check=False)
        local_edits = diff.stdout.split() if diff.returncode == 0 else []
        if tag_exists(root, rel.tag):
            raise PrepareError(f"tag {rel.tag} already exists")
        if is_dirty(root) and not args.allow_dirty:
            raise PrepareError(
                "the working tree has uncommitted changes in tracked files "
                "(commit them first or pass --allow-dirty)"
            )
    if args.tag and not args.commit:
        raise PrepareError("--tag needs --commit when files change (the tag would miss them)")

    if args.dry_run:
        for name, new in plan.items():
            sys.stdout.writelines(
                difflib.unified_diff(
                    texts[name].splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    f"a/{name}",
                    f"b/{name}",
                )
            )
        errors = dry_run_check(plan, originals, rel)
        for line in [*ctx.warnings, *errors]:
            print(line if line.startswith("error:") else f"warning: {line}", file=sys.stderr)
        print(f"dry run: {len(plan)} file(s) would change, nothing written")
        return 1 if errors else 0

    write_all(root, plan, originals, rel)
    for warning in ctx.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"release {rel.pep}: updated {', '.join(plan)}")
    if args.commit:
        if local_edits:
            print(
                "WARNING: the release commit also contains your uncommitted edits in "
                + ", ".join(local_edits),
                file=sys.stderr,
            )
        git(root, "add", "--", *plan)
        git(root, "commit", "-q", "-m", f"release: {rel.tag}", "--", *plan)
        print(f"committed: release: {rel.tag}")
        if local_edits:
            print(f"  including local edits in: {', '.join(local_edits)}")
    if args.tag:
        create_tag(root, rel, args.sign, committed=args.commit)
    next_steps(rel, args.commit, args.tag)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("version_tag", metavar="tag", help="release tag, e.g. v0.8.0 or v0.8.0-rc1")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument("--dry-run", action="store_true", help="print a diff, write nothing")
    p.add_argument("--commit", action="store_true", help='commit the changed files ("release: vX")')
    p.add_argument("--tag", action="store_true", help="create the annotated tag (never pushed)")
    p.add_argument("--sign", action="store_true", help="sign the tag (git tag -s)")
    p.add_argument("--allow-dirty", action="store_true", help="allow uncommitted tracked changes")
    p.add_argument("--force", action="store_true", help="skip the version comparison")
    args = p.parse_args(argv)
    try:
        return prepare(args)
    except PrepareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
