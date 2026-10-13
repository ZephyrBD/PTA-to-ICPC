import json
from pathlib import Path

import pytest

from pta2ccs.config import load_manual, load_settings


def make_cfg(tmp_path: Path, **overrides) -> Path:
    data = {
        "contest_id": "1234567890",
        "pta_cookie": "cookie-value",
        "manual_dir": "manual",
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
    assert s.contest_id == "1234567890"
    assert s.pta_cookie == "cookie-value"
    assert s.port == 9001
    assert s.manual_dir == Path("manual")


def test_load_settings_missing_key_fails(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"contest_id": "1"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_settings(p)


def test_load_settings_missing_file_fails(tmp_path: Path):
    with pytest.raises(SystemExit):
        load_settings(tmp_path / "nope.json")


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
    with pytest.raises(SystemExit):
        load_manual(mdir)
