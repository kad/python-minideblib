# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""scripts/prepare_release.py against tmp_path copies (never the real tree)."""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prepare_release.py"
CHECK = ROOT / "scripts" / "check_release.py"
NOON_UTC = "1793534400"  # Sun, 01 Nov 2026 12:00:00 +0000
EVENING_UTC = "1793563200"  # Sun, 01 Nov 2026 20:00:00 +0000 = Mon 05:00 in UTC+9
NAME, EMAIL = "Test Maintainer", "test@example.org"

# the files the helper may change, relative to --root
FILES = {
    "pyproject.toml": (
        '[build-system]\nrequires = ["hatchling"]\n\n'
        '[project]\nname = "minideblib"\nversion = "0.7.0"\ndescription = "x"\n\n'
        '[tool.other]\nversion = "9.9.9"\n'
    ),
    "src/minideblib/__init__.py": (
        '"""Doc."""\n\n__all__ = ["__version__"]\n\n__version__ = "0.7.0"\n'
    ),
    "debian/changelog": (
        "python-minideblib (0.7.0) UNRELEASED; urgency=medium\n\n"
        "  * Modernize.\n\n"
        " -- Someone <someone@example.org>  Sun, 01 Nov 2026 22:09:14 +0200\n\n"
        "python-minideblib (0.6.21.34) unstable; urgency=low\n\n"
        "  * Old.\n\n"
        " -- Someone <someone@example.org>  Mon, 01 Jan 2018 10:00:00 +0200\n"
    ),
    "debian/minideb.1": (
        '.TH MINIDEB 1 "2026-11-01" "minideblib 0.7.0" "User Commands"\n.SH NAME\nminideb\n'
    ),
    "python-minideblib.spec": (
        "Name:           python-minideblib\n"
        "Version:        0.7.0\n"
        "Release:        3%{?dist}\n"
        "Summary:        x\n\n"
        "%description\nx\n\n"
        "%changelog\n"
        "* Sun Nov 01 2026 Someone <someone@example.org> - 0.7.0-1\n"
        "- Rewrite.\n"
    ),
    "CHANGES": (
        "Unreleased\n==========\n\n* Pending note.\n\n"
        "minideblib 0.7.0 (2026-11-01)\n=============================\n\n* Rewrite.\n"
    ),
    "uv.lock": (
        'version = 1\n\n[[package]]\nname = "colorama"\nversion = "0.4.6"\n\n'
        '[[package]]\nname = "minideblib"\nversion = "0.7.0"\nsource = { editable = "." }\n'
    ),
}


def make_tree(root: Path, **override: str) -> Path:
    for rel, text in {**FILES, **override}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "README.md").write_text("readme\n")
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def git_env(tmp_path: Path, user: bool = True) -> dict[str, str]:
    cfg = tmp_path / "gitconfig"
    lines = ["[commit]\n\tgpgsign = false\n", "[tag]\n\tgpgSign = false\n"]
    if user:
        lines.insert(0, f"[user]\n\tname = {NAME}\n\temail = {EMAIL}\n")
    cfg.write_text("".join(lines))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(cfg),
        GIT_CONFIG_NOSYSTEM="1",
        SOURCE_DATE_EPOCH=NOON_UTC,
        TZ="UTC",
        LC_ALL="C",
    )
    return env


def run(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        env=env if env is not None else git_env(root.parent),
    )


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "tree")


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def git(root: Path, *args: str, env: dict[str, str]) -> str:
    res = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=True
    )
    return res.stdout


def git_repo(root: Path, env: dict[str, str]) -> Path:
    git(root, "init", "-q", "-b", "master", env=env)
    git(root, "add", "-A", env=env)
    git(root, "commit", "-q", "-m", "initial", env=env)
    return root


def assert_release_check(root: Path, tag: str) -> None:
    res = subprocess.run(
        [sys.executable, str(CHECK), "--root", str(root), tag], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


# ---- full update ------------------------------------------------------------------------


def test_updates_every_file(tree: Path) -> None:
    res = run(tree, "v0.8.0")
    assert res.returncode == 0, res.stderr
    assert_release_check(tree, "v0.8.0")
    pyproject = read(tree, "pyproject.toml")
    assert '[project]\nname = "minideblib"\nversion = "0.8.0"\n' in pyproject
    assert '[tool.other]\nversion = "9.9.9"\n' in pyproject  # only [project] is touched
    assert '__version__ = "0.8.0"\n' in read(tree, "src/minideblib/__init__.py")
    changelog = read(tree, "debian/changelog")
    assert changelog.startswith(
        "python-minideblib (0.8.0) UNRELEASED; urgency=medium\n\n"
        "  * New upstream release 0.8.0.\n\n"
        f" -- {NAME} <{EMAIL}>  Sun, 01 Nov 2026 12:00:00 +0000\n\n"
        "python-minideblib (0.7.0) UNRELEASED; urgency=medium\n"
    )
    man = read(tree, "debian/minideb.1").splitlines()[0]
    assert man == '.TH MINIDEB 1 "2026-11-01" "minideblib 0.8.0" "User Commands"'
    spec = read(tree, "python-minideblib.spec")
    assert re.search(r"^Version:        0\.8\.0$", spec, re.M)
    assert re.search(r"^Release:        1%\{\?dist\}$", spec, re.M)
    assert (
        "%changelog\n"
        f"* Sun Nov 01 2026 {NAME} <{EMAIL}> - 0.8.0-1\n"
        "- New upstream release 0.8.0.\n\n"
        "* Sun Nov 01 2026 Someone"
    ) in spec
    lock = read(tree, "uv.lock")
    assert 'name = "minideblib"\nversion = "0.8.0"\n' in lock
    assert 'name = "colorama"\nversion = "0.4.6"\n' in lock
    assert "git push origin master v0.8.0" in res.stdout


def test_changes_unreleased_section_is_renamed(tree: Path) -> None:
    assert run(tree, "v0.8.0").returncode == 0
    assert read(tree, "CHANGES").startswith(
        "Unreleased\n==========\n\n"
        "minideblib 0.8.0 (2026-11-01)\n=============================\n\n* Pending note.\n\n"
        "minideblib 0.7.0 (2026-11-01)\n"
    )


def test_changes_empty_unreleased_warns(tmp_path: Path) -> None:
    changes = "Unreleased\n==========\n\nminideblib 0.7.0 (2026-11-01)\n=====\n\n* x\n"
    root = make_tree(tmp_path / "tree", CHANGES=changes)
    res = run(root, "v0.8.0")
    assert res.returncode == 0, res.stderr
    assert "Unreleased section" in res.stderr and "empty" in res.stderr
    assert read(root, "CHANGES").startswith(
        "Unreleased\n==========\n\nminideblib 0.8.0 (2026-11-01)\n"
    )


@pytest.mark.parametrize(
    ("tag", "pep", "deb", "rpm"),
    [
        ("v0.8.0-rc1", "0.8.0rc1", "0.8.0~rc1", "0.8.0~rc1"),
        ("v0.8.0-alpha2", "0.8.0a2", "0.8.0~alpha2", "0.8.0~a2"),
        ("v0.8.0-b3", "0.8.0b3", "0.8.0~b3", "0.8.0~b3"),
    ],
)
def test_prerelease_normalisation(tree: Path, tag: str, pep: str, deb: str, rpm: str) -> None:
    res = run(tree, tag)
    assert res.returncode == 0, res.stderr
    assert_release_check(tree, tag)
    assert f'version = "{pep}"' in read(tree, "pyproject.toml")
    assert f'__version__ = "{pep}"' in read(tree, "src/minideblib/__init__.py")
    assert read(tree, "debian/changelog").startswith(f"python-minideblib ({deb}) UNRELEASED;")
    assert f"  * New upstream release {deb}." in read(tree, "debian/changelog")
    assert f'"minideblib {pep}"' in read(tree, "debian/minideb.1")
    spec = read(tree, "python-minideblib.spec")
    assert re.search(rf"^Version:\s+{re.escape(rpm)}$", spec, re.M)
    assert f"> - {rpm}-1\n" in spec
    assert f"minideblib {pep} (2026-11-01)" in read(tree, "CHANGES")
    assert f'name = "minideblib"\nversion = "{pep}"' in read(tree, "uv.lock")


def test_final_after_release_candidate(tree: Path) -> None:
    assert run(tree, "v0.8.0-rc1").returncode == 0
    res = run(tree, "v0.8.0")
    assert res.returncode == 0, res.stderr
    assert_release_check(tree, "v0.8.0")
    log = read(tree, "debian/changelog")
    assert log.index("(0.8.0)") < log.index("(0.8.0~rc1)") < log.index("(0.7.0)")
    before = snapshot(tree)
    res = run(tree, "v0.8.0-rc2")  # an rc sorts before its final release
    assert res.returncode != 0
    assert "not greater" in res.stderr
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    ("first", "second"),
    [("v0.8.0-a1", "v0.8.0-beta1"), ("v0.8.0-b1", "v0.8.0-rc1"), ("v0.8.0-rc1", "v0.8.0-rc2")],
)
def test_prerelease_ordering(tree: Path, first: str, second: str) -> None:
    assert run(tree, first).returncode == 0
    res = run(tree, second)
    assert res.returncode == 0, res.stderr


def test_dry_run_writes_nothing_and_prints_diff(tree: Path) -> None:
    before = snapshot(tree)
    res = run(tree, "--dry-run", "v0.8.0")
    assert res.returncode == 0, res.stderr
    assert snapshot(tree) == before
    for rel in FILES:
        assert f"--- a/{rel}\n+++ b/{rel}\n" in res.stdout, rel
    assert '-version = "0.7.0"\n+version = "0.8.0"\n' in res.stdout
    assert "+python-minideblib (0.8.0) UNRELEASED; urgency=medium" in res.stdout
    assert "dry run" in res.stdout.lower()


def test_second_run_is_a_no_op(tree: Path) -> None:
    assert run(tree, "v0.8.0").returncode == 0
    before = snapshot(tree)
    res = run(tree, "v0.8.0")
    assert res.returncode == 0, res.stderr
    assert "already at 0.8.0" in res.stdout
    assert snapshot(tree) == before


def test_current_version_is_a_no_op(tree: Path) -> None:
    before = snapshot(tree)
    res = run(tree, "v0.7.0")
    assert res.returncode == 0, res.stderr
    assert "already at 0.7.0" in res.stdout
    assert snapshot(tree) == before


def test_equal_version_with_disagreeing_files_is_refused(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path / "tree", **{"debian/minideb.1": '.TH MINIDEB 1 "2026-01-01" "minideblib 0.6"\n'}
    )
    before = snapshot(root)
    res = run(root, "v0.7.0")
    assert res.returncode != 0
    assert "debian/minideb.1" in res.stderr and "--force" in res.stderr
    assert snapshot(root) == before
    assert run(root, "--force", "v0.7.0").returncode == 0
    assert '"minideblib 0.7.0"' in read(root, "debian/minideb.1")


@pytest.mark.parametrize("tag", ["v0.6.0", "v0.7.0-rc1", "v0.6.99"])
def test_refuses_lower_version(tree: Path, tag: str) -> None:
    before = snapshot(tree)
    res = run(tree, tag)
    assert res.returncode != 0
    assert "not greater than the current version 0.7.0" in res.stderr
    assert snapshot(tree) == before


def test_force_allows_lower_version(tree: Path) -> None:
    res = run(tree, "--force", "v0.6.0")
    assert res.returncode == 0, res.stderr
    assert_release_check(tree, "v0.6.0")


@pytest.mark.parametrize("tag", ["0.8.0", "v0.8", "v08.0.0", "v0.8.0-rc", "v0.8.0rc1", "v0.8.0-c1"])
def test_refuses_malformed_tag(tree: Path, tag: str) -> None:
    before = snapshot(tree)
    res = run(tree, tag)
    assert res.returncode != 0
    assert "vMAJOR.MINOR.PATCH" in res.stderr
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("pyproject.toml", '[project]\nname = "minideblib"\n'),
        ("src/minideblib/__init__.py", '"""No version."""\n'),
        ("debian/changelog", "not a changelog\n"),
        ("debian/minideb.1", ".TH MINIDEB 1\n"),
        ("python-minideblib.spec", "Name: x\nVersion: 0.7.0\nRelease: 1\n"),  # no %changelog
        ("python-minideblib.spec", "Name: x\nRelease: 1\n%changelog\n"),  # no Version:
        ("CHANGES", "minideblib 0.7.0 (2026-11-01)\n=============================\n"),
        ("uv.lock", 'version = 1\n\n[[package]]\nname = "colorama"\nversion = "0.4.6"\n'),
    ],
)
def test_refuses_unexpected_layout(tmp_path: Path, rel: str, text: str) -> None:
    root = make_tree(tmp_path / "tree", **{rel: text})
    before = snapshot(root)
    res = run(root, "v0.8.0")
    assert res.returncode != 0
    assert rel in res.stderr
    assert "expected" in res.stderr
    assert snapshot(root) == before


def test_missing_file_is_refused(tree: Path) -> None:
    (tree / "debian" / "minideb.1").unlink()
    before = snapshot(tree)
    res = run(tree, "v0.8.0")
    assert res.returncode != 0
    assert "debian/minideb.1" in res.stderr
    assert snapshot(tree) == before


def test_local_timezone_and_weekday(tree: Path) -> None:
    env = git_env(tree.parent) | {"SOURCE_DATE_EPOCH": EVENING_UTC, "TZ": "JST-9"}
    assert run(tree, "v0.8.0", env=env).returncode == 0
    assert f" -- {NAME} <{EMAIL}>  Mon, 02 Nov 2026 05:00:00 +0900\n" in read(
        tree, "debian/changelog"
    )
    assert f"* Mon Nov 02 2026 {NAME} <{EMAIL}> - 0.8.0-1\n" in read(tree, "python-minideblib.spec")
    assert '"2026-11-02"' in read(tree, "debian/minideb.1")
    assert "minideblib 0.8.0 (2026-11-02)" in read(tree, "CHANGES")


def test_default_date_is_now(tree: Path) -> None:
    env = git_env(tree.parent)
    del env["SOURCE_DATE_EPOCH"]
    assert run(tree, "v0.8.0", env=env).returncode == 0
    m = re.search(
        r"^\* (\w{3}) (\w{3}) (\d\d) (\d{4}) ", read(tree, "python-minideblib.spec"), re.M
    )
    assert m
    day = datetime.strptime(" ".join(m.groups()[1:]), "%b %d %Y")
    assert day.strftime("%a") == m.group(1)  # weekday agrees with the date
    assert abs((datetime.now() - day).days) <= 1


def test_requires_git_identity(tree: Path) -> None:
    before = snapshot(tree)
    res = run(tree, "v0.8.0", env=git_env(tree.parent, user=False))
    assert res.returncode != 0
    assert "user.name" in res.stderr
    assert snapshot(tree) == before


def test_sign_requires_tag(tree: Path) -> None:
    res = run(tree, "--sign", "v0.8.0")
    assert res.returncode != 0
    assert "--sign" in res.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_failed_write_restores_every_file(tree: Path) -> None:
    before = snapshot(tree)
    debian = tree / "debian"
    debian.chmod(0o555)  # pyproject.toml and __init__.py are written before debian/
    try:
        res = run(tree, "v0.8.0")
    finally:
        debian.chmod(0o755)
    assert res.returncode != 0
    assert "restored" in res.stderr
    assert snapshot(tree) == before


def test_write_keeps_file_mode(tree: Path) -> None:
    (tree / "python-minideblib.spec").chmod(0o640)
    assert run(tree, "v0.8.0").returncode == 0
    assert (tree / "python-minideblib.spec").stat().st_mode & 0o777 == 0o640


def test_real_tree_layout_is_supported(tmp_path: Path) -> None:
    """The real files (copied) match the patterns the helper expects."""
    root = tmp_path / "copy"
    for rel in FILES:
        if not (ROOT / rel).is_file():
            pytest.skip(f"needs {rel} from the source tree")
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, root / rel)
    version = re.search(r'^version = "(\d+)\.(\d+)\.', read(root, "pyproject.toml"), re.M)
    assert version
    tag = f"v{version.group(1)}.{int(version.group(2)) + 1}.0"
    res = run(root, tag)
    assert res.returncode == 0, res.stderr
    assert_release_check(root, tag)


# ---- git behaviour ----------------------------------------------------------------------


@needs_git
def test_refuses_dirty_tree(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    (tree / "README.md").write_text("changed\n")
    before = snapshot(tree)
    res = run(tree, "v0.8.0", env=env)
    assert res.returncode != 0
    assert "uncommitted changes" in res.stderr and "--allow-dirty" in res.stderr
    assert snapshot(tree) == before
    assert run(tree, "--allow-dirty", "v0.8.0", env=env).returncode == 0


@needs_git
def test_untracked_files_do_not_make_the_tree_dirty(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    (tree / "notes.txt").write_text("scratch\n")
    assert run(tree, "v0.8.0", env=env).returncode == 0


@needs_git
def test_refuses_existing_tag(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    git(tree, "tag", "v0.8.0", env=env)
    before = snapshot(tree)
    res = run(tree, "v0.8.0", env=env)
    assert res.returncode != 0
    assert "tag v0.8.0 already exists" in res.stderr
    assert snapshot(tree) == before


@needs_git
def test_changelog_gets_commit_subjects(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    git(tree, "tag", "-a", "v0.7.0", "-m", "minideblib 0.7.0", env=env)
    git(tree, "tag", "v0.1.0-not-a-release-tag", env=env)
    for subject in ["feat: add the thing", "fix(cli)!: repair the other", "Plain subject"]:
        git(tree, "commit", "-q", "--allow-empty", "-m", subject, env=env)
    git(tree, "commit", "-q", "--allow-empty", "-m", "release: v0.7.1", env=env)
    git(tree, "checkout", "-q", "-b", "side", env=env)
    git(tree, "commit", "-q", "--allow-empty", "-m", "docs: side branch", env=env)
    git(tree, "checkout", "-q", "master", env=env)
    git(tree, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side", env=env)
    assert run(tree, "v0.8.0", env=env).returncode == 0
    entry = read(tree, "debian/changelog").split("\n -- ", 1)[0]
    lines = entry.splitlines()[2:]
    assert lines[0] == "  * New upstream release 0.8.0."
    assert sorted(lines[1:]) == [
        "  * Plain subject",
        "  * add the thing",
        "  * repair the other",
        "  * side branch",
    ]


@needs_git
def test_changelog_subjects_are_capped(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    git(tree, "tag", "v0.7.0", env=env)
    for i in range(25):
        git(tree, "commit", "-q", "--allow-empty", "-m", f"chore: change {i}", env=env)
    assert run(tree, "v0.8.0", env=env).returncode == 0
    entry = read(tree, "debian/changelog").split("\n -- ", 1)[0]
    bullets = [ln for ln in entry.splitlines() if ln.startswith("  * ")]
    assert len(bullets) == 21  # "New upstream release" + 20 subjects
    assert bullets[1] == "  * change 24"


@needs_git
def test_long_subject_is_wrapped(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    git(tree, "tag", "v0.7.0", env=env)
    git(tree, "commit", "-q", "--allow-empty", "-m", "fix: " + "word " * 30, env=env)
    assert run(tree, "v0.8.0", env=env).returncode == 0
    entry = read(tree, "debian/changelog").split("\n -- ", 1)[0]
    assert all(len(ln) <= 80 for ln in entry.splitlines())
    assert "\n    word" in entry


@needs_git
def test_control_characters_in_subjects_are_replaced(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    git(tree, "tag", "v0.7.0", env=env)
    git(tree, "commit", "-q", "--allow-empty", "-m", "fix: a\x1b[31mred\x01\tthing", env=env)
    assert run(tree, "v0.8.0", env=env).returncode == 0
    log = read(tree, "debian/changelog")
    assert "  * a [31mred thing\n" in log
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", log)


@needs_git
def test_commit_warns_about_local_edits_in_touched_files(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    changes = read(tree, "CHANGES").replace("* Pending note.", "* Pending note.\n* Local edit.")
    (tree / "CHANGES").write_text(changes)
    (tree / "README.md").write_text("unrelated\n")
    res = run(tree, "--allow-dirty", "--commit", "v0.8.0", env=env)
    assert res.returncode == 0, res.stderr
    warning = next(ln for ln in res.stderr.splitlines() if "WARNING" in ln)
    assert "CHANGES" in warning and "README.md" not in warning
    assert "local edits" in res.stdout and "CHANGES" in res.stdout
    shown = git(tree, "show", "HEAD", "--", "CHANGES", env=env)
    assert "+* Local edit." in shown


@needs_git
def test_commit_without_local_edits_does_not_warn(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    (tree / "README.md").write_text("unrelated\n")
    res = run(tree, "--allow-dirty", "--commit", "v0.8.0", env=env)
    assert res.returncode == 0, res.stderr
    assert "WARNING" not in res.stderr


@needs_git
def test_commit_contains_only_touched_files(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    (tree / "README.md").write_text("unrelated local change\n")
    head = git(tree, "rev-parse", "HEAD", env=env).strip()
    res = run(tree, "--allow-dirty", "--commit", "v0.8.0", env=env)
    assert res.returncode == 0, res.stderr
    assert git(tree, "rev-parse", "HEAD~1", env=env).strip() == head
    assert git(tree, "log", "-1", "--format=%B", env=env).strip() == "release: v0.8.0"
    names = git(tree, "show", "--name-only", "--format=", "HEAD", env=env).split()
    assert sorted(names) == sorted(FILES)
    assert git(tree, "status", "--porcelain", env=env) == " M README.md\n"
    assert git(tree, "tag", "-l", env=env) == ""


@needs_git
def test_tag_is_annotated_and_never_pushed(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    res = run(tree, "--commit", "--tag", "v0.8.0", env=env)
    assert res.returncode == 0, res.stderr
    assert git(tree, "cat-file", "-t", "v0.8.0", env=env).strip() == "tag"
    assert git(tree, "tag", "-l", "--format=%(contents:subject)", "v0.8.0", env=env).strip() == (
        "minideblib 0.8.0"
    )
    assert git(tree, "rev-parse", "v0.8.0^{commit}", env=env) == git(
        tree, "rev-parse", "HEAD", env=env
    )
    assert git(tree, "remote", env=env) == ""
    assert "git push origin master v0.8.0" in res.stdout
    assert git(tree, "status", "--porcelain", env=env) == ""


@needs_git
def test_tag_without_commit_needs_a_clean_tree(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    before = snapshot(tree)
    res = run(tree, "--tag", "v0.8.0", env=env)  # the update would leave the tree dirty
    assert res.returncode != 0
    assert "--commit" in res.stderr
    assert snapshot(tree) == before
    assert git(tree, "tag", "-l", env=env) == ""


@needs_git
def test_tag_refused_on_dirty_tree(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    assert run(tree, "--commit", "v0.8.0", env=env).returncode == 0
    (tree / "README.md").write_text("changed\n")
    res = run(tree, "--allow-dirty", "--tag", "v0.8.0", env=env)
    assert res.returncode != 0
    assert "dirty" in res.stderr or "uncommitted" in res.stderr
    assert git(tree, "tag", "-l", env=env) == ""


@needs_git
def test_tag_after_separate_commit(tree: Path) -> None:
    env = git_env(tree.parent)
    git_repo(tree, env)
    assert run(tree, "--commit", "v0.8.0", env=env).returncode == 0
    res = run(tree, "--tag", "v0.8.0", env=env)  # files already agree: only tag
    assert res.returncode == 0, res.stderr
    assert git(tree, "cat-file", "-t", "v0.8.0", env=env).strip() == "tag"


def test_commit_outside_git_repository_is_refused(tree: Path) -> None:
    before = snapshot(tree)
    res = run(tree, "--commit", "v0.8.0")
    assert res.returncode != 0
    assert "not a git work tree" in res.stderr
    assert snapshot(tree) == before
