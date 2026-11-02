# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The MCP stdio transport: JSON-RPC 2.0 over newline-delimited JSON.

:class:`McpServer` reads one JSON object per line from a binary stdin and
writes one per line to a binary stdout (UTF-8, compact, never an embedded
raw newline). It answers ``initialize``, ``ping``, ``tools/list`` and
``tools/call`` and handles the ``notifications/initialized`` and
``notifications/cancelled`` notifications. Only the ``tools`` capability is
offered (``listChanged: false``); ``tools/list`` is not paginated.

Threads: the thread calling :meth:`McpServer.serve` reads and answers
``initialize``, ``ping``, ``tools/list`` and cancellations itself, so it
stays responsive while a tool runs; one daemon worker thread runs the tool
calls one at a time, in arrival order. A lock serialises every write to
stdout.

Decisions where the MCP and JSON-RPC texts leave a choice:

* **Before initialize**: ``initialize`` and ``ping`` are answered; any other
  request gets error ``-32002`` ("server not initialized"), even an unknown
  method. Notifications never get a response; before ``initialize`` they
  find nothing to act on (no call can be pending). A second ``initialize`` gets
  ``-32600``. Requests are accepted once ``initialize`` has been answered,
  without waiting for ``notifications/initialized``.
* **Version negotiation**: the requested ``protocolVersion`` is answered if
  it is in :data:`SUPPORTED_VERSIONS`, else the newest supported version.
  ``structuredContent`` is added to tool results only when the negotiated
  version is ``2025-06-18`` or later.
* **Framing errors**: a line that is not UTF-8 or not JSON gets ``-32700``
  with ``id: null``; so do NaN/Infinity, a float overflowing to infinity,
  an integer outside +-(2**53 - 1) and nesting deeper than
  :data:`MAX_DEPTH` containers (the whole message counts, including
  ``RecursionError`` from the parser), because such a message is refused
  before its ``id`` is read. A line longer than ``max_line_bytes`` (newline
  excluded) is discarded up to and including its newline and answered with
  ``-32600`` ("message too long") and ``id: null``; the session continues.
  Blank lines are ignored; a trailing ``\\r`` is accepted.
* **Envelopes**: batches (JSON arrays) get ``-32600``; so does a message
  that is not an object, lacks ``"jsonrpc": "2.0"`` or a string
  ``method``, has an ``id`` that is not a string or integer (``null``
  included) or non-object ``params`` (``null`` counts as absent; positional
  array ``params`` get ``-32602``). The error carries the request's ``id``
  when that is valid, else ``null``. Messages carrying ``result``/``error``
  (client responses) are ignored, as are unknown notifications. Unknown
  methods get ``-32601``. A ``tools/call`` reusing the id of a call still
  in flight gets ``-32600``.
* **Tool calls**: an unknown tool name, a non-string ``name`` or
  non-object ``arguments`` get ``-32602``. Arguments failing the tool's
  input schema (see :func:`validate`) get a tool result with ``isError:
  true`` naming the argument. A :class:`ToolError` from the handler becomes
  an ``isError`` result with its message; any other exception becomes an
  ``isError`` result with a generic message (or "timed out" if the deadline
  has passed) and is logged with its traceback to the log (stderr), never to
  the client. A result whose JSON-RPC message would exceed
  ``max_response_bytes`` (the whole encoded line) is replaced by an
  ``isError`` result saying so. From protocol ``2025-06-18`` on that line
  carries the result twice, as ``content`` text and as
  ``structuredContent``, so the usable payload is about half the cap.
* **Deadlines and load**: each call runs in its own daemon thread; the
  worker waits for it until ``deadline = time.monotonic() + call_timeout``.
  A call still running then is cancelled, answered with an ``isError``
  result ("tool call timed out after N s"), its id is freed and its late
  result dropped; while that thread is still alive every new or queued
  ``tools/call`` is answered at once with ``isError`` "server busy: a
  timed-out call is still running" (handlers share caches, so calls never
  run in parallel). At most ``max_queued`` (:data:`MAX_QUEUED`, 8) calls may
  be pending (running or queued); further calls get an ``isError`` "server
  busy" result at once. A ``BaseException`` from a handler (``SystemExit``
  included) becomes a generic ``isError`` result; the worker survives. An
  unexpected error while handling an incoming line is logged and answered
  with ``-32603`` ("Internal error", ``id: null``).
* **Cancellation**: ``notifications/cancelled`` cancels the request's
  :class:`~minideblib.repo.fetch.CancelToken` (queued or running) and frees
  its id for reuse; a cancelled request gets no response. Unknown or
  finished ids are ignored. Handlers get the deadline in
  :class:`ToolCall` and should stop by then; the server abandons them at
  the deadline (see above).
* **Stdout purity**: during :meth:`McpServer.serve`, ``sys.stdout`` is
  replaced by ``sys.stderr`` and logging stream handlers writing to the
  original ``sys.stdout`` (or ``sys.__stdout__``) are pointed at stderr, so
  ``print()`` and logging in handlers cannot corrupt the stream; protocol
  messages go only to the ``stdout`` object given. Writes to file
  descriptor 1 by other means cannot be intercepted.
* **Shutdown**: on EOF (or a read error) every queued or running call is
  cancelled and :meth:`McpServer.serve` returns 0 once the worker has
  finished, or after :data:`SHUTDOWN_GRACE` seconds (5 s) if a handler
  ignores the cancellation (the worker is a daemon thread and its late
  result is dropped). Only one :meth:`McpServer.serve` may run per process
  at a time (it swaps ``sys.stdout``), and each :class:`McpServer` serves
  once: a second ``serve()`` on the same instance raises
  :class:`RuntimeError`; a new session needs a new instance (which works
  even while an abandoned handler of an old one still runs).
"""

from __future__ import annotations

import json
import logging
import math
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, TextIO, TypeGuard

from minideblib.errors import MinidebError
from minideblib.repo.fetch import CancelToken

__all__ = [
    "MAX_DEPTH",
    "MAX_QUEUED",
    "SHUTDOWN_GRACE",
    "SUPPORTED_VERSIONS",
    "McpServer",
    "Tool",
    "ToolCall",
    "ToolError",
    "validate",
]

#: protocol versions this server speaks, newest first
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
#: the first version whose tool results carry ``structuredContent``
STRUCTURED_CONTENT_SINCE = "2025-06-18"
#: maximum nesting of arrays/objects in an incoming message
MAX_DEPTH = 64
#: largest integer magnitude accepted in incoming JSON (IEEE double safe range)
MAX_SAFE_INTEGER = 2**53 - 1
#: seconds :meth:`McpServer.serve` waits for a running call after EOF
SHUTDOWN_GRACE = 5.0
#: default maximum of pending (running or queued) tool calls
MAX_QUEUED = 8

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002

#: longest client-supplied string quoted back in an error message
_QUOTE_LIMIT = 100
#: chunk size used to discard the rest of an over-long line
_DISCARD_CHUNK = 64 * 1024
#: held by the running :meth:`McpServer.serve` (it swaps ``sys.stdout``)
_SERVE_LOCK = threading.Lock()
_BUSY_STUCK = "server busy: a timed-out call is still running"


class ToolError(MinidebError):
    """A tool failure reported to the model as an ``isError`` result.

    Raise it from a :class:`Tool` handler with a short message meant for
    the model; :func:`validate` raises it for invalid arguments.
    """


class _RpcError(MinidebError):
    """A JSON-RPC error answer (internal)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _BadJson(ValueError):
    """Valid JSON syntax with a value the server refuses (internal)."""


@dataclass
class ToolCall:
    """What a :class:`Tool` handler receives for one ``tools/call``."""

    #: the validated arguments
    arguments: dict[str, object]
    #: cancelled by ``notifications/cancelled`` or EOF; pass it to the Fetcher
    token: CancelToken
    #: a :func:`time.monotonic` instant the call should finish by
    deadline: float | None


@dataclass(frozen=True)
class Tool:
    """A tool offered by ``tools/list`` and run by ``tools/call``.

    *handler* returns a JSON-serialisable ``dict`` (sent as compact JSON
    text and, for newer protocol versions, as ``structuredContent``) or
    raises :class:`ToolError`. The flags become the MCP tool annotations.
    """

    name: str
    description: str
    #: JSON schema of the arguments (the subset :func:`validate` checks)
    input_schema: dict[str, object]
    handler: Callable[[ToolCall], object]
    read_only: bool
    open_world: bool = True
    idempotent: bool = True
    destructive: bool = False
    #: human readable name; default: the name with spaces, capitalised
    title: str | None = None

    @property
    def display_title(self) -> str:
        return self.title or self.name.replace("_", " ").capitalize()

    def describe(self) -> dict[str, object]:
        """The ``tools/list`` entry."""
        title = self.display_title
        return {
            "name": self.name,
            "title": title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": self.open_world,
            },
        }


# -- argument validation ------------------------------------------------------

_TYPE_NAMES = {
    "object": "an object",
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "array": "an array",
    "null": "null",
}


def _is_type(value: object, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "null":
        return value is None
    return True  # unknown type names are ignored


def _fail(path: str, message: str) -> ToolError:
    where = _quote(path) if path else "arguments"
    return ToolError(f"{where}: {message}")


def _number(value: object) -> str:
    return json.dumps(value) if isinstance(value, int | float) else str(value)


def validate(arguments: object, schema: Mapping[str, object], path: str = "") -> None:
    """Check *arguments* against a JSON schema subset; raise :class:`ToolError`.

    Supported keywords: ``type`` (``object``, ``string``, ``integer``,
    ``number``, ``boolean``, ``array``, ``null`` or a list of them;
    ``integer`` means a JSON integer, not ``1.0``, and booleans are never
    numbers), ``properties``, ``required``, ``additionalProperties: false``,
    ``enum``, ``minimum``/``maximum``, ``minLength``/``maxLength`` and
    ``items`` (a single schema). Other keywords, ``default`` included, are
    ignored. The first problem found is reported, naming the argument path
    (``"scope.component"``, ``"names[1]"``), as in ``"limit": must be <= 500``.
    """
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
    kinds = [k for k in kinds if isinstance(k, str)]
    if kinds and not any(_is_type(arguments, k) for k in kinds):
        names = " or ".join(_TYPE_NAMES.get(k, k) for k in kinds)
        raise _fail(path, f"must be {names}")
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(
        arguments == e and type(arguments) is type(e) for e in enum
    ):
        choices = ", ".join(json.dumps(e) for e in enum)
        raise _fail(path, f"must be one of {choices}")
    if isinstance(arguments, int | float) and not isinstance(arguments, bool):
        low = schema.get("minimum")
        if isinstance(low, int | float) and arguments < low:
            raise _fail(path, f"must be >= {_number(low)}")
        high = schema.get("maximum")
        if isinstance(high, int | float) and arguments > high:
            raise _fail(path, f"must be <= {_number(high)}")
    if isinstance(arguments, str):
        shortest = schema.get("minLength")
        if isinstance(shortest, int) and len(arguments) < shortest:
            raise _fail(path, f"must be at least {shortest} characters")
        longest = schema.get("maxLength")
        if isinstance(longest, int) and len(arguments) > longest:
            raise _fail(path, f"must be at most {longest} characters")
    if isinstance(arguments, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, item in enumerate(arguments):
                validate(item, items, f"{path}[{i}]")
    if isinstance(arguments, dict):
        _validate_object(arguments, schema, path)


def _validate_object(value: dict[str, object], schema: Mapping[str, object], path: str) -> None:
    prefix = f"{path}." if path else ""
    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in value:
                raise _fail(prefix + name, "required")
    properties = schema.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    for name, item in value.items():
        sub = props.get(name)
        if isinstance(sub, Mapping):
            validate(item, sub, prefix + name)
        elif schema.get("additionalProperties") is False:
            raise _fail(prefix + name, "unknown argument")


# -- JSON parsing ---------------------------------------------------------------


def _parse_constant(name: str) -> object:
    raise _BadJson(f"{name} is not allowed")


def _parse_int(text: str) -> int:
    if len(text.lstrip("-")) > 16:  # 2**53 has 16 digits
        raise _BadJson("integer out of range")
    value = int(text)
    if abs(value) > MAX_SAFE_INTEGER:
        raise _BadJson("integer out of range")
    return value


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _BadJson("number out of range")
    return value


def _too_deep(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: list[object] = list(item.values())
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > MAX_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _parse(line: bytes) -> object:
    """Decode one message; raise :class:`_RpcError` (``-32700``) if refused."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise _RpcError(PARSE_ERROR, "Parse error: not valid UTF-8") from None
    try:
        message = json.loads(
            text,
            parse_constant=_parse_constant,
            parse_int=_parse_int,
            parse_float=_parse_float,
        )
    except RecursionError:
        raise _RpcError(PARSE_ERROR, f"Parse error: nesting too deep (limit {MAX_DEPTH})") from None
    except ValueError as exc:  # JSONDecodeError, _BadJson, int() digit limit
        raise _RpcError(PARSE_ERROR, f"Parse error: {exc}") from None
    if _too_deep(message):
        raise _RpcError(PARSE_ERROR, f"Parse error: nesting too deep (limit {MAX_DEPTH})")
    return message


def _encode(message: Mapping[str, object]) -> bytes:
    """One compact JSON line (lone surrogates fall back to ASCII escapes)."""
    try:
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        data = text.encode("ascii")
    return data + b"\n"


def _valid_id(value: object) -> TypeGuard[str | int]:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _quote(text: str) -> str:
    return json.dumps(text if len(text) <= _QUOTE_LIMIT else text[:_QUOTE_LIMIT] + "...")


def _error_result(message: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


_Key = tuple[bool, str | int]


def _key(request_id: str | int) -> _Key:
    return (isinstance(request_id, str), request_id)


@dataclass
class _Job:
    request_id: str | int
    tool: Tool
    arguments: dict[str, object]
    token: CancelToken


class McpServer:
    """An MCP server over a pair of binary streams (see the module docstring).

    *stdin*/*stdout* are binary streams (``sys.stdin.buffer`` and
    ``sys.stdout.buffer`` for the real thing). *max_line_bytes* caps one
    incoming message, *call_timeout* sets each call's deadline,
    *max_response_bytes* caps one tool result message, *max_queued* the
    number of pending (running or queued) calls. Logs go to *log*
    (default: the ``minideblib.mcp`` logger), which must not write to
    *stdout*.
    """

    def __init__(
        self,
        tools: Sequence[Tool],
        *,
        name: str,
        version: str,
        instructions: str,
        stdin: BinaryIO,
        stdout: BinaryIO,
        max_line_bytes: int = 4 * 1024 * 1024,
        call_timeout: float = 120.0,
        max_response_bytes: int = 200_000,
        max_queued: int = MAX_QUEUED,
        log: logging.Logger | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            try:
                json.dumps(tool.describe(), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"tool {tool.name!r}: description or input schema is not JSON: {exc}"
                ) from None
            self._tools[tool.name] = tool
        self._name = name
        self._version = version
        self._instructions = instructions
        self._stdin = stdin
        self._stdout = stdout
        self._max_line = max_line_bytes
        self._call_timeout = call_timeout
        self._max_response = max_response_bytes
        self._max_queued = max_queued
        self._log = log if log is not None else logging.getLogger("minideblib.mcp")
        #: seconds :meth:`serve` waits for a running call after EOF
        self.shutdown_grace = SHUTDOWN_GRACE
        #: the negotiated protocol version (``None`` before ``initialize``)
        self.protocol_version: str | None = None
        self._write_lock = threading.Lock()
        self._broken = False
        self._lock = threading.Lock()
        self._pending: dict[_Key, CancelToken] = {}
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._served = False
        #: the running call's token and thread
        self._current: tuple[CancelToken, threading.Thread] | None = None
        #: a call abandoned at its deadline whose thread may still run
        self._stuck: tuple[CancelToken, threading.Thread] | None = None

    # -- the loop -------------------------------------------------------------

    def serve(self) -> int:
        """Serve one session until EOF on stdin; return the exit code (0).

        Single use: raises :class:`RuntimeError` if this instance has served
        already or if another ``serve()`` is running in this process. The
        streams stay open. If a write failed with a broken
        pipe, closing *stdout* afterwards may report the unflushed data;
        callers close it inside ``contextlib.suppress(OSError)``.
        """
        if not _SERVE_LOCK.acquire(blocking=False):
            raise RuntimeError("serve() is already running in this process")
        try:
            if self._served:
                raise RuntimeError("serve() can only be called once per McpServer")
            self._served = True
            saved = sys.stdout
            targets = {id(s) for s in (saved, sys.__stdout__) if s is not None}
            moved = self._move_log_handlers(targets)
            sys.stdout = sys.stderr
            worker = threading.Thread(
                target=self._work, args=(self._jobs,), name="mcp-worker", daemon=True
            )
            worker.start()
            try:
                self._read_loop()
            finally:
                self._shutdown(worker)
                sys.stdout = saved
                for handler, stream in moved:
                    handler.setStream(stream)
        finally:
            _SERVE_LOCK.release()
        return 0

    @staticmethod
    def _move_log_handlers(
        targets: set[int],
    ) -> list[tuple[logging.StreamHandler[TextIO], TextIO]]:
        loggers: list[logging.Logger] = [logging.getLogger()]
        loggers.extend(
            lg
            for lg in logging.Logger.manager.loggerDict.values()
            if isinstance(lg, logging.Logger)
        )
        moved: list[tuple[logging.StreamHandler[TextIO], TextIO]] = []
        for lg in loggers:
            for handler in lg.handlers:
                if isinstance(handler, logging.StreamHandler) and id(handler.stream) in targets:
                    moved.append((handler, handler.stream))
                    handler.setStream(sys.stderr)
        return moved

    def _read_loop(self) -> None:
        while True:
            try:
                line = self._stdin.readline(self._max_line + 1)
            except (OSError, ValueError) as exc:
                self._log.error("reading stdin failed: %s", exc)
                return
            if not line:
                return
            if len(line) > self._max_line and not line.endswith(b"\n"):
                eof = self._discard_rest()
                self._send_error(
                    None, INVALID_REQUEST, f"message too long (limit {self._max_line} bytes)"
                )
                if eof:
                    return
                continue
            try:
                self._handle_line(line)
            except Exception:
                self._log.exception("unexpected error handling a message")
                self._send_error(None, INTERNAL_ERROR, "Internal error")

    def _discard_rest(self) -> bool:
        """Skip input through the next newline; True at EOF."""
        while True:
            try:
                chunk = self._stdin.readline(_DISCARD_CHUNK)
            except (OSError, ValueError):
                return True
            if not chunk:
                return True
            if chunk.endswith(b"\n"):
                return False

    def _shutdown(self, worker: threading.Thread) -> None:
        end = time.monotonic() + self.shutdown_grace
        with self._lock:
            tokens = list(self._pending.values())
            threads = [worker]
            for running in (self._current, self._stuck):
                if running is not None:
                    tokens.append(running[0])
                    threads.append(running[1])
        for token in tokens:
            token.cancel()
        self._jobs.put(None)
        for thread in threads:
            thread.join(max(0.0, end - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            with self._lock:
                for running in (self._current, self._stuck):
                    if running is not None and running[1].is_alive():
                        self._stuck = running  # still accounted for, never rejoined
            self._log.warning(
                "a tool call ignored cancellation for %.1f s; exiting anyway", self.shutdown_grace
            )

    # -- writing --------------------------------------------------------------

    def _send(self, message: Mapping[str, object]) -> bool:
        return self._write(_encode(message))

    def _write(self, data: bytes) -> bool:
        with self._write_lock:
            if self._broken:
                return False
            try:
                self._stdout.write(data)
                self._stdout.flush()
            except (OSError, ValueError) as exc:
                self._broken = True
                self._log.error("writing stdout failed: %s", exc)
                return False
        return True

    def _send_result(self, request_id: str | int, result: Mapping[str, object]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_error(self, request_id: str | int | None, code: int, message: str) -> None:
        error = {"code": code, "message": message}
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    # -- reading and dispatch -------------------------------------------------

    def _handle_line(self, line: bytes) -> None:
        line = line.removesuffix(b"\n").removesuffix(b"\r")
        if not line.strip():
            return
        try:
            message = _parse(line)
        except _RpcError as exc:
            self._send_error(None, exc.code, str(exc))
            return
        self._dispatch(message)

    def _dispatch(self, message: object) -> None:
        if isinstance(message, list):
            self._send_error(None, INVALID_REQUEST, "Invalid Request: batches are not supported")
            return
        if not isinstance(message, dict):
            self._send_error(None, INVALID_REQUEST, "Invalid Request: not a JSON object")
            return
        has_id = "id" in message
        raw_id = message.get("id")
        request_id: str | int | None = raw_id if _valid_id(raw_id) else None
        if "method" not in message and has_id and ("result" in message or "error" in message):
            self._log.debug("ignoring a response from the client")
            return
        if message.get("jsonrpc") != "2.0":
            self._send_error(
                request_id, INVALID_REQUEST, 'Invalid Request: "jsonrpc" must be "2.0"'
            )
            return
        method = message.get("method")
        if not isinstance(method, str):
            self._send_error(
                request_id, INVALID_REQUEST, 'Invalid Request: "method" must be a string'
            )
            return
        if has_id and request_id is None:
            self._send_error(
                None, INVALID_REQUEST, 'Invalid Request: "id" must be a string or an integer'
            )
            return
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict | list):
            self._send_error(
                request_id, INVALID_REQUEST, 'Invalid Request: "params" must be an object'
            )
            return
        if request_id is None:
            if isinstance(params, dict):
                self._notification(method, params)
            return
        try:
            if isinstance(params, list):
                raise _RpcError(INVALID_PARAMS, "Invalid params: positional params not supported")
            result = self._request(request_id, method, params)
        except _RpcError as exc:
            self._send_error(request_id, exc.code, str(exc))
            return
        if result is not None:
            self._send_result(request_id, result)

    def _request(
        self, request_id: str | int, method: str, params: dict[str, object]
    ) -> dict[str, object] | None:
        """Answer a request; ``None`` when the answer comes later (tool calls)."""
        if method == "ping":
            return {}
        if method == "initialize":
            return self._initialize(params)
        if self.protocol_version is None:
            raise _RpcError(SERVER_NOT_INITIALIZED, "server not initialized")
        if method == "tools/list":
            return {"tools": [tool.describe() for tool in self._tools.values()]}
        if method == "tools/call":
            return self._enqueue(request_id, params)
        raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {_quote(method)}")

    def _initialize(self, params: dict[str, object]) -> dict[str, object]:
        if self.protocol_version is not None:
            raise _RpcError(INVALID_REQUEST, "Invalid Request: already initialized")
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise _RpcError(INVALID_PARAMS, 'Invalid params: "protocolVersion" must be a string')
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        self.protocol_version = version
        client = params.get("clientInfo")
        client_name = client.get("name") if isinstance(client, dict) else None
        self._log.info(
            "client %s requested protocol %s; using %s",
            _quote(client_name) if isinstance(client_name, str) else "(unnamed)",
            _quote(requested),
            version,
        )
        result: dict[str, object] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self._name, "version": self._version},
        }
        if self._instructions:
            result["instructions"] = self._instructions
        return result

    def _notification(self, method: str, params: dict[str, object]) -> None:
        if method == "notifications/initialized":
            self._log.debug("client reports initialized")
        elif method == "notifications/cancelled":
            request_id = params.get("requestId")
            if not _valid_id(request_id):
                return
            with self._lock:
                token = self._pending.pop(_key(request_id), None)
            if token is not None:
                self._log.info("request %s cancelled by the client", _quote(str(request_id)))
                token.cancel()
        else:
            self._log.debug("ignoring notification %s", _quote(method))

    # -- tool calls -------------------------------------------------------------

    def _enqueue(
        self, request_id: str | int, params: dict[str, object]
    ) -> dict[str, object] | None:
        """Queue a call; a result to send at once when the server is busy."""
        name = params.get("name")
        if not isinstance(name, str):
            raise _RpcError(INVALID_PARAMS, 'Invalid params: "name" must be a string')
        tool = self._tools.get(name)
        if tool is None:
            raise _RpcError(INVALID_PARAMS, f"Invalid params: unknown tool {_quote(name)}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, 'Invalid params: "arguments" must be an object')
        token = CancelToken()
        key = _key(request_id)
        with self._lock:
            if key in self._pending:
                raise _RpcError(INVALID_REQUEST, "Invalid Request: id already in use")
            if self._stuck_alive():
                return _error_result(_BUSY_STUCK)
            if len(self._pending) >= self._max_queued:
                return _error_result(
                    f"server busy: {len(self._pending)} calls pending; wait for results"
                )
            self._pending[key] = token
        self._jobs.put(_Job(request_id, tool, arguments, token))
        return None

    def _stuck_alive(self) -> bool:
        """True while an abandoned call still runs (call with the lock held)."""
        if self._stuck is not None and not self._stuck[1].is_alive():
            self._stuck = None
        return self._stuck is not None

    def _work(self, jobs: queue.Queue[_Job | None]) -> None:
        """Run the jobs of *jobs* (this session's queue only) until ``None``."""
        while True:
            job = jobs.get()
            if job is None:
                return
            try:
                self._run(job)
            except BaseException:
                self._log.exception("tool call failed unexpectedly")
                self._release(job)

    def _release(self, job: _Job) -> None:
        """Free the job's id (unless it was freed and reused meanwhile)."""
        key = _key(job.request_id)
        with self._lock:
            if self._pending.get(key) is job.token:
                del self._pending[key]

    def _run(self, job: _Job) -> None:
        if job.token.is_cancelled:  # cancelled while queued
            self._release(job)
            return
        timed_out = False
        with self._lock:
            busy = self._stuck_alive()
        if busy:
            result = _error_result(_BUSY_STUCK)
        else:
            deadline = time.monotonic() + self._call_timeout
            box: list[dict[str, object]] = []

            def call() -> None:
                try:
                    box.append(self._invoke(job, deadline))
                except BaseException:
                    self._log.exception("tool %s failed", job.tool.name)

            thread = threading.Thread(target=call, name=f"mcp-call-{job.tool.name}", daemon=True)
            with self._lock:
                self._current = (job.token, thread)
            thread.start()
            thread.join(max(0.0, deadline - time.monotonic()))
            with self._lock:
                self._current = None
                if thread.is_alive():
                    timed_out = not job.token.is_cancelled
                    self._stuck = (job.token, thread)
            if thread.is_alive():
                job.token.cancel()
                self._log.warning(
                    "tool %s still running after %g s; abandoned", job.tool.name, self._call_timeout
                )
                result = _error_result(f"tool call timed out after {self._call_timeout:g} s")
            elif box:
                result = box[0]
            else:
                result = _error_result(f"internal error in tool {job.tool.name}")
        self._release(job)
        if job.token.is_cancelled and not timed_out:
            self._log.debug("request %s cancelled: no response", _quote(str(job.request_id)))
            return
        data = _encode({"jsonrpc": "2.0", "id": job.request_id, "result": result})
        if len(data) > self._max_response:
            self._log.warning(
                "tool %s: response of %d bytes over the limit", job.tool.name, len(data)
            )
            replacement = _error_result(
                f"response too large: {len(data)} bytes exceeds the limit of "
                f"{self._max_response} bytes; narrow the request (smaller limit, more filters)"
            )
            data = _encode({"jsonrpc": "2.0", "id": job.request_id, "result": replacement})
        self._write(data)

    def _invoke(self, job: _Job, deadline: float) -> dict[str, object]:
        tool = job.tool
        try:
            validate(job.arguments, tool.input_schema)
        except ToolError as exc:
            return _error_result(str(exc))
        try:
            value = tool.handler(ToolCall(job.arguments, job.token, deadline))
        except ToolError as exc:
            return _error_result(str(exc))
        except BaseException:  # SystemExit and friends from library code included
            if job.token.is_cancelled:
                return _error_result("cancelled")
            if time.monotonic() >= deadline:
                self._log.warning("tool %s timed out", tool.name, exc_info=True)
                return _error_result(f"tool call timed out after {self._call_timeout:g} s")
            self._log.exception("tool %s failed", tool.name)
            return _error_result(f"internal error in tool {tool.name}")
        if not isinstance(value, dict):
            self._log.error("tool %s returned %s, not a dict", tool.name, type(value).__name__)
            return _error_result(f"internal error in tool {tool.name}")
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            self._log.error("tool %s returned a non-JSON result: %s", tool.name, exc)
            return _error_result(f"internal error in tool {tool.name}")
        result: dict[str, object] = {"content": [{"type": "text", "text": text}]}
        version = self.protocol_version
        if version is not None and version >= STRUCTURED_CONTENT_SINCE:
            result["structuredContent"] = value
        return result
