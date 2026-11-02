# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Every public exception of the library derives from :class:`minideblib.MinidebError`."""

import importlib
import inspect
import pkgutil

import pytest

import minideblib
from minideblib import MinidebError
from minideblib.errors import MinidebError as Base

#: command line modules; their exceptions (cli.NotFound) are not library API
_NOT_LIBRARY = {"minideblib.cli", "minideblib.__main__"}


def _public_exceptions() -> list[type[BaseException]]:
    found: list[type[BaseException]] = []
    for info in pkgutil.walk_packages(minideblib.__path__, "minideblib."):
        if info.name in _NOT_LIBRARY:
            continue
        mod = importlib.import_module(info.name)
        for name, obj in vars(mod).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseException)
                and obj.__module__ == info.name
                and not name.startswith("_")
            ):
                found.append(obj)
    return found


def test_base_exported():
    assert MinidebError is Base
    assert minideblib.__all__ == ["MinidebError", "__version__"]
    assert issubclass(MinidebError, Exception)


def test_every_public_exception_is_a_minideb_error():
    excs = _public_exceptions()
    names = {e.__name__ for e in excs}
    assert {"FetchError", "HashMismatch", "DebError", "DebSizeLimitError", "VersionError"} <= names
    assert len(excs) >= 15
    bad = [e.__qualname__ for e in excs if not issubclass(e, MinidebError)]
    assert bad == []


def test_existing_bases_kept_and_instances_work():
    from minideblib.ar import ArError
    from minideblib.compress import DecompressionLimitError, MissingCodecError
    from minideblib.deb import DebSizeLimitError
    from minideblib.dsc import DscError
    from minideblib.repo.dep11 import Dep11Error, MissingYamlError
    from minideblib.repo.fetch import FetchError
    from minideblib.repo.release import HashMismatch, MissingIndexError, ReleaseError
    from minideblib.version import Version, VersionError

    for cls, base in (
        (ArError, ValueError),
        (DscError, ValueError),
        (Dep11Error, ValueError),
        (VersionError, ValueError),
        (MissingCodecError, ImportError),
        (MissingYamlError, ImportError),
        (MissingIndexError, KeyError),
        (HashMismatch, ReleaseError),
        (DebSizeLimitError, DecompressionLimitError),
    ):
        assert issubclass(cls, base) and issubclass(cls, MinidebError), cls
    e = DebSizeLimitError("too big")
    assert isinstance(e, ValueError) and str(e) == "too big"
    assert FetchError("x", "http://h/", 404).status == 404
    assert MissingCodecError("no zstd").msg == "no zstd"
    try:
        Version("1:")
    except MinidebError as exc:
        assert isinstance(exc, VersionError)
    else:  # pragma: no cover
        raise AssertionError("no error")


# -- malformed input through the public entry points ---------------------------

SUITE = "stable/slash-test/1.0"


def _is_data_error(exc: BaseException, *bases: type[BaseException]) -> None:
    assert isinstance(exc, MinidebError), type(exc)
    for base in bases:
        assert isinstance(exc, base), (type(exc), base)


def test_new_classes_have_value_error_base_and_are_exported():
    from minideblib import changelog, deb, sources
    from minideblib.repo import client

    for mod, name, base in (
        (changelog, "ChangelogError", ValueError),
        (sources, "SourcesError", ValueError),
        (client, "RepoError", ValueError),
        (deb, "MissingMemberError", KeyError),
    ):
        cls = getattr(mod, name)
        assert name in mod.__all__
        assert issubclass(cls, MinidebError) and issubclass(cls, base), cls
    assert issubclass(deb.MissingMemberError, deb.DebError)
    assert str(deb.MissingMemberError("no x")) == "no x"  # not KeyError's repr quoting


def test_packages_index_with_non_field_line(tmp_path):
    from minideblib.repo.client import RepoClient
    from tests.fixtures.repo import make_repo

    base = make_repo(tmp_path, packages="Package: a\nnot a field line\n", arches=["amd64"])
    client = RepoClient.from_url(base, suites=[SUITE])
    # a malformed index is recorded as a problem; the query goes on without it
    assert client.packages(SUITE) == []
    [problem] = client.problems
    assert "malformed index ignored: line 2:" in problem


def test_bad_release(tmp_path):
    from minideblib.repo.client import RepoClient
    from minideblib.repo.release import ReleaseError
    from tests.fixtures.repo import make_repo

    base = make_repo(tmp_path, arches=["amd64"], inrelease=False)
    (tmp_path / "dists" / SUITE / "Release").write_text("Origin: x\n continuation\nno colon\n")
    with pytest.raises(ReleaseError) as ei:
        RepoClient.from_url(base, suites=[SUITE]).components(SUITE)
    _is_data_error(ei.value)


def test_bad_changelog():
    from minideblib.changelog import ChangelogError, parse

    for text in (
        "garbage\n",
        "pkg (-1) unstable; urgency=low\n",  # invalid version
        "pkg (1.0) unstable; urgency=low\n\n  * x\n",
        "pkg (1.0) unstable; low\n",
        "pkg (1.0) unstable; foo=bar\n",
        "pkg (1.0) unstable; urgency=low\n\n  * x\n\n -- A <a@b>  not a date\n",
        "pkg (1.0) unstable; urgency=low\n\n  * x\nbad trailer\n",
    ):
        with pytest.raises(ChangelogError, match=r"^line \d+: ") as ei:
            parse(text)
        _is_data_error(ei.value, ValueError)


def test_bad_sources_file(tmp_path):
    from minideblib.sources import SourcesError, load

    bad = tmp_path / "a.sources"
    bad.write_text("Types: deb\nnocolon\n")
    with pytest.raises(SourcesError, match=r"a\.sources: line 2") as ei:
        load(bad)
    _is_data_error(ei.value, ValueError)
    binary = tmp_path / "b.list"
    binary.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(SourcesError, match=r"b\.list: not valid UTF-8") as ei:
        load(binary)
    _is_data_error(ei.value, ValueError)


def test_missing_deb_member(tmp_path):
    from minideblib.deb import DebError, DebPackage, MissingMemberError
    from tests.fixtures.debs import make_deb

    pkg = DebPackage(make_deb({"Package": "p"}, {"usr/x": b"x"}))
    with pytest.raises(MissingMemberError, match="nope") as ei:
        pkg.read("nope")
    _is_data_error(ei.value, DebError, KeyError)


def test_bad_dsc():
    from minideblib.dsc import Dsc, DscError

    for text in ("Source: x\nnot a field\n", "Source: x\n"):
        with pytest.raises(DscError) as ei:
            Dsc.parse(text)
        _is_data_error(ei.value, ValueError)


def test_repo_record_and_client_data_errors(tmp_path):
    from minideblib.control import Paragraph
    from minideblib.repo.client import PackageRecord, RepoClient, RepoError, SourceRecord
    from minideblib.sources import SourceEntry

    rec = PackageRecord(Paragraph([("Package", "p")]), "s", "main", "amd64", "http://h/")
    with pytest.raises(RepoError, match="no Filename") as ei:
        rec.url  # noqa: B018
    _is_data_error(ei.value, ValueError)
    unsafe = PackageRecord(Paragraph([("Filename", "../x")]), "s", "main", "amd64", "http://h/")
    with pytest.raises(RepoError, match="unsafe repository path"):
        unsafe.url  # noqa: B018
    src = SourceRecord(Paragraph([("Package", "p"), ("Version", "1")]), "s", "main")
    with pytest.raises(RepoError, match="no .dsc file listed") as ei:
        RepoClient([]).fetch_dsc(src)
    _is_data_error(ei.value, ValueError)
    client = RepoClient([SourceEntry("deb", "http://h/", "one", ("main",))])
    with pytest.raises(RepoError, match="not configured") as ei:
        client.packages("other")
    _is_data_error(ei.value, ValueError)


def test_hostile_deb_archives_raise_only_minideb_errors(tmp_path):
    from minideblib.deb import DebError
    from tests.test_deb import HOSTILE, _deb_with_data_tar, _hostile_tar

    for i, (kind, _) in enumerate(HOSTILE):
        pkg = _deb_with_data_tar(tmp_path, _hostile_tar(kind))
        dest = tmp_path / f"d{i}"
        dest.mkdir()
        with pytest.raises(DebError) as ei:
            pkg.extract(dest, skip_absolute_symlinks=True)
        _is_data_error(ei.value)
        pkg.files()
        with pytest.raises(DebError):
            pkg.read("hard")
