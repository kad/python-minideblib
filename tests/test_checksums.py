# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Checksum block parsing and streaming file verification."""

import builtins
import errno
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from minideblib import checksums
from minideblib.checksums import (
    ALGORITHMS,
    ChecksumError,
    HashEntry,
    is_weak,
    parse_multiline_hashes,
    verify_file,
)
from minideblib.control import parse_paragraphs
from minideblib.errors import MinidebError

# -- parsing (moved from test_control.py) --------------------------------------


def test_multiline_hashes():
    value = (
        "\n e9fd0000000000000000000000000000 41913 unified/binary-i386/Packages\n abcd 5 other\n"
    )
    assert parse_multiline_hashes(value) == [
        ("e9fd0000000000000000000000000000", 41913, "unified/binary-i386/Packages"),
        ("abcd", 5, "other"),
    ]
    assert parse_multiline_hashes("") == []


def test_multiline_hashes_via_paragraph():
    p = next(parse_paragraphs("SHA256:\n aa 1 f1\n bb 2 f2\n"))
    assert parse_multiline_hashes(p["sha256"]) == [("aa", 1, "f1"), ("bb", 2, "f2")]


# -- parsing: contract ---------------------------------------------------------


def test_checksum_error_bases():
    assert issubclass(ChecksumError, MinidebError)
    assert issubclass(ChecksumError, ValueError)
    with pytest.raises(ChecksumError):
        parse_multiline_hashes(" onlyonefield\n")


def test_hash_entry_is_a_plain_tuple():
    (entry,) = parse_multiline_hashes(" aa 1 f1\n")
    assert isinstance(entry, HashEntry)
    assert entry == ("aa", 1, "f1")
    assert (entry.digest, entry.size, entry.name) == ("aa", 1, "f1")
    assert type(entry.size) is int


def test_spec_example():
    value = "\n 3f2a 12 main/Packages\r\n\t9c  0\tmain/Sources \n\n"
    assert parse_multiline_hashes(value) == [
        ("3f2a", 12, "main/Packages"),
        ("9c", 0, "main/Sources"),
    ]


def test_entry_on_first_line_and_blank_lines():
    assert parse_multiline_hashes("aa 1 f1\n\n   \n\t\n bb 2 f2") == [
        ("aa", 1, "f1"),
        ("bb", 2, "f2"),
    ]
    assert parse_multiline_hashes("\n \n\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "aa 1",  # name missing
        "onlyonefield",
        "aa 1 f1 extra",  # four columns
        "aa 1 f1 x y",
        "aa x f1",  # size not an integer
        "aa notasize f1",
    ],
)
def test_bad_lines_rejected_and_quoted(line):
    with pytest.raises(ChecksumError) as exc:
        parse_multiline_hashes(f"\n good 1 ok\n {line}\n")
    assert repr(line) in str(exc.value) or repr(f" {line}") in str(exc.value)


def test_all_or_nothing():
    with pytest.raises(ChecksumError):
        parse_multiline_hashes(" aa 1 f1\n bb 2 f2\n cc 3\n dd 4 f4\n")


@pytest.mark.parametrize("eol", ["\r", "\n", "\r\n"])
def test_line_ends(eol):
    value = eol.join(["", " aa 1 f1", " bb 2 f2", ""])
    assert parse_multiline_hashes(value) == [("aa", 1, "f1"), ("bb", 2, "f2")]


def test_mixed_line_ends_and_tabs():
    value = "\taa\t1\tf1\r bb \t 2\t\t f2\r\ncc 3 f3\n"
    assert parse_multiline_hashes(value) == [("aa", 1, "f1"), ("bb", 2, "f2"), ("cc", 3, "f3")]


@pytest.mark.parametrize("sep", ["\x85", "\u2028", "\x0b", "\x0c", "\x1c"])
def test_unicode_line_separators_do_not_end_a_line(sep):
    # not a line break: the two rows stay on one line, which has six columns
    with pytest.raises(ChecksumError):
        parse_multiline_hashes(f"\n aa 1 f1{sep}bb 2 f2\n")


@pytest.mark.parametrize("sep", ["\x0c", "\x0b", "\xa0", "\u2003", "\u3000", "\x85"])
def test_unicode_whitespace_separates_columns(sep):
    assert parse_multiline_hashes(f"\n aa{sep}1{sep}f1\n") == [("aa", 1, "f1")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", 0), ("007", 7), ("-1", -1), ("-00", 0), ("123456789012", 123456789012)],
)
def test_size_syntax_accepted(text, expected):
    assert parse_multiline_hashes(f" aa {text} f\n") == [("aa", expected, "f")]


@pytest.mark.parametrize(
    "text", ["+5", "1_000", "\u0661", "1\u0662", "\uff11", "-", "--1", "1-", "0x10", "1.0"]
)
def test_size_syntax_rejected(text):
    with pytest.raises(ChecksumError):
        parse_multiline_hashes(f" aa {text} f\n")


@pytest.mark.parametrize("digits", [5000, 4301, 21])
def test_oversized_size_is_a_checksum_error(digits):
    with pytest.raises(ChecksumError) as exc:
        parse_multiline_hashes(" aa " + "1" * digits + " f\n")
    assert len(str(exc.value)) < 200


def test_size_up_to_20_digits():
    assert parse_multiline_hashes(" aa " + "9" * 20 + " f\n") == [("aa", int("9" * 20), "f")]
    assert parse_multiline_hashes(" aa -" + "0" * 20 + " f\n") == [("aa", 0, "f")]


def test_long_line_quoted_briefly():
    with pytest.raises(ChecksumError) as exc:
        parse_multiline_hashes(" aa 1 " + "n" * 10000 + " extra\n")
    assert len(str(exc.value)) < 200


def test_digest_and_name_unchanged():
    value = " ABCdef 1 Weird.Name\n 0123 2 a\\b\n x 3 ../up\n"
    assert parse_multiline_hashes(value) == [
        ("ABCdef", 1, "Weird.Name"),
        ("0123", 2, "a\\b"),
        ("x", 3, "../up"),
    ]


def test_parse_100k_rows_fast():
    value = "".join(f"\n {i:064x} {i} main/binary-amd64/Packages{i}" for i in range(100_000))
    started = time.monotonic()
    rows = parse_multiline_hashes(value)
    assert time.monotonic() - started < 1.0
    assert len(rows) == 100_000
    assert rows[-1] == (f"{99_999:064x}", 99_999, "main/binary-amd64/Packages99999")


def test_parse_long_line_fast():
    name = "x" + ".orig-" * 12000 + "a"
    started = time.monotonic()
    assert parse_multiline_hashes(f" aa 1 {name}\n" * 20)[0] == ("aa", 1, name)
    with pytest.raises(ChecksumError):
        parse_multiline_hashes(f" aa {'1' * 70000} {name}\n")
    assert time.monotonic() - started < 1.0


# -- algorithm table -----------------------------------------------------------


def test_algorithm_table():
    rows = [(a.name, a.hex_length, a.release_field, a.sources_field, a.weak) for a in ALGORITHMS]
    assert rows == [
        ("sha512", 128, "SHA512", "Checksums-Sha512", False),
        ("sha256", 64, "SHA256", "Checksums-Sha256", False),
        ("sha1", 40, "SHA1", "Checksums-Sha1", True),
        ("md5", 32, "MD5Sum", "Files", True),
    ]
    assert isinstance(ALGORITHMS, tuple)
    with pytest.raises(AttributeError):
        ALGORITHMS[0].weak = True  # ty: ignore[invalid-assignment]
    for alg in ALGORITHMS:
        assert len(hashlib.new(alg.name, usedforsecurity=False).hexdigest()) == alg.hex_length


def test_is_weak():
    assert [is_weak(n) for n in ("sha512", "sha256", "sha1", "md5")] == [False, False, True, True]
    with pytest.raises(ChecksumError):
        is_weak("crc32")


def test_release_hash_algos_unchanged():
    from minideblib.repo.release import HASH_ALGOS

    assert list(HASH_ALGOS.items()) == [
        ("SHA512", "sha512"),
        ("SHA256", "sha256"),
        ("SHA1", "sha1"),
        ("MD5Sum", "md5"),
    ]


# -- verify_file ---------------------------------------------------------------

DATA = b"hello checksums\n" * 50


def _sums(data: bytes, *algs: str) -> dict[str, str]:
    names = algs or ("md5", "sha1", "sha256", "sha512")
    return {a: hashlib.new(a, data, usedforsecurity=False).hexdigest() for a in names}


@pytest.fixture
def datafile(tmp_path):
    p = tmp_path / "file.bin"
    p.write_bytes(DATA)
    return p


@pytest.fixture
def open_calls(monkeypatch):
    calls: list[str] = []
    real = builtins.open

    def spy(file, *a, **k):
        calls.append(str(file))
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    return calls


class Boom(MinidebError, ValueError):
    pass


def test_verify_ok_str_and_pathlike(datafile, open_calls):
    assert verify_file(str(datafile), len(DATA), _sums(DATA)) is None
    assert verify_file(datafile, len(DATA), _sums(DATA, "sha256"), Boom) is None
    assert open_calls == [str(datafile), str(datafile)]


def test_empty_file(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    verify_file(p, 0, _sums(b""))


@pytest.mark.parametrize("algs", [{}, {"crc32": "00"}, {"sha256": "0" * 64, "MD5": "0" * 32}])
def test_bad_algorithms_rejected_without_io(tmp_path, monkeypatch, algs):
    def no_io(*a, **k):
        raise AssertionError("file I/O attempted")

    monkeypatch.setattr(builtins, "open", no_io)
    monkeypatch.setattr(os, "lstat", no_io)
    monkeypatch.setattr(os, "stat", no_io)
    with pytest.raises(ChecksumError):
        verify_file(tmp_path / "missing", 0, algs)


def test_default_error_type(tmp_path):
    with pytest.raises(ChecksumError) as exc:
        verify_file(tmp_path / "missing", 1, {"md5": "0" * 32})
    assert type(exc.value) is ChecksumError
    assert "missing" in str(exc.value)


def test_missing_file_message_and_cause(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(Boom) as exc:
        verify_file(path, 1, {"md5": "0" * 32}, Boom)
    assert str(path) in str(exc.value)
    assert os.strerror(errno.ENOENT) in str(exc.value)
    assert isinstance(exc.value.__cause__, FileNotFoundError)


def test_symlink_to_good_file_rejected(datafile, tmp_path, open_calls):
    link = tmp_path / "link"
    link.symlink_to(datafile)
    with pytest.raises(Boom, match="regular") as exc:
        verify_file(link, len(DATA), _sums(DATA), Boom)
    assert str(link) in str(exc.value)
    assert open_calls == []


def test_directory_rejected(tmp_path, open_calls):
    with pytest.raises(Boom, match="regular") as exc:
        verify_file(tmp_path, 0, {"md5": "0" * 32}, Boom)
    assert str(tmp_path) in str(exc.value)
    assert open_calls == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_fifo_rejected(tmp_path, open_calls):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(Boom, match="regular"):
        verify_file(fifo, 0, {"md5": "0" * 32}, Boom)
    assert open_calls == []


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="no /dev/null")
def test_device_rejected(open_calls):
    with pytest.raises(Boom, match="regular"):
        verify_file("/dev/null", 0, _sums(b""), Boom)
    assert open_calls == []


@pytest.mark.parametrize("delta", [-1, 1])
def test_size_mismatch_before_open(datafile, open_calls, delta):
    with pytest.raises(Boom, match="size") as exc:
        verify_file(datafile, len(DATA) + delta, _sums(DATA), Boom)
    assert str(datafile) in str(exc.value)
    assert open_calls == []


@pytest.mark.parametrize(
    ("alg", "label"),
    [("md5", "md5sum"), ("sha1", "sha1"), ("sha256", "sha256"), ("sha512", "sha512")],
)
def test_hash_mismatch_label(datafile, alg, label):
    algs = _sums(DATA)
    algs[alg] = _sums(b"other", alg)[alg]
    with pytest.raises(Boom) as exc:
        verify_file(datafile, len(DATA), algs, Boom)
    assert str(datafile) in str(exc.value)
    assert label in str(exc.value)
    assert "\n" not in str(exc.value)


def test_hash_comparison_is_case_sensitive(datafile):
    algs = {"sha256": _sums(DATA, "sha256")["sha256"].upper()}
    with pytest.raises(Boom, match="sha256"):
        verify_file(datafile, len(DATA), algs, Boom)


def test_first_mismatch_in_algs_order(datafile):
    wrong = _sums(b"other")
    with pytest.raises(Boom) as exc:
        verify_file(datafile, len(DATA), {"sha256": wrong["sha256"], "md5": wrong["md5"]}, Boom)
    assert "sha256" in str(exc.value) and "md5sum" not in str(exc.value)
    with pytest.raises(Boom) as exc:
        verify_file(datafile, len(DATA), {"md5": wrong["md5"], "sha256": wrong["sha256"]}, Boom)
    assert "md5sum" in str(exc.value) and "sha256" not in str(exc.value)


def test_every_listed_algorithm_checked(datafile):
    algs = _sums(DATA)
    algs["sha1"] = "0" * 40
    with pytest.raises(Boom, match="sha1"):
        verify_file(datafile, len(DATA), algs, Boom)


def test_open_error_becomes_error_with_cause(datafile, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(datafile))

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(Boom) as exc:
        verify_file(datafile, len(DATA), _sums(DATA), Boom)
    assert str(datafile) in str(exc.value)
    assert isinstance(exc.value.__cause__, PermissionError)
    assert "\n" not in str(exc.value)


class _FailingReader:
    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()

    def fileno(self):
        return self._real.fileno()

    def read(self, *a):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    def readinto(self, *a):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    def close(self):
        self._real.close()


def test_read_error_becomes_error_with_cause(datafile, monkeypatch):
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: _FailingReader(real(*a, **k)))
    with pytest.raises(Boom) as exc:
        verify_file(datafile, len(DATA), _sums(DATA), Boom)
    assert str(datafile) in str(exc.value)
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == errno.EIO


class _RecordingReader:
    def __init__(self, real, sizes):
        self._real = real
        self._sizes = sizes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()

    def fileno(self):
        return self._real.fileno()

    def read(self, n=-1):
        self._sizes.append(n)
        return self._real.read(n)

    def readinto(self, buf):
        self._sizes.append(len(buf))
        return self._real.readinto(buf)

    def close(self):
        self._real.close()


def test_streams_in_bounded_chunks(tmp_path, monkeypatch):
    data = os.urandom(5 * 65536 + 123)
    p = tmp_path / "big"
    p.write_bytes(data)
    sizes: list[int] = []
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: _RecordingReader(real(*a, **k), sizes))
    verify_file(p, len(data), _sums(data))
    assert len(sizes) > 5
    assert all(0 < n <= 65536 for n in sizes)


def test_hash_objects_not_for_security(datafile, monkeypatch):
    expected = _sums(DATA)
    seen: list[tuple[str, object]] = []
    real = hashlib.new

    def spy(name, *a, **k):
        seen.append((name, k.get("usedforsecurity")))
        return real(name, *a, **k)

    monkeypatch.setattr(hashlib, "new", spy)
    verify_file(datafile, len(DATA), expected)
    assert sorted(n for n, _ in seen) == ["md5", "sha1", "sha256", "sha512"]
    assert all(flag is False for _, flag in seen)


# -- TOCTOU hardening: the path changes between the lookup and the open -------


def _swap_before_open(monkeypatch, path: Path, swap):
    real = builtins.open
    calls: list[str] = []

    def spy(file, *a, **k):
        calls.append(str(file))
        if str(file) == str(path):
            swap()
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    return calls


def test_symlink_swapped_in_after_lookup(datafile, tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_bytes(DATA)

    def swap():
        datafile.unlink()
        datafile.symlink_to(target)

    calls = _swap_before_open(monkeypatch, datafile, swap)
    with pytest.raises(Boom, match="regular") as exc:
        verify_file(datafile, len(DATA), _sums(DATA), Boom)
    assert str(datafile) in str(exc.value)
    assert calls == [str(datafile)]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_fifo_swapped_in_after_lookup_does_not_block(datafile, monkeypatch):
    def swap():
        datafile.unlink()
        os.mkfifo(datafile)

    _swap_before_open(monkeypatch, datafile, swap)
    outcome: list[BaseException | None] = []

    def run():
        try:
            verify_file(datafile, len(DATA), _sums(DATA), Boom)
            outcome.append(None)
        except BaseException as exc:  # reported to the main thread
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        # unblock the opener so the thread can finish, then fail
        fd = os.open(datafile, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        pytest.fail("verify_file blocked on a FIFO")
    assert len(outcome) == 1 and isinstance(outcome[0], Boom)
    assert "regular" in str(outcome[0])


@pytest.mark.parametrize("extra", [b"!", b"more data"])
def test_file_grown_after_lookup(datafile, monkeypatch, extra):
    def swap():
        datafile.write_bytes(DATA + extra)

    _swap_before_open(monkeypatch, datafile, swap)
    with pytest.raises(Boom, match="size"):
        verify_file(datafile, len(DATA), _sums(DATA), Boom)


def test_file_replaced_by_directory_after_lookup(datafile, monkeypatch):
    def swap():
        datafile.unlink()
        datafile.mkdir()

    _swap_before_open(monkeypatch, datafile, swap)
    with pytest.raises(Boom, match="regular"):
        verify_file(datafile, len(DATA), _sums(DATA), Boom)


def test_module_exports():
    assert set(checksums.__all__) == {
        "ALGORITHMS",
        "Algorithm",
        "ChecksumError",
        "HashEntry",
        "is_weak",
        "parse_multiline_hashes",
        "verify_file",
    }


class _ProbingReader(_RecordingReader):
    """Records whether the descriptor is in blocking mode at each read."""

    def __init__(self, real, blocking):
        super().__init__(real, [])
        self._blocking = blocking

    def read(self, n=-1):
        self._blocking.append(os.get_blocking(self._real.fileno()))
        return self._real.read(n)


def test_reads_in_blocking_mode(datafile, monkeypatch):
    blocking: list[bool] = []
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: _ProbingReader(real(*a, **k), blocking))
    verify_file(datafile, len(DATA), _sums(DATA))
    assert blocking and all(blocking)


class _GrowingReader(_RecordingReader):
    """Appends to the file after the first read."""

    def __init__(self, real, path):
        super().__init__(real, [])
        self._path = path
        self._reads = 0

    def read(self, n=-1):
        chunk = self._real.read(n)
        self._reads += 1
        if self._reads == 1:
            with self._path.open("ab") as out:
                out.write(b"appended while reading")
        return chunk


@pytest.mark.parametrize("length", [100, 3 * 65536 + 7])
def test_file_grown_during_read(tmp_path, monkeypatch, length):
    data = os.urandom(length)
    p = tmp_path / "growing"
    p.write_bytes(data)
    expected = _sums(data)
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: _GrowingReader(real(*a, **k), p))
    with pytest.raises(Boom, match="size changed while reading"):
        verify_file(p, length, expected, Boom)


def test_verify_file_progress_is_called_before_each_read_and_may_abort(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    sha = hashlib.sha256(b"abc").hexdigest()
    calls: list[int] = []
    verify_file(f, 3, {"sha256": sha}, progress=lambda: calls.append(1))
    assert calls

    class Stop(Exception):
        pass

    def stop() -> None:
        raise Stop

    with pytest.raises(Stop):
        verify_file(f, 3, {"sha256": sha}, progress=stop)
