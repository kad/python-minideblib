# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The MCP server is documented: README section, man page, CHANGES, TODO, examples."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from minideblib.mcp.startup import ServerConfig
from minideblib.mcp.tools import build_tools
from tests.mcp_client import SRC

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MAN = ROOT / "debian" / "minideb.1"

# autopkgtest copies only tests/ next to the installed package
if not (ROOT / "pyproject.toml").is_file():
    pytest.skip("needs the source tree", allow_module_level=True)

TOOLS = [t.name for t in build_tools(ServerConfig())]


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    m = re.compile(end, re.M).search(text, i + len(start))
    return text[i : m.start() if m else len(text)]


@pytest.fixture(scope="module")
def readme_section() -> str:
    return _section(README.read_text(), "## Using minideb with AI agents", r"^## ")


@pytest.fixture(scope="module")
def man_section() -> str:
    text = MAN.read_text()
    section = _section(text, ".SH MCP SERVER", r"^\.SH ")
    return section.replace("\\-", "-").replace("\\fB", "").replace("\\fR", "").replace("\\fI", "")


@pytest.fixture(scope="module")
def mcp_options() -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "minideblib", "mcp", "--help"],
        capture_output=True,
        text=True,
        check=True,
        # PYTHONPATH: the tree under test, also without an editable install (RPM %check)
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "COLUMNS": "200", "PYTHONPATH": SRC},
    ).stdout
    found = re.findall(r"^ {2}((?:-\w, )?--[a-z][a-z-]*|-\w)\b", out, re.M)
    return sorted({opt.split(", ")[-1] for opt in found} | {"-v", "-q"})


def test_option_list_is_complete(mcp_options):
    assert "--download-dir" in mcp_options and "--no-download" in mcp_options
    assert "--call-timeout" in mcp_options and "--url" in mcp_options


@pytest.mark.parametrize("name", TOOLS)
def test_tool_in_readme_and_man(name, readme_section, man_section):
    assert f"`{name}`" in readme_section
    assert name in man_section


def test_every_option_in_man_page(mcp_options, man_section):
    missing = [o for o in mcp_options if o not in man_section]
    assert not missing, missing


def test_server_options_in_readme(mcp_options, readme_section):
    own = ("--download-dir", "--no-download", "--allow-host", "--allow-link-local")
    own += ("--max-response-bytes", "--max-download-bytes", "--call-timeout")
    assert all(o in mcp_options and o in readme_section for o in own)


def test_readme_covers_the_topics(readme_section):
    text = " ".join(readme_section.split()).lower()
    for needle in (
        "mcpservers", "not root", "untrusted", "read-only", "allow-host", "link-local",
        "-vv", "stdout", "linux, macos", "architecture", "http_proxy", "--no-download",
        "sha256", "hash",
    ):  # fmt: skip
        assert needle in text, needle


def test_man_page_exit_status_and_signals(man_section):
    assert "EOF" in man_section and "SIGTERM" in man_section
    assert "Exit status" in man_section or "exit status" in man_section


def test_changes_and_todo_mention_the_server():
    unreleased = _section((ROOT / "CHANGES").read_text(), "Unreleased", r"^minideblib \d")
    assert "minideb mcp" in unreleased and "12_mcp_client.py" in unreleased
    todo = (ROOT / "TODO.md").read_text()
    assert "HTTP transport for MCP" in todo and "MCP resources" in todo
    assert "GPG verification" in todo  # nothing removed


def test_example_listed_in_readmes():
    assert "12_mcp_client.py" in README.read_text()
    assert "12_mcp_client.py" in (ROOT / "examples" / "README.md").read_text()
