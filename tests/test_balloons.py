import asyncio
import datetime as dt
import json
import logging

from pta2ccs.balloons import (BalloonState, _to_epoch, build_balloon,
                              build_contestproblem, build_domjudge_contest,
                              build_domjudge_org, build_domjudge_team,
                              first_ac_pairs, time_sort_key)
from pta2ccs.translate import ContestData, translate

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2023, 5, 21, 10, 0, 0, tzinfo=TZ)
MANUAL = {"contest": {"name": "省赛", "formal_name": "省赛 2023",
                      "start_time": "2023-05-21T09:00:00+08:00",
                      "duration": "5:00:00", "pta_timezone": "+08:00"},
          "groups": {"1": {"name": "正式"}},
          "teams": {"D22": {"group_ids": ["1"], "seat": "A12"}}}
SUBS = [
    {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
     "team_id": "D22", "team_name": "一队", "problem_index": "A", "language": "cpp"},
    {"id": "2", "submit_time": "2023/05/21 09:15:00", "status": "答案正确",
     "team_id": "D22", "team_name": "一队", "problem_index": "B", "language": "cpp"},
    {"id": "3", "submit_time": "2023/05/21 09:05:00", "status": "答案错误",
     "team_id": "D23", "team_name": "二队", "problem_index": "A", "language": "cpp"},
    {"id": "4", "submit_time": "2023/05/21 09:25:00", "status": "答案正确",
     "team_id": "D23", "team_name": "二队", "problem_index": "A", "language": "cpp"},
    {"id": "5", "submit_time": "2023/05/21 09:30:00", "status": "正在评测",
     "team_id": "D24", "team_name": "三队", "problem_index": "A", "language": "cpp"},
    {"id": "6", "submit_time": "2023/05/21 09:35:00", "status": "答案正确",
     "team_id": "D25", "team_name": "四队", "problem_index": "A", "language": "cpp"},
    {"id": "7", "submit_time": "2023/05/21 09:40:00", "status": "答案正确",
     "team_id": "D26", "team_name": "", "problem_index": "B", "language": "cpp"},
]
ROSTER = [
    {"id": "D22", "name": "一队", "label": "2022000001",
     "group_id": "org1", "organization_id": "org1", "organization_name": "某大学"},
    {"id": "D23", "name": "二队", "label": "2022000002",
     "group_id": "org1", "organization_id": "org1", "organization_name": "某大学"},
]
PROBLEMS = [
    {"id": "A", "title": "题A", "time_limit_ms": 100},
    {"id": "B", "title": "题B"},
]


def make_data(now=NOW) -> ContestData:
    return translate("c1", MANUAL, SUBS, PROBLEMS, ROSTER, now)


# --- first_ac_pairs ---------------------------------------------------------


def test_first_ac_pairs():
    pairs = first_ac_pairs(make_data())
    assert pairs[("D22", "A")] == {"time": "2023-05-21T09:10:00+08:00"}
    assert pairs[("D22", "B")] == {"time": "2023-05-21T09:15:00+08:00"}
    # the 09:05 WA does not count; the first AC is 09:25
    assert pairs[("D23", "A")] == {"time": "2023-05-21T09:25:00+08:00"}
    assert pairs[("D25", "A")] == {"time": "2023-05-21T09:35:00+08:00"}
    assert pairs[("D26", "B")] == {"time": "2023-05-21T09:40:00+08:00"}
    # pending judgement (正在评测) is never an AC
    assert ("D24", "A") not in pairs


def test_first_ac_pairs_per_problem_independent():
    data = make_data()
    pairs = first_ac_pairs(data)
    # one team, two problems, each with its own first AC
    assert set(pairs) == {("D22", "A"), ("D22", "B"), ("D23", "A"),
                          ("D25", "A"), ("D26", "B")}
    # a second solved submission for the same pair is ignored (first wins)
    subs = SUBS + [{"id": "8", "submit_time": "2023/05/21 09:50:00",
                    "status": "答案正确", "team_id": "D22",
                    "team_name": "一队", "problem_index": "A", "language": "cpp"}]
    pairs = first_ac_pairs(translate("c1", MANUAL, subs, PROBLEMS, ROSTER, NOW))
    assert pairs[("D22", "A")] == {"time": "2023-05-21T09:10:00+08:00"}


# --- build_contestproblem ---------------------------------------------------


def test_build_contestproblem():
    data = make_data()
    cp = build_contestproblem(data, "A")
    assert set(cp) == {"id", "label", "name", "short_name", "rgb", "color",
                       "time_limit", "statement", "attachments"}
    assert cp["id"] == "A"
    assert cp["label"] == "A"
    assert cp["short_name"] == "A"  # short_name == label
    assert cp["name"] == "题A"
    assert cp["time_limit"] == 100  # reltime "0:00:00.100" → integer ms
    assert cp["statement"] == []
    assert cp["attachments"] == []
    # no time_limit → null
    assert build_contestproblem(data, "B")["time_limit"] is None
    # unknown problem → None
    assert build_contestproblem(data, "ZZZ") is None


# --- build_balloon ----------------------------------------------------------


def make_total_map(data: ContestData) -> dict[str, dict]:
    total: dict[str, dict] = {}
    for (team_id, problem_id), _info in first_ac_pairs(data).items():
        cp = build_contestproblem(data, problem_id)
        if cp is None:
            continue
        total.setdefault(team_id, {})[cp["label"]] = cp
    return total


def test_build_balloon():
    data = make_data()
    total = make_total_map(data)
    row = build_balloon(data,
                        {"team_id": "D22", "problem_id": "A",
                         "time": "2023-05-21T09:10:00+08:00"},
                        1, False, total)
    assert row["balloonid"] == 1
    assert row["time"] == _to_epoch("2023-05-21T09:10:00+08:00")
    assert isinstance(row["time"], int) and not isinstance(row["time"], bool)
    assert row["problem"] == "A"
    assert row["contestproblem"]["id"] == "A"
    assert row["team"] == "D22: 一队"
    assert row["teamid"] == "D22"
    assert row["location"] == "A12"  # manual seat → label
    assert row["affiliation"] == "某大学"
    assert row["affiliationid"] == "org1"
    assert row["category"] == "正式"
    assert row["categoryid"] == "1"
    # total = the team's full first-AC set, keys sorted by label
    assert list(row["total"]) == ["A", "B"]
    assert row["total"]["B"]["short_name"] == "B"
    assert row["done"] is False


def test_build_balloon_derives_time_from_data():
    data = make_data()
    row = build_balloon(data, {"team_id": "D22", "problem_id": "A"},
                        1, False, make_total_map(data))
    assert row["time"] == _to_epoch("2023-05-21T09:10:00+08:00")


def test_build_balloon_epoch_consistent_with_iso_origin():
    # numeric epoch seconds must agree with the ISO first-AC time
    data = make_data()
    row = build_balloon(data, {"team_id": "D22", "problem_id": "A"},
                        1, False, make_total_map(data))
    iso = first_ac_pairs(data)[("D22", "A")]["time"]
    assert row["time"] == int(dt.datetime.fromisoformat(iso).timestamp())


def test_build_balloon_missing_org_group_and_name():
    data = make_data()
    total = make_total_map(data)
    # D25: derived team with no org/group/manual seat → nulls + id fallbacks
    row = build_balloon(data,
                        {"team_id": "D25", "problem_id": "A",
                         "time": "2023-05-21T09:35:00+08:00"},
                        2, True, total)
    assert row["location"] == "D25"  # label falls back to team id
    assert row["affiliation"] is None
    assert row["affiliationid"] is None
    assert row["category"] is None
    assert row["categoryid"] is None
    assert row["done"] is True
    # D26 has an empty team_name → team id fallback
    row = build_balloon(data,
                        {"team_id": "D26", "problem_id": "B",
                         "time": "2023-05-21T09:40:00+08:00"},
                        3, False, total)
    assert row["team"] == "D26: D26"


# --- build_domjudge_contest -------------------------------------------------


def test_build_domjudge_contest():
    c = build_domjudge_contest(make_data())
    assert c["id"] == "c1"
    assert c["name"] == "省赛"
    assert c["formal_name"] == "省赛 2023"
    assert c["shortname"] == c["name"]  # shortname == name
    assert c["start_time"] == "2023-05-21T09:00:00+08:00"
    # end_time = start + duration even while the contest is running
    # (NOW 10:00, planned end 14:00) — never gated on state.ended
    assert c["end_time"] == "2023-05-21T14:00:00+08:00"
    assert c["duration"] == "5:00:00"
    assert c["scoreboard_thaw_time"] is None
    assert c["banner"] == []
    assert c["problemset"] == []
    assert c["allow_submit"] is True
    assert c["runtime_as_score_tiebreaker"] is False
    assert c["warning_message"] == ""


def test_build_domjudge_contest_ended():
    data = make_data(now=dt.datetime(2023, 5, 21, 15, 0, 0, tzinfo=TZ))
    assert build_domjudge_contest(data)["end_time"] == "2023-05-21T14:00:00+08:00"


def test_build_domjudge_contest_missing_or_bad_start_end_time_none():
    manual = {"contest": {"name": "省赛", "duration": "5:00:00",
                          "pta_timezone": "+08:00"}}
    data = translate("c1", manual, SUBS, PROBLEMS, ROSTER, NOW)
    assert data.contest["start_time"] is None
    assert build_domjudge_contest(data)["end_time"] is None
    # unparseable start_time (injected post-translate; translate itself
    # rejects it in _build_state) → end_time None, never an exception
    data = make_data()
    data.contest["start_time"] = "not-a-time"
    assert build_domjudge_contest(data)["end_time"] is None
    # missing duration (parse_reltime(None) would AttributeError) → None
    data = make_data()
    data.contest["start_time"] = "2023-05-21T09:00:00+08:00"
    data.contest["duration"] = None
    assert build_domjudge_contest(data)["end_time"] is None


# --- _to_epoch --------------------------------------------------------------


def test_to_epoch():
    assert _to_epoch("2023-05-21T09:10:00+08:00") == int(
        dt.datetime.fromisoformat("2023-05-21T09:10:00+08:00").timestamp())
    assert _to_epoch("2023-05-21T01:10:00+00:00") == \
        _to_epoch("2023-05-21T09:10:00+08:00")  # same instant
    # unparseable / missing → 0 (defensive, never an exception)
    assert _to_epoch(None) == 0
    assert _to_epoch("garbage") == 0
    assert _to_epoch("") == 0
    # naive (tz-less) input is ambiguous → 0, never host-local interpretation
    assert _to_epoch("2023-05-21T09:10:00") == 0


def test_time_sort_key_accepts_epoch_and_iso():
    iso = "2023-05-21T09:10:00+08:00"
    epoch = _to_epoch(iso)
    # both branches yield the same instant (aware UTC)
    assert time_sort_key(iso) == time_sort_key(epoch)
    # epoch ordering matches ISO ordering
    later_iso = "2023-05-21T10:00:00+08:00"
    assert time_sort_key(epoch) < time_sort_key(later_iso)
    # naive ISO is not mixed into a comparison with aware values
    assert time_sort_key("2023-05-21T09:10:00") == dt.datetime.min.replace(
        tzinfo=dt.timezone.utc)
    assert time_sort_key("garbage") == dt.datetime.min.replace(
        tzinfo=dt.timezone.utc)


# --- build_domjudge_team / build_domjudge_org -------------------------------


def persons_manual() -> dict:
    return {"contest": dict(MANUAL["contest"]),
            "teams": {"D22": {"members": ["甲", "乙"], "coaches": ["丙"]}}}


def test_build_domjudge_team():
    data = translate("c1", persons_manual(), SUBS, PROBLEMS, ROSTER, NOW)
    team = build_domjudge_team(data, "D22")
    assert team["id"] == team["externalid"] == "D22"
    assert team["name"] == team["display_name"] == "一队"
    assert team["organization_id"] == team["affiliation_id"] == "org1"
    assert team["group_ids"] == []
    assert team["label"] == team["location"] == "2022000001"  # roster label
    assert team["icpc_id"] is None
    assert team["public_description"] == "队员：甲、乙；教练：丙"
    assert team["hidden"] is False


def test_build_domjudge_team_public_description_none_without_persons():
    data = make_data()  # no persons in MANUAL
    team = build_domjudge_team(data, "D22")
    assert team["public_description"] is None
    # only members → only the 队员 section; only coaches → only 教练 section
    manual = {"contest": dict(MANUAL["contest"]),
              "teams": {"D22": {"members": ["甲"]}}}
    data = translate("c1", manual, SUBS, PROBLEMS, ROSTER, NOW)
    assert build_domjudge_team(data, "D22")["public_description"] == "队员：甲"
    manual = {"contest": dict(MANUAL["contest"]),
              "teams": {"D22": {"coaches": ["丙"]}}}
    data = translate("c1", manual, SUBS, PROBLEMS, ROSTER, NOW)
    assert build_domjudge_team(data, "D22")["public_description"] == "教练：丙"


def test_build_domjudge_team_unknown_team_id_falls_back():
    data = make_data()
    team = build_domjudge_team(data, "ZZZ")
    assert team["id"] == team["externalid"] == "ZZZ"
    assert team["name"] == team["display_name"] == "ZZZ"
    assert team["organization_id"] is None
    assert team["label"] is None
    assert team["public_description"] is None


def test_build_domjudge_org():
    data = make_data()
    org = build_domjudge_org(data, "org1")
    assert org["id"] == org["externalid"] == "org1"
    assert org["name"] == org["formal_name"] == org["shortname"] == "某大学"
    assert org["country"] is None
    assert org["country_flag"] == []
    assert org["logo"] == []
    assert org["affiliation_id"] == "org1"
    assert org["icpc_id"] is None
    # unknown org → id fallback, never an exception
    org = build_domjudge_org(data, "nope")
    assert org["name"] == "nope"


# --- BalloonState -----------------------------------------------------------


def test_sync_mints_in_submission_time_order(tmp_path):
    state = BalloonState(tmp_path / "balloons.json")

    async def go():
        await state.sync(make_data())
        return await state.snapshot_entries()

    entries = asyncio.run(go())
    order = [(e["team_id"], e["problem_id"]) for e in entries.values()]
    assert order == [("D22", "A"), ("D22", "B"), ("D23", "A"),
                     ("D25", "A"), ("D26", "B")]  # 09:10/09:15/09:25/09:35/09:40
    for e in entries.values():
        assert e["done"] is False
    assert (tmp_path / "balloons.json").exists()


def test_sync_idempotent_no_resync(tmp_path):
    path = tmp_path / "balloons.json"
    state = BalloonState(path)

    async def go():
        data = make_data()
        await state.sync(data)
        first = path.read_text(encoding="utf-8")
        await state.sync(data)  # no change → no write, no re-number
        return first, await state.snapshot_entries()

    first, entries = asyncio.run(go())
    assert path.read_text(encoding="utf-8") == first
    assert sorted(entries) == [1, 2, 3, 4, 5]


def test_sync_prunes_lost_first_ac(tmp_path):
    path = tmp_path / "balloons.json"
    state = BalloonState(path)

    async def go():
        await state.sync(make_data())
        # drop D25's AC → ("D25", "A") is no longer first-AC
        subs = [s for s in SUBS if s["id"] != "6"]
        data2 = translate("c1", MANUAL, subs, PROBLEMS, ROSTER, NOW)
        await state.sync(data2)
        return await state.snapshot_entries()

    entries = asyncio.run(go())
    assert set(entries) == {1, 2, 3, 5}  # 4 (D25-A) pruned


def test_done_undone_persist_across_reload(tmp_path):
    path = tmp_path / "balloons.json"
    state = BalloonState(path)

    async def go():
        await state.sync(make_data())
        assert await state.set_done(1, True) is True
        assert await state.set_done(2, False) is True
        assert await state.set_done(999, True) is False  # unknown id
        await state.set_done(3, True)
        await state.set_done(3, False)  # undone
        return await BalloonState(path).snapshot_entries()

    entries = asyncio.run(go())
    assert entries[1]["done"] is True
    assert entries[2]["done"] is False
    assert entries[3]["done"] is False  # undone persisted


def test_next_id_continues_after_reload(tmp_path):
    path = tmp_path / "balloons.json"
    state = BalloonState(path)
    asyncio.run(state.sync(make_data()))
    reloaded = BalloonState(path)

    async def go():
        subs = SUBS + [{"id": "8", "submit_time": "2023/05/21 09:45:00",
                        "status": "答案正确", "team_id": "D27",
                        "team_name": "五队", "problem_index": "A", "language": "cpp"}]
        await reloaded.sync(translate("c1", MANUAL, subs, PROBLEMS, ROSTER, NOW))
        return await reloaded.snapshot_entries()

    entries = asyncio.run(go())
    assert entries[6]["team_id"] == "D27" and entries[6]["problem_id"] == "A"


def test_missing_file_starts_empty(tmp_path):
    state = BalloonState(tmp_path / "nope.json")
    assert asyncio.run(state.snapshot_entries()) == {}


def test_corrupt_file_starts_empty(tmp_path, caplog):
    path = tmp_path / "balloons.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pta2ccs.balloons"):
        state = BalloonState(path)
    assert asyncio.run(state.snapshot_entries()) == {}
    assert any("starts empty" in rec.message for rec in caplog.records)


def test_wrong_shape_starts_empty(tmp_path, caplog):
    path = tmp_path / "balloons.json"
    path.write_text(json.dumps({"next_id": "x"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pta2ccs.balloons"):
        state = BalloonState(path)
    assert asyncio.run(state.snapshot_entries()) == {}
    assert any("starts empty" in rec.message for rec in caplog.records)


def test_next_id_clamped_above_existing_entries(tmp_path):
    """A loaded next_id ≤ max entry id must not collide: new balloons are
    minted strictly above every existing id (review F3)."""
    path = tmp_path / "balloons.json"
    path.write_text(json.dumps({
        "next_id": 1,
        "entries": {"10": {"team_id": "D22", "problem_id": "A",
                           "done": False}},
    }), encoding="utf-8")
    state = BalloonState(path)

    async def go():
        subs = SUBS + [{"id": "8", "submit_time": "2023/05/21 09:50:00",
                        "status": "答案正确", "team_id": "D27",
                        "team_name": "五队", "problem_index": "A",
                        "language": "cpp"}]
        await state.sync(translate("c1", MANUAL, subs, PROBLEMS, ROSTER, NOW))
        return await state.snapshot_entries()

    entries = asyncio.run(go())
    # next minted id after the pre-existing 10 must be 11, never 10/1
    assert entries[11]["problem_id"] == "B"       # D22-B is the first missing
    assert entries[15]["team_id"] == "D27"         # newest minted, > 10


def test_duplicate_pair_file_keeps_first(tmp_path):
    """A hand-edited/corrupt file with two balloonids for the same
    (team, problem) keeps only the first at load — never rendered twice
    (review F1)."""
    path = tmp_path / "balloons.json"
    path.write_text(json.dumps({
        "next_id": 3,
        "entries": {
            "1": {"team_id": "D22", "problem_id": "A", "done": False},
            "2": {"team_id": "D22", "problem_id": "A", "done": True},
        },
    }), encoding="utf-8")
    state = BalloonState(path)
    entries = asyncio.run(state.snapshot_entries())
    assert list(entries) == [1]
    assert entries[1]["done"] is False


def test_sync_skips_unresolvable_problem(tmp_path):
    """A first AC for a problem absent from data.problems (roster/problem
    list lag) is not minted — no phantom entry consuming an id (review F2)."""
    path = tmp_path / "balloons.json"
    state = BalloonState(path)

    async def go():
        subs = SUBS + [{"id": "8", "submit_time": "2023/05/21 09:50:00",
                        "status": "答案正确", "team_id": "D27",
                        "team_name": "五队", "problem_index": "ZZZ",
                        "language": "cpp"}]
        data = translate("c1", MANUAL, subs, PROBLEMS, ROSTER, NOW)
        await state.sync(data)
        return data, await state.snapshot_entries()

    data, entries = asyncio.run(go())
    assert ("D27", "ZZZ") in first_ac_pairs(data)  # derives, but…
    assert not any(e["problem_id"] == "ZZZ" for e in entries.values())
