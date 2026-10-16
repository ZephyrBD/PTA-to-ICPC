import asyncio
import json

import pytest

import pta2icpc.setup as setup
from pta2icpc.setup import (
    allocate_group_id,
    apply_team_edit,
    apply_team_meta,
    build_contest_setup,
    build_problem_rows,
    build_team_rows,
    build_teams_setup,
    fetch_setup_data,
    load_manual_setup,
    merge_contest_prefill,
    normalize_team_meta,
    prefill_contest_times,
    render_team_seat,
    sort_team_rows,
    sort_teams_by_label,
    toggle_print_problem,
    validate_contest_times,
    write_connection,
    write_contest_setup,
    write_manual_setup,
)
from pta2icpc.translate import _build_contest


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
    write_manual_setup(problems, groups, assignments, "1-1",
                       contest_dir=tmp_path)
    loaded = load_manual_setup(tmp_path)
    assert loaded["problems"] == problems
    assert loaded["groups"] == groups
    assert loaded["teams"] == assignments
    assert loaded["print_problem"] == "1-1"


def test_write_manual_setup_idempotent(tmp_path):
    args = ({"1-1": {"color": "red", "rgb": "#f00"}},
            {"g1": {"name": "正式队伍"}}, {"t1": {"group_ids": ["g1"]}})
    write_manual_setup(*args, contest_dir=tmp_path)
    first = {f.name: f.read_text(encoding="utf-8") for f in tmp_path.glob("*.json")}
    write_manual_setup(*args, contest_dir=tmp_path)
    second = {f.name: f.read_text(encoding="utf-8") for f in tmp_path.glob("*.json")}
    assert first == second


def test_write_manual_setup_keeps_chinese(tmp_path):
    write_manual_setup({}, {"g1": {"name": "正式队伍"}}, {}, contest_dir=tmp_path)
    raw = (tmp_path / "setup-groups.json").read_text(encoding="utf-8")
    assert "正式队伍" in raw  # ensure_ascii=False
    assert "\\u" not in raw


def test_write_manual_setup_creates_directory(tmp_path):
    target = tmp_path / "manual"
    write_manual_setup({}, {}, {}, contest_dir=target)
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

    async def fetch_contest_meta(self):
        return {"name": "赛", "startAt": "2026-10-13T08:00:00Z",
                "durationSeconds": 111600, "hidingTimeMinutes": 360}


def test_fetch_setup_data_returns_problems_teams_and_meta():
    problems, teams, meta = asyncio.run(fetch_setup_data(_StubClient()))
    assert problems[0]["label"] == "A"
    assert teams[0]["id"] == "t1"
    assert meta["durationSeconds"] == 111600


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
    write_manual_setup({}, {}, {}, None, contest_dir=tmp_path)
    data = json.loads((tmp_path / "setup-problems.json").read_text(
        encoding="utf-8"))
    assert data == {"problems": {}, "print_problem": None}


def test_write_load_manual_setup_print_problem_round_trip(tmp_path):
    write_manual_setup({}, {}, {}, "A", contest_dir=tmp_path)
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
# write_connection
# ---------------------------------------------------------------------------

def test_write_connection(tmp_path):
    write_connection({"contest_id": "c1", "pta_cookie": "cookie"},
                     contest_dir=tmp_path)
    data = json.loads((tmp_path / "setup-connection.json").read_text(
        encoding="utf-8"))
    assert data == {"contest_id": "c1", "pta_cookie": "cookie"}


def test_write_connection_creates_directory(tmp_path):
    target = tmp_path / "manual"
    write_connection({"contest_id": "c1", "pta_cookie": "cookie"},
                     contest_dir=target)
    assert (target / "setup-connection.json").exists()


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
    write_manual_setup({}, {"g1": {"name": "正式队伍"}}, teams, None,
                       contest_dir=tmp_path)
    assert load_manual_setup(tmp_path)["teams"] == teams


def test_write_connection_includes_print_server_when_set(tmp_path):
    write_connection({"contest_id": "c1", "pta_cookie": "cookie",
                      "print_server": "http://printer.local/print"},
                     contest_dir=tmp_path)
    data = json.loads((tmp_path / "setup-connection.json").read_text(
        encoding="utf-8"))
    assert data == {"contest_id": "c1", "pta_cookie": "cookie",
                    "print_server": "http://printer.local/print"}


@pytest.mark.parametrize("print_server", ["", None, 42])
def test_write_connection_omits_print_server_when_unset(
        tmp_path, print_server):
    write_connection({"contest_id": "c1", "pta_cookie": "cookie",
                      "print_server": print_server}, contest_dir=tmp_path)
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
    write_contest_setup({"start_time": "2026-10-13T16:00:00+08:00",
                         "duration": "51:00:00"}, contest_dir=tmp_path)
    f = tmp_path / setup.SETUP_CONTEST_FILE
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data == {"contest": {"start_time": "2026-10-13T16:00:00+08:00",
                                "duration": "51:00:00"}}
    assert load_manual_setup(tmp_path)["contest"] == data["contest"]


def test_load_manual_setup_contest_ignores_non_dict(tmp_path):
    (tmp_path / setup.SETUP_CONTEST_FILE).write_text(
        json.dumps({"contest": "nope"}), encoding="utf-8")
    assert load_manual_setup(tmp_path)["contest"] == {}


# --- 座位号 sort column (task 23) ------------------------------------------

def test_sort_team_rows_seat():
    rows = [{"label": "1", "seat": ""}, {"label": "2", "seat": "A12"},
            {"label": "3", "seat": "a3"}]
    out = sort_team_rows(rows, "seat")
    # casefolded strings: "" < "a12" < "a3"
    assert [r["label"] for r in out] == ["1", "2", "3"]


def test_build_team_rows_seat_column_present():
    rows = build_team_rows(
        [{"id": "t1", "name": "甲", "label": "2023001"}], {},
        {"t1": {"seat": "B7"}})
    assert rows[0]["seat"] == "B7"  # seat is the value of the 座位号 column


# --- prefill_contest_times / merge_contest_prefill (task 23) --------------

_META = {"startAt": "2026-10-13T08:00:00Z", "durationSeconds": 111600,
         "hidingTimeMinutes": 360}


# 79h window (111600*2 = 223200s) + 360min hiding, mirroring the live
# 2026-10-15 case where a saved 31h duration must drive the freeze.
_META_79H = {"startAt": "2026-10-15T02:00:00Z",
             "durationSeconds": 223200, "hidingTimeMinutes": 360}


def test_prefill_contest_times_saved_wins():
    saved = {"start_time": "2026-01-01T00:00:00+08:00", "duration": "1:00:00"}
    out = prefill_contest_times(_META, saved)
    assert out["start_time"] == "2026-01-01T00:00:00+08:00"  # saved wins
    assert out["duration"] == "1:00:00"                      # saved wins
    # freeze rides the RESOLVED (saved) duration: 1h − 6h hides at/after the
    # start → clamped to 0; the box shows blank (= 封榜关闭) rather than the
    # literal "0:00:00", which validate_contest_times (:302) rejects.
    assert out["scoreboard_freeze_duration"] == ""


def test_prefill_zero_freeze_is_blank_and_savable():
    # regression: PTA 31h window, hiding 360min, saved duration 1h → resolved
    # freeze clamps to 0. The prefilled box must be blank so an untouched
    # "prefill then save" succeeds instead of being rejected as "封榜时长必须大于 0".
    saved = {"duration": "1:00:00"}
    out = prefill_contest_times(_META, saved)
    assert out["scoreboard_freeze_duration"] == ""
    ok, err, payload = validate_contest_times(
        out["start_time"], out["duration"], out["scoreboard_freeze_duration"], "")
    assert ok, err
    # blank freeze → 封榜关闭: no explicit key, server still resolves 0:00:00
    assert "scoreboard_freeze_duration" not in payload["contest"]
    assert _build_contest("c1", payload["contest"], _META)[
        "scoreboard_freeze_duration"] == "0:00:00"


def test_prefill_freeze_uses_saved_duration_not_pta_window():
    # regression: PTA window 79h, hiding 360min, saved duration 31h. Computing
    # the freeze against PTA's raw window yields 73h (> the 31h duration) and
    # would place `frozen` 42h BEFORE `started`.
    saved = {"duration": "31:00:00"}
    out = prefill_contest_times(_META_79H, saved)
    assert out["duration"] == "31:00:00"
    assert out["scoreboard_freeze_duration"] == "25:00:00"


def test_prefill_freeze_save_unchanged_matches_server_resolution():
    # "prefill then save unchanged" must be a semantic no-op: feeding the
    # prefilled values back as the manual override must reproduce the same
    # resolved times the server would emit.
    saved = {"duration": "31:00:00"}
    prefilled = prefill_contest_times(_META_79H, saved)
    resolved = _build_contest("c1", dict(saved), _META_79H)
    assert resolved["duration"] == prefilled["duration"]
    assert (resolved["scoreboard_freeze_duration"]
            == prefilled["scoreboard_freeze_duration"] == "25:00:00")


def test_prefill_contest_times_pta_when_no_saved():
    out = prefill_contest_times(_META, None)
    assert out == {"start_time": "2026-10-13T16:00:00+08:00",
                   "duration": "31:00:00",
                   "scoreboard_freeze_duration": "25:00:00"}


def test_prefill_contest_times_both_blank_is_empty():
    out = prefill_contest_times({}, {"start_time": "", "duration": "",
                                     "scoreboard_freeze_duration": ""})
    assert out == {"start_time": "", "duration": "",
                   "scoreboard_freeze_duration": ""}


def test_prefill_contest_times_uses_tz():
    out = prefill_contest_times(_META, {}, tz="+00:00")
    assert out["start_time"] == "2026-10-13T08:00:00+00:00"


def test_merge_contest_prefill_updates_untouched_edited_survives():
    current = {"start_time": "X-user", "duration": "31:00:00",
               "scoreboard_freeze_duration": ""}
    last = {"start_time": "", "duration": "31:00:00",
            "scoreboard_freeze_duration": ""}
    new = {"start_time": "T", "duration": "D",
           "scoreboard_freeze_duration": "F"}
    out = merge_contest_prefill(current, last, new)
    # start_time was edited (current != last) → user edit survives
    assert out["values"]["start_time"] == "X-user"
    # duration/freeze were untouched → updated to the new prefill
    assert out["values"]["duration"] == "D"
    assert out["values"]["scoreboard_freeze_duration"] == "F"
    assert out["prefill"] == new


def test_merge_contest_prefill_all_untouched():
    current = {"start_time": "a", "duration": "b",
               "scoreboard_freeze_duration": "c"}
    out = merge_contest_prefill(current, dict(current),
                                {"start_time": "x", "duration": "y",
                                 "scoreboard_freeze_duration": "z"})
    assert out["values"] == {"start_time": "x", "duration": "y",
                             "scoreboard_freeze_duration": "z"}


def test_refetch_does_not_clobber_user_edit():
    # exercises the GUI's exact refetch sequence: the current values must be
    # re-read from the live boxes (StringVars) at merge time, not remembered
    # from the previous prefill — otherwise the "edited" branch is dead and a
    # refetch silently reverts a typed start time.
    fields = {"start_time": "2026-10-15T02:00:00+00:00", "duration": "31:00:00",
              "scoreboard_freeze_duration": "25:00:00"}
    last_prefill = dict(fields)
    # user retypes the start time in the box
    fields["start_time"] = "2026-10-15T03:30:00+00:00"

    def collect():  # mirrors setup.collect_contest_prefill()
        return dict(fields)

    new_prefill = {"start_time": "2026-10-15T02:00:00+00:00",
                   "duration": "31:00:00",
                   "scoreboard_freeze_duration": "25:00:00"}
    merged = merge_contest_prefill(collect(), last_prefill, new_prefill)
    assert merged["values"]["start_time"] == "2026-10-15T03:30:00+00:00"  # kept
    assert merged["values"]["duration"] == "31:00:00"


# --- normalize_team_meta / apply_team_meta (task 23) ----------------------

def test_normalize_team_meta_strips_and_drops_blanks():
    out = normalize_team_meta(" A12 ", ["甲", "", " 乙 "], ["", "丙"])
    assert out == {"seat": "A12", "members": ["甲", "乙"],
                   "coaches": ["丙"]}


def test_normalize_team_meta_empty():
    assert normalize_team_meta(None, None, None) == {
        "seat": "", "members": [], "coaches": []}


def test_apply_team_meta_writes_normalized():
    team_meta: dict = {}
    apply_team_meta(team_meta, "t1", {"seat": " B7 ", "members": ["甲", ""],
                                      "coaches": [" 教练 "]})
    assert team_meta["t1"] == {"seat": "B7", "members": ["甲"],
                               "coaches": ["教练"]}


def test_render_team_seat_prefers_team_meta_over_stale_row():
    # the table row was built before the dialog edit; the rendered seat must
    # come from team_meta so 确定 shows immediately (row["seat"] is stale).
    row = {"id": "t1", "seat": "old"}
    team_meta = {"t1": {"seat": "new", "members": [], "coaches": []}}
    assert render_team_seat(team_meta, row) == "new"


def test_render_team_seat_falls_back_to_row_until_edited():
    row = {"id": "t1", "seat": "B7"}
    assert render_team_seat({}, row) == "B7"


def test_render_team_seat_blank_edit_wins():
    # an edit that clears the seat must show blank, not the stale row value
    row = {"id": "t1", "seat": "old"}
    team_meta = {"t1": {"seat": "", "members": [], "coaches": []}}
    assert render_team_seat(team_meta, row) == ""


def test_apply_team_edit_mirrors_seat_into_row_so_sort_agrees():
    # after 确定 the seat sort key (row["seat"]) must equal the rendered cell
    # (team_meta) — otherwise a 座位号 sort orders rows inconsistently with
    # the column shown before the next refetch.
    rows = [{"id": "t1", "seat": "Z"}, {"id": "t2", "seat": "M"},
            {"id": "t3", "seat": "A"}]
    team_meta = {r["id"]: {"seat": r["seat"], "members": [], "coaches": []}
                 for r in rows}
    apply_team_edit(team_meta, rows, "t1", {"seat": "A0", "members": ["甲"],
                                            "coaches": []})
    assert next(r for r in rows if r["id"] == "t1")["seat"] == "A0"
    by_rendered = [render_team_seat(team_meta, r)
                   for r in sort_team_rows(rows, "seat")]
    assert by_rendered == sorted(by_rendered, key=str.casefold)
    assert by_rendered == ["A", "A0", "M"]


def test_apply_team_edit_missing_row_is_noop_on_rows():
    team_meta: dict = {}
    rows = [{"id": "t2", "seat": "A"}]
    apply_team_edit(team_meta, rows, "t1", {"seat": "B7"})
    assert team_meta["t1"]["seat"] == "B7"
    assert rows == [{"id": "t2", "seat": "A"}]  # untouched, no KeyError
