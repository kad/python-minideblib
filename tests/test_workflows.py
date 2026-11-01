# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Validate the GitHub workflow files and their link to the Makefile."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
# owner/repo@<40 hex commit> # <tag or branch>
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s+\S+\s*$")


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(data, dict)
    return data


def triggers(wf: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads the bare key "on" as boolean True.
    return wf.get("on", wf.get(True))


def steps(wf: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for job in wf["jobs"].values() for s in job.get("steps", [])]


@pytest.mark.parametrize(
    ("name", "jobs"),
    [
        ("ci.yml", {"lint", "test", "build", "network"}),
        ("release.yml", {"build", "release", "publish-pypi"}),
    ],
)
def test_structure(name: str, jobs: set[str]) -> None:
    wf = load(name)
    assert triggers(wf)
    assert "permissions" in wf
    assert set(wf["jobs"]) == jobs


def unpinned_uses(text: str) -> list[str]:
    """Return the ``uses:`` lines not pinned to a commit SHA with a version comment."""
    lines = [ln for ln in text.splitlines() if re.match(r"^\s*(?:-\s+)?uses:", ln)]
    return [ln for ln in lines if not USES_LINE.match(ln)]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_actions_pinned(name: str) -> None:
    text = (WORKFLOWS / name).read_text()
    assert [s["uses"] for s in steps(load(name)) if "uses" in s]
    assert unpinned_uses(text) == []


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4",
        "      - uses: actions/checkout@v4.4.0",
        "      - uses: pypa/gh-action-pypi-publish@release/v1",
        "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "      - uses: actions/checkout@11D5960A326750D5838078E36CF38B85AF677262 # v4.4.0",
        "      - uses: actions/checkout@11d5960a # v4.4.0",
    ],
)
def test_unpinned_uses_rejected(line: str) -> None:
    assert unpinned_uses(f"steps:\n{line}\n") == [line]


def test_pinned_uses_accepted() -> None:
    line = "        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0"
    assert unpinned_uses(f"steps:\n{line}\n") == []


def test_dependabot_updates_actions_weekly() -> None:
    data = yaml.safe_load(DEPENDABOT.read_text())
    assert data["version"] == 2
    gha = [u for u in data["updates"] if u["package-ecosystem"] == "github-actions"]
    assert len(gha) == 1
    assert gha[0]["directory"] == "/"
    assert gha[0]["schedule"]["interval"] == "weekly"


def test_ci_details() -> None:
    wf = load("ci.yml")
    assert wf["permissions"] == {"contents": "read"}
    on = triggers(wf)
    assert {"push", "pull_request", "workflow_dispatch", "schedule"} <= set(on)
    assert wf["jobs"]["test"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    ]
    assert wf["jobs"]["network"]["env"]["MINIDEB_NETWORK"] == "1"


def test_release_details() -> None:
    wf = load("release.yml")
    assert triggers(wf)["push"]["tags"] == ["v*"]
    pub = wf["jobs"]["publish-pypi"]
    assert pub["if"] == "vars.PUBLISH_PYPI == 'true'"
    assert pub["permissions"]["id-token"] == "write"
    assert pub["environment"] == "pypi"
    assert wf["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert wf["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["release"]["needs"] == "build"
    assert wf["jobs"]["publish-pypi"]["needs"] == "build"
    text = (WORKFLOWS / "release.yml").read_text()
    assert "fetch-depth: 0" in text
    assert "check_release.py" in text


def test_ci_make_targets_exist() -> None:
    if shutil.which("make") is None:
        pytest.skip("make not available")
    for wfname in ("ci.yml", "release.yml"):
        for s in steps(load(wfname)):
            for line in str(s.get("run", "")).splitlines():
                m = re.match(r"\s*make\s+([\w -]+?)\s*$", line)
                if not m:
                    continue
                targets = m.group(1).split()
                res = subprocess.run(
                    ["make", "-n", "-C", str(ROOT), *targets], capture_output=True, text=True
                )
                assert res.returncode == 0, (line, res.stderr)


def test_actionlint() -> None:
    exe = shutil.which("actionlint")
    if exe is None:
        pytest.skip("actionlint not installed")
    res = subprocess.run([exe], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


WORKFLOW_FILES = ["ci.yml", "release.yml"]


def job_perms(wf: dict[str, Any], job: dict[str, Any]) -> dict[str, str]:
    return job.get("permissions", wf.get("permissions", {}))


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_no_expression_in_run_scripts(name: str) -> None:
    for s in steps(load(name)):
        assert "${{" not in str(s.get("run", "")), s["run"]


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_no_pull_request_target(name: str) -> None:
    assert "pull_request_target" not in triggers(load(name))


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_checkout_does_not_persist_credentials(name: str) -> None:
    checkouts = [
        s for s in steps(load(name)) if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts
    for s in checkouts:
        assert s.get("with", {}).get("persist-credentials") is False


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_write_permissions_are_narrow(name: str) -> None:
    wf = load(name)
    assert "write" not in wf["permissions"].values()
    for jname, job in wf["jobs"].items():
        perms = job_perms(wf, job)
        if perms.get("contents") == "write":
            assert (name, jname) == ("release.yml", "release")
        if perms.get("id-token") == "write":
            assert (name, jname) == ("release.yml", "publish-pypi")


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_write_jobs_run_no_project_code_and_no_cache(name: str) -> None:
    wf = load(name)
    for job in wf["jobs"].values():
        perms = job_perms(wf, job)
        if "write" not in perms.values():
            continue
        for s in job["steps"]:
            run = str(s.get("run", ""))
            assert not re.search(r"\b(make|uv|pip|python3?)\b", run), run
            uses = str(s.get("uses", ""))
            assert not uses.startswith(
                ("astral-sh/setup-uv", "actions/setup-python", "actions/cache")
            )
            assert "cache" not in str(s.get("with", {}))
