import datetime as dt

from fastapi.testclient import TestClient

from pta2ccs.app import _make_poller, create_app
from pta2ccs.config import Settings
from pta2ccs.store import Store
from pta2ccs.translate import translate

NOW = dt.datetime(2023, 5, 21, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
MANUAL = {"contest": {"name": "省赛", "start_time": "2023-05-21T09:00:00+08:00",
                      "duration": "5:00:00", "pta_timezone": "+08:00"}}
SUBS = [
    {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
     "user": "D22 队", "problem_index": "A", "language": "cpp"},
    {"id": "2", "submit_time": "2023/05/21 09:11:00", "status": "答案错误",
     "user": "D23 队", "problem_index": "A", "language": "cpp"},
]


def make_client() -> TestClient:
    store = Store()
    store.apply_all(translate("c1", MANUAL, SUBS, [{"id": "A", "title": "题A"}], None, NOW))
    app = create_app(store, poller=None, contest_id="c1")
    return TestClient(app)


def test_api_info():
    r = make_client().get("/")
    assert r.status_code == 200
    assert r.json()["version"] == "2026-01"


def test_access():
    r = make_client().get("/contests/c1/access")
    assert r.status_code == 200
    body = r.json()
    assert body["capabilities"] == []
    types = {e["type"] for e in body["endpoints"]}
    assert "submissions" in types
    assert "scoreboard" in types


def test_collections_and_items():
    c = make_client()
    r = c.get("/contests/c1/submissions")
    assert r.status_code == 200
    assert {s["id"] for s in r.json()} == {"1", "2"}
    r = c.get("/contests/c1/submissions/1")
    assert r.status_code == 200
    assert r.json()["team_id"] == "D22"
    r = c.get("/contests/c1/judgement-types/AC")
    assert r.status_code == 200
    assert r.json()["solved"] is True


def test_filtering_by_team_id():
    r = make_client().get("/contests/c1/submissions?team_id=D22")
    assert [s["id"] for s in r.json()] == ["1"]


def test_empty_query_param_matches_null():
    # Contest API: an empty-string query value matches attributes that are null
    manual = {"contest": dict(MANUAL["contest"]),
              "teams": {"D22": {"organization_id": "org1"}}}
    store = Store()
    store.apply_all(translate("c1", manual, SUBS, [{"id": "A", "title": "题A"}],
                              None, NOW))
    c = TestClient(create_app(store, poller=None, contest_id="c1"))
    r = c.get("/contests/c1/teams?organization_id=")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == ["D23"]  # derived team: organization_id None
    r = c.get("/contests/c1/teams?organization_id=org1")
    assert [t["id"] for t in r.json()] == ["D22"]


def test_scoreboard_and_group_filter():
    c = make_client()
    r = c.get("/contests/c1/scoreboard")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows[0]["team_id"] == "D22"
    r = c.get("/contests/c1/scoreboard?group_id=ZZ")
    assert r.json()["rows"] == []


def test_contest_and_state():
    c = make_client()
    assert c.get("/contests/c1").json()["name"] == "省赛"
    assert c.get("/contests/c1/state").json()["started"] is not None


def test_404_unknown_id_and_path():
    c = make_client()
    r = c.get("/contests/c1/submissions/nope")
    assert r.status_code == 404
    assert r.json() == {"code": 404, "message": "submission not found: nope"}
    r = c.get("/contests/c1/unknown-collection")
    assert r.status_code == 404
    assert r.json()["code"] == 404
    r = c.get("/contests/other/submissions")
    assert r.status_code == 404


def test_read_routes_404_on_empty_store():
    # empty Store: snapshot() is None; read routes must 404, not AttributeError-500
    c = TestClient(create_app(Store(), poller=None, contest_id="c1"))
    for path in ("/contests/c1/state", "/contests/c1/submissions"):
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
    settings = Settings(contest_id="c1", pta_cookie="cookie")
    manual = {"contest": {"pta_timezone": "+09:00"}}
    poller, tz = _make_poller(settings, manual, Store())
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
        return None


def _make_lifespan_app(closable: bool):
    from pta2ccs.poller import Poller
    client = _LifespanFakeClient(closable)
    settings = Settings(contest_id="c1", pta_cookie="c",
                        poll_interval_seconds=0.01, config_poll_seconds=0.01)
    poller = Poller(client, Store(), settings, {"contest": {}})
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
