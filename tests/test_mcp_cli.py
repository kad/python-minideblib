# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""``minideb mcp`` end to end: the server as a subprocess over real pipes.

Every child runs ``python -m minideblib`` (or a small harness script) with an
environment built from scratch (no proxy settings, ``HOME``/``TMPDIR`` in the
test's directory), its working directory in ``tmp_path``, hard timeouts on
every wait, and is killed in ``finally``.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

import minideblib
from minideblib import cli
from minideblib.mcp.protocol import SHUTDOWN_GRACE
from minideblib.mcp.run import _EscapingFormatter, escape_log_text
from tests.fixtures.repo import make_repo
from tests.mcp_client import McpClient

VENDOR = "in" + "tel"
SRC = str(Path(minideblib.__file__).resolve().parent.parent)
SUITE = "stable"
WAIT = 20.0

HARNESS = """\
import logging
import os
import subprocess
import sys
import warnings

from minideblib import cli
from minideblib.mcp import run as mcp_run
from minideblib.mcp.protocol import Tool

if os.environ.get("EXTRA_FDS"):
    import resource

    want = int(os.environ["EXTRA_FDS"])
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < want + 100:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, want + 100), hard))
    extra = [os.open(os.devnull, os.O_RDONLY) for _ in range(want)]

if os.environ.get("FAKE_ROOT"):
    os.getresuid = lambda: (0, 0, 0)
    os.getuid = lambda: 0
    os.geteuid = lambda: 0


def slow(call):
    call.token.wait(60)
    return {"cancelled": call.token.is_cancelled}


def noisy(call):
    print("stray print")
    sys.__stdout__.write("stray dunder write\\n")
    sys.__stdout__.flush()
    os.write(1, b"stray fd write\\n")
    return {"ok": True}


def child(call):
    subprocess.run(["sh", "-c", "echo stray child output"], check=True)
    os.write(1, b"stray fd write\\n")
    return {"ok": True}


def thirdparty(call):
    logging.getLogger("thirdparty").warning("third \\x1b[31m\\u202e\\x00 party")
    warnings.warn("warned \\x1b[31m\\u202e\\x00 text", stacklevel=1)
    return {"ok": True}


def stuck(call):
    import time

    time.sleep(60)  # ignores cancellation
    return {"ok": True}


def boom(call):
    raise RuntimeError("boom \\x1b[31m")


schema = {"type": "object", "properties": {}}
original = mcp_run.build_tools
mcp_run.build_tools = lambda config: [
    *original(config),
    Tool("slow", "Waits until cancelled.", schema, slow, read_only=True),
    Tool("noisy", "Writes to stdout by every means.", schema, noisy, read_only=True),
    Tool("child", "A child process writes to stdout.", schema, child, read_only=True),
    Tool("thirdparty", "Logs and warns outside minideblib.", schema, thirdparty, read_only=True),
    Tool("boom", "Fails.", schema, boom, read_only=True),
    Tool("stuck", "Ignores cancellation.", schema, stuck, read_only=True),
]
raise SystemExit(cli.main(sys.argv[1:]))
"""


# -- helpers --------------------------------------------------------------------------------


def _env(tmp: Path) -> dict[str, str]:
    """A clean environment: only what the child needs, no proxy variables."""
    return {
        "PATH": f"{os.path.dirname(sys.executable)}:/usr/bin:/bin",
        "PYTHONPATH": SRC,
        "HOME": str(tmp),
        "TMPDIR": str(tmp),
        "LC_ALL": "C.UTF-8",
    }


class Child:
    """A running server: an :class:`McpClient` on its pipes, stderr collected."""

    def __init__(self, proc: subprocess.Popen[bytes], read_stderr: bool = True) -> None:
        self.proc = proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        self.client = McpClient(cast(BinaryIO, proc.stdin), cast(BinaryIO, proc.stdout))
        self._err: list[bytes] = []
        self._stderr = proc.stderr
        self._err_thread = threading.Thread(target=self._read_err, daemon=True)
        if read_stderr:
            self._err_thread.start()

    def _read_err(self) -> None:
        for line in self._stderr:
            self._err.append(line)

    def wait(self, timeout: float = WAIT) -> int:
        code = self.proc.wait(timeout)
        if self._err_thread.is_alive():
            self._err_thread.join(timeout)
        return code

    @property
    def stderr(self) -> str:
        return b"".join(self._err).decode("utf-8", "replace")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(WAIT)

    def assert_pure_stdout(self) -> None:
        """Every line the server wrote is one JSON-RPC 2.0 message."""
        assert self.client.raw
        for line in self.client.raw:
            assert line.endswith(b"\n"), line
            msg = json.loads(line)
            assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0", line


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Any]:
    work = tmp_path / "work"
    work.mkdir()
    children: list[Child] = []

    def start(
        *args: str,
        harness: bool = False,
        env: dict[str, str] | None = None,
        close_fds: tuple[int, ...] = (),
        read_stderr: bool = True,
    ) -> Child:
        if harness:
            script = tmp_path / "scripts" / "harness.py"
            script.parent.mkdir(exist_ok=True)
            script.write_text(HARNESS)
            cmd = [sys.executable, "-P", str(script), *args]
        else:
            cmd = [sys.executable, "-P", "-m", "minideblib", *args]
        child = Child(
            subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work,
                env=_env(tmp_path) | (env or {}),
                preexec_fn=(lambda: [os.close(fd) for fd in close_fds]) if close_fds else None,
            ),
            read_stderr=read_stderr,
        )
        children.append(child)
        return child

    try:
        yield start
    finally:
        for child in children:
            child.kill()
            for stream in (child.proc.stdin, child.proc.stdout, child.proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()


def _finished(child: Child, timeout: float = WAIT) -> tuple[int, str, bytes]:
    """Exit status, stderr and stdout of a child that exits by itself (stdin left open)."""
    code = child.wait(timeout)
    child.client.wait_closed(timeout)
    return code, child.stderr, b"".join(child.client.raw)


@pytest.fixture
def repo(tmp_path: Path) -> str:
    return make_repo(
        tmp_path / "repo", SUITE, comps=["main"], arches=("amd64", "arm64", "all"), multiarch=True
    )


def _result(resp: dict[str, Any]) -> dict[str, Any]:
    assert "result" in resp, resp
    result = resp["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


# -- help -----------------------------------------------------------------------------------


def test_top_level_help_lists_mcp() -> None:
    text = cli.build_parser().format_help()
    assert "mcp" in text
    assert "MCP server" in text


def test_mcp_help(spawn: Any) -> None:
    child = spawn("mcp", "--help")
    code, err, out = _finished(child)
    assert code == 0, err
    text = " ".join(out.decode().split())
    assert "security model:" in text
    assert "refuses to run as root" in text
    assert "untrusted" in text
    assert "SHA256" in text
    for option in ("--download-dir", "--no-download", "--allow-host", "--allow-link-local"):
        assert option in text
    assert "at least 4096" in text
    assert VENDOR not in text.lower()


def test_mcp_has_no_alias() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["mc"])


# -- startup errors ---------------------------------------------------------------------------


def _one_error_line(err: str, text: str) -> None:
    lines = err.splitlines()
    assert len(lines) == 1, err
    assert lines[0].startswith("minideb: error: "), err
    assert text in lines[0]
    assert "Traceback" not in err


@pytest.mark.parametrize("verbose", [[], ["-vv"]])
def test_refuses_root_before_reading_stdin(spawn: Any, verbose: list[str]) -> None:
    # stdin stays open: a server reading it before the check would hang
    child = spawn(*verbose, "mcp", harness=True, env={"FAKE_ROOT": "1"})
    code, err, out = _finished(child)
    assert (code, out) == (2, b"")
    _one_error_line(err, "refusing to run as root")


def test_missing_download_dir(spawn: Any, tmp_path: Path) -> None:
    child = spawn("mcp", "--download-dir", str(tmp_path / "missing"))
    code, err, out = _finished(child)
    assert (code, out) == (2, b"")
    _one_error_line(err, "--download-dir")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_unwritable_download_dir(spawn: Any, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        child = spawn("mcp", "--download-dir", str(locked))
        code, err, out = _finished(child)
    finally:
        locked.chmod(0o700)
    assert (code, out) == (2, b"")
    _one_error_line(err, "not writable")


def test_max_response_bytes_minimum(spawn: Any) -> None:
    child = spawn("mcp", "--max-response-bytes", "4095")
    code, err, out = _finished(child)
    assert (code, out) == (2, b"")
    _one_error_line(err, "--max-response-bytes: must be at least 4096")


def test_bad_url_is_a_startup_error(spawn: Any) -> None:
    child = spawn("--url", "ftp://deb.debian.org/debian", "mcp")
    code, err, out = _finished(child)
    assert (code, out) == (2, b"")
    _one_error_line(err, "unsupported URL scheme")


# -- sessions -------------------------------------------------------------------------------


def test_eof_exits_zero(spawn: Any) -> None:
    child = spawn("mcp")
    child.client.initialize()
    child.client.close()
    assert child.wait() == 0
    child.client.wait_eof()
    child.assert_pure_stdout()


def test_no_verify_warns(spawn: Any) -> None:
    child = spawn("--no-verify", "mcp")
    child.client.close()
    assert child.wait() == 0
    assert "minideb: warning: --no-verify" in child.stderr
    assert "always verified" in child.stderr


def test_smoke_session(spawn: Any, repo: str, tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    child = spawn("--url", repo, "mcp", "--download-dir", str(downloads))
    init = child.client.initialize()
    assert init["serverInfo"] == {"name": "minideb", "version": minideblib.__version__}
    assert "untrusted" in init["instructions"]
    tools = child.client.request("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"repository_overview", "search_packages", "download_package"} <= names

    overview = _result(child.client.call("repository_overview", {"suites": [SUITE]}))
    assert overview["repositories"][0]["name"] == "repo1"
    assert overview["suites"][0]["suite"] == SUITE
    assert overview["suites"][0]["architectures"] == ["amd64", "arm64", "all"]
    assert overview["proxies"] == {}
    base = overview["write_roots"][0]
    assert (base["path"], base["writable"]) == (str(downloads.resolve()), True)

    got = _result(child.client.call("download_package", {"suite": SUITE, "name": "sample-data"}))
    [entry] = got["files"]
    assert entry["status"] == "downloaded"
    assert (downloads / entry["name"]).is_file()

    child.client.close()
    assert child.wait() == 0
    child.client.wait_eof()
    child.assert_pure_stdout()


def test_no_download_hides_the_tool(spawn: Any) -> None:
    child = spawn("mcp", "--no-download")
    child.client.initialize()
    tools = child.client.request("tools/list")["result"]["tools"]
    assert "download_package" not in {t["name"] for t in tools}
    child.client.close()
    assert child.wait() == 0


def test_sources_file_names_repositories(spawn: Any, repo: str, tmp_path: Path) -> None:
    sources = tmp_path / "test.list"
    sources.write_text(
        f"deb {repo} {SUITE} main\n"
        "deb http://deb.debian.org/debian stable main\n"
        "deb http://deb.debian.org/debian stable-updates main\n"
    )
    child = spawn("--sources", str(sources), "mcp")
    child.client.initialize()
    overview = _result(child.client.call("repository_overview"))
    shown = [(r["name"], r["url"], r["suites"]) for r in overview["repositories"]]
    assert shown == [
        ("repo1", repo, [SUITE]),
        ("repo2", "http://deb.debian.org/debian", ["stable", "stable-updates"]),
    ]
    child.client.close()
    assert child.wait() == 0


def test_stdout_purity_with_debug_logs(spawn: Any, repo: str) -> None:
    child = spawn("-vv", "--url", repo, "mcp", harness=True)
    child.client.initialize()
    # library warnings: a suite that does not exist
    resp = child.client.call("repository_overview", {"suites": [SUITE, "nosuch"]})
    assert "result" in resp
    assert child.client.call("noisy")["result"]["structuredContent"] == {"ok": True}
    child.client.close()
    assert child.wait() == 0
    child.client.wait_eof()
    child.assert_pure_stdout()
    err = child.stderr
    assert "stray print" in err
    assert "stray dunder write" in err
    assert "stray fd write" in err
    assert "minideb: [+" in err  # -vv: timed debug logs reached stderr
    assert "nosuch" in err


def test_log_records_are_escaped(spawn: Any, repo: str) -> None:
    hostile = "no\x1b[31msuch\u202e\r"
    child = spawn("-v", "--url", repo, "mcp")
    child.client.initialize()
    assert "result" in child.client.call("repository_overview", {"suites": [hostile]})
    child.client.close()
    assert child.wait() == 0
    err = child.stderr
    assert "\\x1b[31msuch\\u202e\\x0d" in err
    for raw in ("\x1b", "\u202e", "\r"):
        assert raw not in err
    child.assert_pure_stdout()


def test_sigterm_cancels_a_call_in_flight(spawn: Any) -> None:
    child = spawn("mcp", harness=True)
    child.client.initialize()
    child.client.send_request("tools/call", {"name": "slow", "arguments": {}})
    assert child.client.no_message(0.5)  # the call is running
    start = time.monotonic()
    child.proc.send_signal(signal.SIGTERM)
    assert child.wait() == 0
    assert time.monotonic() - start < 3.0
    child.client.wait_closed()
    child.assert_pure_stdout()
    # only the initialize answer: no response to the cancelled call
    [only] = [json.loads(line) for line in child.client.raw]
    assert only["id"] == 1 and "protocolVersion" in only["result"]
    assert "Traceback" not in child.stderr


def test_sigint_exits_zero(spawn: Any) -> None:
    child = spawn("mcp")
    child.client.initialize()
    child.proc.send_signal(signal.SIGINT)
    assert child.wait() == 0
    assert "Traceback" not in child.stderr
    assert "KeyboardInterrupt" not in child.stderr


class _Stall(http.server.BaseHTTPRequestHandler):
    """Accepts every request and never answers (until the test releases it)."""

    reached = threading.Event()
    release = threading.Event()

    def do_GET(self) -> None:
        type(self).reached.set()
        type(self).release.wait(WAIT)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def stalled() -> Iterator[str]:
    _Stall.reached.clear()
    _Stall.release.clear()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stall)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/debian"
    finally:
        _Stall.release.set()
        srv.shutdown()
        srv.server_close()


def test_sigterm_with_a_stalled_network_call(spawn: Any, stalled: str) -> None:
    # a socket read ignores cancellation: the server still exits after its grace period
    child = spawn("--url", stalled, "--timeout", "60", "mcp")
    child.client.initialize()
    child.client.send_request("tools/call", {"name": "repository_overview", "arguments": {
        "suites": [SUITE]}})  # fmt: skip
    assert _Stall.reached.wait(WAIT)
    start = time.monotonic()
    child.proc.send_signal(signal.SIGTERM)
    assert child.wait(SHUTDOWN_GRACE + WAIT) == 0
    assert time.monotonic() - start < SHUTDOWN_GRACE + 4
    child.assert_pure_stdout()


# -- in-process units -----------------------------------------------------------------------


def test_escape_log_text() -> None:
    assert escape_log_text("plain text, tab\tand\nnewline") == "plain text, tab\\x09and\\x0anewline"
    assert escape_log_text("\x00\x1b\x7f\x9b") == "\\x00\\x1b\\x7f\\x9b"
    assert escape_log_text("a\u202eb\u200bc\ufeff") == "a\\u202eb\\u200bc\\ufeff"
    assert escape_log_text("tag\U000e0041") == "tag\\U000e0041"
    assert escape_log_text("Debian GNU/Linux été") == "Debian GNU/Linux été"


def test_escaping_formatter_wraps_the_base() -> None:
    fmt = _EscapingFormatter(logging.Formatter("%(levelname)s %(message)s"))
    record = logging.LogRecord("minideblib", logging.WARNING, __file__, 1, "x\x1b%s", ("\r",), None)
    assert fmt.format(record) == "WARNING x\\x1b\\x0d"


# -- review fixes -----------------------------------------------------------------------------


@pytest.mark.parametrize("closed", [(2,), (0, 2)])
def test_closed_std_fds_keep_stdout_pure(spawn: Any, closed: tuple[int, ...]) -> None:
    # a server started with stderr closed must not hand fd 2 to the protocol stream
    child = spawn("--no-verify", "mcp", harness=True, close_fds=closed)
    if 0 in closed:  # no stdin at all: EOF at once
        assert child.wait() == 0
        child.client.wait_closed()
        assert child.client.raw == []
        return
    child.client.initialize()
    assert child.client.call("child")["result"]["structuredContent"] == {"ok": True}
    assert child.client.call("noisy")["result"]["structuredContent"] == {"ok": True}
    child.client.close()
    assert child.wait() == 0
    child.client.wait_closed()
    first = json.loads(child.client.raw[0])
    assert first["id"] == 1 and "result" in first
    child.assert_pure_stdout()


def test_child_process_output_goes_to_stderr(spawn: Any) -> None:
    child = spawn("mcp", harness=True)
    child.client.initialize()
    assert child.client.call("child")["result"]["structuredContent"] == {"ok": True}
    child.client.close()
    assert child.wait() == 0
    child.client.wait_closed()
    child.assert_pure_stdout()
    assert "stray child output" in child.stderr


@pytest.mark.parametrize("option", ["--call-timeout", "--download-timeout", "--timeout"])
def test_time_options_bounded(spawn: Any, option: str) -> None:
    child = spawn("mcp", option, "1e300") if option != "--timeout" else spawn(option, "1e9", "mcp")
    code, err, out = _finished(child)
    assert (code, out) == (2, b"")
    _one_error_line(err, f"{option}: must be at most 604800 seconds")


@pytest.mark.parametrize(
    "argv",
    [["-c", "main", "mcp"], ["mcp", "-a", "amd64"], ["-j", "mcp"], ["mcp", "--json"]],
)
def test_scope_and_json_options_rejected(spawn: Any, argv: list[str]) -> None:
    child = spawn(*argv)
    code, err, out = _finished(child)
    assert (code, out) == (2, b"")
    _one_error_line(err, "minideb mcp does not accept")


def test_mcp_help_states_sources_default_and_eof(spawn: Any) -> None:
    child = spawn("mcp", "--help")
    code, err, out = _finished(child)
    assert code == 0, err
    text = " ".join(out.decode().split())
    assert "/etc/apt" not in text
    assert (
        "--sources PATH sources.list/.sources file or directory whose repositories are "
        "offered as repo1, repo2, ... (default: none; the system sources are not read)"
    ) in text
    assert "At EOF on stdin, as at SIGTERM or SIGINT, calls still running or queued are " \
        "cancelled and get no response." in text  # fmt: skip
    for hidden in ("--component", "--arch", "--json"):
        assert hidden not in text


def test_sigterm_with_a_blocked_stderr(spawn: Any) -> None:
    # nobody reads stderr: -vv logs fill the pipe and the server blocks writing a log
    child = spawn("-vv", "mcp", read_stderr=False)
    child.client.initialize()
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/x" + "A" * 200}).encode() + b"\n"

    def flood() -> None:
        try:
            for _ in range(20000):
                child.client.send_raw(note)
        except (OSError, ValueError):
            pass

    writer = threading.Thread(target=flood, daemon=True)
    writer.start()
    writer.join(2.0)
    assert writer.is_alive()  # the server stopped reading: it is stuck in a log write
    start = time.monotonic()
    child.proc.send_signal(signal.SIGTERM)
    assert child.proc.wait(SHUTDOWN_GRACE + WAIT) == 0
    assert time.monotonic() - start < SHUTDOWN_GRACE + 5


def test_second_signal_exits_at_once(spawn: Any) -> None:
    child = spawn("mcp", harness=True)
    child.client.initialize()
    # the first signal waits SHUTDOWN_GRACE for a call that ignores cancellation
    child.client.send_request("tools/call", {"name": "stuck", "arguments": {}})
    assert child.client.no_message(0.3)
    child.proc.send_signal(signal.SIGTERM)
    time.sleep(0.2)
    start = time.monotonic()
    child.proc.send_signal(signal.SIGTERM)
    assert child.proc.wait(WAIT) == 0
    assert time.monotonic() - start < 2.0


def test_third_party_logs_and_warnings_are_escaped(spawn: Any) -> None:
    child = spawn("mcp", harness=True)
    child.client.initialize()
    assert child.client.call("thirdparty")["result"]["structuredContent"] == {"ok": True}
    child.client.close()
    assert child.wait() == 0
    err = child.stderr
    assert "third \\x1b[31m\\u202e\\x00 party" in err
    assert "warned \\x1b[31m\\u202e\\x00 text" in err
    for raw in ("\x1b", "\u202e", "\x00"):
        assert raw not in err


def test_tracebacks_at_debug_level_are_one_escaped_line(spawn: Any) -> None:
    child = spawn("-vv", "mcp", harness=True)
    child.client.initialize()
    assert child.client.call("boom")["result"]["isError"] is True
    child.client.close()
    assert child.wait() == 0
    [line] = [x for x in child.stderr.splitlines() if "Traceback" in x]
    assert "tool boom failed\\x0aTraceback (most recent call last):" in line
    assert "RuntimeError: boom \\x1b[31m" in line
    assert "\x1b" not in child.stderr


def test_tracebacks_hidden_without_debug(spawn: Any) -> None:
    child = spawn("mcp", harness=True)
    child.client.initialize()
    assert child.client.call("boom")["result"]["isError"] is True
    child.client.close()
    assert child.wait() == 0
    assert "tool boom failed" in child.stderr
    assert "Traceback" not in child.stderr


def test_stdin_descriptor_above_1024(spawn: Any) -> None:
    import resource

    if resource.getrlimit(resource.RLIMIT_NOFILE)[1] < 1300:
        pytest.skip("descriptor limit too low")
    child = spawn("mcp", harness=True, env={"EXTRA_FDS": "1100"})
    child.client.initialize()
    assert child.client.request("ping")["result"] == {}
    child.client.close()
    assert child.wait() == 0
    child.assert_pure_stdout()


def test_stdin_from_a_regular_file(tmp_path: Path) -> None:
    # epoll refuses regular files; the reader falls back to plain reads
    requests = tmp_path / "requests.jsonl"
    requests.write_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
    with requests.open("rb") as stdin:
        done = subprocess.run(
            [sys.executable, "-P", "-m", "minideblib", "mcp"],
            stdin=stdin,
            capture_output=True,
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout=WAIT,
        )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {}}
