# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Repository fetcher: http(s) and file:// retrieval with safe defaults."""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass
from typing import IO, Any, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from minideblib.errors import MinidebError

__all__ = [
    "CancelToken",
    "FetchCancelled",
    "FetchError",
    "FetchTimeout",
    "Fetcher",
    "HostNotAllowed",
    "redact_url",
]

log = logging.getLogger(__name__)

_ALLOWED = ("http", "https", "file")
_CHUNK = 64 * 1024
_RETRY_STATUS = (429, 500, 502, 503, 504)
_MAX_DELAY = 10.0
# how often a backoff wait on a CancelToken also looks at Fetcher.cancel()
_POLL = 0.05
# cloud instance metadata services (refused with block_link_local)
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
# metadata service addresses outside the link-local ranges: the AWS IPv6
# instance metadata service and the Alibaba Cloud metadata service
_METADATA_ADDRS = frozenset(
    {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
)
# a host name that inet_aton might read as an IPv4 address (``2852039166``,
# ``0xa9.0xfe.0xa9.0xfe``): digits, dots and hex digits with an ``x``
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_NUMERIC_HOST_RE = re.compile(r"[0-9a-fx.]+")

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
    """The request was stopped by :meth:`Fetcher.cancel` or its :class:`CancelToken`.

    It has no status and is not transient.
    """


class FetchTimeout(FetchError):
    """The deadline of the call passed before the request completed (not transient)."""


class HostNotAllowed(FetchError):
    """The host of the URL or of a redirect target is refused by the host policy."""


class CancelToken:
    """A thread-safe cancellation flag for one or more calls.

    Pass it as ``token=`` to :meth:`Fetcher.get` or :meth:`Fetcher.exists`;
    :meth:`cancel` (from any thread) makes those calls raise
    :class:`FetchCancelled` before their next attempt, during their backoff
    wait or between two chunks of a response body. A cancelled token stays
    cancelled.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Cancel every call using this token (idempotent)."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for cancellation; True if cancelled."""
        return self._event.wait(timeout)


@dataclass(frozen=True)
class _Call:
    """Per-call limits of a :meth:`Fetcher.get`/:meth:`Fetcher.exists` call."""

    #: every token of the enclosing scopes; any of them cancels the call
    tokens: tuple[CancelToken, ...] = ()
    #: a :func:`time.monotonic` instant (the earliest of the enclosing scopes)
    deadline: float | None = None

    @property
    def cancelled(self) -> bool:
        return any(t.is_cancelled for t in self.tokens)

    def remaining(self) -> float | None:
        """Seconds left before the deadline (``None``: no deadline)."""
        return None if self.deadline is None else self.deadline - time.monotonic()

    def check(self, url: str) -> None:
        """Raise FetchCancelled or FetchTimeout if the call must stop now."""
        if self.cancelled:
            raise FetchCancelled(f"request cancelled: {redact_url(url)}", url)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise FetchTimeout(f"deadline exceeded: {redact_url(url)}", url)


_NO_LIMITS = _Call()


def _normalize_host(host: str) -> str:
    """Lower case ASCII (IDNA) form, without brackets and a trailing dot.

    Full-width characters and the ideographic full stop map to their ASCII
    forms, as the resolver would map them. Raises :class:`UnicodeError` for
    a host name that has no IDNA form.
    """
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    # the codec only length-checks ASCII labels and nameprep-maps the others
    return host.encode("idna").decode("ascii").lower().rstrip(".")


def _is_blocked_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Link-local or a known metadata service address.

    IPv6 forms that embed an IPv4 address (IPv4-mapped ``::ffff:0:0/96``,
    NAT64 ``64:ff9b::/96`` and IPv4-compatible ``::/96`` except ``::`` and
    ``::1``) are judged by that IPv4 address.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        # without the zone id (``%eth0``), which takes part in comparisons
        addr = ipaddress.IPv6Address(addr.packed)
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr in _NAT64 or (addr in _IPV4_COMPATIBLE and int(addr) > 1):
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr.is_link_local or addr in _METADATA_ADDRS


def _is_blocked_text(ip: str) -> bool:
    """:func:`_is_blocked_addr` for an address string; True if it does not parse."""
    try:
        return _is_blocked_addr(ipaddress.ip_address(ip))
    except ValueError:
        return True  # fail closed


def _is_link_local(host: str) -> bool:
    """Whether *host* (normalized) is a link-local address or a metadata host."""
    if host in _METADATA_HOSTS:
        return True
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if _NUMERIC_HOST_RE.fullmatch(host):
            # the resolver accepts the inet_aton forms of an IPv4 address too
            with contextlib.suppress(OSError):
                addr = ipaddress.IPv4Address(socket.inet_aton(host))
    return addr is not None and _is_blocked_addr(addr)


def _allowed_entry(entry: str) -> str:
    """Normalized *allowed_hosts* entry; ValueError for a URL, path or port."""
    if not isinstance(entry, str):
        raise TypeError(f"allowed_hosts entries must be str, not {type(entry).__name__}")
    if "/" in entry:
        raise ValueError(f"allowed_hosts entry {entry!r}: a host name, not a URL or path")
    bare = entry.strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    if ":" in bare:
        try:
            ipaddress.IPv6Address(bare)
        except ValueError:
            raise ValueError(
                f"allowed_hosts entry {entry!r}: ports are not supported (hosts only)"
            ) from None
    wildcard = bare.startswith("*.")
    try:
        host = _normalize_host(bare[2:] if wildcard else bare)
    except UnicodeError as e:
        raise ValueError(f"allowed_hosts entry {entry!r}: invalid host name ({e})") from None
    if not host:
        raise ValueError(f"allowed_hosts entry {entry!r}: empty host name")
    return "*." + host if wildcard else host


class _ResolvedLinkLocal(OSError):
    """A host name resolved to a refused address (see :func:`_vetted_connection`)."""


def _vetted_connection(
    address: tuple[str, int],
    timeout: object = None,
    source_address: tuple[str, int] | None = None,
    **_kw: object,
) -> socket.socket:
    """Like :func:`socket.create_connection`, refusing link-local addresses.

    The host is resolved once; if any address is link-local or a metadata
    service address, :class:`_ResolvedLinkLocal` is raised; otherwise the
    connection goes to the vetted addresses (so a second lookup cannot
    return a different one).
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        if _is_blocked_text(str(sockaddr[0])):
            raise _ResolvedLinkLocal(
                f"refusing host {host!r}: it resolves to the link-local or "
                f"metadata address {sockaddr[0]}"
            )
    if not infos:
        raise OSError(f"getaddrinfo returned no address for {host!r}")
    error: OSError | None = None
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            if isinstance(timeout, (int, float)):  # not socket._GLOBAL_DEFAULT_TIMEOUT
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            error = e
            sock.close()
    assert error is not None
    raise error


class _PeerCheck(http.client.HTTPConnection):
    """Backstop: refuse a connected peer with a blocked address.

    The ``_create_connection`` hook (:func:`_vetted_connection`) does the
    check before connecting and pins the address; this check after
    connecting keeps the policy closed should http.client stop using that
    hook. It runs before anything is sent: for HTTPS, the MRO of
    :class:`_VettedHTTPSConnection` places it below the TLS wrap.
    """

    def connect(self) -> None:
        super().connect()
        sock = self.sock
        if sock is None:
            raise _ResolvedLinkLocal(f"refusing host {self.host!r}: no socket to check")
        try:
            peer = str(sock.getpeername()[0])
        except (OSError, IndexError):
            peer = "?"  # fails closed
        if _is_blocked_text(peer):
            sock.close()
            self.sock = None
            raise _ResolvedLinkLocal(
                f"refusing host {self.host!r}: connected to the link-local or "
                f"metadata address {peer}"
            )


class _VettedHTTPConnection(_PeerCheck):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _vetted_connection


class _VettedHTTPSConnection(http.client.HTTPSConnection, _PeerCheck):
    # MRO: HTTPSConnection.connect -> _PeerCheck.connect -> HTTPConnection.connect,
    # so the peer is checked before the TLS handshake. TLS still uses
    # server_hostname=self.host: certificate checks are unchanged.
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _vetted_connection


def _via_proxy(req: urllib.request.Request) -> bool:
    """Whether *req* goes through a proxy (which then resolves the host)."""
    return req.has_proxy() or getattr(req, "_tunnel_host", None) is not None


class _VettedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        if _via_proxy(req):
            return super().http_open(req)
        return self.do_open(_VettedHTTPConnection, req)


class _VettedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        if _via_proxy(req):
            return super().https_open(req)
        kwargs: dict[str, Any] = {"context": getattr(self, "_context", None)}
        if hasattr(self, "_check_hostname"):  # Python 3.11
            kwargs["check_hostname"] = self._check_hostname
        return self.do_open(_VettedHTTPSConnection, req, **kwargs)


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
    """Follow redirects only to http/https targets, and never from https to http.

    *check_host* (the Fetcher's host policy) is applied to every target.
    """

    def __init__(self, check_host: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._check_host = check_host

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
        if self._check_host is not None:
            try:
                self._check_host(newurl)
            except HostNotAllowed as e:
                raise HostNotAllowed(f"refusing redirect: {e}", req.full_url, code) from None
        log.debug("redirect %s -> %s (%s)", redact_url(req.full_url), redact_url(newurl), code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(
    check_host: Callable[[str], None] | None = None,
    vet_addresses: bool = False,
) -> urllib.request.OpenerDirector:
    """The Fetcher's opener; *vet_addresses* adds the connect-time check."""
    opener = urllib.request.OpenerDirector()
    http_handler, https_handler = (
        (_VettedHTTPHandler(), _VettedHTTPSHandler())
        if vet_addresses
        else (urllib.request.HTTPHandler(), urllib.request.HTTPSHandler())
    )
    for handler in (
        urllib.request.ProxyHandler(),
        http_handler,
        https_handler,
        urllib.request.FileHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _SafeRedirectHandler(check_host),
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

    Host policy: with *allowed_hosts* (exact host names or ``*.suffix``
    wildcards matching sub-domains of *suffix*, case-insensitive; ports are
    not compared) only those hosts are requested, also as redirect targets;
    ``file:`` URLs have no host and are not affected. With
    *block_link_local* (the default) link-local addresses (``169.254.0.0/16``,
    ``fe80::/10``) and the cloud metadata host names ``metadata``,
    ``metadata.google.internal`` and ``instance-data`` are refused for every
    URL and redirect, even when listed in *allowed_hosts*; so are the
    metadata service addresses ``fd00:ec2::254`` (AWS, IPv6) and
    ``100.100.100.200`` (Alibaba Cloud). Host names are compared in their
    IDNA (ASCII) form; a name without one is refused. A refused URL raises
    :class:`HostNotAllowed` before any connection is made. With
    *block_link_local*, every host name is also checked when connecting:
    if any address it resolves to is refused, :class:`HostNotAllowed` is
    raised (not retried), and the connection goes to the checked address.
    Loopback and private addresses are allowed.

    With a proxy (``http_proxy``/``https_proxy`` from the environment) the
    proxy resolves the host names: only the URL text is checked (literal
    link-local addresses and the metadata names are still refused), the
    connect-time check is not made for the proxy connection, and the proxy
    itself is trusted configuration.

    The deadline does not bound DNS resolution (:func:`socket.getaddrinfo`
    cannot be interrupted); a slow resolver can make a call overrun it.

    :meth:`scope` applies a token and a deadline to every request made
    inside it, also by library code (such as
    :class:`~minideblib.repo.client.RepoClient`) that calls :meth:`get` and
    :meth:`exists` without them.
    """

    def __init__(
        self,
        timeout: float = 30,
        user_agent: str = "minideblib/0.7",
        max_bytes: int = 2 * 1024**3,
        retries: int = 2,
        retry_delay: float = 0.5,
        sleep: Callable[[float], None] | None = None,
        *,
        allowed_hosts: Collection[str] | None = None,
        block_link_local: bool = True,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.retries = retries
        self.retry_delay = retry_delay
        self._sleep = sleep
        if isinstance(allowed_hosts, (str, bytes)):
            raise TypeError("allowed_hosts must be a collection of host names, not a string")
        self.allowed_hosts: frozenset[str] | None = (
            None if allowed_hosts is None else frozenset(map(_allowed_entry, allowed_hosts))
        )
        #: fixed at construction (the connect-time check is part of the opener)
        self.block_link_local = block_link_local
        self._cancel = threading.Event()
        # per-thread override of the cancellation event (see _bind_cancel)
        self._local = threading.local()
        self._opener = _build_opener(self._check_host, vet_addresses=block_link_local)

    def _check_host(self, url: str) -> None:
        """Raise :class:`HostNotAllowed` if the host policy refuses *url*."""
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return
        try:
            host = _normalize_host(parts.hostname or "")
        except UnicodeError:
            raise HostNotAllowed(
                f"refusing host without an IDNA form: {redact_url(url)}", url
            ) from None
        if self.block_link_local and _is_link_local(host):
            raise HostNotAllowed(
                f"refusing link-local or metadata host {host!r}: {redact_url(url)}", url
            )
        allowed = self.allowed_hosts
        if allowed is None or host in allowed:
            return
        if any(h.startswith("*.") and host.endswith(h[1:]) for h in allowed):
            return
        raise HostNotAllowed(f"host {host!r} is not allowed: {redact_url(url)}", url)

    def _current_call(self) -> _Call:
        """The token and deadline of the call running in this thread."""
        return getattr(self._local, "call", None) or _NO_LIMITS

    @contextlib.contextmanager
    def scope(
        self, token: CancelToken | None = None, deadline: float | None = None
    ) -> Generator[None]:
        """Apply *token* and *deadline* to every request of this thread inside.

        Scopes nest: the tokens of all enclosing scopes apply, and the
        earliest deadline. A ``token``/``deadline`` passed to :meth:`get` or
        :meth:`exists` adds to the scope in the same way.
        """
        with self._call_scope(token, deadline):
            yield

    @contextlib.contextmanager
    def _call_scope(
        self,
        token: CancelToken | None = None,
        deadline: float | None = None,
        base: _Call | None = None,
    ) -> Generator[_Call]:
        """Enter *base* (default: this thread's scope) plus *token* and *deadline*.

        Worker threads pass the scope captured in the calling thread as *base*.
        """
        previous = getattr(self._local, "call", None)
        outer = base if base is not None else self._current_call()
        tokens = outer.tokens
        if token is not None and token not in tokens:
            tokens += (token,)
        deadlines = [d for d in (outer.deadline, deadline) if d is not None]
        call = _Call(tokens, min(deadlines) if deadlines else None)
        self._local.call = call
        try:
            yield call
        finally:
            self._local.call = previous

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
        whole call, as do the token and deadline of :meth:`_call_scope`.
        """
        if cancelled is None:
            cancelled = self._current_cancel()
        call = self._current_call()
        retries = max(0, self.retries)
        for n in range(retries + 1):
            if cancelled.is_set():
                raise FetchCancelled(f"request cancelled: {redact_url(url)}", url)
            call.check(url)
            try:
                return attempt(n + 1)
            except (FetchCancelled, FetchTimeout, HostNotAllowed):
                raise
            except FetchError as e:
                # a failure caused by the deadline (socket timeout) or seen
                # after a cancellation is reported as such
                try:
                    call.check(url)
                except FetchError as stop:
                    raise stop from e
                if n >= retries or not (e.transient or (retry_status and e.retryable)):
                    raise
                delay = min(self.retry_delay * 2**n, _MAX_DELAY)
                log.debug(
                    "attempt %d of %d failed (%s); retrying in %ss", n + 1, retries + 1, e, delay
                )
            self._backoff(delay, cancelled, call)
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, delay: float, cancelled: threading.Event, call: _Call) -> None:
        """Wait *delay* seconds, at most until the deadline; ends early on cancellation."""
        if call.deadline is not None:
            delay = max(0.0, min(delay, call.deadline - time.monotonic()))
        if self._sleep is not None:
            self._sleep(delay)
            return
        if not call.tokens:
            cancelled.wait(delay)
            return
        end = time.monotonic() + delay
        while not cancelled.is_set() and not call.cancelled:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            call.tokens[0].wait(min(remaining, _POLL))

    def _request(self, url: str, method: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"},
        )

    def _open(self, url: str, method: str, attempt: int = 1) -> _Response:
        _check_scheme(url)
        self._check_host(url)
        timeout = self.timeout
        remaining = self._current_call().remaining()
        if remaining is not None:
            if remaining <= 0:
                raise FetchTimeout(f"deadline exceeded: {redact_url(url)}", url)
            timeout = min(timeout, remaining)
        started = time.monotonic()
        try:
            return self._opener.open(self._request(url, method), timeout=timeout)
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
            reason = getattr(e, "reason", None)
            if isinstance(reason, _ResolvedLinkLocal):
                # a policy decision, not a network failure: not retried
                raise HostNotAllowed(f"{reason}: {redact_url(url)}", url) from e
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

    def get(
        self, url: str, *, token: CancelToken | None = None, deadline: float | None = None
    ) -> bytes:
        """Return the raw body served at ``url``.

        Transient failures are retried; see the ``retries`` argument.
        A cancelled *token* raises :class:`FetchCancelled`; once *deadline*
        (a :func:`time.monotonic` instant) has passed, :class:`FetchTimeout`
        is raised. Both are checked before every attempt, end the backoff
        waits early and are checked between chunks of the body; the
        deadline also caps the socket timeout of the connection and of every
        read. A cancellation during a blocked read takes effect when the
        read returns. Both add to an enclosing :meth:`scope`.
        """
        _check_scheme(url)
        self._check_host(url)
        with self._call_scope(token, deadline):
            return self._with_retries(lambda n: self._get_once(url, n), url)

    def _get_once(self, url: str, attempt: int = 1) -> bytes:
        started = time.monotonic()
        call = self._current_call()
        resp = self._open(url, "GET", attempt)
        # read1 returns what has arrived, so that the checks between
        # chunks also happen on a slow connection
        read = getattr(resp, "read1", resp.read)
        sock = _socket_of(resp) if call.deadline is not None else None
        try:
            expected = _content_length(resp)
            if expected is not None and expected > self.max_bytes:
                raise FetchError(f"{redact_url(url)} exceeds {self.max_bytes} bytes", url)
            chunks: list[bytes] = []
            total = 0
            while True:
                call.check(url)
                remaining = call.remaining()
                if sock is not None and remaining is not None and sock.fileno() >= 0:
                    # each read ends by the deadline, not only the first one
                    sock.settimeout(max(min(self.timeout, remaining), 0.001))
                try:
                    chunk = read(_CHUNK)
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

    def exists(
        self, url: str, *, token: CancelToken | None = None, deadline: float | None = None
    ) -> bool:
        """Return True if ``url`` is retrievable (HEAD, falling back to GET).

        One :meth:`cancel` stops both the HEAD request and the GET fallback;
        *token* and *deadline* work as for :meth:`get` and cover both.
        """
        _check_scheme(url)
        self._check_host(url)
        cancelled = self._current_cancel()
        with self._call_scope(token, deadline):
            try:
                self._head(url, cancelled).close()
                return True
            except (FetchCancelled, FetchTimeout, HostNotAllowed):
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


def _socket_of(resp: _Response) -> socket.socket | None:
    """The socket under an http.client response, if it can be found."""
    raw = getattr(getattr(resp, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


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
