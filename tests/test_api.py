import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from pta2ccs.app import _make_poller, create_app
from pta2ccs.balloons import BalloonState
from pta2ccs.config import Settings
from pta2ccs.store import Store
from pta2ccs.translate import translate

NOW = dt.datetime(2023, 5, 21, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
MANUAL = {"contest": {"name": "省赛", "start_time": "2023-05-21T09:00:00+08:00",
                      "duration": "5:00:00", "pta_timezone": "+08:00"}}
SUBS = [
    {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
     "team_id": "D22", "team_name": "队", "problem_index": "A", "language": "cpp"},
    {"id": "2", "submit_time": "2023/05/21 09:11:00", "status": "答案错误",
     "team_id": "D23", "team_name": "二队", "problem_index": "A", "language": "cpp"},
]


def make_client() -> TestClient:
    store = Store()
    store.apply_all(translate("c1", MANUAL, SUBS, [{"id": "A", "title": "题A"}], None, NOW))
    app = create_app(store, poller=None, contest_id="c1")
    return TestClient(app)


def make_detail_client() -> TestClient:
    """Store seeded from a detail-enriched row: files href + sources +
    one run (task 13)."""
    detail_subs = [dict(SUBS[0], detail_fetched=True,
                        program="#include <iostream>\nint main(){}\n",
                        compiler_enum="GXX",
                        run_results=[{"case": "1", "result": "ACCEPTED",
                                      "time": 0.003, "memory": 2048,
                                      "exitcode": 0}]),
                   SUBS[1]]  # second row without detail
    store = Store()
    store.apply_all(translate("c1", MANUAL, detail_subs,
                              [{"id": "A", "title": "题A"}], None, NOW))
    return TestClient(create_app(store, poller=None, contest_id="c1"))


def test_api_info():
    r = make_client().get("/api/v4/")
    assert r.status_code == 200
    assert r.json()["version"] == "2026-01"


def test_access():
    r = make_client().get("/api/v4/contests/c1/access")
    assert r.status_code == 200
    body = r.json()
    assert body["capabilities"] == []
    types = {e["type"] for e in body["endpoints"]}
    assert "submissions" in types
    assert "scoreboard" in types
    assert "runs" in types
    assert "contest" in types and "contests" not in types
    by_type = {e["type"]: e["properties"] for e in body["endpoints"]}
    assert "files" in by_type["submissions"]
    assert {"max_run_time", "score"} <= set(by_type["judgements"])
    assert {"time_limit", "memory_limit", "code_limit", "test_data_count"} <= \
        set(by_type["problems"])
    assert by_type["runs"] == [
        "id", "judgement_id", "ordinal", "judgement_type_id", "time",
        "contest_time", "submission_id", "team_id", "problem_id", "run_time",
        "memory", "exit_code", "judge_time"]
    assert "group_ids" in by_type["teams"]
    assert "group_id" not in by_type["teams"]
    # DOMjudge superset properties advertised on the shared collections
    assert {"externalid", "display_name", "affiliation_id",
            "public_description", "hidden", "location"} <= \
        set(by_type["teams"])
    assert {"externalid", "formal_name", "shortname", "country",
            "country_flag", "logo", "affiliation_id", "icpc_id"} <= \
        set(by_type["organizations"])


def test_collections_and_items():
    c = make_client()
    r = c.get("/api/v4/contests/c1/submissions")
    assert r.status_code == 200
    assert {s["id"] for s in r.json()} == {"1", "2"}
    r = c.get("/api/v4/contests/c1/submissions/1")
    assert r.status_code == 200
    assert r.json()["team_id"] == "D22"
    r = c.get("/api/v4/contests/c1/judgement-types/AC")
    assert r.status_code == 200
    assert r.json()["solved"] is True


def test_filtering_by_team_id():
    r = make_client().get("/api/v4/contests/c1/submissions?team_id=D22")
    assert [s["id"] for s in r.json()] == ["1"]


def test_runs_collection_item_and_filter():
    c = make_detail_client()
    r = c.get("/api/v4/contests/c1/runs")
    assert r.status_code == 200
    runs = r.json()
    assert [x["id"] for x in runs] == ["1-1"]
    assert runs[0]["submission_id"] == "1"
    assert runs[0]["team_id"] == "D22"
    assert runs[0]["problem_id"] == "A"
    assert runs[0]["run_time"] == 0.003
    assert runs[0]["memory"] == 2  # 2048 bytes → 2 KB
    assert runs[0]["exit_code"] == 0
    # spec-required run fields present and non-null
    assert runs[0]["judgement_type_id"] == "AC"
    assert runs[0]["ordinal"] == 1
    assert runs[0]["time"] == "2023-05-21T09:10:00+08:00"
    assert runs[0]["contest_time"] == "0:10:00"
    r = c.get("/api/v4/contests/c1/runs/1-1")
    assert r.status_code == 200
    assert r.json()["judgement_id"] == "1"
    r = c.get("/api/v4/contests/c1/runs?team_id=D22")
    assert [x["id"] for x in r.json()] == ["1-1"]
    r = c.get("/api/v4/contests/c1/runs?team_id=ZZ")
    assert r.json() == []
    r = c.get("/api/v4/contests/c1/runs/nope")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "run not found: nope"}


def test_submission_file_route():
    c = make_detail_client()
    r = c.get("/api/v4/contests/c1/submissions/1/files/main.cpp")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "#include <iostream>\nint main(){}\n"
    # submissions without detail → file 404 (no sources entry)
    r = c.get("/api/v4/contests/c1/submissions/2/files/main.cpp")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "file not found: main.cpp"}
    # unknown submission → submission 404
    r = c.get("/api/v4/contests/c1/submissions/nope/files/main.cpp")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "submission not found: nope"}
    # submissions JSON advertises the files href
    sub = c.get("/api/v4/contests/c1/submissions/1").json()
    assert sub["files"][0]["href"] == "contests/c1/submissions/1/files/main.cpp"
    assert "files" not in c.get("/api/v4/contests/c1/submissions/2").json()


def test_teams_route_shadows_catch_all_and_ignores_query():
    # the explicit /teams route (DOMjudge superset) shadows the Contest API
    # collection catch-all: query params no longer filter, all teams return
    manual = {"contest": dict(MANUAL["contest"]),
              "teams": {"D22": {"organization_id": "org1"}}}
    store = Store()
    store.apply_all(translate("c1", manual, SUBS, [{"id": "A", "title": "题A"}],
                              None, NOW))
    c = TestClient(create_app(store, poller=None, contest_id="c1"))
    r = c.get("/api/v4/contests/c1/teams?organization_id=")
    assert r.status_code == 200
    teams = r.json()
    assert [t["id"] for t in teams] == ["D22", "D23"]  # sorted, not filtered
    assert teams[0]["organization_id"] == "org1"
    assert teams[1]["organization_id"] is None


def test_scoreboard_and_group_filter():
    c = make_client()
    r = c.get("/api/v4/contests/c1/scoreboard")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows[0]["team_id"] == "D22"
    r = c.get("/api/v4/contests/c1/scoreboard?group_id=ZZ")
    assert r.json()["rows"] == []


def test_contest_and_state():
    c = make_client()
    assert c.get("/api/v4/contests/c1").json()["name"] == "省赛"
    assert c.get("/api/v4/contests/c1/state").json()["started"] is not None


def test_404_unknown_id_and_path():
    c = make_client()
    r = c.get("/api/v4/contests/c1/submissions/nope")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "submission not found: nope"}
    r = c.get("/api/v4/contests/c1/unknown-collection")
    assert r.status_code == 404
    assert r.json()["code"] == 404
    r = c.get("/api/v4/contests/other/submissions")
    assert r.status_code == 404


def test_read_routes_404_on_empty_store():
    # empty Store: snapshot() is None; read routes must 404, not AttributeError-500
    c = TestClient(create_app(Store(), poller=None, contest_id="c1"))
    for path in ("/api/v4/contests/c1/state", "/api/v4/contests/c1/submissions"):
        r = c.get(path)
        assert r.status_code == 404
        assert r.json() == {"code": 404, "message": "contest data not ready"}


def test_healthz_without_poller():
    r = make_client().get("/healthz")
    assert r.status_code == 200
    assert r.json()["ready"] is True  # store has data


def test_default_poller_client_gets_manual_timezone():
    # Fix A: tz must reach PtaClient (its default +08:00 would mis-convert
    # UTC submitAt for any other contest timezone)
    settings = Settings()
    manual = {"contest": {"pta_timezone": "+09:00"}}
    poller, tz = _make_poller(settings, {"contest_id": "c1",
                                         "pta_cookie": "cookie"},
                              manual, Store())
    assert tz == "+09:00"
    assert poller.client._tz == "+09:00"


class _LifespanFakeClient:
    def __init__(self, closable: bool):
        self.closed = False
        if closable:
            self.aclose = self._aclose

    async def _aclose(self):
        self.closed = True

    async def fetch_submissions(self):
        return []

    async def fetch_problems(self):
        return []

    async def fetch_teams(self):
        return []


def _make_lifespan_app(closable: bool):
    from pta2ccs.poller import Poller
    client = _LifespanFakeClient(closable)
    settings = Settings(poll_interval_seconds=0.01, config_poll_seconds=0.01)
    poller = Poller(client, Store(), settings, {"contest": {}},
                    contest_id="c1")
    app = create_app(Store(), poller=poller, contest_id="c1")
    return app, client


def test_lifespan_closes_poller_client():
    app, client = _make_lifespan_app(closable=True)
    with TestClient(app):
        assert client.closed is False
    assert client.closed is True


def test_lifespan_tolerates_client_without_aclose():
    app, client = _make_lifespan_app(closable=False)
    with TestClient(app):
        pass  # shutdown must not raise AttributeError on missing aclose


# --- rate limiting surface (task 14) ---------------------------------------

class _StubPoller:
    """Minimal poller stand-in for /healthz spread testing (no PTA I/O)."""

    def __init__(self):
        self.status = {"ready": False, "last_success": None, "cookie_ok": True,
                       "consecutive_failures": 0,
                       "rate_limited_until": "2026-10-14T12:00:00+00:00"}
        self._stop = False

    async def run(self):
        while not self._stop:
            await asyncio.sleep(0.01)

    def stop(self):
        self._stop = True


def test_healthz_spreads_poller_rate_limited_until():
    # healthz spreads poller.status, so rate_limited_until appears with
    # no healthz-specific code (task 14)
    with TestClient(create_app(Store(), poller=_StubPoller(),
                               contest_id="c1")) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["rate_limited_until"] == "2026-10-14T12:00:00+00:00"


def test_default_poller_client_gets_rate_limit_settings():
    settings = Settings(pta_requests_per_second=5.0)
    poller, _tz = _make_poller(settings, {"contest_id": "c1",
                                          "pta_cookie": "cookie"},
                               {"contest": {}}, Store())
    assert poller.client._min_interval == 1.0 / 5.0


# --- persons collection (task: 扮演 DOMjudge) -------------------------------

def make_persons_client() -> TestClient:
    manual = {"contest": dict(MANUAL["contest"]),
              "teams": {"D22": {"members": ["甲", "乙"], "coaches": ["丙"]}}}
    store = Store()
    store.apply_all(translate("c1", manual, SUBS,
                              [{"id": "A", "title": "题A"}], None, NOW))
    return TestClient(create_app(store, poller=None, contest_id="c1"))


def test_persons_collection_and_item():
    c = make_persons_client()
    r = c.get("/api/v4/contests/c1/persons")
    assert r.status_code == 200
    persons = {p["id"]: p for p in r.json()}
    assert set(persons) == {"D22-m0", "D22-m1", "D22-c0"}
    assert persons["D22-m0"] == {"id": "D22-m0", "name": "甲",
                                 "role": "contestant", "team_ids": ["D22"]}
    assert persons["D22-c0"]["role"] == "coach"
    r = c.get("/api/v4/contests/c1/persons/D22-m1")
    assert r.status_code == 200
    assert r.json()["name"] == "乙"
    assert r.json()["team_ids"] == ["D22"]


def test_persons_404_unknown_id():
    r = make_persons_client().get("/api/v4/contests/c1/persons/nope")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "person not found: nope"}


def test_access_advertises_persons_endpoint_with_properties():
    body = make_client().get("/api/v4/contests/c1/access").json()
    by_type = {e["type"]: e["properties"] for e in body["endpoints"]}
    assert by_type["persons"] == ["id", "name", "role", "team_ids"]


# --- DOMjudge v4 balloon surface (/api/v4/) ----------------------------------

V4_MANUAL = {"contest": dict(MANUAL["contest"]),
             "groups": {"1": {"name": "正式"}},
             "teams": {"D22": {"group_ids": ["1"], "seat": "A12"},
                       "D23": {"group_ids": ["1"], "seat": "B07"}}}
V4_SUBS = [
    {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
     "team_id": "D22", "team_name": "一队", "problem_index": "A", "language": "cpp"},
    {"id": "2", "submit_time": "2023/05/21 09:15:00", "status": "答案正确",
     "team_id": "D22", "team_name": "一队", "problem_index": "B", "language": "cpp"},
    {"id": "3", "submit_time": "2023/05/21 09:25:00", "status": "答案正确",
     "team_id": "D23", "team_name": "二队", "problem_index": "A", "language": "cpp"},
]
V4_PROBLEMS = [{"id": "A", "title": "题A"}, {"id": "B", "title": "题B"}]


def make_v4_client(tmp_path) -> TestClient:
    store = Store()
    store.apply_all(translate("c1", V4_MANUAL, V4_SUBS, V4_PROBLEMS, None, NOW))
    app = create_app(store, poller=None, contest_id="c1",
                     balloon_state=BalloonState(tmp_path / "balloons.json"))
    return TestClient(app)


def test_v4_contests_snap_none_returns_empty(tmp_path):
    app = create_app(Store(), poller=None, contest_id="c1",
                     balloon_state=BalloonState(tmp_path / "balloons.json"))
    assert TestClient(app).get("/api/v4/contests").json() == []


def test_v4_contests_shape(tmp_path):
    body = make_v4_client(tmp_path).get("/api/v4/contests").json()
    assert len(body) == 1
    contest = body[0]
    assert contest["id"] == "c1"
    assert contest["shortname"] == contest["name"] == "省赛"
    assert contest["banner"] == []
    assert contest["problemset"] == []
    assert contest["allow_submit"] is True


def test_v4_balloons_order_todo_done_undone(tmp_path):
    c = make_v4_client(tmp_path)
    body = c.get("/api/v4/contests/c1/balloons").json()
    # undone-first, each group first-AC time DESC: D23-A(09:25), D22-B(09:15), D22-A(09:10)
    assert [b["balloonid"] for b in body] == [3, 2, 1]
    assert body[0]["team"] == "D23: 二队"
    assert body[0]["problem"] == "A"
    assert body[0]["location"] == "B07"
    assert body[0]["category"] == "正式"
    assert body[0]["total"]["A"]["short_name"] == "A"
    assert all(b["done"] is False for b in body)
    # mark D22-A (balloonid 1) done → moves to the done group
    assert c.post("/api/v4/contests/c1/balloons/1/done").status_code == 204
    body = c.get("/api/v4/contests/c1/balloons").json()
    assert [b["balloonid"] for b in body] == [3, 2, 1]
    assert [b["done"] for b in body] == [False, False, True]
    # todo=true hides the done balloon
    body = c.get("/api/v4/contests/c1/balloons?todo=true").json()
    assert [b["balloonid"] for b in body] == [3, 2]
    # undone returns it to the pending pool
    assert c.post("/api/v4/contests/c1/balloons/1/undone").status_code == 204
    body = c.get("/api/v4/contests/c1/balloons?todo=true").json()
    assert [b["balloonid"] for b in body] == [3, 2, 1]


def test_v4_balloons_404_unknown_id_and_cid(tmp_path):
    c = make_v4_client(tmp_path)
    r = c.post("/api/v4/contests/c1/balloons/999/done")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "balloon not found: 999"}
    assert c.post("/api/v4/contests/c1/balloons/999/undone").status_code == 404
    r = c.get("/api/v4/contests/nope/balloons")
    assert r.status_code == 404
    assert r.json()["code"] == 404
    assert c.post("/api/v4/contests/nope/balloons/1/done").status_code == 404


def test_v4_balloons_empty_store_404(tmp_path):
    app = create_app(Store(), poller=None, contest_id="c1",
                     balloon_state=BalloonState(tmp_path / "balloons.json"))
    r = TestClient(app).get("/api/v4/contests/c1/balloons")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "contest data not ready"}


def test_v4_balloons_tied_times_stable_order(tmp_path):
    """Equal first-AC times keep mint order (balloonid ascending) inside
    an ordering group — DOMjudge's submittime DESC then stable done sort
    must not reorder equal-time rows."""
    store = Store()
    subs = [
        {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
         "team_id": "D22", "team_name": "一队", "problem_index": "A", "language": "cpp"},
        {"id": "2", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
         "team_id": "D22", "team_name": "一队", "problem_index": "B", "language": "cpp"},
    ]
    store.apply_all(translate("c1", V4_MANUAL, subs, V4_PROBLEMS, None, NOW))
    c = TestClient(create_app(store, poller=None, contest_id="c1",
                              balloon_state=BalloonState(tmp_path / "b.json")))
    body = c.get("/api/v4/contests/c1/balloons").json()
    assert [b["balloonid"] for b in body] == [1, 2]
    assert [b["problem"] for b in body] == ["A", "B"]


def test_v4_balloons_excludes_inactive_entry(tmp_path):
    """A pair that leaves the first-AC set keeps its id/state internally but
    is filtered out of both the full and todo balloon lists, and the row
    shape is unchanged."""
    store = Store()
    with_d25 = V4_SUBS + [
        {"id": "4", "submit_time": "2023/05/21 09:35:00", "status": "答案正确",
         "team_id": "D25", "team_name": "四队", "problem_index": "A",
         "language": "cpp"}]
    store.apply_all(translate("c1", V4_MANUAL, with_d25, V4_PROBLEMS, None, NOW))
    c = TestClient(create_app(store, poller=None, contest_id="c1",
                              balloon_state=BalloonState(tmp_path / "balloons.json")))
    ids = [b["balloonid"] for b in
           c.get("/api/v4/contests/c1/balloons").json()]
    assert 4 in ids  # D25-A minted (the 4th, latest first-AC)
    # D25-A's AC drops out of the snapshot → marked inactive, not deleted
    store.apply_all(translate("c1", V4_MANUAL, V4_SUBS, V4_PROBLEMS, None, NOW))
    full = c.get("/api/v4/contests/c1/balloons").json()
    todo = c.get("/api/v4/contests/c1/balloons?todo=true").json()
    assert 4 not in [b["balloonid"] for b in full]
    assert 4 not in [b["balloonid"] for b in todo]
    # response shape is unchanged (no stray "active" key leaked)
    assert set(full[0]) == {
        "balloonid", "time", "problem", "contestproblem", "team", "teamid",
        "location", "affiliation", "affiliationid", "category", "categoryid",
        "total", "done"}


# --- unified /api/v4/ surface (spec 2026-10-15-v4-unify-design) ------------


def test_v4_single_contest_domjudge_shape_end_time_while_running():
    # contest still running (NOW 10:00, planned end 14:00): the DOMjudge
    # contest endpoint advertises start + duration, never state.ended
    body = make_client().get("/api/v4/contests/c1").json()
    assert body["id"] == "c1"
    assert body["shortname"] == body["name"] == "省赛"
    assert body["start_time"] == "2023-05-21T09:00:00+08:00"
    assert body["end_time"] == "2023-05-21T14:00:00+08:00"
    assert body["scoreboard_thaw_time"] is None
    assert body["banner"] == []
    assert body["problemset"] == []
    assert body["allow_submit"] is True


def test_v4_teams_domjudge_shape_with_public_description():
    manual = {"contest": dict(MANUAL["contest"]),
              "groups": {"1": {"name": "正式"}},
              "organizations": {"org1": {"name": "某大学"}},
              "teams": {"D22": {"group_ids": ["1"], "seat": "A12",
                                "organization_id": "org1",
                                "members": ["甲", "乙"], "coaches": ["丙"]}}}
    store = Store()
    store.apply_all(translate("c1", manual, SUBS,
                              [{"id": "A", "title": "题A"}], None, NOW))
    c = TestClient(create_app(store, poller=None, contest_id="c1"))
    body = c.get("/api/v4/contests/c1/teams").json()
    assert [t["id"] for t in body] == ["D22", "D23"]  # sorted by id
    team = next(t for t in body if t["id"] == "D22")
    assert team["externalid"] == "D22"
    assert team["name"] == team["display_name"] == "队"
    assert team["organization_id"] == team["affiliation_id"] == "org1"
    assert team["group_ids"] == ["1"]
    assert team["label"] == team["location"] == "A12"
    assert team["icpc_id"] is None
    assert team["hidden"] is False
    assert team["public_description"] == "队员：甲、乙；教练：丙"
    # derived team D23 has no persons → public_description null
    assert next(t for t in body if t["id"] == "D23")["public_description"] is None


def test_v4_teams_public_description_none_without_persons(tmp_path):
    # V4_MANUAL has no members/coaches → public_description null
    body = make_v4_client(tmp_path).get("/api/v4/contests/c1/teams").json()
    assert all(t["public_description"] is None for t in body)
    assert [t["location"] for t in body] == ["A12", "B07"]  # sorted by id


def test_v4_organizations_domjudge_shape():
    manual = {"contest": dict(MANUAL["contest"]),
              "organizations": {"org1": {"name": "某大学"}}}
    store = Store()
    store.apply_all(translate("c1", manual, SUBS,
                              [{"id": "A", "title": "题A"}], None, NOW))
    c = TestClient(create_app(store, poller=None, contest_id="c1"))
    body = c.get("/api/v4/contests/c1/organizations").json()
    assert [o["id"] for o in body] == ["org1"]
    org = body[0]
    assert org["externalid"] == "org1"
    assert org["name"] == org["formal_name"] == org["shortname"] == "某大学"
    assert org["country"] is None
    assert org["country_flag"] == []
    assert org["logo"] == []
    assert org["affiliation_id"] == "org1"
    assert org["icpc_id"] is None


def test_v4_balloon_time_is_numeric_epoch_seconds(tmp_path):
    body = make_v4_client(tmp_path).get("/api/v4/contests/c1/balloons").json()
    # find the D23-A first-AC (09:25+08:00); time must be an epoch int
    # matching the ISO origin, not a string (xcpctools does o.time*1e3)
    row = next(b for b in body if b["teamid"] == "D23" and b["problem"] == "A")
    origin = dt.datetime(2023, 5, 21, 9, 25, 0,
                         tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert row["time"] == int(origin.timestamp())
    assert isinstance(row["time"], int) and not isinstance(row["time"], bool)


def test_legacy_contests_redirect_to_api_v4():
    c = make_client()
    r = c.get("/contests/c1/scoreboard?group_id=g", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith(
        "/api/v4/contests/c1/scoreboard?group_id=g")


def test_legacy_contests_redirect_keeps_subpath():
    c = make_client()
    r = c.get("/contests/c1/submissions/1", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith("/api/v4/contests/c1/submissions/1")


@pytest.mark.parametrize("old_path", [
    "/contests/c1/access",
    "/contests/c1/state",
    "/contests/c1/teams",
    "/contests/c1/organizations",
    "/contests/c1/event-feed",
    "/contests/c1/balloons?todo=true",
    "/contests/c1/submissions/1/files/main.cpp",
])
def test_legacy_contests_redirect_parametrized(old_path):
    c = make_client()
    r = c.get(old_path, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith(
        "/api/v4" + old_path)


def test_legacy_bare_contests_redirect_to_api_v4():
    c = make_client()
    r = c.get("/contests", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith("/api/v4/contests")


def test_legacy_root_redirect_to_api_v4():
    c = make_client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith("/api/v4/")
