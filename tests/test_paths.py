import asyncio
import inspect
import json
from pathlib import Path

from pta2icpc import paths
from pta2icpc.balloons import BalloonState
from pta2icpc.config import Settings, load_manual, load_settings
from pta2icpc.poller import Poller
from pta2icpc.setup import (write_connection, write_contest_setup,
                           write_manual_setup)
from pta2icpc.translate import ContestData


# --- path constants ---------------------------------------------------------

def test_path_constants():
    assert paths.MANUAL_DIR == Path("manual")
    assert paths.CONFIG_DIR == Path("manual/config")
    assert paths.CONTEST_DIR == Path("manual/contest")
    assert paths.CONFIG_FILE == Path("manual/config/config.json")
    assert paths.CONNECTION_FILE == Path("manual/contest/setup-connection.json")
    assert paths.SETUP_CONTEST_FILE == Path(
        "manual/contest/setup-contest.json")
    assert paths.SETUP_PROBLEMS_FILE == Path(
        "manual/contest/setup-problems.json")
    assert paths.SETUP_GROUPS_FILE == Path("manual/contest/setup-groups.json")
    assert paths.SETUP_TEAMS_FILE == Path("manual/contest/setup-teams.json")
    assert paths.BALLOONS_FILE == Path("manual/balloons.json")
    assert paths.PRINT_SENT_FILE == Path("manual/print_sent.json")


def test_ensure_dirs_creates_three_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths.ensure_dirs()
    assert (tmp_path / "manual").is_dir()
    assert (tmp_path / "manual" / "config").is_dir()
    assert (tmp_path / "manual" / "contest").is_dir()


# --- Settings / load_settings ----------------------------------------------

def test_settings_has_no_manual_dir_field():
    assert "manual_dir" not in Settings.model_fields


def test_load_settings_ignores_residual_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "manual_dir": "manual",
        "contest_id": "1234567890",
        "pta_cookie": "cookie",
        "port": 9111,
    }), encoding="utf-8")
    s = load_settings(p)
    assert s.port == 9111
    assert s.host == "127.0.0.1"
    assert not hasattr(s, "manual_dir")


# --- load_manual uses manual/contest only ----------------------------------

def test_load_manual_uses_contest_dir_not_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths.ensure_dirs()
    (tmp_path / "manual" / "contest" / "setup-problems.json").write_text(
        json.dumps({"print_problem": "1-1"}), encoding="utf-8")
    (tmp_path / "manual" / "config" / "config.json").write_text(
        json.dumps({"print_problem": "2-2"}), encoding="utf-8")
    assert load_manual()["options"]["print_problem"] == "1-1"


def test_load_manual_missing_config_dir_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "manual" / "contest").mkdir(parents=True)
    merged = load_manual()
    assert merged["contest"] is None
    assert merged["teams"] == {}


# --- runtime state default paths -------------------------------------------

def _minimal_data() -> ContestData:
    data = ContestData()
    data.teams = {"t1": {"name": "甲", "label": "A1"}}
    data.problems = {"p1": {"id": "p1", "label": "A", "name": "题A"}}
    data.submissions = {
        "s1": {"id": "s1", "team_id": "t1", "problem_id": "p1",
               "contest_time": "0:10:00", "time": "2026-01-01T00:00:00+08:00"},
    }
    data.judgements = {"s1": {"judgement_type_id": "AC"}}
    data.judgement_types = {"AC": {"solved": True}}
    return data


def test_balloon_state_default_path_persists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths.ensure_dirs()
    state = BalloonState()
    assert state._path == paths.BALLOONS_FILE
    asyncio.run(state.sync(_minimal_data()))
    written = tmp_path / "manual" / "balloons.json"
    assert written.exists()
    raw = json.loads(written.read_text(encoding="utf-8"))
    assert raw["entries"]["1"]["team_id"] == "t1"


def test_poller_default_print_sent_path_is_constant():
    sig = inspect.signature(Poller.__init__)
    assert sig.parameters["print_sent_path"].default == paths.PRINT_SENT_FILE


def test_setup_writers_default_dir_creates_manual_contest(tmp_path,
                                                          monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_connection({"contest_id": "c1", "pta_cookie": "placeholder"})
    write_contest_setup({"duration": "1:00:00"})
    write_manual_setup({}, {}, {}, None)
    contest = tmp_path / "manual" / "contest"
    assert contest.is_dir()
    assert (contest / "setup-connection.json").exists()
    assert (contest / "setup-contest.json").exists()
    assert (contest / "setup-problems.json").exists()
    assert (contest / "setup-groups.json").exists()
    assert (contest / "setup-teams.json").exists()


def test_poller_persist_print_sent_creates_missing_dir(tmp_path):
    missing = tmp_path / "missing" / "print_sent.json"
    poller = Poller.__new__(Poller)
    poller._print_sent_path = missing
    poller._print_sent = {"s1"}
    poller._persist_print_sent()
    assert missing.exists()
    assert json.loads(missing.read_text(encoding="utf-8")) == {"sent": ["s1"]}
