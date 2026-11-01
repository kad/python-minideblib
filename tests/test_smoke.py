# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import re
import tomllib
from pathlib import Path

import pytest

import minideblib
from tests import conftest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version():
    # not pinned: "make release-prepare" changes it (autopkgtest has no pyproject.toml)
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", minideblib.__version__)
    if PYPROJECT.is_file():
        project = tomllib.loads(PYPROJECT.read_text())["project"]
        assert minideblib.__version__ == project["version"]


class _Item:
    def __init__(self, *keywords: str) -> None:
        self.keywords = dict.fromkeys(keywords, True)
        self.markers: list[pytest.MarkDecorator] = []

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.markers.append(marker)


@pytest.mark.parametrize("env", [None, "0", "1"])
def test_network_marker_skip_logic(monkeypatch, env):
    if env is None:
        monkeypatch.delenv("MINIDEB_NETWORK", raising=False)
    else:
        monkeypatch.setenv("MINIDEB_NETWORK", env)
    net, plain = _Item("network"), _Item()
    conftest.pytest_collection_modifyitems(None, [net, plain])
    assert plain.markers == []
    if env == "1":
        assert net.markers == []
    else:
        assert [m.name for m in net.markers] == ["skip"]
