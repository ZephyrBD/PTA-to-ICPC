import asyncio

import pta2ccs.setup as setup
from pta2ccs.setup import (
    allocate_group_id,
    build_problem_rows,
    build_team_rows,
    fetch_setup_data,
    load_manual_setup,
    sort_team_rows,
    sort_teams_by_label,
    write_manual_setup,
)


def test_import_setup_does_not_require_display():
    # the module import itself must not instantiate Tk / need a display;
    # only launch_gui()/main() touch tkinter (imported lazily inside)
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
    write_manual_setup(tmp_path, problems, groups, assignments)
    loaded = load_manual_setup(tmp_path)
    assert loaded["problems"] == problems
    assert loaded["groups"] == groups
    assert loaded["teams"] == assignments


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
    assert load_manual_setup(tmp_path) == {"problems": {}, "groups": {}, "teams": {}}


class _StubClient:
    async def fetch_problems(self):
        return [{"id": "A", "label": "A", "title": "题A", "ordinal": 1}]

    async def fetch_teams(self):
        return [{"id": "t1", "name": "甲", "label": "2023001"}]


def test_fetch_setup_data_returns_problems_and_teams():
    problems, teams = asyncio.run(fetch_setup_data(_StubClient()))
    assert problems[0]["label"] == "A"
    assert teams[0]["id"] == "t1"
