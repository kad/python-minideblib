# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""A minimal MCP stdio test client: newline-delimited JSON over two byte streams.

:class:`McpClient` drives any server whose stdin/stdout are given as binary
files (pipes to an in-process :class:`~minideblib.mcp.protocol.McpServer` or
to a subprocess). A background thread reads the server's output, so every
wait is bounded. :func:`start_in_process` runs a server on ``os.pipe`` pairs
in a thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
from collections.abc import Sequence
from typing import Any, BinaryIO

from minideblib.mcp.protocol import McpServer, Tool

#: default bound of every wait, in seconds
TIMEOUT = 10.0


class McpClient:
    """Send requests to a server's stdin and collect its stdout lines."""

    def __init__(self, to_server: BinaryIO, from_server: BinaryIO) -> None:
        self._out = to_server
        self._in = from_server
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        #: every raw line received (with its newline), for stdout purity checks
        self.raw: list[bytes] = []
        self._next_id = 1
        self._reader = threading.Thread(target=self._read, daemon=True)
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
