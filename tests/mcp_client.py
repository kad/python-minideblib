# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""A minimal MCP stdio test client: newline-delimited JSON over two byte streams.

:class:`McpClient` drives any server whose stdin/stdout are given as binary
files (pipes to an in-process :class:`~minideblib.mcp.protocol.McpServer` or
to a subprocess). A background thread reads the server's output, so every
wait is bounded. :func:`start_in_process` runs a server on ``os.pipe`` pairs
in a thread.

For subprocess tests: :class:`Spawner` starts ``python -m minideblib`` (or a
harness script) with an environment built from scratch (:func:`child_env`)
and kills every child it started on :meth:`Spawner.close`;
:class:`RepoServer` serves a directory over HTTP from a thread (with hooks
that run halfway through a body and trickled bodies) and
:class:`ProxyServer` is a forwarding HTTP proxy for absolute-URI requests.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

import minideblib
from minideblib.mcp.protocol import McpServer, Tool

#: default bound of every wait, in seconds
TIMEOUT = 10.0


class McpClient:
    """Send requests to a server's stdin and collect its stdout lines."""

    def __init__(self, to_server: BinaryIO, from_server: BinaryIO, *, read: bool = True) -> None:
        self._out = to_server
        self._in = from_server
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        #: every raw line received (with its newline), for stdout purity checks
        self.raw: list[bytes] = []
        self._next_id = 1
        self._reader = threading.Thread(target=self._read, daemon=True)
        if read:  # read=False: nobody reads the server's output (a full pipe)
            self._reader.start()

    def _read(self) -> None:
        try:
            for line in self._in:
                self.raw.append(line)
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    # -- sending ---------------------------------------------------------

    def send_raw(self, data: bytes) -> None:
        self._out.write(data)
        self._out.flush()

    def send(self, message: object) -> None:
        self.send_raw(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        """Send a request without waiting; return its id."""
        rid = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return rid

    def close(self) -> None:
        """Close the server's stdin (EOF)."""
        with contextlib.suppress(OSError):
            self._out.close()

    # -- receiving -------------------------------------------------------

    def recv(self, timeout: float = TIMEOUT) -> dict[str, Any]:
        """The next message from the server (AssertionError on timeout/EOF)."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no message from the server within {timeout} s") from None
        if line is None:
            raise AssertionError("server closed its output")
        assert line.endswith(b"\n"), line
        msg = json.loads(line)
        assert isinstance(msg, dict), msg
        return msg

    def no_message(self, timeout: float = 0.2) -> bool:
        """True if nothing arrives within *timeout* seconds."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return True
        self._lines.put(line)
        return False

    def wait_eof(self, timeout: float = TIMEOUT) -> None:
        """Wait until the server closes its output; fail on any message."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("server output not closed") from None
        assert line is None, line

    def wait_closed(self, timeout: float = TIMEOUT) -> None:
        """Wait until the server closes its output; messages stay in :attr:`raw`."""
        self._reader.join(timeout)
        assert not self._reader.is_alive(), "server output not closed"

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = TIMEOUT
    ) -> dict[str, Any]:
        """Send a request and return its response (the next message)."""
        rid = self.send_request(method, params)
        msg = self.recv(timeout)
        assert msg.get("id") == rid, msg
        return msg

    def initialize(self, version: str = "2025-06-18") -> dict[str, Any]:
        """Run the handshake; return the ``initialize`` result."""
        resp = self.request(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        )
        assert "result" in resp, resp
        self.notify("notifications/initialized")
        return resp["result"]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """A ``tools/call`` response (the whole JSON-RPC message)."""
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params)


class InProcess:
    """A server on ``os.pipe`` pairs, served by a thread."""

    def __init__(self, server: McpServer, client: McpClient, thread: threading.Thread) -> None:
        self.server = server
        self.client = client
        self.thread = thread
        self.exit_code: int | None = None

    def stop(self, timeout: float = TIMEOUT) -> int | None:
        """Send EOF, wait for ``serve()`` to return; its exit code."""
        self.client.close()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "serve() did not return"
        return self.exit_code


def start_in_process(tools: Sequence[Tool], **kwargs: Any) -> InProcess:
    """Start ``McpServer(tools, ...).serve()`` in a thread, connected by pipes."""
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    stdin = os.fdopen(in_r, "rb")
    stdout = os.fdopen(out_w, "wb")
    kwargs.setdefault("name", "test-server")
    kwargs.setdefault("version", "1.0")
    kwargs.setdefault("instructions", "Test instructions.")
    server = McpServer(tools, stdin=stdin, stdout=stdout, **kwargs)
    client = McpClient(os.fdopen(in_w, "wb"), os.fdopen(out_r, "rb"))
    holder: list[InProcess] = []

    def run() -> None:
        try:
            code = server.serve()
        finally:
            stdin.close()
            stdout.close()
        holder[0].exit_code = code

    thread = threading.Thread(target=run, daemon=True)
    proc = InProcess(server, client, thread)
    holder.append(proc)
    thread.start()
    return proc


def structured(resp: dict[str, Any]) -> dict[str, Any]:
    """The ``structuredContent`` of a successful ``tools/call`` response."""
    assert "result" in resp, resp
    result = resp["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def error_text(resp: dict[str, Any]) -> str:
    """The message of an ``isError`` tool result."""
    assert "result" in resp, resp
    result = resp["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


# -- subprocesses ---------------------------------------------------------------------------

#: the source tree the children import minideblib from
SRC = str(Path(minideblib.__file__).resolve().parent.parent)
#: bound of every wait on a child, in seconds
WAIT = 20.0


def child_env(tmp: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A clean environment: only what the child needs, no proxy variables (unless *extra*)."""
    env = {
        "PATH": f"{os.path.dirname(sys.executable)}:/usr/bin:/bin",
        "PYTHONPATH": SRC,
        "HOME": str(tmp),
        "TMPDIR": str(tmp),
        "LC_ALL": "C.UTF-8",
    }
    env.update(extra or {})
    return env


def skip_as_root() -> None:
    """Skip the calling test when running as root (``minideb mcp`` refuses to start)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("minideb mcp refuses to run as root")


class Child:
    """A running server: an :class:`McpClient` on its pipes, stderr collected."""

    def __init__(
        self, proc: subprocess.Popen[bytes], read_stderr: bool = True, read_stdout: bool = True
    ) -> None:
        self.proc = proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        self.client = McpClient(
            cast(BinaryIO, proc.stdin), cast(BinaryIO, proc.stdout), read=read_stdout
        )
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

    def stop(self) -> None:
        """EOF on stdin; the server must exit 0 and close its output."""
        self.client.close()
        assert self.wait() == 0, self.stderr
        self.client.wait_closed()

    def assert_pure_stdout(self) -> None:
        """Every line the server wrote is one JSON-RPC 2.0 message."""
        assert self.client.raw
        for line in self.client.raw:
            assert line.endswith(b"\n"), line
            msg = json.loads(line)
            assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0", line


class Spawner:
    """Starts servers below *tmp* (cwd ``tmp/work``); :meth:`close` kills them all."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.work = tmp / "work"
        self.work.mkdir(exist_ok=True)
        self.children: list[Child] = []

    def __call__(
        self,
        *args: str,
        script: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        close_fds: tuple[int, ...] = (),
        read_stderr: bool = True,
        read_stdout: bool = True,
        allow_root: bool = False,
    ) -> Child:
        """``python -P -m minideblib *args`` (or ``python -P <script> *args``).

        As root the server refuses to start, so the test is skipped unless
        *allow_root* (a test of that refusal, or one faking the user ids).
        """
        if not allow_root:
            skip_as_root()
        if script is not None:
            path = self.tmp / "scripts" / f"harness{len(self.children)}.py"
            path.parent.mkdir(exist_ok=True)
            path.write_text(script)
            cmd = [sys.executable, "-P", str(path), *args]
        else:
            cmd = [sys.executable, "-P", "-m", "minideblib", *args]
        child = Child(
            subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or self.work,
                env=child_env(self.tmp, env),
                preexec_fn=(lambda: [os.close(fd) for fd in close_fds]) if close_fds else None,
            ),
            read_stderr=read_stderr,
            read_stdout=read_stdout,
        )
        self.children.append(child)
        return child

    def close(self) -> None:
        for child in self.children:
            child.kill()
            for stream in (child.proc.stdin, child.proc.stdout, child.proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()


# -- HTTP servers ---------------------------------------------------------------------------


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(srv: http.server.ThreadingHTTPServer) -> threading.Thread:
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    return thread


class _Body:
    """How :class:`RepoServer` sends one file: halfway hook and trickle rate."""

    def __init__(
        self, hooks: Mapping[str, Callable[[], None]], trickle: Mapping[str, float]
    ) -> None:
        self.hooks = hooks
        self.trickle = trickle

    def send(self, handler: http.server.BaseHTTPRequestHandler, path: str, data: bytes) -> None:
        hook = next((h for s, h in self.hooks.items() if path.endswith(s)), None)
        delay = next((d for s, d in self.trickle.items() if path.endswith(s)), None)
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Content-Type", "application/octet-stream")
        handler.end_headers()
        try:
            if delay is not None:
                for i in range(len(data)):
                    handler.wfile.write(data[i : i + 1])
                    handler.wfile.flush()
                    time.sleep(delay)
                return
            half = len(data) // 2
            handler.wfile.write(data[:half])
            handler.wfile.flush()
            if hook is not None:
                hook()
            handler.wfile.write(data[half:])
            handler.wfile.flush()
        except OSError:  # the client went away (cancelled)
            pass


def _file_under(root: Path, url_path: str) -> bytes | None:
    """The file *url_path* (``/a/b``) below *root*, or None (no traversal, no directories)."""
    parts = [p for p in url_path.split("?", 1)[0].split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None
    target = root.joinpath(*parts)
    return target.read_bytes() if target.is_file() else None


class RepoServer:
    """Serves the directory *root* at ``http://127.0.0.1:PORT/`` from a thread.

    :attr:`hooks` maps a path suffix to a callable run halfway through that
    body (in the server thread); :attr:`trickle` maps a path suffix to the
    seconds slept after each byte; :attr:`replace` maps a path suffix to the
    body sent instead of the file (with its own ``Content-Length``);
    :attr:`paths` records every request path.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hooks: dict[str, Callable[[], None]] = {}
        self.trickle: dict[str, float] = {}
        self.replace: dict[str, bytes] = {}
        self.paths: list[str] = []
        self.started = threading.Event()
        body = _Body(self.hooks, self.trickle)
        owner = self

        class Handler(_Quiet):
            def do_GET(self) -> None:
                owner.paths.append(self.path)
                data = _file_under(owner.root, self.path)
                swap = next((b for x, b in owner.replace.items() if self.path.endswith(x)), None)
                if data is not None and swap is not None:
                    data = swap
                if data is None:
                    self.send_error(404)
                    return
                owner.started.set()
                body.send(self, self.path, data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = _serve(self.srv)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class ProxyServer:
    """A forwarding HTTP proxy answering absolute-URI ``GET`` requests from local files.

    *sites* maps ``http://host[:port]`` prefixes to local directories; every
    request line goes to :attr:`lines` and its ``Proxy-Authorization`` header
    (or None) to :attr:`auth`. ``CONNECT`` is refused (405) and recorded.
    """

    def __init__(self, sites: Mapping[str, Path]) -> None:
        self.sites = dict(sites)
        self.lines: list[str] = []
        self.auth: list[str | None] = []
        owner = self

        class Handler(_Quiet):
            def do_GET(self) -> None:
                owner.lines.append(self.requestline)
                owner.auth.append(self.headers.get("Proxy-Authorization"))
                for prefix, root in owner.sites.items():
                    if self.path.startswith(prefix + "/"):
                        data = _file_under(root, self.path[len(prefix) :])
                        if data is not None:
                            _Body({}, {}).send(self, self.path, data)
                            return
                self.send_error(404)

            def do_CONNECT(self) -> None:
                owner.lines.append(self.requestline)
                self.send_error(405)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = _serve(self.srv)
        self.port = self.srv.server_address[1]

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
