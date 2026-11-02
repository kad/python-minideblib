# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Per-call cancellation tokens, deadlines and the host policy of the Fetcher."""

import http.server
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from minideblib.repo import fetch
from minideblib.repo.fetch import (
    CancelToken,
    FetchCancelled,
    Fetcher,
    FetchError,
    FetchTimeout,
    HostNotAllowed,
)

HITS: dict[str, int] = {}
RELEASE = threading.Event()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_HEAD(self) -> None:
        if self.path.startswith(("/redir?", "/hangrepo/", "http://")):
            self.do_GET()
        else:
            super().do_HEAD()

    def do_GET(self) -> None:
        p = self.path
        HITS[p] = HITS.get(p, 0) + 1
        if p.startswith("http://"):  # a request through this server as a proxy
            body = b"proxied"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.startswith("/503then200-"):
            if HITS[p] <= 2:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
        elif p == "/503":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/trickle":
            # 20 pieces of 100 bytes, 0.1 s apart (2 s in total)
            self.send_response(200)
            self.send_header("Content-Length", "2000")
            self.end_headers()
            for _ in range(20):
                if RELEASE.is_set():
                    break
                try:
                    self.wfile.write(b"x" * 100)
                    self.wfile.flush()
                except OSError:
                    break
                time.sleep(0.1)
            self.close_connection = True
        elif p == "/stall":
            # 3 pieces 0.3 s apart, then nothing until the test ends
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            for _ in range(3):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.3)
            RELEASE.wait(10)
            self.close_connection = True
        elif p == "/hang" or p.startswith("/hangrepo/"):
            RELEASE.wait(10)
            self.close_connection = True
        elif p.startswith("/redir?"):
            self.send_response(302)
            self.send_header("Location", p[len("/redir?") :])
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            super().do_GET()


@pytest.fixture
def server(tmp_path, monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "hello.txt").write_bytes(b"hello world")

    def handler(*a: Any, **kw: Any) -> Handler:
        return Handler(*a, directory=str(tmp_path), **kw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    RELEASE.clear()
    HITS.clear()
    yield srv.server_address[1]
    RELEASE.set()
    srv.shutdown()
    srv.server_close()


class _Recorder:
    """Stands in for the opener: records the URLs and opens no socket."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, req: Any, timeout: float | None = None) -> Any:
        self.urls.append(req.full_url)
        raise urllib.error.URLError("no network in this test")


def _recording(**kw: Any) -> tuple[Fetcher, _Recorder]:
    f = Fetcher(retries=0, **kw)
    rec = _Recorder()
    f._opener = rec  # ty: ignore[invalid-assignment]  # duck-typed opener
    return f, rec


# -- CancelToken ---------------------------------------------------------------


def test_cancel_token_basics():
    token = CancelToken()
    assert not token.is_cancelled
    assert token.wait(0.01) is False
    threading.Timer(0.05, token.cancel).start()
    assert token.wait(5) is True
    assert token.is_cancelled
    token.cancel()  # idempotent
    assert token.is_cancelled and token.wait(0) is True


def test_new_names_are_public_fetch_errors():
    for name in ("CancelToken", "FetchTimeout", "HostNotAllowed"):
        assert name in fetch.__all__
    assert issubclass(FetchTimeout, FetchError)
    assert issubclass(HostNotAllowed, FetchError)
    assert not issubclass(FetchTimeout, FetchCancelled)


# -- cancellation token --------------------------------------------------------


def test_token_cancelled_before_call_makes_no_request(server):
    token = CancelToken()
    token.cancel()
    f = Fetcher()
    with pytest.raises(FetchCancelled):
        f.get(f"http://127.0.0.1:{server}/hello.txt", token=token)
    with pytest.raises(FetchCancelled):
        f.exists(f"http://127.0.0.1:{server}/hello.txt", token=token)
    assert HITS == {}
    # the token belongs to the call: other calls are not affected
    assert f.get(f"http://127.0.0.1:{server}/hello.txt") == b"hello world"


def test_token_cancel_interrupts_backoff(server):
    token = CancelToken()
    f = Fetcher(retries=3, retry_delay=30)
    threading.Timer(0.2, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(FetchCancelled):
        f.get(f"http://127.0.0.1:{server}/503", token=token)
    assert time.monotonic() - started < 5
    assert HITS["/503"] == 1


def test_token_cancel_between_chunks(server):
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(FetchCancelled):
        Fetcher(retries=0).get(f"http://127.0.0.1:{server}/trickle", token=token)
    assert time.monotonic() - started < 1.5  # the whole body takes 2 s


def test_instance_cancel_still_reaches_token_calls(server):
    f = Fetcher(retries=3, retry_delay=30)
    threading.Timer(0.2, f.cancel).start()
    started = time.monotonic()
    with pytest.raises(FetchCancelled):
        f.get(f"http://127.0.0.1:{server}/503", token=CancelToken())
    assert time.monotonic() - started < 5


def test_token_with_injected_sleep():
    token = CancelToken()
    f, rec = _recording(sleep=lambda d: token.cancel())
    f.retries = 2
    with pytest.raises(FetchCancelled):
        f.get("http://h/x", token=token)
    assert rec.urls == ["http://h/x"]


# -- deadline ------------------------------------------------------------------


def test_deadline_already_past_makes_no_request(server):
    f = Fetcher()
    past = time.monotonic() - 1
    with pytest.raises(FetchTimeout):
        f.get(f"http://127.0.0.1:{server}/hello.txt", deadline=past)
    with pytest.raises(FetchTimeout):
        f.exists(f"http://127.0.0.1:{server}/hello.txt", deadline=past)
    assert HITS == {}


def test_deadline_expires_during_backoff(server):
    f = Fetcher(retries=3, retry_delay=30)
    started = time.monotonic()
    with pytest.raises(FetchTimeout) as ei:
        f.get(f"http://127.0.0.1:{server}/503", deadline=started + 0.3)
    assert time.monotonic() - started < 5
    assert HITS["/503"] == 1
    assert not ei.value.retryable


def test_deadline_between_chunks(server):
    started = time.monotonic()
    with pytest.raises(FetchTimeout):
        Fetcher(retries=0).get(f"http://127.0.0.1:{server}/trickle", deadline=started + 0.3)
    assert time.monotonic() - started < 1.5


def test_deadline_caps_socket_timeout(server):
    started = time.monotonic()
    with pytest.raises(FetchTimeout):
        Fetcher(timeout=30, retries=2).get(
            f"http://127.0.0.1:{server}/hang", deadline=started + 0.3
        )
    assert time.monotonic() - started < 5


def test_deadline_in_future_succeeds(server, tmp_path):
    f = Fetcher()
    soon = time.monotonic() + 30
    assert f.get(f"http://127.0.0.1:{server}/hello.txt", deadline=soon) == b"hello world"
    assert f.exists(f"http://127.0.0.1:{server}/hello.txt", deadline=soon, token=CancelToken())
    (tmp_path / "f").write_bytes(b"data")
    assert f.get((tmp_path / "f").as_uri(), deadline=soon) == b"data"


# -- allowed_hosts -------------------------------------------------------------


def test_allowed_hosts_rejects_other_host(server):
    f = Fetcher(allowed_hosts=["localhost"])
    for call in (f.get, f.exists):
        with pytest.raises(HostNotAllowed, match="127.0.0.1"):
            call(f"http://127.0.0.1:{server}/hello.txt")
    assert HITS == {}
    ok = Fetcher(allowed_hosts=["127.0.0.1"])
    assert ok.get(f"http://127.0.0.1:{server}/hello.txt") == b"hello world"
    assert ok.exists(f"http://127.0.0.1:{server}/hello.txt")


def test_allowed_hosts_refuses_redirect_to_other_host(server):
    f = Fetcher(allowed_hosts=["127.0.0.1"], retries=0)
    target = f"http://localhost:{server}/hello.txt"
    for call in (f.get, f.exists):
        with pytest.raises(HostNotAllowed, match="localhost"):
            call(f"http://127.0.0.1:{server}/redir?{target}")
    # a redirect to an allowed host is followed
    same = f"http://127.0.0.1:{server}/hello.txt"
    assert f.get(f"http://127.0.0.1:{server}/redir?{same}") == b"hello world"


def test_allowed_hosts_case_insensitive_and_wildcard():
    f, rec = _recording(allowed_hosts=["Mirror.Example.ORG", "*.debian.org"])
    for url in (
        "http://mirror.example.org/x",
        "https://MIRROR.example.org:8443/x",
        "http://deb.debian.org/x",
        "http://a.b.debian.org/x",
    ):
        with pytest.raises(FetchError, match="no network"):
            f.get(url)
    assert len(rec.urls) == 4
    for url in (
        "http://debian.org/x",  # the wildcard needs a sub-domain
        "http://evildebian.org/x",
        "http://debian.org.evil.net/x",
        "http://example.org/x",
        "http://other.mirror.example.org/x",
    ):
        with pytest.raises(HostNotAllowed):
            f.get(url)
        with pytest.raises(HostNotAllowed):
            f.exists(url)
    assert len(rec.urls) == 4


def test_allowed_hosts_ignores_file_urls(tmp_path):
    (tmp_path / "f").write_bytes(b"data")
    f = Fetcher(allowed_hosts=["example.org"])
    assert f.get((tmp_path / "f").as_uri()) == b"data"
    assert f.exists((tmp_path / "f").as_uri())
    # the other file URL rules still apply
    with pytest.raises(FetchError, match=r"\.\."):
        f.get((tmp_path / "f").as_uri() + "/../f")


def test_allowed_hosts_keeps_other_rules():
    f, rec = _recording(allowed_hosts=["example.org"])
    with pytest.raises(FetchError, match="credentials"):
        f.get("http://u:p@example.org/x")
    with pytest.raises(FetchError, match="scheme"):
        f.get("ftp://example.org/x")
    assert rec.urls == []


def test_empty_allowed_hosts_refuses_every_host():
    f, rec = _recording(allowed_hosts=[])
    with pytest.raises(HostNotAllowed):
        f.get("http://example.org/x")
    assert rec.urls == []


# -- link-local and metadata hosts ---------------------------------------------

_BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.0.1:8080/x",
    "http://[fe80::1]/x",
    "http://[FE80::abcd%25eth0]/x",
    "http://[febf::1]/x",
    "http://[::ffff:169.254.169.254]/x",
    "http://2852039166/x",  # 169.254.169.254 as one number
    "http://0xa9.0xfe.0xa9.0xfe/x",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://METADATA.Google.Internal./x",
    "http://metadata/x",
    "https://instance-data/x",
    "http://instance-data./x",
]


@pytest.mark.parametrize("url", _BLOCKED)
def test_link_local_and_metadata_refused_without_connection(url):
    f, rec = _recording()
    with pytest.raises(HostNotAllowed):
        f.get(url)
    with pytest.raises(HostNotAllowed):
        f.exists(url)
    assert rec.urls == []


@pytest.mark.parametrize("url", _BLOCKED[:3])
def test_block_link_local_can_be_disabled(url):
    f, rec = _recording(block_link_local=False)
    with pytest.raises(FetchError, match="no network"):
        f.get(url)
    assert len(rec.urls) == 1


def test_link_local_check_applies_with_allowed_hosts():
    f, rec = _recording(allowed_hosts=["169.254.169.254", "metadata"])
    for url in ("http://169.254.169.254/x", "http://metadata/x"):
        with pytest.raises(HostNotAllowed):
            f.get(url)
    assert rec.urls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://[::1]/x",
        "http://[64:ff9b::a01:101]/x",  # NAT64 of 10.1.1.1
        "http://[fd00::1]/x",
        "http://localhost/x",
        "http://metadata.example.org/x",
        "http://my-metadata/x",
    ],
)
def test_private_and_loopback_hosts_allowed(url):
    f, rec = _recording()
    with pytest.raises(FetchError, match="no network"):
        f.get(url)
    assert rec.urls == [url]


def test_loopback_server_allowed_by_default(server):
    assert Fetcher().get(f"http://127.0.0.1:{server}/hello.txt") == b"hello world"


@pytest.mark.parametrize(
    "target", ["http://169.254.169.254/latest/", "http://[fe80::1]/x", "http://metadata/x"]
)
def test_redirect_to_link_local_refused(server, target):
    f = Fetcher(retries=0, timeout=5)
    started = time.monotonic()
    with pytest.raises(HostNotAllowed):
        f.get(f"http://127.0.0.1:{server}/redir?{target}")
    assert time.monotonic() - started < 3  # refused, not connected


def test_redirect_handler_policy_direct():
    import email.message
    import io
    import urllib.request

    from minideblib.repo.fetch import _SafeRedirectHandler

    f = Fetcher(allowed_hosts=["a.example"])
    handler = _SafeRedirectHandler(f._check_host)
    headers = email.message.Message()
    with pytest.raises(HostNotAllowed) as ei:
        handler.redirect_request(
            urllib.request.Request("https://a.example/x"),
            io.BytesIO(),
            302,
            "Found",
            headers,  # ty: ignore[invalid-argument-type]  # Message suffices here
            "https://b.example/x",
        )
    assert ei.value.status == 302


# -- connect-time check of resolved addresses ----------------------------------


def _addrinfo(ip: str, port: int) -> Any:
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))


@pytest.fixture
def resolve(monkeypatch):
    """``table[name] = [ip, ...]`` makes *name* resolve to those addresses."""
    table: dict[str, list[str]] = {}
    calls: list[str] = []
    real = socket.getaddrinfo

    def fake(host: Any, port: Any, *a: Any, **kw: Any) -> Any:
        if host in table:
            calls.append(host)
            return [_addrinfo(ip, port) for ip in table[host]]
        return real(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table, calls


_BAD_RESOLUTIONS = [
    ["169.254.169.254"],
    ["127.0.0.1", "169.254.1.1"],  # one bad entry is enough
    ["fe80::1"],
    ["::ffff:169.254.169.254"],
    ["fd00:ec2::254"],
    ["100.100.100.200"],
    ["::169.254.169.254"],  # IPv4-compatible
    ["64:ff9b::a9fe:a9fe"],  # NAT64
]


@pytest.mark.parametrize("ips", _BAD_RESOLUTIONS)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_name_resolving_to_link_local_refused(server, resolve, ips, scheme):
    table, calls = resolve
    table["evil.test"] = ips
    f = Fetcher(retries=2, sleep=lambda d: None)
    url = f"{scheme}://evil.test:{server}/hello.txt"
    with pytest.raises(HostNotAllowed, match="evil.test"):
        f.get(url)
    assert calls == ["evil.test"]  # not retried
    with pytest.raises(HostNotAllowed):
        f.exists(url)
    assert HITS == {}


def test_redirect_to_name_resolving_to_link_local_refused(server, resolve):
    table, _ = resolve
    table["evil.test"] = ["169.254.169.254"]
    f = Fetcher(retries=0)
    with pytest.raises(HostNotAllowed):
        f.get(f"http://127.0.0.1:{server}/redir?http://evil.test:{server}/hello.txt")


def test_safe_resolution_connects_to_the_vetted_address(server, resolve):
    table, calls = resolve
    table["good.test"] = ["127.0.0.1"]
    f = Fetcher(retries=0)
    assert f.get(f"http://good.test:{server}/hello.txt") == b"hello world"
    assert calls == ["good.test"]  # resolved once, by the check
    # https gets as far as the TLS handshake (the server speaks plain http)
    with pytest.raises(FetchError) as ei:
        f.get(f"https://good.test:{server}/hello.txt")
    assert not isinstance(ei.value, HostNotAllowed)


def test_connect_check_off_with_block_link_local_false():
    plain = getattr(Fetcher(block_link_local=False)._opener, "handlers")  # noqa: B009
    guarded = getattr(Fetcher()._opener, "handlers")  # noqa: B009
    assert {type(h) for h in plain} != {type(h) for h in guarded}
    assert any(type(h) is urllib.request.HTTPHandler for h in plain)
    assert not any(type(h) is urllib.request.HTTPHandler for h in guarded)


def test_proxy_resolves_names_and_literal_urls_still_refused(server, resolve, monkeypatch):
    table, calls = resolve
    table["evil.test"] = ["169.254.169.254"]
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{server}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    f = Fetcher(retries=0)
    # the proxy resolves the name: the connect-time check sees the proxy only
    assert f.get("http://evil.test/x") == b"proxied"
    assert calls == []
    with pytest.raises(HostNotAllowed):
        f.get("http://169.254.169.254/latest/")
    with pytest.raises(HostNotAllowed):
        f.get("http://metadata/x")


# -- host name normalisation and the extra metadata addresses -------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://\uff11\uff16\uff19.\uff12\uff15\uff14.\uff11\uff16\uff19.\uff12\uff15\uff14/x",
        "http://169\u3002254\u3002169\u3002254/x",
        "http://\uff4d\uff45\uff54\uff41\uff44\uff41\uff54\uff41/x",
        "http://metadata\uff0egoogle\uff0einternal/x",
        "http://[fd00:ec2::254]/x",
        "http://100.100.100.200/x",
        "http://[::169.254.169.254]/x",
        "http://[64:ff9b::a9fe:a9fe]/x",
    ],
)
def test_unicode_and_extra_metadata_hosts_refused(url):
    f, rec = _recording()
    with pytest.raises(HostNotAllowed):
        f.get(url)
    assert rec.urls == []


def test_non_idna_host_refused():
    f, rec = _recording()
    with pytest.raises(HostNotAllowed):
        f.get("http://" + "a" * 64 + ".example/x")
    assert rec.urls == []


def test_unicode_allowed_host_matches_its_idna_form():
    f, rec = _recording(allowed_hosts=["b\u00fccher.example"])
    with pytest.raises(FetchError, match="no network"):
        f.get("http://xn--bcher-kva.example/x")
    with pytest.raises(FetchError, match="no network"):
        f.get("http://B\u00dcCHER.example/x")
    assert len(rec.urls) == 2


# -- allowed_hosts validation ---------------------------------------------------


def test_allowed_hosts_bare_string_rejected():
    with pytest.raises(TypeError, match="allowed_hosts"):
        Fetcher(allowed_hosts="example.org")


@pytest.mark.parametrize(
    "entry", ["example.org:80", "example.org/path", "http://example.org", "[::1]:80", ""]
)
def test_allowed_hosts_bad_entries_rejected(entry):
    with pytest.raises(ValueError, match="allowed_hosts"):
        Fetcher(allowed_hosts=[entry])


def test_allowed_hosts_ipv6_literals_accepted():
    f, rec = _recording(allowed_hosts=["::1", "[fd00::1]"])
    for url in ("http://[::1]/x", "http://[fd00::1]/x"):
        with pytest.raises(FetchError, match="no network"):
            f.get(url)
    assert len(rec.urls) == 2


# -- nested scopes ---------------------------------------------------------------


def test_scope_token_reaches_plain_calls(server):
    f = Fetcher()
    token = CancelToken()
    token.cancel()
    with f.scope(token=token):
        with pytest.raises(FetchCancelled):
            f.get(f"http://127.0.0.1:{server}/hello.txt")
        # an inner token does not replace the outer one
        with pytest.raises(FetchCancelled):
            f.exists(f"http://127.0.0.1:{server}/hello.txt", token=CancelToken())
    assert HITS == {}
    assert f.get(f"http://127.0.0.1:{server}/hello.txt") == b"hello world"  # scope left


def test_scope_deadlines_combine_to_the_earliest(server):
    f = Fetcher()
    now = time.monotonic()
    with f.scope(deadline=now - 1), pytest.raises(FetchTimeout):
        f.get(f"http://127.0.0.1:{server}/hello.txt", deadline=now + 60)
    with f.scope(deadline=now + 60):
        with pytest.raises(FetchTimeout):
            f.get(f"http://127.0.0.1:{server}/hello.txt", deadline=now - 1)
        with f.scope(deadline=now + 30), f.scope(deadline=now + 90):
            assert f._current_call().deadline == now + 30
        with f.scope():
            assert f._current_call().deadline == now + 60
    assert f._current_call().deadline is None
    assert HITS == {}


def test_probe_workers_honour_the_outer_scope(server):
    from minideblib.repo.client import RepoClient

    f = Fetcher(timeout=30, retries=0)
    client = RepoClient.from_url(f"http://127.0.0.1:{server}/hangrepo", fetcher=f)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with f.scope(token=token), pytest.raises(FetchCancelled):
        client.suites()
    assert time.monotonic() - started < 3


def test_probe_workers_inherit_scope_before_their_requests(server):
    from minideblib.repo.client import RepoClient

    f = Fetcher(retries=0)
    client = RepoClient.from_url(f"http://127.0.0.1:{server}/repo", fetcher=f)
    token = CancelToken()
    token.cancel()
    with f.scope(token=token), pytest.raises(FetchCancelled):
        client.suites()
    assert HITS == {}  # no probe request was made


# -- deadline during a blocked body read ----------------------------------------


def test_deadline_bounds_a_stalled_body_read(server):
    started = time.monotonic()
    with pytest.raises(FetchTimeout):
        Fetcher(timeout=30, retries=0).get(
            f"http://127.0.0.1:{server}/stall", deadline=started + 1.0
        )
    # the read after the last piece (at 0.9 s) must not wait the full 1 s
    assert time.monotonic() - started < 1.6


# -- probe runs do not cancel other requests of a shared Fetcher -----------------


def test_probe_timeout_does_not_cancel_other_threads(server):
    from minideblib.repo.client import RepoClient

    f = Fetcher(timeout=30, retries=3, retry_delay=0.3)
    victim: list[object] = []

    def other_request() -> None:
        try:
            victim.append(f.get(f"http://127.0.0.1:{server}/503then200-b"))
        except BaseException as e:
            victim.append(e)

    b = threading.Thread(target=other_request)
    b.start()
    client = RepoClient.from_url(f"http://127.0.0.1:{server}/hangrepo", fetcher=f)
    with f.scope(deadline=time.monotonic() + 0.3), pytest.raises(FetchTimeout):
        client.suites()
    b.join(10)
    assert victim == [b"ok"]  # its retries went on and finished
    assert HITS["/503then200-b"] == 3


def test_fetcher_cancel_still_stops_a_probe_run(server):
    from minideblib.repo.client import RepoClient

    f = Fetcher(timeout=30, retries=0)
    client = RepoClient.from_url(f"http://127.0.0.1:{server}/hangrepo", fetcher=f)
    threading.Timer(0.3, f.cancel).start()
    started = time.monotonic()
    with pytest.raises(FetchCancelled):
        client.suites()  # the probes hang in a read
    assert time.monotonic() - started < 3


# -- post-connect backstop --------------------------------------------------------


@pytest.fixture
def tcp_sink(monkeypatch):
    """A TCP server on 127.0.0.1 that counts the bytes it receives."""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    received: list[int] = []
    accepted = threading.Event()

    def serve() -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        accepted.set()
        conn.settimeout(2)
        total = 0
        with conn:
            try:
                while chunk := conn.recv(4096):
                    total += len(chunk)
            except OSError:
                pass
        received.append(total)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1], received, t, accepted
    srv.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_peer_check_refuses_when_the_connect_hook_is_bypassed(
    tcp_sink, resolve, monkeypatch, scheme
):
    import http.client
    import ipaddress

    port, received, t, accepted = tcp_sink
    table, _ = resolve
    table["sink.test"] = ["127.0.0.1"]  # a name: the URL text check passes

    def bypassing_connect(self: Any) -> None:  # as if http.client changed
        self.sock = socket.create_connection((self.host, self.port), self.timeout)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", bypassing_connect)
    real = fetch._is_blocked_addr
    loopback = ipaddress.ip_address("127.0.0.1")
    monkeypatch.setattr(fetch, "_is_blocked_addr", lambda a: a == loopback or real(a))
    f = Fetcher(retries=2, sleep=lambda d: None)
    with pytest.raises(HostNotAllowed, match="127.0.0.1"):
        f.get(f"{scheme}://sink.test:{port}/x")
    assert accepted.wait(2)  # it did connect (the hook was bypassed) ...
    t.join(5)
    assert received == [0]  # ... but sent nothing, not even a TLS ClientHello
