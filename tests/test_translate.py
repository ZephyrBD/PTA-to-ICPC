import datetime as dt

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
     "user": "D22 鸡你太美队", "problem_index": "A", "language": "cpp"},
    {"id": "102", "submit_time": "2023/05/21 09:11:00", "status": "正在评测",
     "user": "D23 另一队", "problem_index": "B", "language": "cpp"},
    {"id": "103", "submit_time": "2023/05/21 09:12:00", "status": "答案错误",
     "user": "D22 鸡你太美队", "problem_index": "B", "language": "cpp"},
    {"id": "104", "submit_time": "bad-time", "status": "答案正确",
     "user": "D24 脏队", "problem_index": "A", "language": "cpp"},
    {"id": "105", "submit_time": "2023/05/21 09:13:00", "status": "答案正确",
     "user": "无法解析的用户", "problem_index": "A", "language": "cpp"},
]


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


def test_team_reverse_derivation_and_manual_merge():
    data = t()
    t22 = data.teams["D22"]
    assert t22["label"] == "D22"
    assert t22["name"] == "鸡你太美队"
    assert t22["group_id"] == "D"
    assert t22["organization_id"] == "org1"
    assert t22["icpc_id"] == "2023001"
    assert data.teams["D23"]["name"] == "另一队"
    assert data.teams["D23"]["group_id"] == "D"
    assert data.groups["D"]["name"] == "4078"
    assert "D23" not in data.groups


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
    assert "105" not in data.submissions  # unparsable user skipped
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


def test_languages_collected_from_submissions():
    data = t()
    assert data.languages["cpp"]["name"] == "C++"


def test_translate_accepts_enum_status():
    rows = [
        {"id": "201", "submit_time": "2023/05/21 09:10:00", "status": "ACCEPTED",
         "user": "D22 鸡你太美队", "problem_index": "A", "language": "cpp"},
        {"id": "202", "submit_time": "2023/05/21 09:11:00", "status": "WRONG_ANSWER",
         "user": "D22 鸡你太美队", "problem_index": "A", "language": "cpp"},
        {"id": "203", "submit_time": "2023/05/21 09:12:00", "status": "JUDGING",
         "user": "D22 鸡你太美队", "problem_index": "A", "language": "cpp"},
    ]
    data = translate("999", MANUAL, rows, RAW_PROBLEMS, None, NOW)
    assert data.judgements["201"]["judgement_type_id"] == "AC"
    assert data.judgements["202"]["judgement_type_id"] == "WA"
    assert data.judgements["203"]["judgement_type_id"] is None
    assert data.judgements["203"]["end_time"] is None
