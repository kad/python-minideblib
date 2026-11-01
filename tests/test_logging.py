# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Verbose/debug logging, ``-q`` and command aliases of the ``minideb`` CLI."""

import json
import logging
import re
import time
from typing import Any

import pytest

from minideblib import cli
from minideblib.repo import client as client_mod
from minideblib.repo.fetch import Fetcher, FetchError, redact_url
from tests import test_fetch
from tests.fixtures.debs import make_deb
from tests.fixtures.repo import make_repo
from tests.test_cli import PKG, SUITE, mk, srcrepo  # noqa: F401

VENDOR = "in" + "tel"
server = test_fetch.server  # reuse the local HTTP server fixture


def run(capsys: pytest.CaptureFixture[str], *argv: Any) -> tuple[int, str, str]:
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def repo(tmp_path: Any) -> str:
    return mk(tmp_path / "repo", packages=PKG)


def test_redact_url() -> None:
    assert redact_url("http://u:secret@h:80/p?q=1") == "http://h:80/p?q=1"
    assert redact_url("https://tok@h/p") == "https://h/p"
    assert redact_url("http://h/a@b") == "http://h/a@b"
    assert redact_url("file:///tmp/x") == "file:///tmp/x"
    assert redact_url("not a url") == "not a url"


def test_debug_logs_requests_and_info_found_suite(repo, capsys):  # a
    code, out, err = run(capsys, "-vv", "--url", repo, "suites", "--suite", SUITE)
    assert (code, out) == (0, SUITE + "\n")
    assert re.search(r"^minideb: \[\+\d+\.\d{3}s\] .*InRelease.*status", err, re.M)
    assert f"found suite {SUITE}" in err
    assert "attempt" in err
    assert "Traceback" not in err


def test_verbose_info_only(repo, capsys):  # b
    code, out, err = run(capsys, "-v", "--url", repo, "suites", "--suite", SUITE)
    assert (code, out) == (0, SUITE + "\n")
    assert f"minideb: found suite {SUITE}" in err
    assert "status" not in err and "[+" not in err


def test_default_is_quiet(repo, capsys):  # c
    assert run(capsys, "--url", repo, "suites", "--suite", SUITE) == (0, SUITE + "\n", "")


def test_json_debug_keeps_stdout_pure(repo, capsys):  # d
    code, out, err = run(capsys, "--json", "-vv", "--url", repo, "components", SUITE)
    assert (code, json.loads(out)) == (0, ["main"])
    assert "InRelease" in err
    code, out, err = run(capsys, "-j", "-vv", "--url", repo, "list", SUITE, "-c", "main")
    assert code == 0 and isinstance(json.loads(out), list) and "index " in err


def test_info_lines_for_release_and_index(repo, capsys):
    _, _, err = run(capsys, "-v", "--url", repo, "list", SUITE, "-c", "main", "-a", "amd64")
    assert f"fetching Release for suite {SUITE}" in err
    assert "using InRelease" in err
    assert re.search(r"index main/binary-amd64/Packages\S* \(\d+ bytes, sha512 verified\)", err)


def test_debug_details(tmp_path, capsys):
    url = mk(tmp_path / "r", packages=PKG, by_hash=True)
    _, _, err = run(capsys, "-vv", "--url", url, "list", SUITE, "-c", "main")
    for needle in ("by-hash", "candidate", "codec", "parsed"):
        assert needle in err, needle
    # one parse line per index, not per package
    assert len([ln for ln in err.splitlines() if "parsed" in ln]) <= 2


def test_credentials_never_logged(capsys, tmp_path):  # e
    url = "http://user:secret@127.0.0.1:1/debian"
    for flags in (["-vv"], ["-v"], []):
        code, out, err = run(capsys, *flags, "--retries", "1", "--url", url, "components", "x")
        assert code == 2
        assert "secret" not in err and "user:" not in err
        assert "secret" not in out
    code, _, err = run(capsys, "-vv", "--url", url, "--retries", "0", "suites", "--suite", "x")
    assert "secret" not in err


def test_credentials_not_in_logs_via_fetcher(server, caplog):
    base, _ = server
    url = base.replace("http://", "http://user:secret@") + "/hello.txt"
    caplog.set_level(logging.DEBUG, logger="minideblib")
    # credentials in URLs are rejected up front, without echoing them
    with pytest.raises(FetchError) as ei:
        Fetcher(retries=0, sleep=lambda s: None).get(url)
    assert "secret" not in str(ei.value) and "secret" not in ei.value.url
    assert "secret" not in caplog.text


def test_retry_logging(server, caplog):  # f
    base, _ = server
    caplog.set_level(logging.DEBUG, logger="minideblib")
    sleeps: list[float] = []
    f = Fetcher(sleep=sleeps.append)
    assert f.get(base + "/flaky-503-1") == b"finally"
    text = caplog.text
    assert "attempt 1" in text and "attempt 2" in text
    assert "503" in text
    assert "retrying in 0.5s" in text
    assert "status=200" in text and "7 bytes" in text


def test_retry_logging_cli(server, capsys, monkeypatch):  # f, CLI level
    base, _ = server
    monkeypatch.setattr("minideblib.repo.fetch._MAX_DELAY", 0.0)  # no backoff wait
    code, _, err = run(capsys, "-vv", "--retries", "1", "--url", base, "suites", "--suite", "x")
    assert code == 1  # nothing found
    assert "attempt" in err


def test_redirect_logged(server, caplog):
    base, _ = server
    caplog.set_level(logging.DEBUG, logger="minideblib")
    assert Fetcher(retries=0).get(base + "/redir-ok") == b"hello world"
    assert re.search(r"redirect .*/redir-ok -> .*/hello.txt", caplog.text)


def test_flags_after_subcommand(repo, capsys):  # g
    before = run(capsys, "-vv", "--url", repo, "suites", "--suite", SUITE)
    after = run(capsys, "suites", "--suite", SUITE, "-vv", "--url", repo)
    assert before[:2] == after[:2]
    assert "InRelease" in after[2] and f"found suite {SUITE}" in after[2]
    code, out, _ = run(capsys, "components", SUITE, "--url", repo, "--json")
    assert (code, json.loads(out)) == (0, ["main"])


def test_verbose_counts_add(repo, capsys):
    _, _, err = run(capsys, "-v", "--url", repo, "suites", "--suite", SUITE, "-v")
    assert "[+" in err


def test_after_value_wins_without_default_clobber(repo, capsys, tmp_path):
    # value before the subcommand survives when not repeated afterwards
    code, out, _ = run(capsys, "--json", "--url", repo, "components", SUITE, "--timeout", "5")
    assert code == 0 and json.loads(out) == ["main"]
    ns = cli.parse_args(["--timeout", "7", "components", "x", "--timeout", "9", "--url", "u"])
    assert ns.timeout == 9 and ns.url == ["u"]
    ns = cli.parse_args(["--timeout", "7", "components", "x"])
    assert ns.timeout == 7 and ns.retries == 2 and ns.verify is True and ns.json is False
    ns = cli.parse_args(["deb", "info", "f", "--no-verify", "-q", "-j"])
    assert ns.verify is False and ns.quiet and ns.json


def test_quiet_hides_problems(tmp_path, capsys):  # h
    url = mk(tmp_path / "r", packages=PKG)
    code, _, err = run(capsys, "--url", url, "list", SUITE, "-c", "nosuchcomp")
    assert code == 0 and "minideb: warning:" in err
    code, out, err = run(capsys, "-q", "--url", url, "list", SUITE, "-c", "nosuchcomp")
    assert code == 0 and err == ""
    code, _, err = run(capsys, "--url", url, "list", SUITE, "-c", "nosuchcomp", "-q")
    assert code == 0 and err == ""
    code, _, err = run(capsys, "-q", "--url", url, "components", "nosuchsuite")
    assert code == 2 and err.startswith("minideb: error:")


ALIASES = [
    ("su", "suites", ["--suite", SUITE]),
    ("comps", "components", [SUITE]),
    ("arch", "archs", [SUITE]),
    ("ls", "list", [SUITE, "-c", "main"]),
    ("sh", "show", [SUITE, "sample"]),
    ("ver", "versions", ["sample", "--suite", SUITE]),
    ("find", "search", [SUITE, "sample"]),
    ("contents", "files", [SUITE, "tool-i386"]),
    ("src", "source", ["--help"]),
]


@pytest.mark.parametrize(("alias", "canon", "rest"), ALIASES)
def test_aliases_same_behaviour(repo, capsys, alias, canon, rest):  # i
    if rest == ["--help"]:
        ns = cli.parse_args([alias, "x", "y"])
        assert ns.command == canon
        return
    a = run(capsys, "--url", repo, alias, *rest)
    b = run(capsys, "--url", repo, canon, *rest)
    assert a == b and a[0] == 0
    assert cli.parse_args(["--url", repo, alias, *rest]).command == canon


def test_alias_source_and_appstream_resolve(srcrepo, capsys):  # noqa: F811
    a = run(capsys, "--url", srcrepo, "src", SUITE, "sample-pkg")
    b = run(capsys, "--url", srcrepo, "source", SUITE, "sample-pkg")
    assert a == b
    assert cli.parse_args(["as", "s"]).command == "appstream"


def test_deb_dsc_aliases(tmp_path):
    p = cli.parse_args
    for alias, canon, extra in (
        ("i", "info", ["f"]),
        ("control", "ctrl", ["f", "d"]),
        ("list", "ls", ["f"]),
        ("x", "extract", ["f", "d"]),
        ("f", "field", ["f"]),
    ):
        ns = p(["deb", alias, *extra])
        assert ns.command == "deb" and ns.deb_command == canon
        assert ns.func is p(["deb", canon, *extra]).func


def test_deb_alias_output_equal(tmp_path, capsys):
    deb = tmp_path / "x.deb"
    deb.write_bytes(
        make_deb({"Package": "x", "Version": "1", "Architecture": "all"}, {"usr/x": b"x"})
    )
    assert run(capsys, "deb", "ls", deb) == run(capsys, "deb", "list", deb)
    assert run(capsys, "deb", "i", deb) == run(capsys, "deb", "info", deb)


@pytest.mark.parametrize("bad", ["l", "s", "co"])
def test_abbreviations_not_guessed(bad, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main([bad])
    assert ei.value.code == 2


def test_ls_works_and_l_rejected(repo, capsys):
    assert run(capsys, "--url", repo, "ls", SUITE, "-c", "main")[0] == 0
    with pytest.raises(SystemExit) as ei:
        cli.main(["--url", repo, "l", SUITE])
    assert ei.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_help_lists_aliases(capsys):  # j
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for line in (
        "list (ls)",
        "suites (su)",
        "components (comps)",
        "archs (arch)",
        "show (sh)",
        "versions (ver)",
        "search (find)",
        "files (contents)",
        "appstream (as)",
        "source (src)",
    ):
        assert line in text, line
    assert "-v, --verbose" in text and "-q, --quiet" in text and "-j, --json" in text
    assert VENDOR not in text.lower()
    with pytest.raises(SystemExit):
        cli.main(["deb", "--help"])
    dtext = capsys.readouterr().out
    for line in ("info (i)", "ctrl (control)", "ls (list)", "extract (x)", "field (f)"):
        assert line in dtext, line
    assert VENDOR not in dtext.lower()


def test_no_stdout_logging_handlers(repo, capsys):
    run(capsys, "-vv", "--url", repo, "suites", "--suite", SUITE)
    lg = logging.getLogger("minideblib")
    assert not [h for h in lg.handlers if getattr(h, "_minideb", False)]


def test_cache_hits_logged(repo, caplog):
    from minideblib.repo.client import RepoClient

    caplog.set_level(logging.DEBUG, logger="minideblib")
    client = RepoClient.from_url(repo, suites=[SUITE])
    client.components(SUITE)
    client.packages(SUITE, "main", "amd64")
    client.packages(SUITE, "main", "amd64")
    assert "Release cache hit" in caplog.text
    assert "index cache hit" in caplog.text


# -- Task 24: quiet probing, --probe, --url extends, redaction, teardown --------


@pytest.fixture
def two_suites(tmp_path: Any) -> str:
    for s in ("stable", "noble"):
        url = make_repo(tmp_path / "two", suite=s, comps=("main",), arches=("amd64",))
    return url


def _lines(err: str) -> list[str]:
    return [re.sub(r"^minideb: (\[\+\d+\.\d{3}s\] )?", "", ln) for ln in err.splitlines()]


def test_suites_info_output_is_short(two_suites, capsys):
    code, out, err = run(capsys, "-v", "--url", two_suites, "suites")
    n = len(client_mod.CANDIDATE_SUITES)
    assert (code, out) == (0, "stable\nnoble\n")
    assert _lines(err) == [
        f"probing {n} candidate suites on {two_suites}",
        "found suite stable",
        "found suite noble",
        f"probed {n} suites: 2 found, {n - 2} absent, 0 errors",
    ]


def test_suites_debug_output_one_absent_line(two_suites, capsys):
    code, _, err = run(capsys, "-vv", "--url", two_suites, "suites")
    assert code == 0
    absent = [ln for ln in _lines(err) if ln.startswith("absent: ")]
    expected = [s for s in client_mod.CANDIDATE_SUITES if s not in ("stable", "noble")]
    assert absent == ["absent: " + ", ".join(expected)]
    assert not re.search(r"probe \S+: absent", err)
    assert "HEAD " in err  # Fetcher request lines are kept


class _Counting(Fetcher):
    calls: list[str] = []

    def exists(self, url: str) -> bool:
        type(self).calls.append(url)
        return super().exists(url)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _Counting.calls = []
    monkeypatch.setattr(cli, "Fetcher", _Counting)
    return _Counting.calls


def _suite_of(url: str) -> str:
    return url.split("/dists/", 1)[1].rsplit("/", 1)[0]


def test_cli_probe_debian(two_suites, capsys, counting):
    code, out, _ = run(capsys, "--url", two_suites, "suites", "--probe", "debian")
    assert (code, out) == (0, "stable\n")
    probed = [_suite_of(u) for u in counting]
    assert set(probed) <= set(client_mod.DEBIAN_SUITES)
    assert all(probed.count(s) <= 2 for s in client_mod.DEBIAN_SUITES)
    code, out, _ = run(capsys, "--url", two_suites, "suites", "--probe", "ubuntu")
    assert (code, out) == (0, "noble\n")


@pytest.mark.parametrize("flag", [["--probe", "none"], ["--no-probe"]])
def test_cli_probe_none(two_suites, capsys, counting, flag):
    code, out, _ = run(capsys, "--url", two_suites, "suites", *flag)
    assert (code, out, counting) == (1, "", [])  # nothing to probe: nothing found
    code, out, _ = run(capsys, "--url", two_suites, "suites", *flag, "--suite", "noble")
    assert (code, out) == (0, "noble\n")
    assert {_suite_of(u) for u in counting} == {"noble"}


def test_cli_probe_help_explains_cost(capsys):
    with pytest.raises(SystemExit):
        cli.main(["suites", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--no-probe" in text and "two requests" in text and "directory listing" in text


def test_url_after_command_extends(tmp_path, capsys):
    ns = cli.parse_args(["--url", "A", "suites", "--url", "B", "--url", "A"])
    assert ns.url == ["A", "B"]
    assert cli.parse_args(["suites", "--url", "B"]).url == ["B"]
    assert cli.parse_args(["--url", "A", "suites"]).url == ["A"]
    a = mk(tmp_path / "a", packages=[{"Package": "p", "Version": "1", "Filename": "a.deb"}])
    b = mk(tmp_path / "b", packages=[{"Package": "p", "Version": "2", "Filename": "b.deb"}])
    code, out, _ = run(capsys, "--url", a, "versions", "p", "--suite", SUITE, "-a", "amd64",
                       "--url", b)  # fmt: skip
    assert code == 0
    assert out.splitlines() == [f"{SUITE} p 2 amd64 main", f"{SUITE} p 1 amd64 main"]


def test_redact_url_more() -> None:
    assert redact_url("u:p@h/x") == "h/x"
    assert redact_url("tok@h/x") == "h/x"
    assert redact_url("jane@example.com") == "jane@example.com"  # not URL-like enough
    msg = "cannot fetch http://user:pw@host/x: refused, retry https://a:b@c/d"
    assert redact_url(msg) == "cannot fetch http://host/x: refused, retry https://c/d"
    assert redact_url("file:///a@b/c") == "file:///a@b/c"
    assert redact_url("http://[::1]:8080/a@b") == "http://[::1]:8080/a@b"
    assert redact_url("http://u:p@[::1]:8080/x") == "http://[::1]:8080/x"
    assert redact_url("see file:///a@b/c and http://h/a@b") == "see file:///a@b/c and http://h/a@b"


def test_teardown_restores_logger_state(repo, capsys):
    lg = logging.getLogger("minideblib")
    for level, propagate in ((logging.INFO, True), (logging.ERROR, False)):
        lg.setLevel(level)
        lg.propagate = propagate
        try:
            run(capsys, "-vv", "--url", repo, "suites", "--suite", SUITE)
            assert (lg.level, lg.propagate) == (level, propagate)
        finally:
            lg.setLevel(logging.NOTSET)
            lg.propagate = True


@pytest.mark.parametrize(
    "text",
    [
        "a" * 200_000,
        "a:" * 100_000,
        "a://" * 50_000,
        "@" * 200_000,
        "a.b+c-" * 40_000,
        "x" * 199_990 + "://u@",
    ],
)
def test_redact_url_linear_on_hostile_input(text) -> None:
    started = time.monotonic()
    redact_url(text)
    redact_url("see " + text + " end")
    assert time.monotonic() - started < 2.0


def test_redact_url_unchanged_output() -> None:
    assert redact_url("see http://u:p@h/x and ftp+x://a:b@c d") == "see http://h/x and ftp+x://c d"
