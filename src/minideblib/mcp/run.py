# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""``minideb mcp``: start the MCP server on the process's stdin and stdout.

:func:`run` is the whole command. In order it:

1. fills closed standard descriptors (0, 1, 2) with ``/dev/null``, so no
   descriptor opened later can take their place;
2. rejects the CLI-only options ``-c``, ``-a`` and ``-j``, checks the
   platform and refuses root (:func:`~minideblib.mcp.startup.check_platform`,
   :func:`~minideblib.mcp.startup.check_not_root`), before anything is read
   from stdin or built;
3. validates the options into a :class:`~minideblib.mcp.startup.ServerConfig`
   and builds the tools; any :class:`~minideblib.mcp.startup.StartupError` is
   written as one ``minideb: error: ...`` line to descriptor 2 (never a
   traceback, never stdout, even when ``sys.stderr`` is None) and the exit
   status is 2; ``--no-verify`` writes a warning the same way;
4. moves the protocol streams to private close-on-exec descriptors (3 or
   higher): descriptor 1 becomes a copy of stderr and descriptor 0 reads
   ``/dev/null``, so nothing else in the process (``print``, C extensions,
   child processes) can write to the protocol channel or read from it;
5. escapes control and invisible characters in every log record: handlers
   of the ``minideblib`` and root loggers, :data:`logging.lastResort` (the
   output of loggers without handlers) and :mod:`warnings` (captured into
   logging for the session); a traceback (``-vv``) becomes part of its
   one-line record;
6. serves until EOF on stdin, SIGTERM or SIGINT. A signal ends the session
   like EOF: in-flight and queued calls are cancelled and get no response,
   the server waits for them up to :data:`~minideblib.mcp.protocol.SHUTDOWN_GRACE`
   seconds, and the exit status is 0. Signals are noticed by a watcher
   thread (via :func:`signal.set_wakeup_fd`), so they work whichever thread
   the system delivers them to and while the main thread is blocked
   writing (a full stderr or stdout pipe): the first signal also starts a
   timer that ends the process with status 0 after ``SHUTDOWN_GRACE + 2``
   seconds, and a second signal ends it at once (``os._exit(0)``, status 0:
   a signal always means a clean stop for this server).

The base download directory defaults to the server's working directory,
which the server never changes; it is checked when a download is requested.
Stale temporary files are removed by the download tool in the directory it
writes to (:meth:`~minideblib.mcp.safeio.WriteRoot.cleanup_stale`).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import selectors
import signal
import sys
import threading
from collections.abc import Generator
from types import FrameType
from typing import BinaryIO, cast

from minideblib import __version__
from minideblib.mcp.protocol import SHUTDOWN_GRACE, McpServer
from minideblib.mcp.startup import StartupError, build_config, check_not_root, check_platform
from minideblib.mcp.tools import INSTRUCTIONS, build_tools

__all__ = ["NO_VERIFY_WARNING", "escape_log_text", "run"]

#: C0/C1 controls (tab and newline included: one record stays one line), DEL,
#: and invisible or direction-changing format characters
_LOG_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f"
    "\ufeff\ufff9-\ufffb\ud800-\udfff\U000e0000-\U000e007f]"
)
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
_READ_CHUNK = 64 * 1024
#: seconds after the first stop signal at which the process ends regardless
_HARD_STOP = SHUTDOWN_GRACE + 2

NO_VERIFY_WARNING = (
    "minideb: warning: --no-verify: repository index hashes are not checked; "
    "downloaded packages are still always verified"
)


def _escape(match: re.Match[str]) -> str:
    code = ord(match.group())
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def escape_log_text(text: str) -> str:
    """*text* with control and invisible characters as ``\\xNN``/``\\uNNNN`` escapes."""
    return _LOG_UNSAFE.sub(_escape, text)


class _EscapingFormatter(logging.Formatter):
    """Formats with *base*, then escapes the result (:func:`escape_log_text`).

    The base output may span lines (a traceback); escaped, it is one line.
    """

    def __init__(self, base: logging.Formatter | None) -> None:
        super().__init__()
        self.base = base if base is not None else logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return escape_log_text(self.base.format(record).rstrip("\n"))


class _LastResort(logging.Handler):
    """:data:`logging.lastResort` for the session: escaped, to ``sys.stderr`` if any."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.setFormatter(_EscapingFormatter(None))

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stderr
        if stream is None:
            return
        try:
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


@contextlib.contextmanager
def _escaped_logs() -> Generator[None]:
    """Escape every log record for the session (see the module docstring)."""
    saved: list[tuple[logging.Handler, logging.Formatter | None]] = []
    for logger in (logging.getLogger("minideblib"), logging.getLogger()):
        for handler in logger.handlers:
            if not isinstance(handler.formatter, _EscapingFormatter):
                saved.append((handler, handler.formatter))
                handler.setFormatter(_EscapingFormatter(handler.formatter))
    last_resort = logging.lastResort
    logging.lastResort = _LastResort()
    capturing = getattr(logging, "_warnings_showwarning", None) is not None
    logging.captureWarnings(True)
    # captured warnings go to "py.warnings", which gets a NullHandler when it has none
    py_warnings = logging.getLogger("py.warnings")
    warn_handler = _LastResort()
    py_warnings.addHandler(warn_handler)
    try:
        yield
    finally:
        py_warnings.removeHandler(warn_handler)
        if not capturing:
            logging.captureWarnings(False)
        logging.lastResort = last_resort
        for handler, formatter in saved:
            handler.setFormatter(formatter)


def _say(line: str) -> None:
    """Write *line* to descriptor 2 (safe when ``sys.stderr`` is None or closed)."""
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stderr.flush()
    with contextlib.suppress(OSError):
        os.write(2, (line + "\n").encode("utf-8", "backslashreplace"))


def _fill_std_fds() -> None:
    """Open ``/dev/null`` on every closed descriptor among 0, 1 and 2."""
    while True:
        fd = os.open(os.devnull, os.O_RDWR)
        if fd > 2:
            os.close(fd)
            return


class _Stdin:
    """Line reader over a descriptor that also stops when *stop_fd* is readable.

    ``readline(limit)`` behaves like a binary file's: at most *limit*
    bytes, ending at the first newline; ``b""`` at EOF. Once a stop was
    requested (*stop* set, or *stop_fd* readable) every read returns
    ``b""``. Uses :mod:`selectors`, so descriptor numbers are not limited
    to ``FD_SETSIZE``; a regular file or device the selector refuses is read
    directly (such reads do not wait for input).
    """

    def __init__(self, fd: int, stop_fd: int | None, stop: threading.Event) -> None:
        self._fd = fd
        self._stop_fd = stop_fd
        self._stop = stop
        self._buf = bytearray()
        self._eof = False
        self._selector = selectors.DefaultSelector()
        #: a regular file or device that the selector refuses (epoll: EPERM);
        #: reading it never blocks for long, so it is read without waiting
        self._direct = False
        try:
            self._selector.register(fd, selectors.EVENT_READ)
        except PermissionError:
            self._direct = True
        if stop_fd is not None:
            self._selector.register(stop_fd, selectors.EVENT_READ)

    def close(self) -> None:
        self._selector.close()

    def readline(self, limit: int = -1) -> bytes:
        while True:
            if self._stop.is_set():
                return b""
            end = len(self._buf) if limit < 0 else min(limit, len(self._buf))
            nl = self._buf.find(b"\n", 0, end)
            if nl >= 0:
                return self._take(nl + 1)
            if self._eof or (limit >= 0 and len(self._buf) >= limit):
                return self._take(end)
            self._fill()

    def _take(self, n: int) -> bytes:
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _fill(self) -> None:
        # direct: the stop Event (set by the watcher) is checked by readline
        selected = () if self._direct else self._selector.select()
        ready = {self._fd} if self._direct else {key.fd for key, _ in selected}
        if self._stop_fd is not None and self._stop_fd in ready:
            self._stop.set()
            return
        if self._fd in ready:
            data = os.read(self._fd, _READ_CHUNK)
            if data:
                self._buf += data
            else:
                self._eof = True


class _Streams:
    """The private protocol descriptors set up by :func:`_protocol_streams`."""

    def __init__(self, stdin_fd: int, stdout: BinaryIO) -> None:
        self.stdin_fd = stdin_fd
        self.stdout = stdout


@contextlib.contextmanager
def _protocol_streams() -> Generator[_Streams]:
    """Take descriptors 0 and 1 for the protocol; leave stderr and /dev/null in their place.

    Descriptors 0-2 must be open (:func:`_fill_std_fds`). The private copies
    are close-on-exec and 3 or higher. Descriptor 1 stays a copy of stderr
    after the session (the process is about to exit; late writes must not
    reach the client).
    """
    import fcntl  # POSIX only; the platform was checked

    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.flush()
    out_fd = fcntl.fcntl(1, fcntl.F_DUPFD_CLOEXEC, 3)
    try:
        in_fd = fcntl.fcntl(0, fcntl.F_DUPFD_CLOEXEC, 3)
    except OSError:
        os.close(out_fd)
        raise
    null = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(2, 1)
        os.dup2(null, 0)
    finally:
        os.close(null)
    stdout = os.fdopen(out_fd, "wb")
    try:
        yield _Streams(in_fd, stdout)
    finally:
        with contextlib.suppress(OSError):
            stdout.close()
        with contextlib.suppress(OSError):
            os.close(in_fd)


def _ignore(signum: int, frame: FrameType | None) -> None:
    """Python-level handler: the watcher thread does the work (see :func:`_watch`)."""


def _watch(wake_fd: int, stop_fd: int, stop: threading.Event) -> None:
    """Read signal numbers from *wake_fd*; a NUL byte ends the watcher.

    The first SIGTERM/SIGINT sets *stop*, makes *stop_fd* readable and
    starts the hard-stop timer; a second one ends the process at once.
    """
    seen = 0
    while True:
        try:
            data = os.read(wake_fd, 64)
        except InterruptedError:
            continue
        except OSError:
            return
        for byte in data:
            if byte not in _STOP_SIGNALS:
                continue
            seen += 1
            if seen > 1:
                os._exit(0)
            stop.set()
            with contextlib.suppress(OSError):
                os.write(stop_fd, b"x")
            timer = threading.Timer(_HARD_STOP, os._exit, (0,))
            timer.daemon = True
            timer.start()
        if not data or 0 in data:
            return


@contextlib.contextmanager
def _stop_signals(stop: threading.Event) -> Generator[int | None]:
    """Catch SIGTERM/SIGINT for the session; yields a descriptor readable once stopping.

    Yields None (and changes nothing) outside the main thread, where Python
    cannot install signal handlers.
    """
    if threading.current_thread() is not threading.main_thread():
        yield None
        return
    wake_r, wake_w = os.pipe()
    stop_r, stop_w = os.pipe()
    os.set_blocking(wake_w, False)
    previous = {sig: signal.signal(sig, _ignore) for sig in _STOP_SIGNALS}
    old_wake = signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    watcher = threading.Thread(
        target=_watch, args=(wake_r, stop_w, stop), name="mcp-signals", daemon=True
    )
    watcher.start()
    try:
        yield stop_r
    finally:
        signal.set_wakeup_fd(old_wake)
        for sig, old in previous.items():
            signal.signal(sig, old)
        with contextlib.suppress(OSError):
            os.write(wake_w, b"\0")
        watcher.join(1.0)
        fds = (wake_w, stop_r, stop_w) if watcher.is_alive() else (wake_r, wake_w, stop_r, stop_w)
        for fd in fds:
            os.close(fd)


def _check_options(args: argparse.Namespace) -> None:
    if getattr(args, "components", None) or getattr(args, "architectures", None):
        raise StartupError(
            "minideb mcp does not accept -c/--component or -a/--arch "
            "(tool calls take components and architectures)"
        )
    if getattr(args, "json", False):
        raise StartupError("minideb mcp does not accept -j/--json (it always speaks JSON-RPC)")


def run(args: argparse.Namespace) -> int:
    """Run ``minideb mcp`` with the parsed command line *args*; the exit status."""
    _fill_std_fds()
    try:
        _check_options(args)
        check_platform()
        check_not_root()
        config = build_config(args)
        tools = build_tools(config)
    except StartupError as exc:
        _say(f"minideb: error: {exc}")
        return 2
    if not config.verify:
        _say(NO_VERIFY_WARNING)
    stop = threading.Event()
    try:
        with _protocol_streams() as streams, _escaped_logs(), _stop_signals(stop) as stop_fd:
            reader = _Stdin(streams.stdin_fd, stop_fd, stop)
            try:
                server = McpServer(
                    tools,
                    name="minideb",
                    version=__version__,
                    instructions=INSTRUCTIONS,
                    stdin=cast(BinaryIO, reader),
                    stdout=streams.stdout,
                    call_timeout=config.call_timeout,
                    max_response_bytes=config.max_response_bytes,
                )
                code = server.serve()
            finally:
                reader.close()
    except OSError as exc:
        _say(f"minideb: error: cannot set up the protocol streams: {exc.strerror or exc}")
        return 2
    if stop.is_set():
        logging.getLogger("minideblib.mcp").info("stopped by a signal")
    return code
