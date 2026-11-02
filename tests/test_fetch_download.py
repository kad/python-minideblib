# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Fetcher.download: streaming, limits, hashes, retries and cancellation."""

import errno
import hashlib
import http.server
import io
import os
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from minideblib.checksums import ChecksumError
from minideblib.repo.fetch import (
    CancelToken,
    DownloadResult,
    FetchCancelled,
    Fetcher,
    FetchError,
    FetchTimeout,
    HostNotAllowed,
)
from minideblib.repo.release import HashMismatch

HITS: dict[str, int] = {}
RELEASE = threading.Event()
BIG = bytes(range(256)) * (3 * 4096 + 7)  # about 3 MB


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _head(self, status: int, length: int | None) -> None:
        self.send_response(status)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self) -> None:
        p = self.path
        HITS[p] = HITS.get(p, 0) + 1
        if p.startswith("/nolen-"):
            # no Content-Length: the body ends when the connection closes
            self._head(200, None)
            self.wfile.write(b"z" * int(p.rsplit("-", 1)[1]))
            self.close_connection = True
        elif p == "/hugelen":
            self._head(200, 10**9)
            self.close_connection = True
        elif p.startswith("/503then200"):
            if HITS[p] == 1:
                self._head(503, 0)
            else:
                self._head(200, 2)
                self.wfile.write(b"ok")
        elif p == "/reset":
            # promises 100000 bytes, sends 1000, then drops the connection
            self._head(200, 100000)
            self.wfile.write(b"r" * 1000)
            self.wfile.flush()
            self.close_connection = True
        elif p == "/stall":
            self._head(200, 1000)
            self.wfile.write(b"s" * 10)
            self.wfile.flush()
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
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "big.bin").write_bytes(BIG)
    (tmp_path / "hello.txt").write_bytes(b"hello world")

    def handler(*a: Any, **kw: Any) -> Handler:
        return Handler(*a, directory=str(tmp_path), **kw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    RELEASE.clear()
    HITS.clear()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    RELEASE.set()
    srv.shutdown()
    srv.server_close()


class Spy(io.BytesIO):
    """Records the size of every write; can cancel a token on the first one."""

    def __init__(self, token: CancelToken | None = None) -> None:
        super().__init__()
        self.sizes: list[int] = []
        self.token = token

    def write(self, data: Any, /) -> int:
        self.sizes.append(len(data))
        if self.token is not None:
            self.token.cancel()
        return super().write(data)


def _fetcher(**kw: Any) -> Fetcher:
    kw.setdefault("timeout", 5)
    kw.setdefault("retry_delay", 0)
    return Fetcher(**kw)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_download_streams_exact_bytes_and_digests(server: str) -> None:
    out = Spy()
    expected = {"sha512": hashlib.sha512(BIG).hexdigest(), "sha256": sha256(BIG)}
    res = _fetcher().download(
        f"{server}/big.bin",
        out,
        max_bytes=len(BIG),
        expected_size=len(BIG),
        expected_hashes=expected,
    )
    assert isinstance(res, DownloadResult)
    assert out.getvalue() == BIG
    assert res.size == len(BIG)
    assert res.digests == expected
    # streamed in many pieces, none of them the whole body
    assert len(out.sizes) > 10
    assert max(out.sizes) <= 256 * 1024


def test_download_sha256_always_reported_and_case_insensitive(server: str) -> None:
    out = io.BytesIO()
    md5 = hashlib.md5(b"hello world").hexdigest()
    res = _fetcher().download(
        f"{server}/hello.txt", out, max_bytes=100, expected_hashes={"md5": md5.upper()}
    )
    assert res.digests == {"md5": md5, "sha256": sha256(b"hello world")}
    res = _fetcher().download(f"{server}/hello.txt", io.BytesIO(), max_bytes=100)
    assert res.digests == {"sha256": sha256(b"hello world")}


def test_download_max_bytes_via_content_length_writes_nothing(server: str) -> None:
    out = Spy()
    with pytest.raises(FetchError, match="exceeds"):
        _fetcher().download(f"{server}/hugelen", out, max_bytes=1000)
    assert out.sizes == []
    assert HITS["/hugelen"] == 1  # not retried


def test_download_max_bytes_while_streaming(server: str) -> None:
    out = io.BytesIO()
    with pytest.raises(FetchError, match="exceeds 5000 bytes"):
        _fetcher().download(f"{server}/nolen-200000", out, max_bytes=5000)
    assert len(out.getvalue()) <= 5000
    assert HITS["/nolen-200000"] == 1


def test_download_without_content_length_ok(server: str) -> None:
    out = io.BytesIO()
    res = _fetcher().download(f"{server}/nolen-70000", out, max_bytes=70000)
    assert res.size == 70000 and out.getvalue() == b"z" * 70000


def test_download_expected_size_mismatch(server: str) -> None:
    out = Spy()
    # announced by Content-Length: refused before anything is written
    with pytest.raises(HashMismatch, match="size"):
        _fetcher().download(f"{server}/hello.txt", out, max_bytes=100, expected_size=5)
    assert out.sizes == []
    # no Content-Length: found while streaming (too long, then too short)
    with pytest.raises(HashMismatch, match="size"):
        _fetcher().download(f"{server}/nolen-3000", io.BytesIO(), max_bytes=10**6, expected_size=10)
    with pytest.raises(HashMismatch, match="size"):
        _fetcher().download(
            f"{server}/nolen-3000", io.BytesIO(), max_bytes=10**6, expected_size=4000
        )


def test_expected_size_over_max_bytes_fails_without_request(server: str) -> None:
    with pytest.raises(FetchError, match="exceeds"):
        _fetcher().download(f"{server}/hello.txt", io.BytesIO(), max_bytes=5, expected_size=11)
    assert HITS == {}


def test_download_hash_mismatch_after_writing(server: str) -> None:
    out = io.BytesIO()
    with pytest.raises(HashMismatch, match="sha256"):
        _fetcher().download(
            f"{server}/hello.txt", out, max_bytes=100, expected_hashes={"sha256": "0" * 64}
        )
    # the bytes were written; discarding them is up to the caller
    assert out.getvalue() == b"hello world"
    assert HITS["/hello.txt"] == 1  # a mismatch is not retried


def test_download_unknown_algorithm_refused_before_request(server: str) -> None:
    with pytest.raises(ChecksumError, match="crc32"):
        _fetcher().download(
            f"{server}/hello.txt", io.BytesIO(), max_bytes=100, expected_hashes={"crc32": "00"}
        )
    assert HITS == {}


def test_download_bad_max_bytes() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        Fetcher().download("file:///nonexistent", io.BytesIO(), max_bytes=-1)


def test_download_cancel_mid_stream(server: str) -> None:
    token = CancelToken()
    out = Spy(token)  # cancels on the first write
    with pytest.raises(FetchCancelled):
        _fetcher().download(f"{server}/big.bin", out, max_bytes=len(BIG), token=token)
    assert len(out.sizes) == 1


def test_download_cancel_through_scope(server: str) -> None:
    token = CancelToken()
    token.cancel()
    f = _fetcher()
    out = Spy()
    with f.scope(token=token), pytest.raises(FetchCancelled):
        f.download(f"{server}/big.bin", out, max_bytes=len(BIG))
    assert out.sizes == []


def test_download_deadline(server: str) -> None:
    out = io.BytesIO()
    started = time.monotonic()
    with pytest.raises(FetchTimeout):
        _fetcher(timeout=30).download(
            f"{server}/stall", out, max_bytes=10**6, deadline=time.monotonic() + 0.5
        )
    assert time.monotonic() - started < 5
    assert out.getvalue() == b"s" * 10


def test_download_retries_before_first_byte(server: str) -> None:
    out = io.BytesIO()
    res = _fetcher(retries=2).download(f"{server}/503then200", out, max_bytes=100)
    assert out.getvalue() == b"ok" and res.size == 2
    assert HITS["/503then200"] == 2


def test_download_no_retry_after_bytes_were_written(server: str) -> None:
    out = io.BytesIO()
    with pytest.raises(FetchError, match="truncated"):
        _fetcher(retries=3).download(f"{server}/reset", out, max_bytes=10**6)
    assert HITS["/reset"] == 1
    assert out.getvalue() == b"r" * 1000


def test_download_404_writes_nothing(server: str) -> None:
    out = Spy()
    with pytest.raises(FetchError) as info:
        _fetcher().download(f"{server}/missing", out, max_bytes=100)
    assert info.value.status == 404
    assert out.sizes == []


def test_download_redirect_host_policy(server: str) -> None:
    port = server.rsplit(":", 1)[1]
    out = Spy()
    f = _fetcher(allowed_hosts=["127.0.0.1"])
    with pytest.raises(HostNotAllowed):
        f.download(f"{server}/redir?http://localhost:{port}/hello.txt", out, max_bytes=100)
    assert out.sizes == []
    res = f.download(f"{server}/redir?{server}/hello.txt", out, max_bytes=100)
    assert res.size == 11 and out.getvalue() == b"hello world"


def test_download_refused_host_before_request(server: str) -> None:
    with pytest.raises(HostNotAllowed):
        _fetcher(allowed_hosts=["example.org"]).download(
            f"{server}/hello.txt", io.BytesIO(), max_bytes=100
        )
    assert HITS == {}


def test_download_file_url(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(BIG)
    out = Spy()
    res = Fetcher().download(
        path.as_uri(), out, max_bytes=len(BIG), expected_hashes={"sha256": sha256(BIG)}
    )
    assert out.getvalue() == BIG and res.size == len(BIG)
    assert len(out.sizes) > 10
    with pytest.raises(FetchError, match="exceeds"):
        Fetcher().download(path.as_uri(), io.BytesIO(), max_bytes=100)
    with pytest.raises(FetchError):
        Fetcher().download((tmp_path / "missing").as_uri(), io.BytesIO(), max_bytes=100)


def test_download_to_real_file(tmp_path: Path, server: str) -> None:
    target = tmp_path / "out.bin"
    with target.open("wb") as fh:
        res = _fetcher().download(f"{server}/big.bin", fh, max_bytes=len(BIG))
    assert target.read_bytes() == BIG
    assert res.digests["sha256"] == sha256(BIG)


class FailingWriter(io.BytesIO):
    def write(self, data: Any, /) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


class NoneWriter:
    """An ad-hoc writer that returns None and wants bytes."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, data: bytes) -> None:
        assert isinstance(data, bytes)
        self.parts.append(data)


def test_download_write_error_is_fetch_error(server: str) -> None:
    with pytest.raises(FetchError, match="cannot write") as info:
        _fetcher(retries=3).download(f"{server}/hello.txt", FailingWriter(), max_bytes=100)
    assert not info.value.transient and not info.value.retryable
    assert isinstance(info.value.__cause__, OSError)
    assert HITS["/hello.txt"] == 1


def test_download_writer_returning_none(server: str) -> None:
    out = NoneWriter()
    res = _fetcher().download(f"{server}/big.bin", cast(Any, out), max_bytes=len(BIG))
    assert b"".join(out.parts) == BIG and res.size == len(BIG)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_file_url_fifo_refused_at_once(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    f = Fetcher()
    started = time.monotonic()
    deadline = time.monotonic() + 0.5
    for call in (
        lambda: f.download(fifo.as_uri(), io.BytesIO(), max_bytes=10, deadline=deadline),
        lambda: f.get(fifo.as_uri(), deadline=deadline),
        lambda: f.exists(fifo.as_uri(), deadline=deadline),
    ):
        with pytest.raises(FetchError, match="not a regular file"):
            call()
    assert time.monotonic() - started < 0.5
    # a directory is not a regular file either; a symlink to a file is followed
    with pytest.raises(FetchError, match="not a regular file"):
        f.get(tmp_path.as_uri())
    (tmp_path / "real").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert f.get((tmp_path / "link").as_uri()) == b"x"
