# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import http.server
import threading
from typing import Any

import pytest

from minideblib.repo.fetch import Fetcher, FetchError

RELEASE = threading.Event()


def _fetcher(**kw):
    kw.setdefault("sleep", lambda s: None)  # never really sleep in tests
    return Fetcher(**kw)


HITS: dict[str, int] = {}


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_HEAD(self):
        if self.path.startswith("/nohead"):
            self.send_response(405)
            self.end_headers()
            return
        if self.path.startswith("/head-err"):
            self.send_response(500)
            self.end_headers()
            return
        super().do_HEAD()

    def do_GET(self):
        p = self.path
        HITS[p] = HITS.get(p, 0) + 1
        if p.startswith("/flaky-"):
            # /flaky-<status>-<failures>: fail that many times, then succeed
            _, status, failures = p.split("-")
            if HITS[p] <= int(failures):
                self.send_response(int(status))
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b"finally"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif p == "/trunc-once":
            if HITS[p] == 1:
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"abc")
                self.close_connection = True
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
        elif p == "/redir-ok":
            self.send_response(302)
            self.send_header("Location", "/hello.txt")
            self.end_headers()
        elif p == "/redir-ftp":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1/hello.txt")
            self.end_headers()
        elif p in ("/redir-creds", "/redir-creds-rel"):
            target = f"//jane:s3cret@{self.headers['Host']}/hello.txt"
            if p == "/redir-creds":
                target = "http:" + target
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
        elif p == "/redir-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
        elif p == "/slow":
            RELEASE.wait(10)
            self.close_connection = True
        elif p == "/trunc":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"abc")
            self.close_connection = True
        elif p == "/big":
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.close_connection = True
        elif p == "/chunk-trunc":
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"3\r\nabc\r\n")
            self.close_connection = True
        elif p == "/garbage":
            self.wfile.write(b"garbage\r\n\r\n")
            self.close_connection = True
        elif p == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif p == "/redir-rel":
            self.send_response(302)
            self.send_header("Location", "hello.txt")
            self.end_headers()
        elif p == "/redir-rel-bad":
            self.send_response(302)
            self.send_header("Location", "ftp:x")
            self.end_headers()
        elif p == "/gz":
            body = b"\x1f\x8b raw"
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.startswith("/nohead/"):
            self.path = p[len("/nohead") :]
            super().do_GET()
        else:
            super().do_GET()


@pytest.fixture
def server(tmp_path, monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "hello.txt").write_bytes(b"hello world")

    def handler(*a, **kw):
        return Handler(*a, directory=str(tmp_path), **kw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    RELEASE.clear()
    HITS.clear()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    RELEASE.set()
    srv.shutdown()
    srv.server_close()


def test_get_ok(server):
    base, _ = server
    assert _fetcher().get(base + "/hello.txt") == b"hello world"


def test_get_404(server):
    base, _ = server
    with pytest.raises(FetchError) as ei:
        _fetcher().get(base + "/missing")
    assert ei.value.status == 404
    assert ei.value.url == base + "/missing"
    assert ei.value.__cause__ is not None


def test_exists(server):
    base, _ = server
    f = _fetcher()
    assert f.exists(base + "/hello.txt") is True
    assert f.exists(base + "/missing") is False


def test_exists_head_not_allowed_falls_back(server):
    base, _ = server
    assert _fetcher().exists(base + "/nohead/hello.txt") is True
    assert _fetcher().exists(base + "/nohead/missing") is False


def test_exists_head_server_error_falls_back(server):
    base, _ = server
    assert _fetcher().exists(base + "/head-err") is False  # GET (404) decides


def test_redirect_http_ok(server):
    base, _ = server
    assert _fetcher().get(base + "/redir-ok") == b"hello world"


@pytest.mark.parametrize("path", ["/redir-ftp", "/redir-file"])
def test_redirect_to_other_scheme_refused(server, path):
    base, _ = server
    with pytest.raises(FetchError):
        _fetcher().get(base + path)


@pytest.mark.parametrize("path", ["/redir-creds", "/redir-creds-rel"])
def test_redirect_with_credentials_refused(server, path):
    base, _ = server
    with pytest.raises(FetchError) as ei:
        _fetcher(retries=0).get(base + path)
    assert "credentials" in str(ei.value)
    assert "s3cret" not in str(ei.value)
    assert "s3cret" not in str(ei.value.url)


def test_timeout(server):
    base, _ = server
    with pytest.raises(FetchError) as ei:
        _fetcher(timeout=0.3, retries=0).get(base + "/slow")
    assert ei.value.status is None


def test_max_bytes(server):
    base, _ = server
    with pytest.raises(FetchError):
        _fetcher(max_bytes=5).get(base + "/hello.txt")
    assert _fetcher(max_bytes=11).get(base + "/hello.txt") == b"hello world"


def test_content_encoding_not_decoded(server):
    base, _ = server
    assert _fetcher().get(base + "/gz") == b"\x1f\x8b raw"


def test_file_scheme(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"abc")
    f = _fetcher()
    assert f.get(p.as_uri()) == b"abc"
    assert f.exists(p.as_uri()) is True


def test_file_missing(tmp_path):
    u = (tmp_path / "nope").as_uri()
    with pytest.raises(FetchError) as ei:
        _fetcher().get(u)
    assert ei.value.status is None
    assert ei.value.__cause__ is not None
    assert _fetcher().exists(u) is False


@pytest.mark.parametrize("url", ["ftp://example.com/x", "gopher://x/", "/etc/passwd", "data:,x"])
def test_unsupported_scheme(url):
    f = _fetcher()
    with pytest.raises(FetchError):
        f.get(url)
    with pytest.raises(FetchError):
        f.exists(url)


@pytest.mark.parametrize("path", ["/trunc", "/chunk-trunc", "/garbage", "/loop", "/redir-rel-bad"])
def test_broken_responses(server, path):
    base, _ = server
    with pytest.raises(FetchError):
        _fetcher().get(base + path)


def test_content_length_over_max_fails_fast(server):
    base, _ = server
    with pytest.raises(FetchError, match="exceeds"):
        _fetcher(max_bytes=100).get(base + "/big")


def test_relative_redirect(server):
    base, _ = server
    assert _fetcher().get(base + "/redir-rel") == b"hello world"


@pytest.mark.parametrize("url", ["http://[bad/", "file://otherhost/etc/passwd"])
def test_malformed_or_remote_file_url(url):
    f = _fetcher()
    with pytest.raises(FetchError):
        f.get(url)
    with pytest.raises(FetchError):
        f.exists(url)


def test_issue4_retry_transient(server):
    base, _ = server
    sleeps: list[float] = []
    f = _fetcher(sleep=sleeps.append)
    assert f.get(base + "/flaky-503-2") == b"finally"
    assert sleeps == [0.5, 1.0]
    assert HITS["/flaky-503-2"] == 3


def test_issue4_no_retry_on_404_and_4xx(server):
    base, _ = server
    sleeps: list[float] = []
    f = _fetcher(sleep=sleeps.append)
    for path, status in (("/missing", 404), ("/flaky-410-5", 410), ("/flaky-403-5", 403)):
        with pytest.raises(FetchError) as ei:
            f.get(base + path)
        assert ei.value.status == status
        assert HITS[path] == 1
    assert sleeps == []


def test_issue4_retry_429(server):
    base, _ = server
    sleeps: list[float] = []
    assert _fetcher(sleep=sleeps.append).get(base + "/flaky-429-1") == b"finally"
    assert sleeps == [0.5]


def test_issue4_retries_exhausted_keeps_last_status(server):
    base, _ = server
    sleeps: list[float] = []
    with pytest.raises(FetchError) as ei:
        _fetcher(sleep=sleeps.append).get(base + "/flaky-503-99")
    assert ei.value.status == 503
    assert HITS["/flaky-503-99"] == 3
    assert sleeps == [0.5, 1.0]


def test_issue4_retries_zero_and_backoff_cap(server):
    base, _ = server
    sleeps: list[float] = []
    with pytest.raises(FetchError):
        _fetcher(retries=0, sleep=sleeps.append).get(base + "/flaky-503-99")
    assert sleeps == [] and HITS["/flaky-503-99"] == 1
    sleeps.clear()
    with pytest.raises(FetchError):
        _fetcher(retries=6, retry_delay=3, sleep=sleeps.append).get(base + "/flaky-500-99")
    assert sleeps == [3, 6, 10, 10, 10, 10]


def test_issue4_truncated_body_retried(server):
    base, _ = server
    sleeps: list[float] = []
    assert _fetcher(sleep=sleeps.append).get(base + "/trunc-once") == b"ok"
    assert sleeps == [0.5]


def test_issue4_connection_error_retried():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    sleeps: list[float] = []
    with pytest.raises(FetchError):
        _fetcher(sleep=sleeps.append).get(f"http://127.0.0.1:{port}/x")
    assert sleeps == [0.5, 1.0]


def test_issue4_timeout_retried(server):
    base, _ = server
    sleeps: list[float] = []
    with pytest.raises(FetchError):
        _fetcher(timeout=0.2, retries=1, sleep=sleeps.append).get(base + "/slow")
    assert sleeps == [0.5]


def test_issue4_no_retry_for_file_scheme_max_bytes_or_bad_url(server, tmp_path):
    base, _ = server
    sleeps: list[float] = []
    f = _fetcher(sleep=sleeps.append, max_bytes=5)
    for url in (
        (tmp_path / "nope").as_uri(),
        "ftp://example.com/x",
        "http://[bad/x",
        base + "/hello.txt",
    ):
        with pytest.raises(FetchError):
            f.get(url)
    assert sleeps == []


def test_issue4_exists_retries_network_errors(server):
    base, _ = server
    sleeps: list[float] = []
    f = _fetcher(sleep=sleeps.append)
    assert f.exists(base + "/missing") is False
    assert sleeps == []
    assert f.exists(base + "/hello.txt") is True


def test_issue4_ssl_error_not_retried(monkeypatch):
    import ssl
    import urllib.error

    f = _fetcher(sleep=lambda s: pytest.fail("slept"))
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))

    monkeypatch.setattr(f._opener, "open", boom)
    with pytest.raises(FetchError):
        f.get("https://example.invalid/x")
    assert len(calls) == 1


def test_invalid_url_not_retried(monkeypatch):
    f = _fetcher(sleep=lambda s: pytest.fail("slept"))
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise ValueError("bad")

    monkeypatch.setattr(f._opener, "open", boom)
    with pytest.raises(FetchError):
        f.get("http://example.invalid/x")
    assert len(calls) == 1


def test_exists_head_status_error_not_retried(server):
    base, _ = server
    sleeps: list[float] = []
    assert Fetcher(sleep=sleeps.append).exists(base + "/head-err") is False
    assert sleeps == []


def test_negative_retries_acts_like_zero(server):
    base, _ = server
    with pytest.raises(FetchError) as ei:
        _fetcher(retries=-1).get(base + "/flaky-503-99")
    assert ei.value.status == 503
    assert HITS["/flaky-503-99"] == 1


def test_https_to_http_redirect_refused():
    import email.message
    import io
    import urllib.request

    from minideblib.repo.fetch import _SafeRedirectHandler

    handler = _SafeRedirectHandler()
    req = urllib.request.Request("https://example.org/dists/x/InRelease")
    headers = email.message.Message()
    with pytest.raises(FetchError, match="https -> http") as ei:
        handler.redirect_request(
            req,
            io.BytesIO(),
            302,
            "Found",
            headers,  # ty: ignore[invalid-argument-type]  # Message suffices here
            "http://u:secret@example.org/dists/x/InRelease",
        )
    assert "secret" not in str(ei.value)
    assert ei.value.status == 302
    # https -> https and http -> http still allowed
    for src, dst in (("https://a/x", "https://b/x"), ("http://a/x", "http://b/x")):
        new = handler.redirect_request(
            urllib.request.Request(src),
            io.BytesIO(),
            302,
            "Found",
            headers,  # ty: ignore[invalid-argument-type]  # Message suffices here
            dst,
        )
        assert new is not None and new.full_url == dst


@pytest.mark.parametrize(
    "url", ["http://u:secret@example.org/x", "https://secret@example.org/x", "http://:secret@h/"]
)
def test_credentials_in_url_rejected(url):
    f = _fetcher()
    for call in (f.get, f.exists):
        with pytest.raises(FetchError, match="credentials in URLs are not supported") as ei:
            call(url)
        assert "secret" not in str(ei.value)


def test_file_url_with_encoded_dotdot_rejected(tmp_path):
    (tmp_path / "secret.txt").write_bytes(b"s")
    repo = tmp_path / "repo"
    repo.mkdir()
    f = _fetcher()
    for url in (
        repo.as_uri() + "/%2e%2e/secret.txt",
        repo.as_uri() + "/%2E%2E/secret.txt",
        repo.as_uri() + "/../secret.txt",
    ):
        with pytest.raises(FetchError, match=r"\.\."):
            f.get(url)
        with pytest.raises(FetchError, match=r"\.\."):
            f.exists(url)
    # "..." and names containing ".." are fine
    (repo / "a..b").write_bytes(b"ok")
    assert f.get(repo.as_uri() + "/a..b") == b"ok"


# -- cooperative cancellation -------------------------------------------------


class _Flaky(Fetcher):
    """Every attempt fails with a transient error; counts the attempts."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.attempts = 0

    def _get_once(self, url: str, attempt: int = 1) -> bytes:
        self.attempts += 1
        raise FetchError("connection reset", url, transient=True)


def test_cancel_interrupts_backoff_wait():
    import time

    f = _Flaky(retries=3, retry_delay=30)  # default sleep: waits on the cancel event
    threading.Timer(0.1, f.cancel).start()
    started = time.monotonic()
    with pytest.raises(FetchError, match="cancelled"):
        f.get("http://h/x")
    assert time.monotonic() - started < 5
    assert f.attempts == 1  # no attempt after the cancellation


def test_cancel_only_affects_requests_in_progress(tmp_path):
    (tmp_path / "x").write_bytes(b"data")
    f = _fetcher()
    f.cancel()  # nothing in progress: later requests are not affected
    assert f.get((tmp_path / "x").as_uri()) == b"data"


def test_cancel_checked_before_each_attempt():
    sleeps: list[float] = []
    f: _Flaky

    def sleep(delay: float) -> None:  # an injected sleep is still used
        sleeps.append(delay)
        f.cancel()

    f = _Flaky(retries=3, sleep=sleep)
    with pytest.raises(FetchError, match="cancelled"):
        f.get("http://h/x")
    assert (f.attempts, sleeps) == (1, [0.5])


def test_cancelled_error_type_is_public():
    from minideblib.repo import fetch

    assert "FetchCancelled" in fetch.__all__
    assert issubclass(fetch.FetchCancelled, FetchError)
    f = _Flaky(retries=1, sleep=lambda d: f.cancel())
    with pytest.raises(fetch.FetchCancelled):
        f.get("http://h/x")


def test_cancel_during_head_stops_get_fallback():
    from minideblib.repo.fetch import FetchCancelled

    class HeadRefused(Fetcher):
        def __init__(self) -> None:
            super().__init__(retries=0)
            self.methods: list[str] = []

        def _open(self, url: str, method: str, attempt: int = 1) -> Any:
            self.methods.append(method)
            if method == "HEAD":
                self.cancel()  # arrives while HEAD is in progress
                raise FetchError("HTTP 405", url, 405)
            raise AssertionError("GET must not start after cancel")

    f = HeadRefused()
    with pytest.raises(FetchCancelled):
        f.exists("http://h/x")
    assert f.methods == ["HEAD"]
