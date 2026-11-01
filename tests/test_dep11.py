# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""DEP-11 / AppStream metadata: parsing, index selection, client and CLI."""

import gzip
import importlib.util
import io
import json
import re
import sys
import tarfile
import time

import pytest

from minideblib import cli
from minideblib.repo import dep11, release
from minideblib.repo.client import RepoClient
from minideblib.repo.fetch import Fetcher
from minideblib.repo.release import Release
from tests.fixtures.repo import DEP11_YAML, icons_tar, make_repo

SUITE = "stable/slash-test/1.0"
COMPONENTS = "main/dep11/Components-amd64.yml.gz"

needs_yaml = pytest.mark.skipif(
    importlib.util.find_spec("yaml") is None, reason="PyYAML not installed (extra 'dep11')"
)


def mk(root, **kw):
    kw.setdefault("comps", ("main",))
    kw.setdefault("dep11", True)
    return make_repo(root, SUITE, **kw)


@pytest.fixture
def repo(tmp_path):
    return mk(tmp_path / "repo")


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def _synthetic(paths):
    lines = "".join(f" {'0' * 64} 0 {p}\n" for p in paths)
    return Release.parse(f"Components: main\nArchitectures: amd64\nSHA256:\n{lines}")


# --- missing PyYAML (runs without PyYAML too) ----------------------------------


def test_missing_yaml_error_names_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # makes `import yaml` raise ImportError
    with pytest.raises(dep11.MissingYamlError) as exc:
        dep11.parse_components(b"File: DEP-11\n")
    assert isinstance(exc.value, ImportError)
    assert "pip install 'minideblib[dep11]'" in str(exc.value)


def test_cli_missing_yaml_exit_2(repo, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    code, out, err = run(capsys, "--url", repo, "appstream", SUITE)
    assert (code, out) == (2, "")
    assert re.fullmatch(r"minideb: error: .*minideblib\[dep11\].*\n", err)


def test_summarize_without_yaml(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    s = dep11.summarize({"ID": "x", "Name": {"C": "X"}})
    assert (s.id, s.name) == ("x", "X")


# --- parse_components ------------------------------------------------------------


@needs_yaml
def test_parse_components_header_and_components():
    header, comps = dep11.parse_components(DEP11_YAML.encode())
    assert header["File"] == "DEP-11" and header["Origin"] == "example-main"
    assert [c["ID"] for c in comps] == ["org.example.Hello", "org.example.sample-tool"]
    assert comps[0]["Name"] == {"C": "Hello", "de": "Hallo"}
    assert comps[1]["Provides"]["binaries"] == ["sample-tool"]
    assert comps[0]["Icon"]["cached"][0]["width"] == 48  # unknown keys kept, typed scalars
    # timestamps stay strings (JSON-safe)
    assert comps[1]["Releases"][0]["date"] == "2026-01-01"


@needs_yaml
def test_parse_components_empty():
    assert dep11.parse_components(b"") == ({}, [])
    assert dep11.parse_components(b"  \n") == ({}, [])


@needs_yaml
def test_parse_components_missing_header():
    data = b"---\nID: a\nPackage: pa\n---\nID: b\nPackage: pb\n"
    header, comps = dep11.parse_components(data)
    assert header == {}
    assert [c["ID"] for c in comps] == ["a", "b"]


@needs_yaml
def test_parse_components_malformed_names_document():
    data = b"---\nFile: DEP-11\n---\nID: a\n---\nID: [unclosed\n"
    with pytest.raises(dep11.Dep11Error, match="document 2") as exc:
        dep11.parse_components(data)
    assert isinstance(exc.value, ValueError)


@needs_yaml
def test_parse_components_limit():
    _, comps = dep11.parse_components(DEP11_YAML.encode(), limit=1)
    assert [c["ID"] for c in comps] == ["org.example.Hello"]


@needs_yaml
def test_parse_components_never_unsafe():
    with pytest.raises(dep11.Dep11Error):
        dep11.parse_components(b"--- !!python/object/apply:os.system ['true']\n")


@needs_yaml
def test_load_components_size_limit():
    data = gzip.compress(DEP11_YAML.encode())
    header, _ = dep11.load_components(data, "x.yml.gz")
    assert header["Origin"] == "example-main"
    with pytest.raises(ValueError, match="exceeds"):
        dep11.load_components(data, "x.yml.gz", max_size=10)


# --- summarize -------------------------------------------------------------------


def test_summarize_locale_fallback():
    comp = {
        "Type": "desktop-application",
        "ID": "org.example.Hello",
        "Package": "hello",
        "Name": {"C": "Hello", "de": "Hallo"},
        "Summary": {"C": "Greets", "de_DE": "Grüßt"},
        "Categories": ["Utility"],
    }
    s = dep11.summarize(comp, "de_DE.UTF-8")
    assert (s.name, s.summary) == ("Hallo", "Grüßt")
    s = dep11.summarize(comp, "de")
    assert (s.name, s.summary) == ("Hallo", "Greets")
    s = dep11.summarize(comp, "fr")
    assert (s.name, s.summary) == ("Hello", "Greets")
    assert dep11.summarize(comp) == dep11.Dep11Summary(
        "org.example.Hello", "desktop-application", "hello", "Hello", "Greets", ["Utility"]
    )


def test_summarize_any_value_and_missing_keys():
    s = dep11.summarize({"Name": {"fr": "Bonjour"}, "Categories": "bogus"})
    assert s == dep11.Dep11Summary("", "", "", "Bonjour", "", [])
    assert dep11.summarize({}).categories == []
    assert dep11.summarize({"Name": "plain"}).name == "plain"


# --- select_indices ---------------------------------------------------------------


def test_select_dep11_and_icons():
    rel = _synthetic(
        [
            "main/dep11/Components-amd64.yml.gz",
            "main/dep11/Components-amd64.yml.xz",
            "main/dep11/Components-i386.yml.gz",
            "main/dep11/icons-128x128@2.tar.gz",
            "main/dep11/icons-64x64.tar.gz",
            "main/dep11/icons-48x48.tar.gz",
            "main/dep11/icons-48x48.tar.xz",
            "main/dep11/icons-128x128.tar.gz",
            "main/dep11/icons-64x64.tar.bogus",
            "main/dep11/CID-Index-amd64.json.gz",
            "contrib/dep11/icons-32x32.tar.gz",
        ]
    )
    paths = [i.path for i in release.select_indices(rel, "main", "amd64", "dep11")]
    assert paths == ["main/dep11/Components-amd64.yml.xz", "main/dep11/Components-amd64.yml.gz"]
    paths = [i.path for i in release.select_indices(rel, "main", "", "icons")]
    assert paths == [
        "main/dep11/icons-48x48.tar.xz",
        "main/dep11/icons-48x48.tar.gz",
        "main/dep11/icons-64x64.tar.gz",
        "main/dep11/icons-128x128.tar.gz",
        "main/dep11/icons-128x128@2.tar.gz",
    ]
    one = release.select_indices(rel, "main", "", "icons", size="64x64")
    assert [i.path for i in one] == ["main/dep11/icons-64x64.tar.gz"]
    idx = release.select_indices(rel, "main", "amd64", "dep11-index")
    assert [i.path for i in idx] == ["main/dep11/CID-Index-amd64.json.gz"]
    assert release.icon_size("main/dep11/icons-128x128@2.tar.gz") == "128x128@2"
    assert release.icon_size("main/dep11/Components-amd64.yml.gz") is None


def test_select_icons_unsafe_size():
    rel = _synthetic(["main/dep11/icons-48x48.tar.gz"])
    with pytest.raises(release.ReleaseError):
        release.select_indices(rel, "main", "", "icons", size="../x")


def test_select_dep11_fixture(repo):
    rel = release.fetch_release(Fetcher(), repo, SUITE)
    [idx] = release.select_indices(rel, "main", "amd64", "dep11")
    assert idx.path == COMPONENTS
    [icons] = release.select_indices(rel, "main", "amd64", "icons")
    assert icons.path == "main/dep11/icons-48x48.tar.gz"
    data = Fetcher().get(release.index_url(rel, icons))
    rel.verify(icons.path, data)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert tar.getnames() == ["hello_org.example.Hello.png"]


# --- RepoClient ------------------------------------------------------------------


@needs_yaml
def test_client_appstream_tagged(repo):
    c = RepoClient.from_url(repo)
    docs = c.appstream(SUITE)
    assert len(docs) == 4  # two components x two architectures
    assert {(d["ID"], d["_arch"]) for d in docs} == {
        ("org.example.Hello", "amd64"),
        ("org.example.Hello", "i386"),
        ("org.example.sample-tool", "amd64"),
        ("org.example.sample-tool", "i386"),
    }
    assert all(d["_suite"] == SUITE and d["_component"] == "main" for d in docs)
    one = c.appstream(SUITE, "main", "amd64")
    assert [d["ID"] for d in one] == ["org.example.Hello", "org.example.sample-tool"]
    assert c.problems == []


@needs_yaml
def test_client_appstream_cached(repo, monkeypatch):
    c = RepoClient.from_url(repo)
    c.appstream(SUITE, "main", "amd64")
    calls = []
    orig = c.fetcher.get
    monkeypatch.setattr(c.fetcher, "get", lambda url: calls.append(url) or orig(url))
    c.appstream(SUITE, "main", "amd64")
    assert calls == []


@needs_yaml
def test_client_appstream_no_dep11(tmp_path):
    c = RepoClient.from_url(mk(tmp_path / "r", dep11=False))
    assert c.appstream(SUITE) == []
    assert c.icon_indices(SUITE) == []
    assert c.problems == []


@needs_yaml
def test_client_appstream_listed_but_missing_is_problem(tmp_path):
    url = mk(tmp_path / "r", arches=("amd64",))
    (tmp_path / "r" / "dists" / SUITE / COMPONENTS).unlink()
    c = RepoClient.from_url(url)
    assert c.appstream(SUITE) == []
    assert len(c.problems) == 1 and "dep11/Components-amd64.yml" in c.problems[0]


def test_client_icon_indices(repo):
    c = RepoClient.from_url(repo)
    assert [i.path for i in c.icon_indices(SUITE)] == ["main/dep11/icons-48x48.tar.gz"]
    assert c.icon_indices(SUITE, "other") == []


@needs_yaml
def test_client_hash_mismatch(tmp_path):
    url = mk(tmp_path / "r")
    target = tmp_path / "r" / "dists" / SUITE / COMPONENTS
    target.write_bytes(gzip.compress(b"---\nID: evil\n"))
    with pytest.raises(release.HashMismatch):
        RepoClient.from_url(url).appstream(SUITE, "main", "amd64")


# --- CLI -------------------------------------------------------------------------


@needs_yaml
def test_cli_appstream_text(repo, capsys):
    code, out, err = run(capsys, "--url", repo, "appstream", SUITE, "-a", "amd64")
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "org.example.Hello  hello  Hello  Greets the world",
        "org.example.sample-tool  sample-tool  Sample Tool  A command line tool",
    ]


@needs_yaml
def test_cli_appstream_id_and_locale(repo, capsys):
    code, out, _ = run(
        capsys, "--url", repo, "appstream", SUITE, "-a", "amd64", "--id", "org.example.Hello",
        "--locale", "de_DE",
    )  # fmt: skip
    assert (code, out) == (0, "org.example.Hello  hello  Hallo  Grüßt die Welt\n")
    code, out, err = run(capsys, "--url", repo, "appstream", SUITE, "--id", "nope")
    assert (code, out) == (1, "") and err.startswith("minideb: error:")


@needs_yaml
def test_cli_appstream_package_and_search(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "appstream", SUITE, "-a", "amd64", "--package",
                       "sample-tool")  # fmt: skip
    assert code == 0 and out.startswith("org.example.sample-tool  ")
    code, out, _ = run(capsys, "--url", repo, "appstream", SUITE, "-a", "i386", "--search", "GREET")
    assert (code, out) == (0, "org.example.Hello  hello  Hello  Greets the world\n")
    code, out, _ = run(capsys, "--url", repo, "appstream", SUITE, "--search", "zzz")
    assert (code, out) == (0, "")


def test_cli_appstream_filters_exclusive(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--url", repo, "appstream", SUITE, "--id", "a", "--search", "b"])
    assert exc.value.code == 2


@needs_yaml
def test_cli_appstream_json(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "--json", "appstream", SUITE, "-c", "main",
                       "-a", "amd64", "--id", "org.example.sample-tool")  # fmt: skip
    assert code == 0
    [doc] = json.loads(out)
    assert doc["Provides"] == {"binaries": ["sample-tool"], "mediatypes": ["text/x-sample"]}
    assert (doc["_suite"], doc["_component"], doc["_arch"]) == (SUITE, "main", "amd64")
    assert doc["Releases"][0]["date"] == "2026-01-01"


@needs_yaml
def test_cli_appstream_hash_mismatch_exit_2(tmp_path, capsys):
    url = mk(tmp_path / "r")
    target = tmp_path / "r" / "dists" / SUITE / COMPONENTS
    target.write_bytes(gzip.compress(b"---\nID: evil\n"))
    code, out, err = run(capsys, "--url", url, "appstream", SUITE, "-a", "amd64")
    assert code == 2 and out == ""
    assert re.fullmatch(r"minideb: error: .+\n", err)
    code, out, _ = run(capsys, "--url", url, "--no-verify", "appstream", SUITE, "-a", "amd64")
    assert code == 0 and "evil" in out


@needs_yaml
def test_cli_appstream_text_dedupes_arches(repo, capsys):
    code, out, _ = run(capsys, "--url", repo, "appstream", SUITE)
    assert code == 0 and len(out.splitlines()) == 2
    code, out, _ = run(capsys, "--url", repo, "--json", "appstream", SUITE)
    assert len(json.loads(out)) == 4


@needs_yaml
def test_cli_appstream_no_dep11(tmp_path, capsys):
    url = mk(tmp_path / "r", dep11=False)
    assert run(capsys, "--url", url, "appstream", SUITE) == (0, "", "")


def test_cli_icons(tmp_path, capsys):
    repo = mk(tmp_path / "r")
    code, out, err = run(capsys, "--url", repo, "icons", SUITE)
    size = tmp_path / "r" / "dists" / SUITE / "main/dep11/icons-48x48.tar.gz"
    assert (code, err) == (0, "")
    assert out == f"main/dep11/icons-48x48.tar.gz  48x48  {size.stat().st_size}\n"
    code, out, _ = run(capsys, "--url", repo, "--json", "icons", SUITE, "-c", "main")
    assert json.loads(out) == [
        {
            "path": "main/dep11/icons-48x48.tar.gz",
            "component": "main",
            "icon_size": "48x48",
            "bytes": size.stat().st_size,
        }
    ]


def test_cli_help_mentions_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "appstream" in out and "icons" in out


def test_client_flat_repo_has_no_dep11(tmp_path):
    url = make_repo(tmp_path, "./", flat=True)
    c = RepoClient.from_url(url)
    assert c.appstream("./") == [] and c.icon_indices("./") == []
    assert c.problems == []


# --- hostile input (fix round 1) -------------------------------------------------


@needs_yaml
def test_deep_flow_nesting_rejected():
    # crashed the C loader (segfault) before the pre-scan; must fail cleanly in-process
    bomb = ("ID: " + "[" * 50000 + "]" * 50000).encode()
    with pytest.raises(dep11.Dep11Error, match="nesting deeper than 100"):
        dep11.parse_components(bomb)
    with pytest.raises(dep11.Dep11Error, match="nesting"):
        dep11.parse_components(("ID: " + "{a: " * 200 + "}" * 200).encode())


@needs_yaml
def test_deep_block_nesting_rejected():
    deep = "".join(f"{' ' * i}k{i}:\n" for i in range(150)) + " " * 150 + "v: 1\n"
    with pytest.raises(dep11.Dep11Error, match="nesting"):
        dep11.parse_components(deep.encode())
    compact = "ID: x\nL:\n" + "- " * 200 + "x\n"
    with pytest.raises(dep11.Dep11Error, match="nesting"):
        dep11.parse_components(compact.encode())


@needs_yaml
def test_moderate_nesting_accepted():
    ok = "".join(f"{' ' * i}k{i}:\n" for i in range(50)) + " " * 50 + "v: 1\n"
    header, comps = dep11.parse_components(("---\nID: a\n" + ok).encode())
    assert comps[0]["k0"]["k1"]["k2"] is not None
    _, comps = dep11.parse_components(("ID: " + "[" * 90 + "]" * 90).encode())
    assert isinstance(comps[0]["ID"], list)


@needs_yaml
def test_prescan_ignores_brackets_in_scalars_and_comments():
    many = "[" * 500
    data = (
        f'---\nID: \'{many}\'\nName: "{many}\\"{many}"\nKeywords: x  # {many}\n'
        f"Summary: it's {many}\nDescription:\n  C: |-\n    {many}\n    {many}\n"
        f"Url: 'it''s {many}'\n"
    ).encode()
    _, [comp] = dep11.parse_components(data)
    assert comp["ID"] == many and comp["Summary"] == f"it's {many}"
    assert comp["Description"]["C"] == f"{many}\n{many}"


@needs_yaml
def test_quoted_brackets_parse():
    data = ("ID: '" + "[" * 500 + "'\nPackage: p\n").encode()
    _, [comp] = dep11.parse_components(data)
    assert comp["ID"] == "[" * 500


@needs_yaml
def test_alias_bomb_rejected_quickly():
    lines = ['a0: &a0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for i in range(1, 8):
        refs = ",".join([f"*a{i - 1}"] * 9)
        lines.append(f"a{i}: &a{i} [{refs}]")
    lines.append("ID: *a7")
    start = time.monotonic()
    with pytest.raises(dep11.Dep11Error, match="aliases are not supported"):
        dep11.parse_components("\n".join(lines).encode())
    assert time.monotonic() - start < 2


@needs_yaml
def test_recursive_alias_rejected():
    with pytest.raises(dep11.Dep11Error, match="aliases"):
        dep11.parse_components(b"ID: &a [*a]\n")
    with pytest.raises(dep11.Dep11Error, match="aliases"):
        dep11.parse_components(b"Name: &n {C: x}\nSummary: *n\n")


@needs_yaml
def test_set_becomes_sorted_list_and_json_safe(tmp_path, capsys):
    data = b"ID: x\nKeywords: !!set {b: null, a: null}\n"
    _, [comp] = dep11.parse_components(data)
    assert comp["Keywords"] == ["a", "b"]
    url = mk(tmp_path / "r", dep11="---\nID: x\nPackage: p\nKeywords: !!set {b, a}\n")
    code, out, _ = run(capsys, "--url", url, "--json", "appstream", SUITE, "-a", "amd64")
    assert code == 0 and json.loads(out)[0]["Keywords"] == ["a", "b"]


def test_summarize_ignores_non_scalars_and_truncates():
    s = dep11.summarize({"ID": ["x"] * 3, "Package": {"a": 1}, "Name": "n" * 10000, "Type": 5})
    assert (s.id, s.package, s.type) == ("", "", "5")
    assert len(s.name) == dep11.MAX_TEXT
    assert dep11.summarize({"Name": {"C": ["x"]}}).name == ""


def test_select_dep11_kinds_on_flat_repo():
    rel = _synthetic(["Packages.gz"])
    for kind in ("dep11", "dep11-index", "icons"):
        assert release.select_indices(rel, "", "amd64", kind) == []
    with pytest.raises(ValueError):
        release.select_indices(
            rel,
            "",
            "amd64",
            "bogus",  # ty: ignore[invalid-argument-type]  # deliberately invalid kind
        )


@needs_yaml
def test_client_updates_prefix_fallback(tmp_path):
    extra = {
        "main/dep11/Components-amd64.yml.gz": gzip.compress(DEP11_YAML.encode()),
        "main/dep11/icons-48x48.tar.gz": icons_tar(),
    }
    url = make_repo(
        tmp_path, "stable/updates", comps=("updates/main",), arches=("amd64",), extra_files=extra
    )
    c = RepoClient.from_url(url)
    assert [i.path for i in c.icon_indices("stable/updates")] == ["main/dep11/icons-48x48.tar.gz"]
    docs = c.appstream("stable/updates")
    assert {d["_component"] for d in docs} == {"main"} and len(docs) == 2


@needs_yaml
def test_cli_deep_nesting_exit_2(tmp_path, capsys):
    url = mk(tmp_path / "r", dep11="ID: " + "[" * 50000 + "]" * 50000 + "\n")
    code, out, err = run(capsys, "--url", url, "appstream", SUITE, "-a", "amd64")
    assert (code, out) == (2, "")
    assert re.fullmatch(r"minideb: error: .*nesting.*\n", err)


@pytest.mark.parametrize(
    "exc", [RecursionError("deep"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")]
)
def test_cli_recursion_and_unicode_errors_exit_2(repo, capsys, monkeypatch, exc):
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(RepoClient, "icon_indices", boom)
    code, out, err = run(capsys, "--url", repo, "icons", SUITE)
    assert (code, out) == (2, "")
    assert re.fullmatch(r"minideb: error: .+\n", err)


@needs_yaml
def test_huge_integers_kept_as_strings(tmp_path, capsys):
    dec, hexa = "9" * 5000, "0x" + "f" * 5000
    data = f"ID: {dec}\nName: {dec}\nSummary: {hexa}\nPackage: 12\n".encode()
    _, [comp] = dep11.parse_components(data)
    assert comp["ID"] == dec and comp["Summary"] == hexa
    assert comp["Package"] == 12  # ordinary integers are still integers
    s = dep11.summarize(comp)
    assert s.name == dec[: dep11.MAX_TEXT] and s.summary == hexa[: dep11.MAX_TEXT]
    url = mk(tmp_path / "r", dep11=f"---\nID: x\nPackage: p\nName: {dec}\nSummary: {hexa}\n")
    code, out, _ = run(capsys, "--url", url, "--json", "appstream", SUITE, "-a", "amd64")
    assert code == 0 and json.loads(out)[0]["Name"] == dec
    code, out, _ = run(capsys, "--url", url, "appstream", SUITE, "-a", "amd64")
    assert code == 0 and dec[: dep11.MAX_TEXT] in out


@needs_yaml
def test_value_error_from_loader_becomes_dep11_error(monkeypatch):
    yaml = dep11.require_yaml()

    def boom(loader, node):
        raise ValueError("bad value")

    loader = dep11._loader(yaml)
    monkeypatch.setitem(loader.yaml_constructors, "tag:yaml.org,2002:float", boom)
    with pytest.raises(dep11.Dep11Error, match="document 0"):
        dep11.parse_components(b"ID: x\nScore: 1.5\n")
