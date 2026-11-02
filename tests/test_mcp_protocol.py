# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The MCP stdio protocol layer: framing, JSON-RPC, lifecycle, tool calls."""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any, BinaryIO, cast

import pytest

from minideblib.errors import MinidebError
from minideblib.mcp import protocol
from minideblib.mcp.protocol import (
    SUPPORTED_VERSIONS,
    McpServer,
    Tool,
    ToolCall,
    ToolError,
    validate,
)
from minideblib.repo.fetch import CancelToken
from tests.mcp_client import InProcess, McpClient, start_in_process

WAIT = 10.0

SEARCH_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "minLength": 1, "maxLength": 10},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        "kind": {"type": "string", "enum": ["binary", "source"]},
        "ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "exact": {"type": "boolean"},
        "names": {"type": "array", "items": {"type": "string", "maxLength": 3}},
        "scope": {
            "type": "object",
            "properties": {"component": {"type": "string"}},
            "required": ["component"],
            "additionalProperties": False,
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def _echo(call: ToolCall) -> dict[str, object]:
    return {"echo": call.arguments}


class Blocker:
    """A tool handler that blocks until released (or its call is cancelled)."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled: bool | None = None
        self.calls: list[ToolCall] = []

    def __call__(self, call: ToolCall) -> dict[str, object]:
        self.calls.append(call)
        self.started.set()
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if self.release.is_set() or call.token.wait(0.01):
                break
        self.cancelled = call.token.is_cancelled
        return {"done": True}


def _tools(blocker: Blocker | None = None, **extra: Any) -> list[Tool]:
    tools = [
        Tool("echo", "Echo the arguments.", {"type": "object"}, _echo, read_only=True),
        Tool("search", "Search.", SEARCH_SCHEMA, _echo, read_only=True, open_world=False),
        Tool(
            "download",
            "Download.",
            {"type": "object"},
            _echo,
            read_only=False,
            title="Download a package",
        ),
    ]
    if blocker is not None:
        tools.append(Tool("block", "Block.", {"type": "object"}, blocker, read_only=True))
    for name, handler in extra.items():
        tools.append(Tool(name, name, {"type": "object"}, handler, read_only=True))
    return tools


@pytest.fixture
def blocker() -> Blocker:
    return Blocker()


@pytest.fixture
def srv(blocker: Blocker) -> Iterator[InProcess]:
    proc = start_in_process(_tools(blocker))
    yield proc
    blocker.release.set()
    proc.stop()


@pytest.fixture
def client(srv: InProcess) -> McpClient:
    return srv.client


@pytest.fixture
def ready(client: McpClient) -> McpClient:
    client.initialize()
    return client


def _error(msg: dict[str, Any], code: int) -> str:
    assert "result" not in msg, msg
    assert msg["error"]["code"] == code, msg
    return msg["error"]["message"]


def _text(resp: dict[str, Any]) -> str:
    result = resp["result"]
    assert [c["type"] for c in result["content"]] == ["text"], result
    return result["content"][0]["text"]


# -- initialize and version negotiation -----------------------------------------


def test_supported_versions():
    assert SUPPORTED_VERSIONS == ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_initialize_echoes_supported_version(client: McpClient, version: str):
    result = client.initialize(version)
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "test-server", "version": "1.0"}
    assert result["instructions"] == "Test instructions."


@pytest.mark.parametrize("version", ["1999-01-01", "2030-01-01", "garbage"])
def test_initialize_unsupported_version_gets_newest(client: McpClient, version: str):
    assert client.initialize(version)["protocolVersion"] == SUPPORTED_VERSIONS[0]


def test_initialize_bad_params(client: McpClient):
    _error(client.request("initialize", {"protocolVersion": 5}), -32602)
    _error(client.request("initialize"), -32602)
    # the session is still uninitialised and can be initialised now
    assert client.initialize()["protocolVersion"] == "2025-06-18"


def test_second_initialize_rejected(ready: McpClient):
    resp = ready.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    _error(resp, -32600)


def test_requests_before_initialize(client: McpClient):
    assert client.request("ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert "not initialized" in _error(client.request("tools/list"), -32002)
    _error(client.call("echo", {}), -32002)
    _error(client.request("no/such/method"), -32002)
    client.initialize()
    assert "result" in client.request("tools/list")


def test_ping(ready: McpClient):
    resp = ready.request("ping")
    assert resp["result"] == {} and resp["jsonrpc"] == "2.0"


def test_string_ids_are_echoed(ready: McpClient):
    ready.send({"jsonrpc": "2.0", "id": "abc", "method": "ping"})
    assert ready.recv() == {"jsonrpc": "2.0", "id": "abc", "result": {}}


# -- tools/list ------------------------------------------------------------------


def test_tools_list_shape(ready: McpClient):
    result = ready.request("tools/list")["result"]
    assert "nextCursor" not in result
    tools = {t["name"]: t for t in result["tools"]}
    assert list(tools) == ["echo", "search", "download", "block"]
    search = tools["search"]
    assert search["description"] == "Search."
    assert search["inputSchema"] == SEARCH_SCHEMA
    assert search["title"] == "Search"
    assert search["annotations"] == {
        "title": "Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    dl = tools["download"]
    assert dl["title"] == "Download a package"
    assert dl["annotations"]["readOnlyHint"] is False
    assert dl["annotations"]["openWorldHint"] is True
    assert dl["annotations"]["destructiveHint"] is False
    assert dl["annotations"]["idempotentHint"] is True


def test_tools_list_accepts_cursor_param(ready: McpClient):
    assert len(ready.request("tools/list", {"cursor": None})["result"]["tools"]) == 4


def test_duplicate_tool_names_rejected():
    tools = _tools() + _tools()
    with pytest.raises(ValueError, match="duplicate tool name"):
        McpServer(
            tools,
            name="n",
            version="1",
            instructions="",
            stdin=io.BytesIO(),
            stdout=io.BytesIO(),
        )


# -- tools/call ------------------------------------------------------------------


def test_call_returns_text_and_structured(ready: McpClient):
    resp = ready.call("echo", {"a": [1, "x"]})
    result = resp["result"]
    assert json.loads(_text(resp)) == {"echo": {"a": [1, "x"]}}
    assert _text(resp) == '{"echo":{"a":[1,"x"]}}'  # compact JSON
    assert result["structuredContent"] == {"echo": {"a": [1, "x"]}}
    assert result.get("isError", False) is False


def test_call_without_arguments(ready: McpClient):
    assert json.loads(_text(ready.call("echo"))) == {"echo": {}}


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_structured_content_per_version(client: McpClient, version: str):
    client.initialize(version)
    result = client.call("echo", {"a": 1})["result"]
    if version >= "2025-06-18":
        assert result["structuredContent"] == {"echo": {"a": 1}}
    else:
        assert "structuredContent" not in result


def test_unknown_tool(ready: McpClient):
    assert "nope" in _error(ready.call("nope", {}), -32602)


@pytest.mark.parametrize(
    "params",
    [{}, {"name": 5}, {"name": "echo", "arguments": [1]}, {"name": "echo", "arguments": "x"}],
)
def test_call_bad_params(ready: McpClient, params: dict[str, Any]):
    _error(ready.request("tools/call", params), -32602)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, '"pattern": required'),
        ({"pattern": 5}, '"pattern": must be a string'),
        ({"pattern": ""}, '"pattern": must be at least 1 characters'),
        ({"pattern": "x" * 11}, '"pattern": must be at most 10 characters'),
        ({"pattern": "x", "limit": 501}, '"limit": must be <= 500'),
        ({"pattern": "x", "limit": 0}, '"limit": must be >= 1'),
        ({"pattern": "x", "limit": 1.5}, '"limit": must be an integer'),
        ({"pattern": "x", "limit": True}, '"limit": must be an integer'),
        ({"pattern": "x", "kind": "deb"}, '"kind": must be one of "binary", "source"'),
        ({"pattern": "x", "ratio": "1"}, '"ratio": must be a number'),
        ({"pattern": "x", "ratio": 2}, '"ratio": must be <= 1'),
        ({"pattern": "x", "exact": 1}, '"exact": must be a boolean'),
        ({"pattern": "x", "names": "a"}, '"names": must be an array'),
        ({"pattern": "x", "names": ["a", "long"]}, '"names[1]": must be at most 3 characters'),
        ({"pattern": "x", "scope": {}}, '"scope.component": required'),
        ({"pattern": "x", "scope": {"component": "m", "x": 1}}, '"scope.x": unknown argument'),
        ({"pattern": "x", "bogus": 1}, '"bogus": unknown argument'),
    ],
)
def test_argument_validation_is_error_result(
    ready: McpClient, arguments: dict[str, Any], expected: str
):
    resp = ready.call("search", arguments)
    assert resp["result"]["isError"] is True
    assert "structuredContent" not in resp["result"]
    assert expected in _text(resp)


def test_valid_arguments_pass(ready: McpClient):
    args = {
        "pattern": "x",
        "limit": 500,
        "kind": "source",
        "ratio": 0.5,
        "exact": False,
        "names": ["a", "bc"],
        "scope": {"component": "main"},
    }
    assert json.loads(_text(ready.call("search", args))) == {"echo": args}


def test_validate_function_directly():
    validate({"pattern": "a", "limit": 1}, SEARCH_SCHEMA)
    with pytest.raises(ToolError, match=r'^"limit": must be <= 500$'):
        validate({"pattern": "a", "limit": 501}, SEARCH_SCHEMA)
    with pytest.raises(ToolError, match="must be an object"):
        validate([], SEARCH_SCHEMA)
    # unknown keywords are ignored; a type list accepts any listed type
    validate("x", {"type": ["string", "null"], "pattern": "^z$", "format": "uri"})
    validate(None, {"type": ["string", "null"]})
    validate(3.0, {"type": "number"})
    with pytest.raises(ToolError, match="must be a string or null"):
        validate(1, {"type": ["string", "null"]})
    assert issubclass(ToolError, MinidebError)


def test_handler_tool_error_message():
    def bad(call: ToolCall) -> dict[str, object]:
        raise ToolError("no such package: foo")

    proc = start_in_process(_tools(bad=bad))
    try:
        proc.client.initialize()
        resp = proc.client.call("bad", {})
        assert resp["result"]["isError"] is True
        assert _text(resp) == "no such package: foo"
    finally:
        proc.stop()


def test_handler_exception_hidden(caplog: pytest.LogCaptureFixture):
    def boom(call: ToolCall) -> dict[str, object]:
        raise RuntimeError("secret detail /etc/shadow")

    def not_a_dict(call: ToolCall) -> object:
        return [1, 2]

    def not_json(call: ToolCall) -> object:
        return {"x": object()}

    def nan(call: ToolCall) -> object:
        return {"x": float("nan")}

    log = logging.getLogger("test.mcp.hidden")
    proc = start_in_process(
        _tools(boom=boom, not_a_dict=not_a_dict, not_json=not_json, nan=nan), log=log
    )
    try:
        proc.client.initialize()
        with caplog.at_level(logging.ERROR, logger="test.mcp.hidden"):
            for name in ("boom", "not_a_dict", "not_json", "nan"):
                resp = proc.client.call(name, {})
                assert resp["result"]["isError"] is True, name
                text = _text(resp)
                assert "internal error" in text and "secret" not in text, text
                assert "Traceback" not in text
        assert "secret detail" in caplog.text
        assert "RuntimeError" in caplog.text
    finally:
        proc.stop()


def test_deadline_from_call_timeout(blocker: Blocker):
    blocker.release.set()
    proc = start_in_process(_tools(blocker), call_timeout=33.0)
    try:
        proc.client.initialize()
        before = time.monotonic()
        proc.client.call("block", {})
        call = blocker.calls[0]
        assert call.deadline is not None
        assert before + 32 < call.deadline < time.monotonic() + 33.5
        assert isinstance(call.token, CancelToken)
    finally:
        proc.stop()


def test_tool_timeout_overrides_call_timeout():
    seen: list[ToolCall] = []

    def record(call: ToolCall) -> dict[str, object]:
        seen.append(call)
        return {}

    release = threading.Event()

    def slow(call: ToolCall) -> dict[str, object]:
        release.wait(5)
        return {}

    tools = [
        Tool("long", "Long.", {"type": "object"}, record, read_only=False, timeout=900.0),
        Tool("short", "Short.", {"type": "object"}, slow, read_only=True, timeout=0.1),
    ]
    proc = start_in_process(tools, call_timeout=33.0)
    try:
        proc.client.initialize()
        before = time.monotonic()
        proc.client.call("long", {})
        deadline = seen[0].deadline
        assert deadline is not None and before + 899 < deadline < time.monotonic() + 900.5
        resp = proc.client.call("short", {})
        assert resp["result"]["isError"] is True
        assert "timed out after 0.1 s" in _text(resp)
    finally:
        release.set()
        proc.stop()


def test_tool_timeout_stuck_call_makes_server_busy():
    gate = threading.Event()

    def stuck(call: ToolCall) -> dict[str, object]:
        gate.wait(WAIT)  # ignores cancellation
        return {"late": True}

    tools = [
        *_tools(),
        Tool("stuck", "Stuck.", {"type": "object"}, stuck, read_only=False, timeout=0.2),
    ]
    proc = start_in_process(tools, call_timeout=60.0)
    try:
        c = proc.client
        c.initialize()
        start = time.monotonic()
        resp = c.call("stuck", {})
        assert time.monotonic() - start < 5
        assert "timed out after 0.2 s" in _text(resp)
        busy = c.call("echo", {})
        assert busy["result"]["isError"] is True
        assert "server busy: a timed-out call is still running" in _text(busy)
        gate.set()
        assert json.loads(_text(_wait_not_busy(c))) == {"echo": {}}
    finally:
        gate.set()
        proc.stop()


def test_tool_timeout_long_call_ends_at_shutdown_grace():
    gate = threading.Event()
    started = threading.Event()

    def long(call: ToolCall) -> dict[str, object]:
        started.set()
        gate.wait(WAIT)  # ignores cancellation
        return {}

    tools = [Tool("long", "Long.", {"type": "object"}, long, read_only=False, timeout=900.0)]
    proc = start_in_process(tools, call_timeout=0.1)
    proc.server.shutdown_grace = 0.2
    try:
        proc.client.initialize()
        proc.client.send_request("tools/call", {"name": "long", "arguments": {}})
        assert started.wait(WAIT)
        time.sleep(0.3)  # past call_timeout: the tool's own timeout applies
        assert proc.client.no_message(0.1)
        start = time.monotonic()
        assert proc.stop() == 0
        assert time.monotonic() - start < 2
    finally:
        gate.set()


def test_handler_failing_after_deadline_reports_timeout():
    def slow(call: ToolCall) -> dict[str, object]:
        assert call.deadline is not None
        while time.monotonic() < call.deadline:
            time.sleep(0.01)
        raise MinidebError("deadline exceeded: http://h/x")

    proc = start_in_process(_tools(slow=slow), call_timeout=0.1)
    try:
        proc.client.initialize()
        resp = proc.client.call("slow", {})
        assert resp["result"]["isError"] is True
        assert "timed out" in _text(resp)
    finally:
        proc.stop()


def test_response_cap_replaced():
    def big(call: ToolCall) -> dict[str, object]:
        return {"x": "a" * 5000}

    proc = start_in_process(_tools(big=big), max_response_bytes=2000)
    try:
        proc.client.initialize()
        resp = proc.client.call("big", {})
        assert resp["result"]["isError"] is True
        assert "2000" in _text(resp) and "too large" in _text(resp)
        assert "structuredContent" not in resp["result"]
        # small results still pass
        assert "isError" not in proc.client.call("echo", {})["result"]
    finally:
        proc.stop()


# -- JSON-RPC envelope and parsing -------------------------------------------------


def test_unknown_method(ready: McpClient):
    assert "no/such" in _error(ready.request("no/such"), -32601)


def test_unknown_notification_ignored(ready: McpClient):
    ready.notify("notifications/whatever", {"x": 1})
    ready.notify("no/such")
    assert ready.request("ping")["result"] == {}


def test_malformed_json(ready: McpClient):
    ready.send_raw(b'{"jsonrpc": "2.0", "id": 1, "method": \n')
    msg = ready.recv()
    assert msg["id"] is None
    _error(msg, -32700)
    assert ready.request("ping")["result"] == {}


def test_invalid_utf8(ready: McpClient):
    ready.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"\xff\xfe"}\n')
    msg = ready.recv()
    assert msg["id"] is None
    _error(msg, -32700)
    assert ready.request("ping")["result"] == {}


def test_batch_rejected(ready: McpClient):
    ready.send([{"jsonrpc": "2.0", "id": 7, "method": "ping"}])
    msg = ready.recv()
    assert msg["id"] is None
    assert "batch" in _error(msg, -32600)
    ready.send([])
    _error(ready.recv(), -32600)


@pytest.mark.parametrize(
    "message",
    [
        5,
        "ping",
        None,
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": [1], "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": 5},
    ],
)
def test_invalid_envelope(ready: McpClient, message: object):
    ready.send(message)
    _error(ready.recv(), -32600)
    assert ready.request("ping")["result"] == {}


def test_invalid_envelope_keeps_valid_id(ready: McpClient):
    ready.send({"jsonrpc": "1.0", "id": 9, "method": "ping"})
    msg = ready.recv()
    assert msg["id"] == 9
    _error(msg, -32600)


def test_positional_params_rejected(ready: McpClient):
    ready.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]})
    _error(ready.recv(), -32602)


def test_client_responses_ignored(ready: McpClient):
    ready.send({"jsonrpc": "2.0", "id": 99, "result": {}})
    ready.send({"jsonrpc": "2.0", "id": 98, "error": {"code": 1, "message": "x"}})
    assert ready.request("ping")["result"] == {}


def test_blank_lines_ignored(ready: McpClient):
    ready.send_raw(b"\n  \r\n")
    assert ready.request("ping")["result"] == {}


def test_crlf_line_endings(ready: McpClient):
    ready.send_raw(b'{"jsonrpc":"2.0","id":"c","method":"ping"}\r\n')
    assert ready.recv()["id"] == "c"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":Infinity}}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":-Infinity}}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1e400}}',
        b'{"jsonrpc":"2.0","id":9007199254740993,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":-9007199254740993}}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":' + b"9" * 5000 + b"}}",
    ],
    ids=["nan", "inf", "-inf", "float-overflow", "huge-id", "huge-negative", "5000-digits"],
)
def test_rejected_numbers(ready: McpClient, raw: bytes):
    ready.send_raw(raw + b"\n")
    msg = ready.recv()
    assert msg["id"] is None
    _error(msg, -32700)
    assert ready.request("ping")["result"] == {}


def test_largest_safe_integer_accepted(ready: McpClient):
    big = 2**53 - 1
    ready.send({"jsonrpc": "2.0", "id": big, "method": "ping"})
    assert ready.recv()["id"] == big


@pytest.mark.parametrize("depth", [65, 1000, 100_000])
def test_deep_nesting(ready: McpClient, depth: int):
    nested = b"[" * depth + b"]" * depth
    ready.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":' + nested + b"}}\n")
    msg = ready.recv()
    assert msg["id"] is None
    assert "deep" in _error(msg, -32700)
    assert ready.request("ping")["result"] == {}


def test_nesting_within_cap_accepted(ready: McpClient):
    # envelope, params and arguments are 3 levels; 61 lists make 64 in total
    arguments: object = 1
    for _ in range(61):
        arguments = [arguments]
    resp = ready.call("echo", {"a": arguments})
    assert "isError" not in resp["result"], resp
    ready.send_raw(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"a": [arguments]}},
            }
        ).encode()
        + b"\n"
    )
    _error(ready.recv(), -32700)


def test_overlong_line_discarded_then_session_continues(blocker: Blocker):
    proc = start_in_process(_tools(blocker), max_line_bytes=1000)
    try:
        c = proc.client
        c.initialize()
        # exactly at the cap: accepted
        pad = 1000 - len(b'{"jsonrpc":"2.0","id":"p","method":"ping","x":""}')
        c.send_raw(b'{"jsonrpc":"2.0","id":"p","method":"ping","x":"' + b"a" * pad + b'"}\n')
        assert c.recv()["id"] == "p"
        # over the cap, written in pieces: one error, the rest of the line discarded
        c.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"' + b"a" * 3000)
        c.send_raw(b"b" * 70_000)
        c.send_raw(b'"}\n')
        msg = c.recv()
        assert msg["id"] is None
        assert "too long" in _error(msg, -32600)
        assert c.request("ping")["result"] == {}
        assert c.no_message()
    finally:
        proc.stop()


def test_random_bytes_fuzz(ready: McpClient):
    import random

    rng = random.Random(1234)
    for _ in range(50):
        junk = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200)))
        junk = junk.replace(b"\n", b"")
        if not junk.strip():
            continue
        ready.send_raw(junk + b"\n")
        msg = ready.recv()
        assert msg["id"] is None and msg["error"]["code"] in (-32700, -32600), msg
    assert ready.request("ping")["result"] == {}


# -- concurrency and cancellation ----------------------------------------------------


def test_ping_answered_during_blocked_call(ready: McpClient, blocker: Blocker):
    call_id = ready.send_request("tools/call", {"name": "block", "arguments": {}})
    assert blocker.started.wait(WAIT)
    assert ready.request("ping")["result"] == {}
    blocker.release.set()
    resp = ready.recv()
    assert resp["id"] == call_id and json.loads(_text(resp)) == {"done": True}


def test_calls_run_one_at_a_time(ready: McpClient, blocker: Blocker):
    first = ready.send_request("tools/call", {"name": "block", "arguments": {}})
    second = ready.send_request("tools/call", {"name": "echo", "arguments": {"n": 2}})
    assert blocker.started.wait(WAIT)
    assert ready.no_message()  # echo waits behind the blocked call
    blocker.release.set()
    assert ready.recv()["id"] == first
    assert ready.recv()["id"] == second


def test_cancel_during_call(ready: McpClient, blocker: Blocker):
    call_id = ready.send_request("tools/call", {"name": "block", "arguments": {}})
    assert blocker.started.wait(WAIT)
    ready.notify("notifications/cancelled", {"requestId": call_id, "reason": "user"})
    deadline = time.monotonic() + WAIT
    while blocker.cancelled is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert blocker.cancelled is True
    # no response for the cancelled request: the next message is the ping's
    resp = ready.request("ping")
    assert resp["result"] == {}
    assert ready.no_message()


def test_cancel_before_call_starts(ready: McpClient, blocker: Blocker):
    first = ready.send_request("tools/call", {"name": "block", "arguments": {}})
    assert blocker.started.wait(WAIT)
    queued = ready.send_request("tools/call", {"name": "echo", "arguments": {}})
    ready.notify("notifications/cancelled", {"requestId": queued})
    blocker.release.set()
    assert ready.recv()["id"] == first
    assert ready.request("ping")["result"] == {}
    assert ready.no_message()


def test_cancel_after_completion_and_unknown_ids(ready: McpClient):
    resp = ready.call("echo", {})
    ready.notify("notifications/cancelled", {"requestId": resp["id"]})
    ready.notify("notifications/cancelled", {"requestId": "never-sent"})
    ready.notify("notifications/cancelled", {"bogus": True})
    ready.notify("notifications/cancelled")
    assert ready.request("ping")["result"] == {}
    assert ready.no_message()


def test_duplicate_in_flight_id_rejected(ready: McpClient, blocker: Blocker):
    ready.send({"jsonrpc": "2.0", "id": "x", "method": "tools/call", "params": {"name": "block"}})
    assert blocker.started.wait(WAIT)
    ready.send({"jsonrpc": "2.0", "id": "x", "method": "tools/call", "params": {"name": "echo"}})
    _error(ready.recv(), -32600)
    blocker.release.set()
    assert ready.recv()["id"] == "x"


def test_eof_while_handler_runs(blocker: Blocker):
    proc = start_in_process(_tools(blocker))
    proc.client.initialize()
    proc.client.send_request("tools/call", {"name": "block", "arguments": {}})
    assert blocker.started.wait(WAIT)
    start = time.monotonic()
    assert proc.stop() == 0
    assert time.monotonic() - start < 3
    assert blocker.cancelled is True
    assert blocker.calls[0].token.is_cancelled
    proc.client.wait_eof()  # no response for the cancelled call


def test_eof_with_uncooperative_handler_is_bounded():
    gate = threading.Event()

    def stuck(call: ToolCall) -> dict[str, object]:
        gate.wait(WAIT)
        return {}

    proc = start_in_process(_tools(stuck=stuck))
    proc.server.shutdown_grace = 0.2
    try:
        proc.client.initialize()
        proc.client.send_request("tools/call", {"name": "stuck", "arguments": {}})
        time.sleep(0.1)
        start = time.monotonic()
        assert proc.stop() == 0
        assert time.monotonic() - start < 2
    finally:
        gate.set()


def test_eof_idle_returns_zero(srv: InProcess):
    srv.client.initialize()
    assert srv.stop() == 0


def test_eof_without_trailing_newline_processes_last_line(srv: InProcess):
    srv.client.send_raw(b'{"jsonrpc":"2.0","id":"last","method":"ping"}')
    srv.client.close()
    assert srv.client.recv()["id"] == "last"
    assert srv.stop() == 0


def test_default_shutdown_grace():
    assert protocol.SHUTDOWN_GRACE == 5.0


# -- stdout purity and the write lock --------------------------------------------------


def test_print_and_logging_in_handler_do_not_reach_stdout(capsys: pytest.CaptureFixture[str]):
    stream_logger = logging.getLogger("test.mcp.stdout")
    handler = logging.StreamHandler(sys.stdout)
    stream_logger.addHandler(handler)
    stream_logger.propagate = False

    def noisy(call: ToolCall) -> dict[str, object]:
        print("printed by a handler")
        sys.stdout.write("written by a handler\n")
        stream_logger.warning("logged by a handler")
        return {"ok": True}

    try:
        proc = start_in_process(_tools(noisy=noisy))
        proc.client.initialize()
        resp = proc.client.call("noisy", {})
        assert json.loads(_text(resp)) == {"ok": True}
        proc.stop()
        for line in proc.client.raw:
            assert isinstance(json.loads(line), dict), line
        assert sys.stdout is not sys.stderr  # restored
        assert handler.stream is sys.stdout  # restored
        err = capsys.readouterr().err
        assert "printed by a handler" in err
        assert "written by a handler" in err
        assert "logged by a handler" in err
    finally:
        stream_logger.removeHandler(handler)


class SlowStream(io.RawIOBase):
    """A binary stream that writes in small pieces, yielding between them."""

    def __init__(self) -> None:
        self.data = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        view = bytes(b)
        for i in range(0, len(view), 7):
            self.data += view[i : i + 7]
            time.sleep(0)
        return len(view)


def test_write_lock_serialises_messages():
    out = SlowStream()
    server = McpServer(
        _tools(),
        name="n",
        version="1",
        instructions="",
        stdin=io.BytesIO(),
        stdout=cast(BinaryIO, out),
    )
    send = server._send  # noqa: SLF001

    def writer(tag: str) -> None:
        for i in range(30):
            send({"jsonrpc": "2.0", "id": f"{tag}{i}", "result": {"pad": tag * 300}})

    threads = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT)
    lines = bytes(out.data).split(b"\n")
    assert lines[-1] == b""
    ids = [json.loads(line)["id"] for line in lines[:-1]]
    assert sorted(ids) == sorted([f"{t}{i}" for t in "ab" for i in range(30)])


def test_outgoing_messages_are_single_lines(ready: McpClient):
    resp = ready.call("echo", {"text": "line1\nline2 é"})
    assert json.loads(_text(resp))["echo"]["text"] == "line1\nline2 é"
    for line in ready.raw:
        assert line.count(b"\n") == 1 and line.endswith(b"\n")


def test_lone_surrogate_in_result_is_still_sent():
    def surrogate(call: ToolCall) -> dict[str, object]:
        return {"s": "\ud800"}

    proc = start_in_process(_tools(surrogate=surrogate))
    try:
        proc.client.initialize()
        resp = proc.client.call("surrogate", {})
        assert "isError" not in resp["result"]
        assert json.loads(_text(resp)) == {"s": "\ud800"}
    finally:
        proc.stop()


def test_package_exports():
    import minideblib.mcp as mcp

    for name in ("McpServer", "Tool", "ToolCall", "ToolError", "SUPPORTED_VERSIONS", "validate"):
        assert getattr(mcp, name) is getattr(protocol, name)
        assert name in mcp.__all__
    assert protocol.__doc__ and "-32002" in protocol.__doc__


# -- fix round: deadlines, queue bound, hardening -------------------------------------


def _wait_not_busy(c: McpClient) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT
    while True:
        resp = c.call("echo", {})
        if "busy" not in _text(resp) or time.monotonic() > deadline:
            return resp
        time.sleep(0.02)


def test_stuck_handler_times_out_and_server_refuses_until_it_ends():
    gate = threading.Event()
    calls: list[ToolCall] = []

    def stuck(call: ToolCall) -> dict[str, object]:
        calls.append(call)
        gate.wait(WAIT)
        return {"late": True}

    proc = start_in_process(_tools(stuck=stuck), call_timeout=0.2)
    try:
        c = proc.client
        c.initialize()
        start = time.monotonic()
        resp = c.call("stuck", {})
        elapsed = time.monotonic() - start
        assert resp["result"]["isError"] is True
        assert "timed out after 0.2 s" in _text(resp)
        assert 0.15 < elapsed < 2, elapsed
        assert calls[0].token.is_cancelled
        busy = c.call("echo", {})
        assert busy["result"]["isError"] is True
        assert "server busy: a timed-out call is still running" in _text(busy)
        assert c.request("ping")["result"] == {}
        gate.set()
        resp = _wait_not_busy(c)
        assert json.loads(_text(resp)) == {"echo": {}}
        assert c.no_message()  # the late result was dropped
    finally:
        gate.set()
        proc.stop()


def test_id_reusable_after_timeout():
    gate = threading.Event()

    def stuck(call: ToolCall) -> dict[str, object]:
        gate.wait(WAIT)
        return {}

    proc = start_in_process(_tools(stuck=stuck), call_timeout=0.2)
    try:
        c = proc.client
        c.initialize()
        c.send({"jsonrpc": "2.0", "id": "r", "method": "tools/call", "params": {"name": "stuck"}})
        assert "timed out" in _text(c.recv())
        gate.set()
        _wait_not_busy(c)
        c.send({"jsonrpc": "2.0", "id": "r", "method": "tools/call", "params": {"name": "echo"}})
        resp = c.recv()
        assert resp["id"] == "r" and "error" not in resp
        assert json.loads(_text(resp)) == {"echo": {}}
    finally:
        gate.set()
        proc.stop()


def test_cancel_frees_the_id(ready: McpClient, blocker: Blocker):
    ready.send({"jsonrpc": "2.0", "id": "z", "method": "tools/call", "params": {"name": "block"}})
    assert blocker.started.wait(WAIT)
    ready.notify("notifications/cancelled", {"requestId": "z"})
    ready.send({"jsonrpc": "2.0", "id": "z", "method": "tools/call", "params": {"name": "echo"}})
    resp = ready.recv()
    assert resp["id"] == "z" and "error" not in resp, resp
    assert json.loads(_text(resp)) == {"echo": {}}
    assert ready.no_message()


def test_queue_is_bounded(blocker: Blocker):
    proc = start_in_process(_tools(blocker), max_queued=3)
    try:
        c = proc.client
        c.initialize()
        first = c.send_request("tools/call", {"name": "block", "arguments": {}})
        assert blocker.started.wait(WAIT)
        queued = [c.send_request("tools/call", {"name": "echo", "arguments": {}}) for _ in range(2)]
        flood = [
            c.send_request("tools/call", {"name": "echo", "arguments": {"pad": "x" * 3000}})
            for _ in range(50)
        ]
        for rid in flood:
            resp = c.recv()
            assert resp["id"] == rid
            assert resp["result"]["isError"] is True
            assert "server busy: 3 calls pending; wait for results" in _text(resp)
        assert c.request("ping")["result"] == {}
        blocker.release.set()
        assert [c.recv()["id"] for _ in range(3)] == [first, *queued]
        assert "isError" not in c.call("echo", {})["result"]
    finally:
        blocker.release.set()
        proc.stop()


def test_cancelled_queued_jobs_still_count_against_the_queue(blocker: Blocker):
    proc = start_in_process(_tools(blocker), max_queued=3)
    try:
        c = proc.client
        c.initialize()
        first = c.send_request("tools/call", {"name": "block", "arguments": {}})
        assert blocker.started.wait(WAIT)
        busy = 0
        for i in range(30):
            rid = f"c{i}"
            c.send(
                {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": "echo"}}
            )
            c.notify("notifications/cancelled", {"requestId": rid})
        while not c.no_message():
            resp = c.recv()
            assert "server busy" in _text(resp)
            busy += 1
        # the worker is blocked, so at most max_queued cancelled jobs fit in the queue
        assert busy == 30 - 3
        blocker.release.set()
        assert c.recv()["id"] == first
        assert "isError" not in _wait_not_busy(c)["result"]
    finally:
        blocker.release.set()
        proc.stop()


def test_default_max_queued():
    assert protocol.MAX_QUEUED == 8


def test_system_exit_in_handler_keeps_worker_alive(caplog: pytest.LogCaptureFixture):
    def exits(call: ToolCall) -> dict[str, object]:
        raise SystemExit(3)

    log = logging.getLogger("test.mcp.exit")
    proc = start_in_process(_tools(exits=exits), log=log)
    try:
        proc.client.initialize()
        with caplog.at_level(logging.ERROR, logger="test.mcp.exit"):
            resp = proc.client.call("exits", {})
        assert resp["result"]["isError"] is True
        assert "internal error in tool exits" in _text(resp)
        assert "SystemExit" in caplog.text
        assert json.loads(_text(proc.client.call("echo", {}))) == {"echo": {}}
    finally:
        proc.stop()


def test_unknown_argument_name_is_truncated(ready: McpClient):
    key = "k" * 1_000_000
    resp = ready.call("search", {"pattern": "x", key: 1})
    text = _text(resp)
    assert resp["result"]["isError"] is True
    assert "unknown argument" in text and len(text) < 300, len(text)


def test_overlapping_serve_refused():
    proc = start_in_process(_tools())
    try:
        proc.client.initialize()
        other = McpServer(
            _tools(),
            name="n",
            version="1",
            instructions="",
            stdin=io.BytesIO(),
            stdout=io.BytesIO(),
        )
        with pytest.raises(RuntimeError, match=r"serve\(\) is already running in this process"):
            other.serve()
    finally:
        proc.stop()
    assert sys.stdout is not sys.stderr


def test_serve_is_single_use():
    server = McpServer(
        _tools(), name="n", version="1", instructions="", stdin=io.BytesIO(), stdout=io.BytesIO()
    )
    assert server.serve() == 0
    with pytest.raises(RuntimeError, match=r"serve\(\) can only be called once per McpServer"):
        server.serve()


def test_new_server_works_while_old_handler_still_runs():
    gate = threading.Event()
    started = threading.Event()
    threads: list[threading.Thread] = []

    def stuck(call: ToolCall) -> dict[str, object]:
        threads.append(threading.current_thread())
        started.set()
        gate.wait(WAIT)
        return {"late": True}

    old = start_in_process(_tools(stuck=stuck))
    old.server.shutdown_grace = 0.1
    try:
        old.client.initialize()
        old.client.send_request("tools/call", {"name": "stuck", "arguments": {}})
        assert started.wait(WAIT)
        assert old.stop() == 0
        assert threads[0].is_alive() and threads[0].daemon
        with pytest.raises(RuntimeError, match="only be called once"):
            old.server.serve()

        new = start_in_process(_tools(stuck=stuck))
        try:
            c = new.client
            assert "not initialized" in _error(c.request("tools/list"), -32002)
            c.initialize()
            assert json.loads(_text(c.call("echo", {"n": 1}))) == {"echo": {"n": 1}}
            assert threads[0].is_alive()  # the old call did not block the new server
            workers = [t for t in threading.enumerate() if t.name == "mcp-worker"]
            assert len(workers) <= 2  # the old worker may linger, but never runs new jobs
            assert len(threads) == 1
        finally:
            new.stop()
    finally:
        gate.set()


def test_unexpected_reader_error_answers_internal_error(
    srv: InProcess, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    c = srv.client
    c.initialize()
    assert c.request("ping")["result"] == {}  # notifications/initialized was handled

    def broken(message: object) -> None:
        raise RuntimeError("bug in dispatch")

    monkeypatch.setattr(srv.server, "_dispatch", broken)
    with caplog.at_level(logging.ERROR, logger="minideblib.mcp"):
        c.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        msg = c.recv()
    assert msg["id"] is None
    assert _error(msg, -32603) == "Internal error"
    assert "bug in dispatch" in caplog.text
    monkeypatch.undo()
    assert c.request("ping")["result"] == {}


def test_tools_must_serialise_at_construction():
    bad = Tool("bad", "Bad.", {"type": "object", "default": object()}, _echo, read_only=True)
    with pytest.raises(ValueError, match=r"tool 'bad'.*not JSON"):
        McpServer(
            [bad], name="n", version="1", instructions="", stdin=io.BytesIO(), stdout=io.BytesIO()
        )


@pytest.mark.parametrize(("version", "fits"), [("2024-11-05", True), ("2025-06-18", False)])
def test_response_cap_counts_structured_content(version: str, fits: bool):
    def payload(call: ToolCall) -> dict[str, object]:
        return {"x": "a" * 1500}

    proc = start_in_process(_tools(payload=payload), max_response_bytes=2500)
    try:
        proc.client.initialize(version)
        resp = proc.client.call("payload", {})
        assert ("isError" not in resp["result"]) is fits, resp
    finally:
        proc.stop()


def test_client_response_without_jsonrpc_ignored(ready: McpClient):
    ready.send({"id": 5, "result": {}})
    ready.send({"id": 6, "error": {"code": 1, "message": "x"}})
    assert ready.request("ping")["result"] == {}
    assert ready.no_message()
