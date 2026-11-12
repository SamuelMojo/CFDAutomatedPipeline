"""Tests for cfd_common: option checks, DynamoDB helpers, zip inspection and alerts."""
import io
import math
import zipfile
from decimal import Decimal

import pytest

import cfd_common as cc
from conftest import STEP_TEXT, FakeClientError, FakeSNS, FakeTable, make_zip


def fetcher(data: bytes):
    """A ranged-download function over bytes, counting its calls."""
    def fetch(first, last):
        """Bytes first..last inclusive."""
        fetch.calls += 1
        return data[first:last + 1]
    fetch.calls = 0
    return fetch


# ---- options ---------------------------------------------------------------
def test_settings_defaults_and_rounding():
    """Missing keys fall back to safe defaults; the speed is rounded."""
    assert cc.validate_settings({}) == {"solver": "SA", "preset": "standard", "speed": 20.0, "ground": "moving"}
    assert cc.validate_settings({"speed": "31.23456"})["speed"] == 31.235


@pytest.mark.parametrize("bad", [{"solver": "FluidX3D"}, {"preset": "fine"}, {"ground": "sticky"}, {"speed": 3},
                                 {"speed": 500}, {"speed": "fast"}, {"speed": float("nan")}, "text", None])
def test_settings_refuse_bad_values(bad):
    """Unknown solvers, presets, grounds and silly speeds are all refused."""
    with pytest.raises(cc.ValidationError):
        cc.validate_settings(bad)


def test_file_entry_cleaned():
    """A good entry is cleaned; the label defaults to the file name without its extension."""
    f = cc.validate_file({"filename": " My  wing.step\n", "size": 1000, "units": "mm"}, 5000)
    assert f == {"filename": "My wing.step", "size": 1000, "units": "mm", "half_model": False, "label": "My wing"}


@pytest.mark.parametrize("bad", [{"filename": "", "size": 5, "units": "mm"},
                                 {"filename": "a.zip", "size": 0, "units": "mm"},
                                 {"filename": "a.zip", "size": True, "units": "mm"},
                                 {"filename": "a.zip", "size": 10**9, "units": "mm"},
                                 {"filename": "a.zip", "size": 5, "units": "furlong"},
                                 {"filename": "a.zip", "size": 5}, []])
def test_file_entry_refuses_bad_values(bad):
    """Empty, oversize, unit-less or malformed entries are refused."""
    with pytest.raises(cc.ValidationError):
        cc.validate_file(bad, 5000)


# ---- DynamoDB helpers ------------------------------------------------------
def test_ddb_conversion_round_trip():
    """Floats become Decimals going in, and whole Decimals become ints coming out."""
    stored = cc.to_ddb({"a": 1.5, "b": [2, {"c": 0.1}], "d": "x", "e": None, "f": True})
    assert isinstance(stored["a"], Decimal) and isinstance(stored["b"][1]["c"], Decimal)
    assert cc.from_ddb(stored) == {"a": 1.5, "b": [2, {"c": 0.1}], "d": "x", "e": None, "f": True}
    assert cc.from_ddb(Decimal("7")) == 7 and isinstance(cc.from_ddb(Decimal("7")), int)
    with pytest.raises(ValueError):
        cc.to_ddb(math.inf)


def test_update_fields_condition():
    """update_fields writes only while the guarded attribute has an allowed value."""
    table = FakeTable()
    table.put_item(Item={"job_id": "j", "status": "queued", "created_at": "t"})
    out = cc.update_fields(table, {"job_id": "j"}, {"status": "running", "progress": {"cells": 5, "x": 0.5}},
                           only_if=("status", ("queued",)))
    assert out["status"] == "running" and out["progress"] == {"cells": 5, "x": 0.5}
    with pytest.raises(FakeClientError) as err:
        cc.update_fields(table, {"job_id": "j"}, {"status": "queued"}, only_if=("status", ("queued",)))
    assert cc.is_conditional_failure(err.value)
    assert not cc.is_conditional_failure(ValueError("x"))


def test_fake_table_refuses_floats_like_dynamodb():
    """The stand-in rejects raw floats, so code that forgets to_ddb fails in tests, not in production."""
    with pytest.raises(TypeError):
        FakeTable().put_item(Item={"job_id": "j", "x": 1.5})


def test_query_index_orders_by_creation():
    """query_index returns one partition, oldest first or newest first."""
    table = FakeTable()
    for i, (user, when) in enumerate([("u", "2"), ("u", "1"), ("v", "3"), ("u", "3")]):
        table.put_item(Item={"job_id": f"j{i}", "user_sub": user, "created_at": when})
    assert [j["created_at"] for j in cc.query_index(table, "by_user", "user_sub", "u")] == ["1", "2", "3"]
    newest = cc.query_index(table, "by_user", "user_sub", "u", newest_first=True, limit=1)
    assert [j["job_id"] for j in newest] == ["j3"]


# ---- zip inspection ---------------------------------------------------------
def test_good_zip_is_accepted_by_reading_little():
    """A one-STEP zip passes, and a large one is checked with only a few ranged reads."""
    big = STEP_TEXT + b"#1=CARTESIAN_POINT('',(0.,0.,0.));\n" * 60000
    data = make_zip({"wing.step": big}, compress=False)
    fetch = fetcher(data)
    info = cc.inspect_step_zip(fetch, len(data), max_step_bytes=10**7)
    assert info["entry"] == "wing.step" and info["uncompressed"] == len(big)
    assert fetch.calls <= 6 and len(data) > 2_000_000


def test_mac_zip_junk_is_ignored():
    """Folders and the __MACOSX / ._ files that macOS adds do not count as extra files."""
    data = make_zip({"__MACOSX/._wing.step": b"x", "dir/wing.STP": STEP_TEXT, ".DS_Store": b"x"})
    assert cc.inspect_step_zip(fetcher(data), len(data), 10**6)["entry"] == "dir/wing.STP"


@pytest.mark.parametrize("entries, words", [
    ({}, "empty"),
    ({"a.step": STEP_TEXT, "b.step": STEP_TEXT}, "2 files"),
    ({"model.stl": STEP_TEXT}, "not a .step"),
    ({"a.step": b"hello world, not a step file"}, "ISO-10303-21"),
])
def test_zip_content_rules(entries, words):
    """Wrong contents are refused with a message that says why."""
    data = make_zip(entries)
    with pytest.raises(cc.ValidationError, match=words):
        cc.inspect_step_zip(fetcher(data), len(data), 10**6)


def test_not_a_zip_and_truncated_zip():
    """Garbage and a cut-off zip are refused, not crashed on."""
    for data in (b"this is plainly not a zip file at all" * 10, make_zip({"a.step": STEP_TEXT})[:-30]):
        with pytest.raises(cc.ValidationError):
            cc.inspect_step_zip(fetcher(data), len(data), 10**6)


def test_encrypted_oversize_and_bomb_zips_are_refused():
    """Password-protected, too-large and absurdly-compressed zips are refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.step", STEP_TEXT)
        zf.infolist()[0].flag_bits |= 0x1
    data = buf.getvalue()
    with pytest.raises(cc.ValidationError, match="password"):
        cc.inspect_step_zip(fetcher(data), len(data), 10**6)
    data = make_zip({"a.step": STEP_TEXT + b"0" * 50_000})
    with pytest.raises(cc.ValidationError, match="unpacked"):
        cc.inspect_step_zip(fetcher(data), len(data), 1000)
    bomb = make_zip({"a.step": STEP_TEXT + b"\0" * 30_000_000})
    with pytest.raises(cc.ValidationError, match="ratio"):
        cc.inspect_step_zip(fetcher(bomb), len(bomb), 10**9)


def test_range_file_gives_up_after_too_many_reads():
    """A hostile file cannot keep the reader fetching for ever."""
    rf = cc.RangeFile(lambda a, b: b"x" * (b - a + 1), size=10_000_000, max_fetches=3)
    with pytest.raises(ValueError):
        for i in range(10):
            rf.seek(i * 300_000)
            rf.read(10)


# ---- alerts ------------------------------------------------------------------
def test_alerts_never_raise_and_are_ascii():
    """A broken SNS must not break the caller; subjects are ASCII, prefixed and at most 100 characters."""
    assert cc.send_alert(FakeSNS(fail=True), "arn", "x", "y") is False
    assert cc.send_alert(FakeSNS(), "", "x", "y") is False
    sns = FakeSNS()
    assert cc.send_alert(sns, "arn", "Wing éè failed " + "z" * 300, "body")
    subject = sns.sent[0]["Subject"]
    assert subject.startswith("[CFD site] Wing") and subject.isascii() and len(subject) <= 100


def test_alert_once_limits_repeats():
    """The same alert key is sent once per window; a different key is sent straight away."""
    table, sns = FakeTable(), FakeSNS()
    assert cc.alert_once(table, sns, "arn", "k", "s", "t", min_seconds=600)
    assert not cc.alert_once(table, sns, "arn", "k", "s", "t", min_seconds=600)
    assert cc.alert_once(table, sns, "arn", "other", "s", "t", min_seconds=600)
    assert len(sns.sent) == 2
    table.items["_alert_k"]["last_at"] = "2000-01-01T00:00:00Z"
    assert cc.alert_once(table, sns, "arn", "k", "s", "t", min_seconds=600)


def test_seconds_since():
    """Time differences, with a bad stamp giving None."""
    assert cc.seconds_since("2026-11-12T10:00:00Z", now="2026-11-12T10:01:30Z") == 90
    assert cc.seconds_since(None) is None and cc.seconds_since("junk") is None
