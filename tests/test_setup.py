import asyncio
import json

import pytest

import pta2ccs.setup as setup
from pta2ccs.setup import (
    _residual_config_prefill,
    allocate_group_id,
    build_contest_setup,
    build_problem_rows,
    build_team_rows,
    build_teams_setup,
    fetch_setup_data,
    load_manual_setup,
    sort_team_rows,
    sort_teams_by_label,
    toggle_print_problem,
    validate_contest_times,
    write_connection,
    write_contest_setup,
    write_manual_setup,
)


def test_import_setup_does_not_require_display():
    # the module import itself must not instantiate Tk / need a display;
    # only launch_gui()/launch_connection_dialog()/main() touch tkinter
    # (imported lazily inside)
    assert hasattr(setup, "main")


def test_allocate_group_id_starts_at_g1():
    assert allocate_group_id({}) == "g1"


def test_allocate_group_id_uses_max_plus_one():
    assert allocate_group_id({"g1": {}, "g3": {}, "g2": {}}) == "g4"


def test_allocate_group_id_ignores_unmatched_ids():
    assert allocate_group_id({"g1": {}, "team-a": {}, "g2": {}}) == "g3"


def test_sort_teams_by_label_numeric_aware():
    rows = [{"label": "2023002"}, {"label": "2023001"}, {"label": "20230010"}]
    assert [r["label"] for r in sort_teams_by_label(rows)] == [
        "2023001", "2023002", "20230010"]


def test_sort_teams_by_label_falls_back_to_string():
    rows = [{"label": "张三"}, {"label": "2023002"}, {"label": "2023001"}]
    assert [r["label"] for r in sort_teams_by_label(rows)] == [
        "2023001", "2023002", "张三"]


def test_sort_teams_by_label_empty():
    assert sort_teams_by_label([]) == []


# ---------------------------------------------------------------------------
# sort_team_rows — header-click column sort (view-layer, pure)
# ---------------------------------------------------------------------------

def _rows():
    return [
        {"id": "a", "label": "2023002", "name": "乙", "organization_name": "B校",
         "group_names": ["正式"]},
        {"id": "b", "label": "2023001", "name": "甲", "organization_name": "A校",
         "group_names": []},
        {"id": "c", "label": "20230010", "name": "丙", "organization_name": "B校",
         "group_names": ["女队"]},
    ]


def test_sort_team_rows_label_numeric_ascending():
    out = sort_team_rows(_rows(), "label")
    assert [r["label"] for r in out] == ["2023001", "2023002", "20230010"]


def test_sort_team_rows_label_descending():
    out = sort_team_rows(_rows(), "label", reverse=True)
    assert [r["label"] for r in out] == ["20230010", "2023002", "2023001"]


def test_sort_team_rows_name_codepoint():
    # Name sort is casefolded string order: CJK names sort by Unicode
    # codepoint (deterministic, NOT pinyin); the ASCII assertion
    # ("Apple" < "banana") covers casefolding, not pinyin.
    out = sort_team_rows(_rows(), "name")
    assert [r["name"] for r in out] == ["丙", "乙", "甲"]
    # ascii casefold matters: 'Apple' < 'banana' even though 'a' > 'B'
    rows = [{"name": "banana"}, {"name": "Apple"}, {"name": "cherry"}]
    got = [r["name"] for r in sort_team_rows(rows, "name")]
    assert got == ["Apple", "banana", "cherry"]


def test_sort_team_rows_org_then_groups():
    out = sort_team_rows(_rows(), "org")
    assert [r["organization_name"] for r in out] == ["A校", "B校", "B校"]
    out = sort_team_rows(_rows(), "groups")
    assert [r["group_names"] for r in out] == [[], ["女队"], ["正式"]]


def test_sort_team_rows_unknown_key_falls_back_to_label():
    out = sort_team_rows(_rows(), "bogus")
    assert [r["label"] for r in out] == ["2023001", "2023002", "20230010"]


def test_sort_team_rows_empty():
    assert sort_team_rows([], "label") == []


def test_build_problem_rows_prefills_saved_color():
    problems = [
        {"id": "1-1", "label": "1-1", "title": "题一", "ordinal": 1},
        {"id": "1-2", "label": "1-2", "title": "题二", "ordinal": 2},
    ]
    saved = {"1-2": {"color": "blue", "rgb": "#00f"}}
    rows = build_problem_rows(problems, saved)
    assert rows[0]["label"] == "1-1"
    assert rows[0]["changed"] is False
    assert rows[0]["rgb"]  # palette default
    assert rows[1]["color"] == "blue"
    assert rows[1]["rgb"] == "#00f"
    assert rows[1]["changed"] is True


def test_build_problem_rows_orders_by_ordinal():
    problems = [
        {"id": "B", "label": "B", "ordinal": 2},
        {"id": "A", "label": "A", "ordinal": 1},
    ]
    rows = build_problem_rows(problems, {})
    assert [r["label"] for r in rows] == ["A", "B"]


def test_build_team_rows_prefills_saved_assignments_and_sorts():
    teams = [
        {"id": "t2", "name": "乙", "label": "2023002",
         "organization_id": "o1", "organization_name": "学校一"},
        {"id": "t1", "name": "甲", "label": "2023001",
         "organization_id": "o1", "organization_name": "学校一"},
    ]
    groups = {"g1": {"name": "正式队伍"}}
    saved = {"t2": {"group_ids": ["g1"]}}
    rows = build_team_rows(teams, groups, saved)
    assert [r["id"] for r in rows] == ["t1", "t2"]
    assert rows[0]["group_ids"] == []
    assert rows[1]["group_ids"] == ["g1"]
    assert rows[1]["group_names"] == ["正式队伍"]
    assert rows[0]["organization_name"] == "学校一"


def test_build_team_rows_unknown_group_falls_back_to_id():
    rows = build_team_rows([{"id": "t1", "name": "甲", "label": "2023001"}], {},
                           {"t1": {"group_ids": ["gone"]}})
    assert rows[0]["group_ids"] == ["gone"]
    assert rows[0]["group_names"] == ["gone"]


def test_write_load_manual_setup_round_trip(tmp_path):
    problems = {"1-1": {"color": "red", "rgb": "#f00"}}
    groups = {"g1": {"name": "正式队伍"}}
    assignments = {"t1": {"group_ids": ["g1"]}}
    write_manual_setup(tmp_path, problems, groups, assignments, "1-1")
    loaded = load_manual_setup(tmp_path)
    assert loaded["problems"] == problems
    assert loaded["groups"] == groups
    assert loaded["teams"] == assignments
    assert loaded["print_problem"] == "1-1"


def test_write_manual_setup_idempotent(tmp_path):
    args = ({"1-1": {"color": "red", "rgb": "#f00"}},
            {"g1": {"name": "正式队伍"}}, {"t1": {"group_ids": ["g1"]}})
    write_manual_setup(tmp_path, *args)
    first = {f.name: f.read_text(encoding="utf-8") for f in tmp_path.glob("*.json")}
    write_manual_setup(tmp_path, *args)
    second = {f.name: f.read_text(encoding="utf-8") for f in tmp_path.glob("*.json")}
    assert first == second


def test_write_manual_setup_keeps_chinese(tmp_path):
    write_manual_setup(tmp_path, {}, {"g1": {"name": "正式队伍"}}, {})
    raw = (tmp_path / "setup-groups.json").read_text(encoding="utf-8")
    assert "正式队伍" in raw  # ensure_ascii=False
    assert "\\u" not in raw


def test_write_manual_setup_creates_directory(tmp_path):
    target = tmp_path / "manual"
    write_manual_setup(target, {}, {}, {})
    assert (target / "setup-problems.json").exists()
    assert (target / "setup-groups.json").exists()
    assert (target / "setup-teams.json").exists()


def test_load_manual_setup_missing_files(tmp_path):
    assert load_manual_setup(tmp_path) == {
        "problems": {}, "groups": {}, "teams": {}, "contest": {},
        "print_problem": None}


class _StubClient:
    async def fetch_problems(self):
        return [{"id": "A", "label": "A", "title": "题A", "ordinal": 1}]

    async def fetch_teams(self):
        return [{"id": "t1", "name": "甲", "label": "2023001"}]


def test_fetch_setup_data_returns_problems_and_teams():
    problems, teams = asyncio.run(fetch_setup_data(_StubClient()))
    assert problems[0]["label"] == "A"
    assert teams[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# print_problem — load/write round-trip
# ---------------------------------------------------------------------------

def test_load_manual_setup_print_problem_present(tmp_path):
    (tmp_path / "setup-problems.json").write_text(
        json.dumps({"problems": {"1-1": {"color": "red", "rgb": "#f00"}},
                    "print_problem": "1-1"}),
        encoding="utf-8")
    assert load_manual_setup(tmp_path)["print_problem"] == "1-1"


def test_load_manual_setup_print_problem_null_value(tmp_path):
    (tmp_path / "setup-problems.json").write_text(
        json.dumps({"problems": {}, "print_problem": None}),
        encoding="utf-8")
    assert load_manual_setup(tmp_path)["print_problem"] is None


def test_load_manual_setup_print_problem_malformed(tmp_path):
    (tmp_path / "setup-problems.json").write_text(
        json.dumps({"problems": {}, "print_problem": 42}),
        encoding="utf-8")
    assert load_manual_setup(tmp_path)["print_problem"] is None


def test_write_manual_setup_print_problem_none_writes_key(tmp_path):
    write_manual_setup(tmp_path, {}, {}, {}, None)
    data = json.loads((tmp_path / "setup-problems.json").read_text(
        encoding="utf-8"))
    assert data == {"problems": {}, "print_problem": None}


def test_write_load_manual_setup_print_problem_round_trip(tmp_path):
    write_manual_setup(tmp_path, {}, {}, {}, "A")
    assert load_manual_setup(tmp_path)["print_problem"] == "A"


# ---------------------------------------------------------------------------
# toggle_print_problem — single-select toggle (view-layer, pure)
# ---------------------------------------------------------------------------

def _problem_rows(n=3):
    return [{"label": chr(ord("A") + i), "print": False} for i in range(n)]


def test_toggle_print_problem_selects_one():
    rows = _problem_rows()
    toggle_print_problem(rows, 1)
    assert [r["print"] for r in rows] == [False, True, False]


def test_toggle_print_problem_single_select_clears_others():
    rows = _problem_rows()
    toggle_print_problem(rows, 0)
    toggle_print_problem(rows, 2)
    assert [r["print"] for r in rows] == [False, False, True]


def test_toggle_print_problem_toggle_off():
    rows = _problem_rows()
    toggle_print_problem(rows, 1)
    toggle_print_problem(rows, 1)
    assert [r["print"] for r in rows] == [False, False, False]


def test_toggle_print_problem_empty_list_safe():
    toggle_print_problem([], 0)  # no-op, must not raise


# ---------------------------------------------------------------------------
# build_problem_rows — print prefill
# ---------------------------------------------------------------------------

def test_build_problem_rows_prefills_print():
    problems = [
        {"id": "1-1", "label": "1-1", "ordinal": 1},
        {"id": "1-2", "label": "1-2", "ordinal": 2},
    ]
    rows = build_problem_rows(problems, {}, print_problem="1-2")
    assert rows[0]["print"] is False
    assert rows[1]["print"] is True


def test_build_problem_rows_print_defaults_false():
    rows = build_problem_rows([{"id": "1-1", "label": "1-1", "ordinal": 1}],
                              {})
    assert rows[0]["print"] is False


# ---------------------------------------------------------------------------
# write_connection / _residual_config_prefill
# ---------------------------------------------------------------------------

def test_write_connection(tmp_path):
    write_connection(tmp_path, {"contest_id": "c1", "pta_cookie": "cookie"})
    data = json.loads((tmp_path / "setup-connection.json").read_text(
        encoding="utf-8"))
    assert data == {"contest_id": "c1", "pta_cookie": "cookie"}


def test_write_connection_creates_directory(tmp_path):
    target = tmp_path / "manual"
    write_connection(target, {"contest_id": "c1", "pta_cookie": "cookie"})
    assert (target / "setup-connection.json").exists()


def test_residual_config_prefill_full(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"contest_id": "c1", "pta_cookie": "cookie",
                               "port": 9000}), encoding="utf-8")
    assert _residual_config_prefill(cfg) == {
        "contest_id": "c1", "pta_cookie": "cookie"}


def test_residual_config_prefill_missing_file(tmp_path):
    assert _residual_config_prefill(tmp_path / "config.json") is None


def test_residual_config_prefill_incomplete(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"contest_id": "c1"}), encoding="utf-8")
    assert _residual_config_prefill(cfg) is None
    cfg.write_text(json.dumps({"contest_id": "", "pta_cookie": "cookie"}),
                   encoding="utf-8")
    assert _residual_config_prefill(cfg) is None


def test_residual_config_prefill_malformed(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert _residual_config_prefill(cfg) is None


# ---------------------------------------------------------------------------
# seat / members / coaches (task: 扮演 DOMjudge)
# ---------------------------------------------------------------------------

def test_build_team_rows_prefills_seat_members_coaches():
    teams = [{"id": "t1", "name": "甲", "label": "2023001"}]
    saved = {"t1": {"group_ids": ["g1"], "seat": "A12",
                    "members": ["甲", "乙"], "coaches": ["丙"]}}
    rows = build_team_rows(teams, {"g1": {"name": "正式队伍"}}, saved)
    assert rows[0]["seat"] == "A12"
    assert rows[0]["members"] == ["甲", "乙"]
    assert rows[0]["coaches"] == ["丙"]


def test_build_team_rows_default_meta_is_empty():
    rows = build_team_rows([{"id": "t1", "name": "甲", "label": "2023001"}],
                           {}, {})
    assert rows[0]["seat"] == ""
    assert rows[0]["members"] == []
    assert rows[0]["coaches"] == []


def test_build_teams_setup_omits_empty_values():
    assignments = {"t1": set(), "t2": {"g1"}, "t3": set()}
    team_meta = {"t1": {"seat": "", "members": [], "coaches": []},
                 "t2": {"seat": "", "members": [], "coaches": []},
                 "t3": {"seat": "A12", "members": ["甲"], "coaches": ["丙"]}}
    out = build_teams_setup(assignments, team_meta)
    # t1: nothing to write -> omitted; t2: groups only; t3: meta only
    assert out == {"t2": {"group_ids": ["g1"]},
                   "t3": {"seat": "A12", "members": ["甲"],
                          "coaches": ["丙"]}}


def test_build_teams_setup_keeps_all_fields_and_sorts_groups():
    out = build_teams_setup({"t1": {"g2", "g1"}},
                            {"t1": {"seat": " A12 ", "members": ["", "甲"],
                                    "coaches": [" "]}})
    assert out == {"t1": {"group_ids": ["g1", "g2"], "seat": "A12",
                          "members": ["甲"]}}  # blank entries dropped


def test_build_teams_setup_empty_inputs():
    assert build_teams_setup({}, {}) == {}
    assert build_teams_setup(None, None) == {}
    # assignments exist but no meta entry -> groups still written
    assert build_teams_setup({"t1": {"g1"}}, None) == {
        "t1": {"group_ids": ["g1"]}}


def test_write_load_manual_setup_seat_members_round_trip(tmp_path):
    teams = {"t1": {"group_ids": ["g1"], "seat": "A12",
                    "members": ["甲", "乙"], "coaches": ["丙"]}}
    write_manual_setup(tmp_path, {}, {"g1": {"name": "正式队伍"}}, teams, None)
    assert load_manual_setup(tmp_path)["teams"] == teams


def test_write_connection_includes_print_server_when_set(tmp_path):
    write_connection(tmp_path, {"contest_id": "c1", "pta_cookie": "cookie",
                                "print_server": "http://printer.local/print"})
    data = json.loads((tmp_path / "setup-connection.json").read_text(
        encoding="utf-8"))
    assert data == {"contest_id": "c1", "pta_cookie": "cookie",
                    "print_server": "http://printer.local/print"}


@pytest.mark.parametrize("print_server", ["", None, 42])
def test_write_connection_omits_print_server_when_unset(
        tmp_path, print_server):
    write_connection(tmp_path, {"contest_id": "c1", "pta_cookie": "cookie",
                                "print_server": print_server})
    data = json.loads((tmp_path / "setup-connection.json").read_text(
        encoding="utf-8"))
    assert data == {"contest_id": "c1", "pta_cookie": "cookie"}


# --- 比赛设置 tab: contest section (task 22) ------------------------------

def test_build_contest_setup_keeps_only_nonblank():
    assert build_contest_setup({
        "start_time": "2026-10-13T16:00:00+08:00", "duration": "51:00:00",
        "scoreboard_freeze_duration": "", "finalized_at": "auto"}) == {
        "start_time": "2026-10-13T16:00:00+08:00", "duration": "51:00:00"}


def test_build_contest_setup_never_and_explicit():
    assert build_contest_setup({"finalized_at": "never"}) == {
        "finalized_at": "never"}
    assert build_contest_setup(
        {"finalized_at": "2026-10-14T17:00:00+08:00"}) == {
        "finalized_at": "2026-10-14T17:00:00+08:00"}
    # garbage finalization values are dropped (become automatic)
    assert build_contest_setup({"finalized_at": "junk"}) == {}


def test_build_contest_setup_empty():
    assert build_contest_setup({}) == {}
    assert build_contest_setup(None) == {}


def test_validate_contest_times_all_blank_is_ok():
    ok, err, payload = validate_contest_times("", "", "", "")
    assert ok and err == "" and payload == {"contest": {}}


def test_validate_contest_times_accepts_valid():
    ok, err, payload = validate_contest_times(
        "2026-10-13T16:00:00+08:00", "51:00:00", "45:00:00", "never")
    assert ok, err
    assert payload == {"contest": {
        "start_time": "2026-10-13T16:00:00+08:00", "duration": "51:00:00",
        "scoreboard_freeze_duration": "45:00:00", "finalized_at": "never"}}


def test_validate_contest_times_start_requires_offset():
    ok, err, _ = validate_contest_times("2026-10-13T16:00:00", "", "", "")
    assert not ok and "时区" in err


def test_validate_contest_times_start_bad_format():
    ok, err, _ = validate_contest_times("2026/10/13 16:00", "", "", "")
    assert not ok and err


def test_validate_contest_times_duration_bad():
    ok, err, _ = validate_contest_times("", "abc", "", "")
    assert not ok and "时长" in err


def test_validate_contest_times_freeze_bad():
    ok, err, _ = validate_contest_times("", "", "0", "")
    assert not ok and "封榜" in err


def test_validate_contest_times_finalized_bad():
    ok, err, _ = validate_contest_times("", "", "", "tomorrow")
    assert not ok and "定稿" in err


def test_write_contest_setup_round_trip(tmp_path):
    write_contest_setup(tmp_path, {"start_time": "2026-10-13T16:00:00+08:00",
                                   "duration": "51:00:00"})
    f = tmp_path / setup.SETUP_CONTEST_FILE
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data == {"contest": {"start_time": "2026-10-13T16:00:00+08:00",
                                "duration": "51:00:00"}}
    assert load_manual_setup(tmp_path)["contest"] == data["contest"]


def test_load_manual_setup_contest_ignores_non_dict(tmp_path):
    (tmp_path / setup.SETUP_CONTEST_FILE).write_text(
        json.dumps({"contest": "nope"}), encoding="utf-8")
    assert load_manual_setup(tmp_path)["contest"] == {}
