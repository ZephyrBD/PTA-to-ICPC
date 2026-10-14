import datetime as dt
import logging

from pta2ccs.models import sanitize_id
from pta2ccs.translate import translate

TZ = "+08:00"
NOW = dt.datetime(2023, 5, 21, 10, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

MANUAL = {
    "contest": {
        "name": "省赛", "formal_name": "省赛正式赛",
        "start_time": "2023-05-21T09:00:00+08:00", "duration": "5:00:00",
        "penalty_time": "0:20:00", "scoreboard_freeze_duration": "0:00:00",
        "pta_timezone": TZ,
    },
    "groups": {"D": {"name": "4078"}},
    "organizations": {"org1": {"name": "某大学"}},
    "teams": {"D22": {"organization_id": "org1", "icpc_id": "2023001"}},
    "problems": {"A": {"color": "red", "rgb": "#f00"}},
    "languages": {"cpp": {"name": "C++"}},
    "judgement_types_overrides": {"WA": {"penalty": False}},
}

RAW_PROBLEMS = [
    {"id": "A", "title": "题A"},
    {"id": "B", "title": "题B"},
]
RAW_SUBMISSIONS = [
    {"id": "101", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
     "team_id": "D22", "team_name": "鸡你太美队", "problem_index": "A", "language": "cpp"},
    {"id": "102", "submit_time": "2023/05/21 09:11:00", "status": "正在评测",
     "team_id": "D23", "team_name": "另一队", "problem_index": "B", "language": "cpp"},
    {"id": "103", "submit_time": "2023/05/21 09:12:00", "status": "答案错误",
     "team_id": "D22", "team_name": "鸡你太美队", "problem_index": "B", "language": "cpp"},
    {"id": "104", "submit_time": "bad-time", "status": "答案正确",
     "team_id": "D24", "team_name": "脏队", "problem_index": "A", "language": "cpp"},
    {"id": "105", "submit_time": "2023/05/21 09:13:00", "status": "答案正确",
     "team_name": "缺team_id-secret", "problem_index": "A", "language": "cpp"},
]

ROSTER = [
    {"id": "1901545194649161728", "name": "张三", "label": "2022000001",
     "group_id": "1901545194649161728", "organization_id": "1901545194649161728",
     "organization_name": "DSA_2024"},
    {"id": "2032742953069465601", "name": "李四", "label": "2022000002",
     "group_id": "2032742953069465600", "organization_id": "2032742953069465600",
     "organization_name": "DSA_2025"},
    {"id": "2032742953069465602", "name": "王五", "label": "2022000003",
     "group_id": "2032742953069465600", "organization_id": "2032742953069465600",
     "organization_name": "DSA_2025"},
]

# fetch_contest_meta() shape (live 2026-10-14: hidingTime 360 min AFTER start,
# duration 111600 s → freeze = 111600 − 360×60 = 90000 s = 25:00:00)
META = {
    "name": "示例比赛",
    "startAt": "2026-10-13T08:00:00Z",
    "endAt": "2026-10-14T15:00:00Z",
    "durationSeconds": 111600,
    "compilers": ["GXX", "JAVAC", "CANGJIE"],
    "scoreboardFreezeSeconds": 90000,
}


def t() -> "object":
    return translate("999", MANUAL, RAW_SUBMISSIONS, RAW_PROBLEMS, None, NOW)


def test_contest_and_state():
    data = t()
    assert data.contest["id"] == "999"
    assert data.contest["scoreboard_type"] == "pass-fail"
    assert data.contest["penalty_time"] == "0:20:00"
    assert data.contest["formal_name"] == "省赛正式赛"
    assert data.state["started"] is not None
    assert data.state["ended"] is None


def test_team_backfill_and_manual_merge():
    data = t()
    t22 = data.teams["D22"]
    assert t22["label"] == "D22"
    assert t22["name"] == "鸡你太美队"
    assert t22["group_id"] is None  # no roster → nothing knows the group
    assert t22["organization_id"] == "org1"  # manual wins
    assert t22["icpc_id"] == "2023001"
    assert data.teams["D23"]["name"] == "另一队"
    assert data.teams["D23"]["group_id"] is None
    assert data.groups["D"]["name"] == "4078"  # manual group untouched
    assert "D23" not in data.groups  # submissions no longer invent groups


def test_roster_teams_organizations_and_zero_submission_survival():
    data = translate("999", MANUAL, RAW_SUBMISSIONS, RAW_PROBLEMS, ROSTER, NOW)
    # every roster member is a team, even with zero submissions (王五)
    assert {"1901545194649161728", "2032742953069465601",
            "2032742953069465602"} <= set(data.teams)
    zhang = data.teams["1901545194649161728"]
    assert zhang["name"] == "张三"
    assert zhang["label"] == "2022000001"  # roster label = 学号
    assert zhang["group_id"] == "1901545194649161728"
    assert zhang["organization_id"] == "1901545194649161728"
    assert "2032742953069465602" not in {
        s["team_id"] for s in data.submissions.values()}  # zero submissions
    # organizations + mirrored groups from roster, raw long ids as-is
    assert data.organizations["1901545194649161728"]["name"] == "DSA_2024"
    assert data.organizations["2032742953069465600"]["name"] == "DSA_2025"
    assert data.groups["1901545194649161728"] == {
        "id": "1901545194649161728", "name": "DSA_2024"}
    assert data.groups["2032742953069465600"]["name"] == "DSA_2025"
    # manual entries still present (applied before roster, not overwritten)
    assert data.organizations["org1"]["name"] == "某大学"
    assert data.groups["D"]["name"] == "4078"


def test_roster_sanitizes_group_ids_and_null_missing():
    roster = [
        {"id": "t1", "name": "甲", "label": "2022000011", "group_id": "G 1",
         "organization_id": "G 1", "organization_name": "机房 1"},
        {"id": "t2", "name": "乙", "label": "2022000012", "group_id": None,
         "organization_id": None, "organization_name": None},
    ]
    data = translate("999", MANUAL, [], RAW_PROBLEMS, roster, NOW)
    t1 = data.teams["t1"]
    assert t1["group_id"] == t1["organization_id"] == "G-1"
    # org/group keyed on the same sanitized id the team references
    assert data.organizations["G-1"]["name"] == "机房 1"
    assert data.groups["G-1"]["name"] == "机房 1"
    assert "G 1" not in data.organizations
    t2 = data.teams["t2"]
    assert t2["group_id"] is None
    assert t2["organization_id"] is None
    assert "" not in data.organizations
    assert set(data.organizations) == {"org1", "G-1"}


def test_roster_name_not_overwritten_by_submission_backfill():
    roster = [{"id": "D22", "name": "真名", "label": "2022000009",
               "group_id": "G1",
               "organization_id": "G1", "organization_name": "DSA_2024"}]
    rows = [{"id": "501", "submit_time": "2023/05/21 09:10:00",
             "status": "答案正确", "team_id": "D22", "team_name": "垃圾名",
             "problem_index": "A", "language": "cpp"}]
    data = translate("999", MANUAL, rows, RAW_PROBLEMS, roster, NOW)
    assert data.teams["D22"]["name"] == "真名"
    assert data.teams["D22"]["label"] == "2022000009"  # roster label survives
    assert data.submissions["501"]["team_id"] == "D22"


def test_manual_team_override_wins_over_roster():
    manual = dict(MANUAL, teams={"1901545194649161728": {
        "organization_id": "org1", "icpc_id": "2023001"}})
    data = translate("999", manual, [], RAW_PROBLEMS, ROSTER, NOW)
    t = data.teams["1901545194649161728"]
    assert t["organization_id"] == "org1"  # manual value survives roster
    assert t["icpc_id"] == "2023001"
    assert t["name"] == "张三"  # roster fills what manual omits


def test_submission_team_not_in_roster_still_creates_team():
    rows = [{"id": "601", "submit_time": "2023/05/21 09:10:00",
             "status": "答案正确", "team_id": "9998887776665554443",
             "team_name": "旁听生", "problem_index": "A", "language": "cpp"}]
    data = translate("999", MANUAL, rows, RAW_PROBLEMS, ROSTER, NOW)
    assert data.submissions["601"]["team_id"] == "9998887776665554443"
    t = data.teams["9998887776665554443"]
    assert t["name"] == "旁听生"
    assert t["organization_id"] is None


def test_problems_labels_and_manual_color():
    data = t()
    p = data.problems["A"]
    assert p["label"] == "A"
    assert p["ordinal"] == 1
    assert p["rgb"] == "#f00"
    assert data.problems["B"]["label"] == "B"
    assert data.problems["B"]["rgb"]  # built-in palette


def test_submissions_and_judgements():
    data = t()
    assert "104" not in data.submissions  # bad time skipped
    assert "105" not in data.submissions  # missing team_id skipped
    sub = data.submissions["101"]
    assert sub["team_id"] == "D22"
    assert sub["problem_id"] == "A"
    assert sub["language_id"] == "cpp"
    assert sub["contest_time"] == "0:10:00"
    jud = data.judgements["101"]
    assert jud["judgement_type_id"] == "AC"
    assert jud["current"] is True
    assert data.judgements["102"]["judgement_type_id"] is None  # still judging
    assert data.judgements["102"]["end_time"] is None
    assert data.judgements["103"]["judgement_type_id"] == "WA"


def test_manual_judgement_type_override_applies():
    data = t()
    assert data.judgement_types["WA"]["penalty"] is False
    assert data.judgement_types["AC"]["solved"] is True
    assert data.judgement_types["CE"]["penalty"] is False


def test_judgement_types_include_pa():
    data = t()
    pa = data.judgement_types["PA"]
    assert pa["id"] == "PA"
    assert pa["name"] == "Partial Accepted"
    assert pa["penalty"] is True
    assert pa["solved"] is False


def test_languages_collected_from_submissions():
    data = t()
    assert data.languages["cpp"]["name"] == "C++"


def test_translate_accepts_enum_status():
    rows = [
        {"id": "201", "submit_time": "2023/05/21 09:10:00", "status": "ACCEPTED",
         "team_id": "D22", "team_name": "鸡你太美队", "problem_index": "A", "language": "cpp"},
        {"id": "202", "submit_time": "2023/05/21 09:11:00", "status": "WRONG_ANSWER",
         "team_id": "D22", "team_name": "鸡你太美队", "problem_index": "A", "language": "cpp"},
        {"id": "203", "submit_time": "2023/05/21 09:12:00", "status": "JUDGING",
         "team_id": "D22", "team_name": "鸡你太美队", "problem_index": "A", "language": "cpp"},
    ]
    data = translate("999", MANUAL, rows, RAW_PROBLEMS, None, NOW)
    assert data.judgements["201"]["judgement_type_id"] == "AC"
    assert data.judgements["202"]["judgement_type_id"] == "WA"
    assert data.judgements["203"]["judgement_type_id"] is None
    assert data.judgements["203"]["end_time"] is None


def test_missing_team_id_log_does_not_leak_team_name(caplog):
    rows = [
        {"id": "301", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
         "team_name": "缺team_id-secret", "problem_index": "A", "language": "cpp"},
    ]
    with caplog.at_level(logging.WARNING):
        translate("999", MANUAL, rows, RAW_PROBLEMS, None, NOW)
    assert "301" in caplog.text
    assert "缺team_id-secret" not in caplog.text


# --- contest meta (freeze / compilers / times), task 11 -------------------

def test_meta_fills_contest_fields_when_manual_absent():
    """manual explicit > meta > built-in default: with no manual values the
    meta startAt/duration/freeze win (live shape: 31h contest, freeze 25:00:00
    = 111600 − 360×60 → frozen at start+6h)."""
    manual = {"contest": {"name": "meta赛", "pta_timezone": TZ}}
    # after the freeze moment (2026-10-13 22:00+08) but before the end
    # (2026-10-14 23:00+08)
    now = dt.datetime(2026, 10, 14, 20, 0,
                      tzinfo=dt.timezone(dt.timedelta(hours=8)))
    data = translate("999", manual, [], [], None, now, meta=META)
    c = data.contest
    assert c["start_time"] == "2026-10-13T16:00:00+08:00"  # 08:00Z → +08:00
    assert c["duration"] == "31:00:00"                     # 111600 s
    assert c["scoreboard_freeze_duration"] == "25:00:00"   # 90000 s
    assert data.state["started"] is not None
    assert data.state["ended"] is None
    # end − 25h == start + 6h (hidingTime is minutes after contest start)
    assert data.state["frozen"] == "2026-10-13T22:00:00+08:00"


def test_meta_defaults_when_meta_empty():
    data = translate("999", {}, [], [], None, NOW, meta=None)
    assert data.contest["start_time"] is None
    assert data.contest["duration"] == "5:00:00"
    assert data.contest["scoreboard_freeze_duration"] == "0:00:00"
    assert data.contest["name"] == "contest"
    assert data.contest["formal_name"] == "contest"
    assert data.state["frozen"] is None


def test_meta_name_fills_contest_names_when_manual_lacks_them():
    """No manual name → problemSet.name from the API fills name and
    formal_name (no more auto-generated "PTA <id>" placeholder)."""
    manual = {"contest": {"start_time": "2026-10-13T16:00:00+08:00",
                          "duration": "31:00:00", "pta_timezone": TZ}}
    data = translate("999", manual, [], [], None, NOW, meta=META)
    assert data.contest["name"] == "示例比赛"
    assert data.contest["formal_name"] == "示例比赛"


def test_manual_names_win_over_meta():
    """Manual explicit name AND formal_name beat the API name."""
    data = translate("999", MANUAL, [], [], None, NOW, meta=META)
    assert data.contest["name"] == "省赛"
    assert data.contest["formal_name"] == "省赛正式赛"


def test_manual_name_only_leaves_formal_name_following_manual():
    """formal_name falls through manual formal_name → manual name → meta
    name — a manual name alone must not be overridden by the API name."""
    manual = {"contest": {"name": "手动赛", "pta_timezone": TZ}}
    data = translate("999", manual, [], [], None, NOW, meta=META)
    assert data.contest["name"] == "手动赛"
    assert data.contest["formal_name"] == "手动赛"


def test_empty_manual_name_falls_through_to_meta():
    """`or` semantics: manual "" must not mask the API name."""
    manual = {"contest": {"name": "", "formal_name": "", "pta_timezone": TZ}}
    data = translate("999", manual, [], [], None, NOW, meta=META)
    assert data.contest["name"] == "示例比赛"
    assert data.contest["formal_name"] == "示例比赛"


def test_zero_meta_freeze_renders_zero_duration_and_never_frozen():
    """Client edge cases (hideScoreboard false / hidingTime ≥ duration /
    durationSeconds unknown) all collapse to scoreboardFreezeSeconds 0 →
    translate renders "0:00:00" and state.frozen stays None."""
    data = translate("999", {"contest": {"start_time": "2026-10-13T16:00:00+08:00",
                                         "duration": "31:00:00", "pta_timezone": TZ}},
                     [], [], None, NOW, meta=dict(META, scoreboardFreezeSeconds=0))
    assert data.contest["scoreboard_freeze_duration"] == "0:00:00"
    assert data.state["frozen"] is None


def test_manual_contest_fields_win_over_meta():
    """MANUAL pins start_time, duration and freeze "0:00:00" explicitly —
    the meta freeze must NOT override it (precedence semantics; the real
    deployment keeps freeze off until manual/00-contest.json drops the key)."""
    data = translate("999", MANUAL, [], [], None, NOW, meta=META)
    c = data.contest
    assert c["start_time"] == "2023-05-21T09:00:00+08:00"
    assert c["duration"] == "5:00:00"
    assert c["scoreboard_freeze_duration"] == "0:00:00"
    assert data.state["frozen"] is None  # freeze 0 → never frozen


def test_meta_compilers_seed_languages_with_zero_submissions():
    manual = dict(MANUAL, languages={"cpp": {"name": "C++"}})
    data = translate("999", manual, [], [], None, NOW, meta=META)
    by_name = {l["name"] for l in data.languages.values()}
    assert data.languages["cpp"]["name"] == "C++"          # manual entry
    assert {"C++ (g++)", "Java (javac)", "仓颉 (cjc)"} <= by_name  # meta


def test_submissions_still_add_language_not_in_meta():
    rows = [{"id": "701", "submit_time": "2023/05/21 09:10:00",
             "status": "答案正确", "team_id": "D22", "team_name": "鸡你太美队",
             "problem_index": "A", "language": "Fortran95 (gfortran)"}]
    data = translate("999", MANUAL, rows, RAW_PROBLEMS, None, NOW, meta=META)
    by_name = {l["name"] for l in data.languages.values()}
    assert "Fortran95 (gfortran)" in by_name  # observed, not in meta compilers
    assert "C++ (g++)" in by_name              # meta still seeded
    assert data.submissions["701"]["language_id"] == \
        sanitize_id("Fortran95 (gfortran)")


def test_manual_language_entry_wins_over_meta():
    lid = sanitize_id("C++ (g++)")  # the id meta would seed for GXX
    manual = dict(MANUAL, languages={lid: {"name": "Manual C++"}})
    data = translate("999", manual, [], [], None, NOW, meta=META)
    assert data.languages[lid]["name"] == "Manual C++"


# --- judgements enrichment (run_time/score), task 13 ----------------------

def test_judgements_carry_max_run_time_and_score():
    rows = [dict(RAW_SUBMISSIONS[0], run_time=0.003, score=5.0)]
    data = translate("999", MANUAL, rows, RAW_PROBLEMS, None, NOW)
    jud = data.judgements["101"]
    assert jud["max_run_time"] == 0.003
    assert jud["score"] == 5.0


def test_judgements_missing_run_time_score_are_none():
    data = t()  # RAW_SUBMISSIONS carry neither key
    jud = data.judgements["101"]
    assert jud["max_run_time"] is None
    assert jud["score"] is None


# --- problem limits, task 13 ---------------------------------------------

def test_problem_limits_from_raw():
    raw = [dict(RAW_PROBLEMS[0], time_limit_ms=100, memory_limit_kb=256000,
                code_limit_kb=64)]
    data = translate("999", MANUAL, [], raw, None, NOW)
    p = data.problems["A"]
    assert p["time_limit"] == "0:00:00.100"  # ms preserved via reltime
    assert p["memory_limit"] == 256000
    assert p["code_limit"] == 64


def test_problem_limits_absent_when_raw_lacks_them():
    data = t()
    assert "time_limit" not in data.problems["A"]
    assert "memory_limit" not in data.problems["A"]
    assert "code_limit" not in data.problems["A"]


def test_manual_problem_limits_win_over_raw():
    manual = dict(MANUAL, problems={"A": {
        "color": "red", "rgb": "#f00",
        "time_limit": "0:00:02.000", "memory_limit": 1, "code_limit": 2}})
    raw = [dict(RAW_PROBLEMS[0], time_limit_ms=100, memory_limit_kb=256000,
                code_limit_kb=64)]
    data = translate("999", manual, [], raw, None, NOW)
    p = data.problems["A"]
    assert p["time_limit"] == "0:00:02.000"  # manual wins
    assert p["memory_limit"] == 1
    assert p["code_limit"] == 2


def test_problem_test_data_count_from_raw():
    raw = [dict(RAW_PROBLEMS[0], test_data_count=3)]
    data = translate("999", MANUAL, [], raw, None, NOW)
    assert data.problems["A"]["test_data_count"] == 3


def test_problem_test_data_count_absent_when_raw_lacks_it():
    data = t()  # RAW_PROBLEMS carry no test_data_count
    assert "test_data_count" not in data.problems["A"]


def test_manual_problem_test_data_count_wins_over_raw():
    manual = dict(MANUAL, problems={"A": {"test_data_count": 9}})
    raw = [dict(RAW_PROBLEMS[0], test_data_count=3)]
    data = translate("999", manual, [], raw, None, NOW)
    assert data.problems["A"]["test_data_count"] == 9


# --- per-submission detail: runs / files / sources, task 13 ---------------

def detail_row(sid: str = "801", *, program: str | None = "int main(){}",
               compiler: str = "GXX", runs: list[dict] | None = None) -> dict:
    return {"id": sid, "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
            "team_id": "D22", "team_name": "鸡你太美队", "problem_index": "A",
            "language": "C++ (g++)", "detail_fetched": True,
            "program": program, "compiler_enum": compiler,
            "run_results": runs if runs is not None else [
                {"case": "1", "result": "ACCEPTED", "time": 0.003,
                 "memory": 598016, "exitcode": 0},
                {"case": "data00", "result": "SKIPPED", "time": None,
                 "memory": None, "exitcode": None},
            ]}


def test_detail_row_adds_files_sources_and_runs():
    data = translate("999", MANUAL, [detail_row()], RAW_PROBLEMS, None, NOW)
    sub = data.submissions["801"]
    assert sub["files"] == [{
        "href": "contests/999/submissions/801/files/main.cpp",
        "filename": "main.cpp", "mime": "text/plain"}]
    assert data.sources["801"] == "int main(){}"
    # ACCEPTED case emitted; the SKIPPED "data00" case is omitted entirely
    # (a null judgement_type_id is what CDS rejects as "Invalid judgement
    # type null")
    assert list(data.runs) == ["801-1"]
    r1 = data.runs["801-1"]
    assert r1 == {
        "id": "801-1", "judgement_id": "801", "ordinal": 1,
        "judgement_type_id": "AC", "time": "2023-05-21T09:10:00+08:00",
        "contest_time": "0:10:00", "run_time": 0.003,
        "submission_id": "801", "team_id": "D22", "problem_id": "A",
        "memory": 584,  # 598016 bytes // 1024
        "exit_code": 0, "judge_time": "2023-05-21T09:10:00+08:00"}


def test_skipped_cases_never_emit_null_judgement_type():
    # all cases map to None (SKIPPED/WAITING) → zero runs, not runs with
    # judgement_type_id null
    runs = [
        {"case": "data00", "result": "SKIPPED", "time": None,
         "memory": None, "exitcode": None},
        {"case": "data01", "result": "WAITING", "time": None,
         "memory": None, "exitcode": None},
    ]
    data = translate("999", MANUAL, [detail_row(runs=runs)],
                     RAW_PROBLEMS, None, NOW)
    assert data.runs == {}


def test_run_judgement_type_and_ordinal_enumerate_emitted_cases():
    runs = [
        {"case": "1", "result": "ACCEPTED", "time": 0.003,
         "memory": 1024, "exitcode": 0},
        {"case": "data00", "result": "SKIPPED", "time": None,
         "memory": None, "exitcode": None},
        {"case": "2", "result": "WRONG_ANSWER", "time": 0.5,
         "memory": 2048, "exitcode": 1},
    ]
    data = translate("999", MANUAL, [detail_row(runs=runs)],
                     RAW_PROBLEMS, None, NOW)
    assert set(data.runs) == {"801-1", "801-2"}
    assert data.runs["801-1"]["judgement_type_id"] == "AC"
    assert data.runs["801-1"]["ordinal"] == 1
    assert data.runs["801-2"]["judgement_type_id"] == "WA"
    assert data.runs["801-2"]["ordinal"] == 2
    assert all(data.runs[k]["judgement_type_id"] for k in data.runs)


def test_detail_row_file_extension_by_compiler():
    cases = [("JAVAC", "main.java"), ("PYTHON3", "main.py"),
             ("GCC", "main.c"), ("RUBY", "main.txt")]
    for compiler, filename in cases:
        data = translate("999", MANUAL, [detail_row(compiler=compiler)],
                         RAW_PROBLEMS, None, NOW)
        assert data.submissions["801"]["files"][0]["filename"] == filename


def test_detail_row_without_program_has_sources_but_no_files():
    data = translate("999", MANUAL,
                     [detail_row(program=None)], RAW_PROBLEMS, None, NOW)
    assert data.sources["801"] == ""
    assert "files" not in data.submissions["801"]
    assert data.runs  # runs still built from run_results


def test_non_detail_row_has_no_files_sources_runs():
    data = t()  # plain RAW_SUBMISSIONS, no detail_fetched
    assert data.sources == {}
    assert data.runs == {}
    assert "files" not in data.submissions["101"]


def test_run_id_sanitizes_odd_case_keys():
    runs = [{"case": "data 00/../x", "result": "ACCEPTED", "time": 0.1,
             "memory": 1024, "exitcode": 0}]
    data = translate("999", MANUAL, [detail_row(runs=runs)],
                     RAW_PROBLEMS, None, NOW)
    rid = sanitize_id("801-data 00/../x")
    assert rid in data.runs
    assert data.runs[rid]["submission_id"] == "801"
    assert data.runs[rid]["memory"] == 1
