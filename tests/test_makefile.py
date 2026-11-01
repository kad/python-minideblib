# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Makefile conventions: dry runs, documented targets."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
DRY = "help sync lint typecheck format test test-network check build pyz install examples changelog"
pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not available")


def make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["make", "-C", str(ROOT), *args], capture_output=True, text=True)


def phony() -> list[str]:
    text = MAKEFILE.read_text().replace("\\\n", " ")
    m = re.search(r"^\.PHONY:(.*)$", text, re.M)
    assert m
    return m.group(1).split()


@pytest.mark.parametrize(
    "target", DRY.split() + ["clean", "distclean", "release-check", "release-prepare"]
)
def test_dry_run(target: str) -> None:
    extra = ["TAG=v0.7.0"] if target.startswith("release-") else []
    res = make("-n", target, *extra)
    assert res.returncode == 0, res.stderr


def test_dry_run_combined() -> None:
    assert make("-n", "check", "build", "pyz").returncode == 0


def test_default_goal_is_help() -> None:
    res = make()
    assert res.returncode == 0
    assert "Usage:" in res.stdout


def test_help_lists_every_phony_target() -> None:
    out = make("help").stdout
    for target in phony():
        assert re.search(rf"^\s*{re.escape(target)}\s", out, re.M), target


def test_every_phony_target_documented() -> None:
    text = MAKEFILE.read_text()
    for target in phony():
        assert re.search(rf"^{re.escape(target)}:.*## \S", text, re.M), target


def test_check_order() -> None:
    out = make("-n", "check").stdout
    assert out.index("ruff check") < out.index("ty check") < out.index("pytest")


def test_overridable_variables() -> None:
    out = make("-n", "test", "UV=myuv").stdout
    assert "myuv run pytest" in out


@pytest.mark.parametrize("target", ["deb", "rpm"])
def test_packaging_targets_fail_clearly_when_missing(target: str) -> None:
    tool = {"deb": "dpkg-buildpackage", "rpm": "rpmbuild"}[target]
    marker = {"deb": ROOT / "debian" / "control", "rpm": ROOT / "python-minideblib.spec"}[target]
    if shutil.which(tool) and marker.exists():
        pytest.skip("packaging prerequisites present")
    res = make(target)
    assert res.returncode != 0
    assert "error" in (res.stderr + res.stdout).lower()


def test_network_target_runs_every_network_test() -> None:
    out = make("-n", "test-network").stdout
    assert "MINIDEB_NETWORK=1" in out and "pytest -m network" in out
    assert "test_network.py" not in out  # network tests may live in any module


def test_release_prepare_is_documented() -> None:
    assert "release-prepare" in phony()
    out = make("help").stdout
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("release-prepare"))
    for var in ("TAG=", "DRY_RUN=1", "COMMIT=1", "TAG_IT=1", "SIGN=1"):
        assert var in line, var


def test_release_prepare_flags() -> None:
    plain = make("-n", "release-prepare", "TAG=v0.8.0").stdout
    assert 'scripts/prepare_release.py "$TAG"' in plain  # the shell reads it, quoted
    assert "--" not in plain.split("prepare_release.py", 1)[1]
    out = make(
        "-n", "release-prepare", "TAG=v0.8.0", "DRY_RUN=1", "COMMIT=1", "TAG_IT=1", "SIGN=1"
    ).stdout
    for flag in ("--dry-run", "--commit", "--tag", "--sign"):
        assert flag in out, flag
    off = make("-n", "release-prepare", "TAG=v0.8.0", "DRY_RUN=0", "COMMIT=", "TAG_IT=no").stdout
    assert "--dry-run" not in off and "--commit" not in off and "--tag" not in off


def test_release_prepare_needs_tag() -> None:
    res = make("release-prepare", "TAG=")
    assert res.returncode != 0
    assert "TAG=" in res.stderr


HOSTILE = [
    "v0.9.0$$(touch {marker})",
    'v0.9.0" ; touch {marker} ; echo "',
    "v0.9.0`touch {marker}`",
]


def _sandbox(tmp_path: Path) -> Path:
    """The real Makefile and release scripts, copied (the recipes really run)."""
    box = tmp_path / "box"
    (box / "scripts").mkdir(parents=True)
    shutil.copy2(MAKEFILE, box / "Makefile")
    for name in ("check_release.py", "prepare_release.py"):
        shutil.copy2(ROOT / "scripts" / name, box / "scripts" / name)
    return box


@pytest.mark.parametrize("target", ["release-check", "release-prepare"])
@pytest.mark.parametrize("template", HOSTILE)
def test_hostile_tag_is_not_run_by_the_shell(tmp_path: Path, target: str, template: str) -> None:
    box = _sandbox(tmp_path)
    marker = tmp_path / "PWNED"
    tag = template.format(marker=marker)
    res = subprocess.run(
        ["make", "-C", str(box), target, f"TAG={tag}", "DRY_RUN=1"],
        capture_output=True,
        text=True,
    )
    assert not marker.exists()
    assert res.returncode != 0
    assert "vMAJOR.MINOR.PATCH" in res.stderr


@pytest.mark.parametrize("var", ["DESTDIR", "PREFIX"])
@pytest.mark.parametrize("template", HOSTILE)
def test_hostile_paths_are_not_run_by_the_shell(tmp_path: Path, var: str, template: str) -> None:
    box = _sandbox(tmp_path)
    marker = tmp_path / "PWNED"
    value = template.format(marker=marker)
    res = subprocess.run(
        ["make", "-C", str(box), "help", f"{var}={value}"], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    assert not marker.exists()
    out = make("-n", "install", f"{var}=/x").stdout
    assert '--prefix "$DESTDIR$PREFIX"' in out
