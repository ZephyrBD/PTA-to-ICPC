import datetime as dt

from ptapatch.scoreboard import compute_scoreboard
from ptapatch.translate import BUILTIN_JUDGEMENT_TYPES, ContestData

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2023, 5, 21, 10, 0, 0, tzinfo=TZ)


def make_data(rows=None) -> ContestData:
    data = ContestData()
    data.contest = {
        "id": "1", "start_time": "2023-05-21T09:00:00+08:00",
        "duration": "5:00:00", "penalty_time": "0:20:00",
        "scoreboard_type": "pass-fail", "scoreboard_freeze_duration": "0:00:00",
    }
    data.state = {
        "started": "2023-05-21T09:00:00+08:00", "ended": None, "frozen": None,
        "thawed": None, "finalized": None, "end_of_updates": None,
    }
    data.teams = {
        "D22": {"id": "D22", "group_ids": ["D"]},
        "D23": {"id": "D23", "group_ids": ["D"]},
        "E01": {"id": "E01", "group_ids": ["E"]},
    }
    data.problems = {"A": {"id": "A"}, "B": {"id": "B"}}
    data.judgement_types = {k: dict(v, id=k) for k, v in BUILTIN_JUDGEMENT_TYPES.items()}
    if rows is None:
        rows = [
            ("1", "D22", "A", "0:05:00", "AC"),
            ("2", "D22", "B", "0:10:00", "WA"),
            ("3", "D22", "B", "0:30:00", "AC"),
            ("4", "D23", "A", "0:07:00", "AC"),
            ("5", "E01", "A", "0:01:00", None),
        ]
    for sid, team, prob, ct, jtype in rows:
        data.submissions[sid] = {
            "id": sid, "team_id": team, "problem_id": prob,
            "contest_time": ct, "time": "2023-05-21T09:00:00+08:00",
        }
        data.judgements[sid] = {
            "id": sid, "submission_id": sid, "judgement_type_id": jtype,
        }
    return data


def test_solved_and_penalty():
    sb = compute_scoreboard(make_data(), NOW)
    rows = {r["team_id"]: r for r in sb["rows"]}
    d22 = rows["D22"]
    assert d22["score"]["num_solved"] == 2
    # A solved at 5min; B solved at 30min + 1 penalty * 20min = 50min; total 55min
    assert d22["score"]["total_time"] == "0:55:00"


def test_pending_counted():
    sb = compute_scoreboard(make_data(), NOW)
    rows = {r["team_id"]: r for r in sb["rows"]}
    e01 = rows["E01"]
    assert e01["score"]["num_solved"] == 0
    pa = next(p for p in e01["problems"] if p["problem_id"] == "A")
    assert pa["num_pending"] == 1
    assert pa["solved"] is False


def test_ce_does_not_penalize():
    data = make_data([
        ("1", "D22", "A", "0:10:00", "CE"),
        ("2", "D22", "A", "0:30:00", "AC"),
    ])
    sb = compute_scoreboard(data, NOW)
    d22 = next(r for r in sb["rows"] if r["team_id"] == "D22")
    pa = next(p for p in d22["problems"] if p["problem_id"] == "A")
    assert pa["num_judged"] == 2
    assert pa["solved"] is True
    assert d22["score"]["num_solved"] == 1
    # CE has penalty: False, so total is just the AC time (not +20min).
    assert d22["score"]["total_time"] == "0:30:00"


def test_pa_penalizes_until_ac():
    data = make_data([
        ("1", "D22", "A", "0:10:00", "PA"),
        ("2", "D22", "A", "0:20:00", "AC"),
    ])
    sb = compute_scoreboard(data, NOW)
    d22 = next(r for r in sb["rows"] if r["team_id"] == "D22")
    cell = next(p for p in d22["problems"] if p["problem_id"] == "A")
    assert cell["num_judged"] == 2
    assert cell["solved"] is True
    # PA is penalty-eligible: AC at 0:20 + 1 * 0:20 penalty = 0:40
    assert d22["score"]["total_time"] == "0:40:00"


def test_tie_teams_share_rank_and_next_skips():
    data = make_data([
        ("1", "D22", "A", "0:05:00", "AC"),
        ("2", "D23", "A", "0:05:00", "AC"),
        ("3", "E01", "A", "0:07:00", "AC"),
    ])
    sb = compute_scoreboard(data, NOW)
    ranked = [(r["rank"], r["team_id"]) for r in sb["rows"]]
    assert dict((t, rk) for rk, t in ranked)["D22"] == 1
    assert dict((t, rk) for rk, t in ranked)["D23"] == 1
    assert dict((t, rk) for rk, t in ranked)["E01"] == 3


def test_rank_order_and_ties():
    sb = compute_scoreboard(make_data(), NOW)
    ranked = [(r["rank"], r["team_id"]) for r in sb["rows"]]
    assert ranked[0][1] == "D22"  # 2 solved beats 1 solved
    assert ranked[1][1] == "D23"
    assert ranked[2][1] == "E01"


def test_group_filter():
    sb = compute_scoreboard(make_data(), NOW, group_id="D")
    assert {r["team_id"] for r in sb["rows"]} == {"D22", "D23"}


def test_group_filter_by_membership_multi_group():
    data = make_data()
    # D22 belongs to two groups; membership (not equality) drives the filter
    data.teams["D22"]["group_ids"] = ["D", "official"]
    data.teams["E01"]["group_ids"] = []  # no group → excluded from any filter
    assert {r["team_id"] for r in
            compute_scoreboard(data, NOW, group_id="official")["rows"]} == {"D22"}
    assert {r["team_id"] for r in
            compute_scoreboard(data, NOW, group_id="D")["rows"]} == {"D22", "D23"}
    assert compute_scoreboard(data, NOW, group_id="nope")["rows"] == []


def test_output_shape():
    sb = compute_scoreboard(make_data(), NOW)
    assert sb["state"] == make_data().state
    assert sb["time"] == "2023-05-21T10:00:00+08:00"
    assert sb["contest_time"] == "1:00:00"
    for r in sb["rows"]:
        assert set(r) == {"rank", "team_id", "score", "problems"}
        assert set(r["score"]) == {"num_solved", "total_time"}
