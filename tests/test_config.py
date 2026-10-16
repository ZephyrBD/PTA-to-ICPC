import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pta2icpc.config import Settings, load_connection, load_manual, load_settings


def make_cfg(tmp_path: Path, **overrides) -> Path:
    data = {
        "poll_interval_seconds": 5.0,
        "config_poll_seconds": 60.0,
        "host": "127.0.0.1",
        "port": 8000,
    }
    data.update(overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_settings_reads_all_fields(tmp_path: Path):
    s = load_settings(make_cfg(tmp_path, port=9001))
    assert s.port == 9001


def test_load_settings_invalid_field_fails(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"poll_interval_seconds": 0}), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        load_settings(p)
    assert excinfo.value.code == 1


# --- rate-limit settings (task 14) ----------------------------------------

def test_rate_limit_settings_defaults():
    s = Settings()
    assert s.pta_requests_per_second == 2.0
    assert s.detail_fetch_per_round == 5
    assert s.fetch_details is True


def test_rate_limit_settings_from_file(tmp_path: Path):
    s = load_settings(make_cfg(
        tmp_path, pta_requests_per_second=5.0, detail_fetch_per_round=10,
        fetch_details=False))
    assert s.pta_requests_per_second == 5.0
    assert s.detail_fetch_per_round == 10
    assert s.fetch_details is False


@pytest.mark.parametrize("bad", [{"pta_requests_per_second": 0},
                                 {"pta_requests_per_second": -1},
                                 {"detail_fetch_per_round": -1}])
def test_rate_limit_settings_invalid_rejected(bad: dict):
    with pytest.raises(ValidationError):
        Settings(**bad)


# --- cache / jitter settings (task 28) ------------------------------------

def test_cache_jitter_settings_defaults():
    s = Settings()
    assert s.cache_enabled is True
    assert s.cache_ttl_seconds == 3600.0
    assert s.pta_request_jitter == 0.25


def test_cache_jitter_settings_from_file(tmp_path: Path):
    s = load_settings(make_cfg(
        tmp_path, cache_enabled=False, cache_ttl_seconds=120.0,
        pta_request_jitter=0.0))
    assert s.cache_enabled is False
    assert s.cache_ttl_seconds == 120.0
    assert s.pta_request_jitter == 0.0


@pytest.mark.parametrize("bad", [{"cache_ttl_seconds": 0},
                                 {"cache_ttl_seconds": -1},
                                 {"pta_request_jitter": -0.1},
                                 {"pta_request_jitter": 1.5}])
def test_cache_jitter_settings_invalid_rejected(bad: dict):
    with pytest.raises(ValidationError):
        Settings(**bad)


def test_cache_jitter_settings_bounds_are_inclusive():
    assert Settings(pta_request_jitter=0).pta_request_jitter == 0
    assert Settings(pta_request_jitter=1).pta_request_jitter == 1
    assert Settings(cache_ttl_seconds=0.001).cache_ttl_seconds == 0.001


def test_load_settings_missing_file_returns_defaults(tmp_path: Path):
    assert load_settings(tmp_path / "nope.json") == Settings()


def test_load_settings_empty_json_returns_defaults(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    assert load_settings(p) == Settings()


# --- api_password (task 29) ------------------------------------------------

def test_api_password_defaults_empty():
    assert Settings().api_password == ""


def test_api_password_from_file(tmp_path: Path):
    s = load_settings(make_cfg(tmp_path, api_password="s3cret"))
    assert s.api_password == "s3cret"


# --- connection (manual/contest/setup-connection.json, task 17) --------------

def test_load_connection_returns_both_fields(tmp_path: Path):
    (tmp_path / "setup-connection.json").write_text(
        json.dumps({"contest_id": "1234567890", "pta_cookie": "cookie"}),
        encoding="utf-8")
    assert load_connection(tmp_path) == {
        "contest_id": "1234567890", "pta_cookie": "cookie"}


def test_load_connection_missing_file(tmp_path: Path):
    assert load_connection(tmp_path / "nope") is None


def test_load_connection_malformed_json(tmp_path: Path):
    (tmp_path / "setup-connection.json").write_text("{oops", encoding="utf-8")
    assert load_connection(tmp_path) is None


def test_load_connection_invalid_utf8(tmp_path: Path):
    # never raises: UnicodeDecodeError is a ValueError subclass (task 20 nit)
    (tmp_path / "setup-connection.json").write_bytes(b"\xff\xfe{broken")
    assert load_connection(tmp_path) is None


@pytest.mark.parametrize("payload", [
    {"contest_id": "", "pta_cookie": "cookie"},
    {"contest_id": "1234567890", "pta_cookie": ""},
    {"contest_id": "1234567890"},
    {"pta_cookie": "cookie"},
    {},
    ["contest_id", "pta_cookie"],
    "not-a-dict",
])
def test_load_connection_incomplete_fields(tmp_path: Path, payload):
    (tmp_path / "setup-connection.json").write_text(
        json.dumps(payload), encoding="utf-8")
    assert load_connection(tmp_path) is None


def test_load_connection_includes_print_server_when_set(tmp_path: Path):
    (tmp_path / "setup-connection.json").write_text(
        json.dumps({"contest_id": "1234567890", "pta_cookie": "cookie",
                    "print_server": "http://printer.local/print"}),
        encoding="utf-8")
    assert load_connection(tmp_path) == {
        "contest_id": "1234567890", "pta_cookie": "cookie",
        "print_server": "http://printer.local/print"}


@pytest.mark.parametrize("print_server", [None, "", 123])
def test_load_connection_omits_print_server_when_unset_or_invalid(
        tmp_path: Path, print_server):
    payload: dict = {"contest_id": "1234567890", "pta_cookie": "cookie"}
    if print_server is not None:
        payload["print_server"] = print_server
    (tmp_path / "setup-connection.json").write_text(
        json.dumps(payload), encoding="utf-8")
    assert load_connection(tmp_path) == {
        "contest_id": "1234567890", "pta_cookie": "cookie"}  # no key at all


def write_manual(dirpath: Path, name: str, data: dict) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / name).write_text(json.dumps(data), encoding="utf-8")


def test_load_manual_merges_by_id_later_wins(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "10-a.json", {
        "teams": {"D22": {"name": "old"}},
        "organizations": {"org1": {"name": "School"}},
        "groups": {"D": {"name": "Room D"}},
        "languages": {"cpp": {"name": "C++"}},
        "problems": {"p1": {"color": "red"}},
        "judgement_types": {"overrides": {"WA": {"penalty": True}}},
    })
    write_manual(mdir, "20-b.json", {
        "teams": {"D22": {"name": "new"}, "D23": {"name": "other"}},
        "teams_list_hint": None,
        "contest": {"start_time": "2026-05-21T09:00:00+08:00", "duration": "5:00:00"},
    })
    merged = load_manual(mdir)
    assert merged["teams"]["D22"]["name"] == "new"
    assert merged["teams"]["D23"]["name"] == "other"
    assert merged["organizations"]["org1"]["name"] == "School"
    assert merged["groups"]["D"]["name"] == "Room D"
    assert merged["languages"]["cpp"]["name"] == "C++"
    assert merged["problems"]["p1"]["color"] == "red"
    assert merged["judgement_types_overrides"]["WA"]["penalty"] is True
    assert merged["contest"]["duration"] == "5:00:00"


def test_load_manual_accepts_list_form(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "a.json", {
        "teams": [{"id": "D22", "name": "队"}, {"id": "D23", "name": "二队"}],
    })
    merged = load_manual(mdir)
    assert merged["teams"]["D22"]["name"] == "队"
    assert merged["teams"]["D23"]["name"] == "二队"


def test_load_manual_empty_dir_gives_defaults(tmp_path: Path):
    mdir = tmp_path / "manual"
    mdir.mkdir()
    merged = load_manual(mdir)
    assert merged["contest"] is None
    for key in ("teams", "organizations", "groups", "problems", "languages",
                "judgement_types_overrides"):
        assert merged[key] == {}


def test_load_manual_non_dict_json_fails(tmp_path: Path):
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "bad.json").write_text("[1,2]", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        load_manual(mdir)
    assert excinfo.value.code == 1


def test_load_manual_non_dict_id_value_fails_single_file(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "a.json", {"teams": {"D22": "oops"}})
    with pytest.raises(SystemExit) as excinfo:
        load_manual(mdir)
    assert excinfo.value.code == 1


def test_load_manual_non_dict_id_value_fails_on_merge(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "10-a.json", {"teams": {"D22": "oops"}})
    write_manual(mdir, "20-b.json", {"teams": {"D22": {"name": "x"}}})
    with pytest.raises(SystemExit) as excinfo:
        load_manual(mdir)
    assert excinfo.value.code == 1


def test_load_manual_null_overrides_fails(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "a.json", {"judgement_types": {"overrides": None}})
    with pytest.raises(SystemExit) as excinfo:
        load_manual(mdir)
    assert excinfo.value.code == 1


def test_load_manual_scalar_overrides_fails(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "a.json", {"judgement_types": {"overrides": "WA"}})
    with pytest.raises(SystemExit) as excinfo:
        load_manual(mdir)
    assert excinfo.value.code == 1


def test_load_manual_print_problem_surfaces_under_options(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "setup-problems.json", {"print_problem": "1-3"})
    assert load_manual(mdir)["options"] == {"print_problem": "1-3"}


def test_load_manual_print_problem_null_value(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "setup-problems.json", {"print_problem": None})
    assert load_manual(mdir)["options"] == {"print_problem": None}


def test_load_manual_options_default_empty(tmp_path: Path):
    mdir = tmp_path / "manual"
    write_manual(mdir, "a.json", {"teams": {"D22": {"name": "x"}}})
    assert load_manual(mdir)["options"] == {}


def test_load_manual_round_trips_seat_members_coaches(tmp_path: Path):
    # setup GUI's teams file carries seat/members/coaches on the team entry
    # (task: 扮演 DOMjudge) — load_manual must pass them through untouched
    mdir = tmp_path / "manual"
    write_manual(mdir, "setup-teams.json", {
        "teams": {"D22": {"group_ids": ["1"], "seat": "A12",
                          "members": ["甲", "乙"], "coaches": ["丙"]}}})
    merged = load_manual(mdir)
    assert merged["teams"]["D22"] == {
        "group_ids": ["1"], "seat": "A12",
        "members": ["甲", "乙"], "coaches": ["丙"]}
