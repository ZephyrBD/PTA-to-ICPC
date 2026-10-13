import datetime as dt

from pta2ccs.models import error_body, make_access, make_api_info, sanitize_id
from pta2ccs.timefmt import parse_pta_time, parse_reltime, to_iso_time, to_reltime


def test_parse_offset():
    from pta2ccs.timefmt import parse_offset
    assert parse_offset("+08:00") == dt.timezone(dt.timedelta(hours=8))
    assert parse_offset("-05:00") == dt.timezone(dt.timedelta(hours=-5))
    assert parse_offset("+0800") == dt.timezone(dt.timedelta(hours=8))
    assert parse_offset("+05:30") == dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_parse_pta_time():
    got = parse_pta_time("2023/05/07 17:59:08", "+08:00")
    assert got == dt.datetime(2023, 5, 7, 17, 59, 8,
                              tzinfo=dt.timezone(dt.timedelta(hours=8)))


def test_to_iso_time():
    d = parse_pta_time("2023/05/07 17:59:08", "+08:00")
    assert to_iso_time(d) == "2023-05-07T17:59:08+08:00"


def test_to_reltime():
    assert to_reltime(dt.timedelta(hours=5)) == "5:00:00"
    assert to_reltime(dt.timedelta(minutes=20)) == "0:20:00"
    assert to_reltime(dt.timedelta(hours=26, seconds=1)) == "26:00:01"
    assert to_reltime(dt.timedelta(0)) == "0:00:00"
    assert to_reltime(dt.timedelta(seconds=75)) == "0:01:15"


def test_parse_reltime():
    assert parse_reltime("0:20:00") == dt.timedelta(minutes=20)
    assert parse_reltime("5:00:00") == dt.timedelta(hours=5)
    assert parse_reltime("26:00:01") == dt.timedelta(hours=26, seconds=1)


def test_sanitize_id_keeps_valid():
    assert sanitize_id("D22") == "D22"
    assert sanitize_id("10-asteroids") == "10-asteroids"
    assert sanitize_id("a_b.c-1") == "a_b.c-1"


def test_sanitize_id_fixes_invalid():
    assert sanitize_id("a b!") == "a-b-"
    assert sanitize_id("--x") == "x"
    assert sanitize_id("ok.") == "ok"
    assert sanitize_id(".dot") == "dot"
    assert sanitize_id("x" * 50) == "x" * 36
    assert sanitize_id("中文") == "x"


def test_error_body():
    assert error_body(404, "not found") == {"code": 404, "message": "not found"}


def test_api_info_version():
    assert make_api_info()["version"] == "2026-01"


def test_access_lists_expected_endpoints():
    acc = make_access()
    types = {e["type"] for e in acc["endpoints"]}
    assert types == {
        "contests", "state", "groups", "organizations", "teams", "problems",
        "languages", "judgement-types", "submissions", "judgements", "scoreboard",
    }
    assert acc["capabilities"] == []
    for e in acc["endpoints"]:
        assert e["properties"], f"{e['type']} must list properties"
