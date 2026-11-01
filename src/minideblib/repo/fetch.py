# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Repository fetcher: http(s) and file:// retrieval with safe defaults."""

from __future__ import annotations

import http.client
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import IO, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from minideblib.errors import MinidebError

__all__ = ["FetchCancelled", "FetchError", "Fetcher", "redact_url"]

log = logging.getLogger(__name__)

_ALLOWED = ("http", "https", "file")
_CHUNK = 64 * 1024
_RETRY_STATUS = (429, 500, 502, 503, 504)
_MAX_DELAY = 10.0

_T = TypeVar("_T")

# ``scheme://userinfo@`` anywhere in a string (URLs embedded in messages too);
# userinfo stops at "/", "?", "#" and whitespace, so ``file:///a@b/c`` and
# paths containing "@" are left alone. The scheme is bounded (no ``\b``, at
# most 32 characters) so that a long run of letters cannot make the search
# quadratic on untrusted text.
_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^/?#\s]*@")
# a whole-string ``user[:password]@host[/path]`` without a scheme; only
# redacted with a password or a path, so a bare ``jane@example.com`` stays.
# This rule is a heuristic for scheme-less proxy/host strings only: it does
# not know about other syntaxes, so e.g. ``C:\x@y/z`` also becomes ``y/z``
# (over-redaction is the safe direction for log output).
_BARE_USERINFO_RE = re.compile(
    r"[^\s/?#@:]+(?P<pw>:[^\s/?#@]*)?@(?P<rest>[^\s/?#@]+(?P<path>[/?#]\S*)?)"
)


def redact_url(url: str) -> str:
    """Return *url* without ``user:password@`` credentials (safe to log or print).

    Every ``scheme://user:password@`` occurrence is redacted, also inside a
    longer text such as an exception message. A whole string of the form
    ``user:password@host/path`` (no scheme) is redacted as well.
    """
    bare = _BARE_USERINFO_RE.fullmatch(url)
    if bare is not None and (bare["pw"] is not None or bare["path"] is not None):
        return bare["rest"]
    return _USERINFO_RE.sub(r"\1", url)


class FetchError(MinidebError):
    """Raised for any failure to retrieve a URL."""

    def __init__(
        self, message: str, url: str, status: int | None = None, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        #: True for network-level failures worth retrying (no HTTP status)
        self.transient = transient

    @property
    def retryable(self) -> bool:
        return self.transient or self.status in _RETRY_STATUS


class FetchCancelled(FetchError):
    """The request was stopped by :meth:`Fetcher.cancel` (no status, not transient)."""


def _is_transient(err: BaseException, url: str) -> bool:
    """Whether a low-level error from an http(s) request may go away on retry."""
    if not _is_network(url):
        return False
    reason = getattr(err, "reason", None)
    for e in (err, reason):
        if isinstance(e, (ssl.SSLError, ValueError, http.client.InvalidURL)):
            return False
    return True


def _is_network(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("http", "https")


def _check_scheme(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise FetchError(f"malformed URL {redact_url(url)!r}: {e}", url) from e
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED:
        raise FetchError(f"unsupported URL scheme {scheme!r}: {redact_url(url)}", url)
    if scheme == "file" and parts.netloc not in ("", "localhost"):
        raise FetchError(f"remote file URL not supported: {redact_url(url)}", url)
    if "@" in parts.netloc:
        # urllib would take ``user:password`` for part of the host name, and
        # through a proxy the credentials would end up in the request line.
        raise FetchError("credentials in URLs are not supported", redact_url(url))
    if scheme == "file" and ".." in unquote(parts.path).split("/"):
        # FileHandler unquotes the path, so ``%2e%2e`` is a ``..`` component
        raise FetchError(f"'..' in file URL not allowed: {redact_url(url)}", url)


class _Response(Protocol):
    """The part of an HTTP/file response object that this module uses."""

    @property
    def headers(self) -> http.client.HTTPMessage: ...

    def read(self, amt: int = ..., /) -> bytes: ...

    def close(self) -> None: ...


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http/https targets, and never from https to http."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        parts = urlsplit(newurl)
        target = parts.scheme.lower()
        if target not in ("http", "https"):
            raise FetchError(
                f"refusing redirect to {target!r} URL: {redact_url(newurl)}", req.full_url, code
            )
        # like apt: an https source must not be downgraded to plain http
        # (a URL chosen as https is meant to stay protected in transit)
        if urlsplit(req.full_url).scheme.lower() == "https" and target == "http":
            raise FetchError(
                f"refusing https -> http redirect: {redact_url(newurl)}", req.full_url, code
            )
        if "@" in parts.netloc:
            # same rule as for the requested URL (see _check_scheme)
            raise FetchError(
                f"refusing redirect: credentials in URLs are not supported: {redact_url(newurl)}",
                req.full_url,
                code,
            )
        log.debug("redirect %s -> %s (%s)", redact_url(req.full_url), redact_url(newurl), code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.FileHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _SafeRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


class Fetcher:
    """Fetch URLs over http, https or file.

    Transient failures are retried *retries* times with exponential backoff
    starting at *retry_delay* seconds. The backoff waits are interruptible
    by :meth:`cancel`; an injected *sleep* function is called instead (for
    tests), and :meth:`cancel` then takes effect after it returns.
    """

    def __init__(
        self,
        timeout: float = 30,
        user_agent: str = "minideblib/0.7",
        max_bytes: int = 2 * 1024**3,
        retries: int = 2,
        retry_delay: float = 0.5,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.retries = retries
        self.retry_delay = retry_delay
        self._sleep = sleep
        self._cancel = threading.Event()
        # per-thread override of the cancellation event (see _bind_cancel)
        self._local = threading.local()
        self._opener = _build_opener()

    def cancel(self) -> None:
        """Stop the requests in progress (in any thread) at their next retry point.

        They raise :class:`FetchCancelled` before their next attempt or
        during their backoff wait; an attempt already running is not
        interrupted. Requests started afterwards are not affected.
        """
        event, self._cancel = self._cancel, threading.Event()
        event.set()

    def _current_cancel(self) -> threading.Event:
        """The cancellation event a request starting now in this thread uses."""
        return getattr(self._local, "cancel", None) or self._cancel

    def _bind_cancel(self, event: threading.Event) -> None:
        """Make requests of the calling thread use *event* (worker threads).

        *event* is normally the one current when the work started, so a
        :meth:`cancel` after that point also reaches requests that start later.
        """
        self._local.cancel = event

    def _with_retries(
        self,
        attempt: Callable[[int], _T],
        url: str,
        retry_status: bool = True,
        cancelled: threading.Event | None = None,
    ) -> _T:
        """Run ``attempt``, retrying retryable FetchErrors with backoff.

        With ``retry_status`` false, HTTP status failures are not retried.
        *attempt* receives the 1-based attempt number (for logging).
        *cancelled* (default: the event current at the start) applies to the
        whole call.
        """
        if cancelled is None:
            cancelled = self._current_cancel()
        retries = max(0, self.retries)
        for n in range(retries + 1):
            if cancelled.is_set():
                raise FetchCancelled(f"request cancelled: {redact_url(url)}", url)
            try:
                return attempt(n + 1)
            except FetchError as e:
                if n >= retries or not (e.transient or (retry_status and e.retryable)):
                    raise
                delay = min(self.retry_delay * 2**n, _MAX_DELAY)
                log.debug(
                    "attempt %d of %d failed (%s); retrying in %ss", n + 1, retries + 1, e, delay
                )
            if self._sleep is None:
                cancelled.wait(delay)
            else:
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _request(self, url: str, method: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"},
        )

    def _open(self, url: str, method: str, attempt: int = 1) -> _Response:
        _check_scheme(url)
        started = time.monotonic()
        try:
            return self._opener.open(self._request(url, method), timeout=self.timeout)
        except FetchError:
            raise
        except urllib.error.HTTPError as e:
            e.close()
            self._log_request(method, url, e.code, started, None, attempt)
            raise FetchError(f"HTTP {e.code} fetching {redact_url(url)}", url, e.code) from e
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ) as e:
            log.debug(
                "%s %s failed after %.3fs, attempt %d/%d: %s",
                method,
                redact_url(url),
                time.monotonic() - started,
                attempt,
                max(0, self.retries) + 1,
                redact_url(str(e)),
            )
            raise FetchError(
                f"cannot fetch {redact_url(url)}: {redact_url(str(e))}",
                url,
                transient=_is_transient(e, url),
            ) from e

    def _log_request(
        self,
        method: str,
        url: str,
        status: int | str | None,
        started: float,
        size: int | None,
        attempt: int,
    ) -> None:
        log.debug(
            "%s %s status=%s %.3fs%s attempt %d/%d",
            method,
            redact_url(url),
            status,
            time.monotonic() - started,
            "" if size is None else f" {size} bytes",
            attempt,
            max(0, self.retries) + 1,
        )

    def get(self, url: str) -> bytes:
        """Return the raw body served at ``url``.

        Transient failures are retried; see the ``retries`` argument.
        """
        return self._with_retries(lambda n: self._get_once(url, n), url)

    def _get_once(self, url: str, attempt: int = 1) -> bytes:
        started = time.monotonic()
        resp = self._open(url, "GET", attempt)
        try:
            expected = _content_length(resp)
            if expected is not None and expected > self.max_bytes:
                raise FetchError(f"{redact_url(url)} exceeds {self.max_bytes} bytes", url)
            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = resp.read(_CHUNK)
                except (OSError, http.client.HTTPException) as e:
                    raise FetchError(
                        f"error reading {redact_url(url)}: {e}",
                        url,
                        transient=_is_transient(e, url),
                    ) from e
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise FetchError(f"{redact_url(url)} exceeds {self.max_bytes} bytes", url)
                chunks.append(chunk)
            if expected is not None and total != expected:
                raise FetchError(
                    f"truncated body from {redact_url(url)}: got {total} of {expected} bytes",
                    url,
                    transient=_is_network(url),
                )
            self._log_request("GET", url, _status(resp), started, total, attempt)
            return b"".join(chunks)
        finally:
            resp.close()

    def _opened(self, url: str, method: str, attempt: int) -> _Response:
        """:meth:`_open` plus a request log line for the successful response."""
        started = time.monotonic()
        resp = self._open(url, method, attempt)
        self._log_request(method, url, _status(resp), started, None, attempt)
        return resp

    def _head(self, url: str, cancelled: threading.Event | None = None) -> _Response:
        return self._with_retries(
            lambda n: self._opened(url, "HEAD", n), url, retry_status=False, cancelled=cancelled
        )

    def exists(self, url: str) -> bool:
        """Return True if ``url`` is retrievable (HEAD, falling back to GET).

        One :meth:`cancel` stops both the HEAD request and the GET fallback.
        """
        _check_scheme(url)
        cancelled = self._current_cancel()
        try:
            self._head(url, cancelled).close()
            return True
        except FetchCancelled:
            raise
        except FetchError as e:
            if e.status in (404, 410):
                return False
            if e.status is None:
                if _is_missing_file(e):
                    return False
                raise
        try:
            # headers only; body is not read
            self._with_retries(
                lambda n: self._opened(url, "GET", n), url, cancelled=cancelled
            ).close()
            return True
        except FetchError as e:
            if e.status in (404, 410):
                return False
            raise


def _status(resp: _Response) -> int | str:
    return getattr(resp, "status", None) or "ok"


def _is_missing_file(err: FetchError) -> bool:
    cause = err.__cause__
    reason = getattr(cause, "reason", cause)
    return isinstance(reason, FileNotFoundError)


def _content_length(resp: _Response) -> int | None:
    value = resp.headers.get("Content-Length")
    if value is None:
        return None
    try:
        n = int(value.strip())
    except ValueError:
        return None
    return n if n >= 0 else None
