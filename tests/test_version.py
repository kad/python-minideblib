# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import pickle
import random
import shutil
import subprocess
from typing import Any, cast

import pytest

from minideblib.version import Version, VersionError, compare


@pytest.mark.parametrize(
    ("low", "high"),
    [
        ("1.0~rc1", "1.0"),
        ("1.0~~", "1.0~a"),
        ("1.0~", "1.0~a"),
        ("1.0~", "1.0"),
        ("0.9", "1:0.1"),
        ("2.0", "1:0.9"),
        ("1.0-1", "1.0-2"),
        ("1.0", "1.0+b1"),
        ("1.0", "1.0a"),
        ("1.0a", "1.0+"),
        ("a", "b"),
        ("1.9", "1.10"),
    ],
)
def test_ordering(low, high):
    assert compare(low, high) < 0
    assert compare(high, low) > 0
    assert Version(low) < Version(high)
    assert Version(high) > Version(low)
    assert Version(low) <= Version(high)


@pytest.mark.parametrize(
    ("a", "b"),
    [("1.0", "1.0-0"), ("1.0", "1.00"), ("1.0", "0:1.0"), ("1.0-1", "1.0-1")],
)
def test_equal(a, b):
    assert compare(a, b) == 0
    assert Version(a) == Version(b)
    assert hash(Version(a)) == hash(Version(b))


def test_components():
    v = Version("2:1.0-3")
    assert (v.epoch, v.upstream, v.revision) == (2, "1.0", "3")
    v = Version("1.0")
    assert (v.epoch, v.upstream, v.revision) == (0, "1.0", "")
    assert Version("1.0-2-3").upstream == "1.0-2"


def test_str():
    assert str(Version("2:1.0-3")) == "2:1.0-3"
    assert str(Version("1.0")) == "1.0"


@pytest.mark.parametrize("bad", ["", "x:1", "1:", ":1", "1.0-", "1.0-a_b", "1 0"])
def test_invalid(bad):
    with pytest.raises(VersionError):
        Version(bad)


def test_versionerror_is_valueerror():
    assert issubclass(VersionError, ValueError)


def test_compare_other_types():
    assert Version("1.0") != "1.0"


def _dpkg_lt_batch(pairs):
    """Return dpkg's verdict ('lt', 'eq', 'gt') for each pair, via dpkg."""
    out = []
    for a, b in pairs:
        if (
            subprocess.run(
                ["dpkg", "--compare-versions", a, "eq", b], capture_output=True
            ).returncode
            == 0
        ):
            out.append(0)
        elif (
            subprocess.run(
                ["dpkg", "--compare-versions", a, "lt", b], capture_output=True
            ).returncode
            == 0
        ):
            out.append(-1)
        else:
            out.append(1)
    return out


needs_dpkg = pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg not available")


def test_colon_in_upstream():
    v = Version("1:2:3")
    assert (v.epoch, v.upstream, v.revision) == (1, "2:3", "")
    assert str(v) == "1:2:3"


def test_zero_vs_zero_dash_zero():
    assert compare("0", "0-0") == 0
    assert Version("0") == Version("0-0")


def test_leading_zeros():
    assert compare("1.01", "1.1") == 0
    assert hash(Version("1.01")) == hash(Version("1.1"))
    assert compare("1.01", "1.2") < 0


def test_long_digit_runs():
    big = "9" * 40
    assert compare("1." + big, "1." + "9" * 39) > 0
    assert compare("1." + "0" * 40 + "5", "1.5") == 0
    assert compare(big + "1", big + "2") < 0


def test_non_ascii_rejected():
    for bad in ["1.0é", "١.0", "1:١", "1.0-١"]:
        with pytest.raises(VersionError):
            Version(bad)


def test_immutable():
    v = Version("1:2-3")
    for name in ("epoch", "upstream", "revision"):
        with pytest.raises(AttributeError):
            setattr(v, name, 5)
    with pytest.raises(AttributeError):
        v.extra = 1
    for name in ("epoch", "upstream", "revision", "_key"):
        with pytest.raises(AttributeError):
            delattr(v, name)
    import copy
    import pickle

    for clone in (copy.copy(v), copy.deepcopy(v), pickle.loads(pickle.dumps(v))):
        assert clone == v and hash(clone) == hash(v) and str(clone) == "1:2-3"
        assert isinstance(clone, Version)


def test_huge_digit_runs_compare_without_int_limit():
    big = "9" * 5000
    assert Version(big) > Version("9" * 4999)
    assert Version("1." + big) < Version("1." + "1" + "0" * 5000)
    assert Version("0" * 6000 + "1") == Version("1")
    assert hash(Version("0" * 6000 + "1")) == hash(Version("1"))
    assert compare(big + "-" + big, big + "-" + "0" + big) == 0


def test_leading_zeros_and_empty_digit_runs():
    assert Version("1.01") == Version("1.1")
    assert hash(Version("1.01")) == hash(Version("1.1"))
    assert Version("1.0") == Version("1.00")
    assert Version("1.0") > Version("1")  # "." sorts after the end of text
    assert Version("a") == Version("a0")
    assert Version("10") > Version("9") and Version("010") > Version("9")


def test_epoch_too_long_rejected():
    assert Version("9999999999:1").epoch == 9999999999
    for s in ("1" * 11 + ":1", "9" * 5000 + ":1"):
        with pytest.raises(VersionError, match="epoch"):
            Version(s)


def test_sorting_and_set():
    vs = [Version(s) for s in ["1.0", "1.0~a", "1:0", "1.0-0", "0.1"]]
    assert [str(v) for v in sorted(vs)] == ["0.1", "1.0~a", "1.0", "1.0-0", "1:0"]


@needs_dpkg
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1:2:3", "1:2:4"),
        ("0", "0-0"),
        ("1.01", "1.1"),
        ("9" * 40, "9" * 39),
        ("1.0~", "1.0"),
        ("1.0a", "1.0+"),
    ],
)
def test_dpkg_oracle_examples(a, b):
    assert compare(a, b) == _dpkg_lt_batch([(a, b)])[0]


@needs_dpkg
def test_random_against_dpkg():
    rng = random.Random(1234)
    alphabet = "0123456789.+~-:aAzZ"
    pairs = []
    while len(pairs) < 300:
        a, b = ("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8))) for _ in range(2))
        try:
            Version(a), Version(b)
        except VersionError:
            continue
        # dpkg wants a digit-first upstream to be fully valid; skip others
        if not (_dpkg_ok(a) and _dpkg_ok(b)):
            continue
        pairs.append((a, b))
    expected = _dpkg_lt_batch(pairs)
    for (a, b), want in zip(pairs, expected, strict=True):
        assert compare(a, b) == want, (a, b)


def _dpkg_ok(v):
    r = subprocess.run(["dpkg", "--validate-version", v], capture_output=True)
    return r.returncode == 0 and not r.stderr


_MEG = 1024 * 1024


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("x" * _MEG + ":1", "invalid epoch"),
        ("9" * _MEG + ":1", "epoch longer than"),
        ("1" + "!" * _MEG, "invalid upstream version"),
        ("1-" + "!" * _MEG, "invalid revision"),
    ],
)
def test_error_message_truncates_untrusted_text(text, what):
    with pytest.raises(VersionError, match=what) as ei:
        Version(text)
    msg = str(ei.value)
    assert len(msg) < 120
    assert msg.endswith(f"{text[:40]!r}...")


@pytest.mark.parametrize(
    ("text", "msg"),
    [
        ("a:1", "invalid epoch in version 'a:1'"),
        ("9" * 11 + ":1", "epoch longer than 10 digits in version '99999999999:1'"),
        ("a!b", "invalid upstream version in 'a!b'"),
        ("1-a!", "invalid revision in version '1-a!'"),
        ("1-" + "!" * 38, "invalid revision in version '1-" + "!" * 38 + "'"),  # 40: not cut
    ],
)
def test_error_message_short_text_unchanged(text, msg):
    with pytest.raises(VersionError) as ei:
        Version(text)
    assert str(ei.value) == msg


@pytest.mark.parametrize("s", ["a1", "1:a1-1", "~1", "+x", ".1"])
def test_strict_rejects_upstream_without_leading_digit(s):
    v = Version(s)  # permissive by default, like dpkg (which only warns)
    assert Version(s, strict=False) == v
    with pytest.raises(VersionError, match="digit"):
        Version(s, strict=True)


@pytest.mark.parametrize("s", ["1", "0a", "1:2.3-4", "9~rc1", "1.0+dfsg-1ubuntu2"])
def test_strict_accepts_digit_upstream(s):
    v = Version(s, strict=True)
    assert v == Version(s)
    assert hash(v) == hash(Version(s))
    assert str(v) == s
    assert pickle.loads(pickle.dumps(v)) == v


def test_strict_is_keyword_only():
    with pytest.raises(TypeError):
        cast(Any, Version)("1", True)


def test_compare_strict():
    assert compare("a1", "b1") == -1
    assert compare("1", "2", strict=True) == -1
    with pytest.raises(VersionError):
        compare("a1", "1", strict=True)
    with pytest.raises(VersionError):
        compare("1", "a1", strict=True)
