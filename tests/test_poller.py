import asyncio
import json
import logging
import time

import httpx
import pytest

from pta2icpc.config import Settings
from pta2icpc.poller import PRINT_FETCH_PER_ROUND, PRINT_MAX_ATTEMPTS, Poller, drop_print_problem
from pta2icpc.pta.client import PtaRateLimitError
from pta2icpc.store import Store
from tests.test_printjob import parse_multipart

ZERO_SUB_TEAM_ID = "9001540000000000001"


class FakeClient:
    def __init__(self):
        self.subs = []
        self.problems = [{"id": "A", "title": "题A"}]
        self.teams = [{
            "id": ZERO_SUB_TEAM_ID, "name": "无提交队",
            "label": "2022000099",  # roster label = 学号 (task 11)
            "group_id": "1901545194649161728", "organization_id": "1901545194649161728",
            "organization_name": "DSA_2024",
        }]
        self.teams_calls = 0
        # live-shaped meta: raw freeze input (hidingTime 360 min AFTER
        # start; translate derives 31h − 6h = 25h from the final duration)
        # + contest name + two compilers; times None so the manual
        # contest block keeps providing start/duration
        self.meta = {
            "name": "示例比赛",
            "startAt": None, "endAt": None, "durationSeconds": None,
            "compilers": ["GXX", "JAVAC"], "hidingTimeMinutes": 360,
        }
        self.meta_calls = 0
        # per-submission detail (task 13): call counter + failure set
        self.detail_calls: list[str] = []
        self.detail_fail_ids: set[str] = set()
        self.detail_program = "int main(){}"
        # rate-limit controls (task 14)
        self.subs_calls = 0
        self.subs_error: Exception | None = None
        self.detail_rate_limit_ids: set[str] = set()

    async def fetch_submissions(self):
        self.subs_calls += 1
        if self.subs_error is not None:
            raise self.subs_error
        return list(self.subs)

    async def fetch_problems(self):
        return list(self.problems)

    async def fetch_teams(self):
        self.teams_calls += 1
        return list(self.teams)

    async def fetch_contest_meta(self):
        self.meta_calls += 1
        return dict(self.meta)

    async def fetch_submission_detail(self, sub_id: str):
        self.detail_calls.append(sub_id)
        if sub_id in self.detail_rate_limit_ids:
            raise PtaRateLimitError(retry_after=30.0)
        if sub_id in self.detail_fail_ids:
            raise RuntimeError(f"detail boom {sub_id}")
        return {"program": self.detail_program, "compiler": "GXX",
                "run_results": [{"case": "1", "result": "ACCEPTED",
                                 "time": 0.003, "memory": 2048, "exitcode": 0}]}


def make_poller(config_poll_seconds: float = 0.01, *,
                print_server: str | None = None,
                print_sent_path=None,
                **settings_overrides) -> tuple[Poller, FakeClient, Store]:
    settings = Settings(poll_interval_seconds=0.01,
                        config_poll_seconds=config_poll_seconds,
                        # persisted snapshot cache (task 28) has its own
                        # isolated tests in test_cache.py; here it is off so
                        # these pollers never read/write the default file
                        cache_enabled=False,
                        **settings_overrides)
    client = FakeClient()
    store = Store()
    # finalized_at "never": these tests model a still-updating contest
    # (the real clock is far past the fixture's 2023 window); without it
    # the new auto-finalization would freeze the snapshot after round 1
    manual = {"contest": {"start_time": "2023-05-21T09:00:00+08:00",
                          "duration": "31:00:00", "pta_timezone": "+08:00",
                          "finalized_at": "never"}}
    extra: dict = {}
    if print_server is not None:
        extra["print_server"] = print_server
    if print_sent_path is not None:
        extra["print_sent_path"] = print_sent_path
    return (Poller(client, store, settings, manual, contest_id="c1", **extra),
            client, store)


def test_poll_once_populates_store():
    poller, client, store = make_poller()
    client.subs = [{"id": "1", "submit_time": "2023/05/21 09:10:00",
                    "status": "答案正确", "team_id": "D22", "team_name": "队",
                    "problem_index": "A", "language": "cpp"}]
    asyncio.run(poller.poll_once())
    assert store.snapshot() is not None
    assert "1" in store.snapshot().submissions
    assert poller.status["ready"] is True
    assert poller.status["cookie_ok"] is True


def test_poll_auth_error_sets_cookie_flag():
    poller, client, store = make_poller()

    async def boom():
        from pta2icpc.pta.client import PtaAuthError
        raise PtaAuthError("expired")

    client.fetch_submissions = boom  # type: ignore[method-assign]
    asyncio.run(poller.poll_once())
    assert poller.status["cookie_ok"] is False
    assert poller.status["consecutive_failures"] == 1
    assert poller.status["ready"] is False


def test_poll_once_bad_row_does_not_crash():
    poller, client, store = make_poller()
    client.subs = [
        {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
         "team_id": "D22", "team_name": "队", "problem_index": "A", "language": "cpp"},
        {"id": "2", "submit_time": "???bad", "status": "答案正确",
         "team_id": "D23", "team_name": "二队", "problem_index": "A", "language": "cpp"},
    ]
    asyncio.run(poller.poll_once())
    assert "1" in store.snapshot().submissions
    assert "2" not in store.snapshot().submissions


def test_need_config_fetches_teams_and_non_config_uses_cached_roster():
    # config_poll_seconds=60 → only the first poll is need_config; every
    # later round must reuse the cached roster or the zero-submission team
    # would vanish from translate(raw_teams=None) on the next poll.
    poller, client, store = make_poller(config_poll_seconds=60.0)
    asyncio.run(poller.poll_once())
    assert client.teams_calls == 1
    snap = store.snapshot()
    assert ZERO_SUB_TEAM_ID in snap.teams
    assert snap.organizations["1901545194649161728"]["name"] == "DSA_2024"
    # PTA userGroups no longer mirror into groups; groups are manual-only
    assert "1901545194649161728" not in snap.groups
    assert snap.teams[ZERO_SUB_TEAM_ID]["group_ids"] == []
    for _ in range(2):  # non-config rounds
        asyncio.run(poller.poll_once())
    assert client.teams_calls == 1  # roster not re-fetched
    snap = store.snapshot()
    assert ZERO_SUB_TEAM_ID in snap.teams  # cached roster passed to translate
    assert snap.teams[ZERO_SUB_TEAM_ID]["name"] == "无提交队"


def test_empty_roster_on_config_round_keeps_previous_cache(caplog):
    # 200-but-empty /members on a config round must not wipe the cached
    # roster (zero-submission teams would vanish until the next config round)
    poller, client, store = make_poller(config_poll_seconds=0.01)
    asyncio.run(poller.poll_once())
    assert ZERO_SUB_TEAM_ID in store.snapshot().teams
    client.teams = []  # next config round returns an empty roster
    time.sleep(0.02)   # ...and it must actually be a config round
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert ZERO_SUB_TEAM_ID in snap.teams
    assert snap.teams[ZERO_SUB_TEAM_ID]["name"] == "无提交队"
    assert snap.organizations["1901545194649161728"]["name"] == "DSA_2024"
    assert any("empty roster" in rec.message for rec in caplog.records)


def test_contest_meta_fetched_once_and_freeze_reaches_snapshot():
    # config_poll_seconds=60 → only the first round is need_config; meta
    # (like the roster) is fetched there and cached for later rounds.
    poller, client, store = make_poller(config_poll_seconds=60.0)
    asyncio.run(poller.poll_once())
    assert client.meta_calls == 1
    snap = store.snapshot()
    # manual has no scoreboard_freeze_duration → meta hiding 360 min vs
    # manual duration 31:00:00 → 31h − 6h = 25h freeze
    assert snap.contest["scoreboard_freeze_duration"] == "25:00:00"
    assert snap.state["frozen"] is not None
    # manual has no name → problemSet.name from the API
    assert snap.contest["name"] == "示例比赛"
    assert snap.contest["formal_name"] == "示例比赛"
    names = {l["name"] for l in snap.languages.values()}
    assert {"C++ (g++)", "Java (javac)"} <= names  # meta compilers advertised
    for _ in range(2):  # non-config rounds
        asyncio.run(poller.poll_once())
    assert client.meta_calls == 1  # cached, not re-fetched
    snap = store.snapshot()
    assert snap.contest["scoreboard_freeze_duration"] == "25:00:00"
    assert snap.state["frozen"] is not None


# --- per-submission detail fetch (task 13) --------------------------------

def sub_row(sid: str, status: str = "答案正确") -> dict:
    return {"id": sid, "submit_time": "2023/05/21 09:10:00", "status": status,
            "team_id": "D22", "team_name": "队", "problem_index": "A",
            "language": "cpp"}


def test_detail_first_round_fetches_all():
    poller, client, store = make_poller()
    client.subs = [sub_row("1"), sub_row("2"), sub_row("3")]
    asyncio.run(poller.poll_once())
    assert sorted(client.detail_calls) == ["1", "2", "3"]
    snap = store.snapshot()
    assert snap.sources["1"] == "int main(){}"
    assert snap.submissions["1"]["files"][0]["filename"] == "main.cpp"
    assert "1-1" in snap.runs


def test_detail_second_round_same_status_no_refetch():
    poller, client, store = make_poller()
    client.subs = [sub_row("1")]
    asyncio.run(poller.poll_once())
    assert client.detail_calls == ["1"]
    asyncio.run(poller.poll_once())  # same status
    assert client.detail_calls == ["1"]  # no new call
    # cached detail is re-attached to the fresh row dicts → snapshot keeps
    # files/runs/sources between fetches
    snap = store.snapshot()
    assert snap.sources["1"] == "int main(){}"
    assert snap.submissions["1"]["files"]
    assert "1-1" in snap.runs


def test_detail_status_change_triggers_refetch():
    poller, client, store = make_poller()
    client.subs = [sub_row("1", "正在评测")]
    asyncio.run(poller.poll_once())
    client.subs = [sub_row("1", "答案正确")]  # status flipped
    asyncio.run(poller.poll_once())
    assert client.detail_calls == ["1", "1"]


def test_detail_per_id_failure_retries_next_round(caplog):
    poller, client, store = make_poller()
    client.subs = [sub_row("1"), sub_row("2")]
    client.detail_fail_ids = {"2"}
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        asyncio.run(poller.poll_once())
    snap = store.snapshot()
    # failure doesn't kill the cycle: both submissions present, only the
    # successful one has detail
    assert {"1", "2"} <= set(snap.submissions)
    assert "1" in snap.sources
    assert "2" not in snap.sources
    assert any("detail fetch failed" in rec.message and "2" in rec.message
               for rec in caplog.records)
    # retry next round (id only in the log — no program text leaked)
    assert "int main(){}" not in caplog.text
    client.detail_fail_ids = set()
    asyncio.run(poller.poll_once())
    assert client.detail_calls == ["1", "2", "2"]  # 2 retried
    assert "2" in store.snapshot().sources


def test_detail_fetch_capped_per_round():
    # budget from settings (task 14 replaces the old DETAIL_FETCH_LIMIT
    # module constant): default 5/round drains a 10-id backlog over 2 rounds
    poller, client, store = make_poller(detail_fetch_per_round=5)
    client.subs = [sub_row(str(i)) for i in range(10)]
    asyncio.run(poller.poll_once())
    assert len(client.detail_calls) == 5  # budget for this round
    asyncio.run(poller.poll_once())
    assert len(client.detail_calls) == 10  # remaining backlog drained
    asyncio.run(poller.poll_once())
    assert len(client.detail_calls) == 10  # nothing left


@pytest.mark.parametrize("overrides", [{"detail_fetch_per_round": 0},
                                       {"fetch_details": False}])
def test_detail_fetch_disabled(overrides: dict):
    # either escape hatch (budget 0 or fetch_details false) → zero detail
    # calls; the round itself still succeeds
    poller, client, store = make_poller(**overrides)
    client.subs = [sub_row("1"), sub_row("2")]
    asyncio.run(poller.poll_once())
    assert client.detail_calls == []
    snap = store.snapshot()
    assert {"1", "2"} <= set(snap.submissions)
    assert snap.sources == {}


# --- rate limiting (task 14: live 429 storm 2026-10-14) --------------------

def test_rate_limited_round_is_not_a_failure_and_skips_until_cooldown(caplog):
    poller, client, store = make_poller()
    assert poller.status["rate_limited_until"] is None
    client.subs_error = PtaRateLimitError(retry_after=30.0)
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        asyncio.run(poller.poll_once())
        assert client.subs_calls == 1
        # rate limit is NOT a failure: counter untouched, last_success untouched
        assert poller.status["consecutive_failures"] == 0
        assert poller.status["last_success"] is None
        assert poller.status["cookie_ok"] is True
        assert poller.status["rate_limited_until"] is not None  # ISO string
        assert poller._rate_limit_until is not None  # monotonic deadline
        assert any("poll rate limited" in rec.message for rec in caplog.records)
        # next poll_once during cooldown → round skipped entirely, zero PTA calls
        asyncio.run(poller.poll_once())
    assert client.subs_calls == 1
    assert client.detail_calls == []
    assert client.teams_calls == 0


def test_detail_rate_limit_does_not_fail_round(caplog):
    poller, client, store = make_poller(detail_fetch_per_round=5)
    client.subs = [sub_row("1"), sub_row("2")]
    client.detail_rate_limit_ids = {"1", "2"}
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        asyncio.run(poller.poll_once())
    # list succeeded → translate/apply still ran with what we have
    snap = store.snapshot()
    assert snap is not None
    assert {"1", "2"} <= set(snap.submissions)
    assert snap.sources == {}  # throttled details never attached
    # round is a success; cooldown pushed forward by the detail 429s
    assert poller.status["consecutive_failures"] == 0
    assert poller.status["ready"] is True
    assert poller._rate_limit_until is not None
    assert poller.status["rate_limited_until"] is not None
    # ONE summary line, not one per id
    paused = [rec for rec in caplog.records
              if "detail fetch paused" in rec.message]
    assert len(paused) == 1
    assert "30.0" in paused[0].message or "30" in paused[0].message


def test_config_round_rate_limit_also_backs_off():
    poller, client, store = make_poller(config_poll_seconds=0.01)
    asyncio.run(poller.poll_once())  # first round succeeds (config fetched)
    assert client.teams_calls == 1
    client.subs_error = PtaRateLimitError(retry_after=20.0)
    time.sleep(0.02)  # next round is a config round again
    asyncio.run(poller.poll_once())
    assert poller.status["consecutive_failures"] == 0
    assert poller._rate_limit_until is not None


def test_non_config_rounds_keep_problem_limits():
    # config_poll_seconds=60 → only the first round is need_config; the
    # snapshot-rebuilt raw_problems on later rounds must carry the limits
    # through (time_limit stays seconds, memory_limit stays MiB — the
    # Contest API 2026-01 units).
    poller, client, store = make_poller(config_poll_seconds=60.0)
    client.problems = [{"id": "A", "title": "题A", "time_limit_ms": 100,
                        "memory_limit_kb": 256000, "code_limit_kb": 64,
                        "test_data_count": 3}]
    asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert snap.problems["A"]["time_limit"] == 0.1
    assert snap.problems["A"]["memory_limit"] == 250
    assert snap.problems["A"]["code_limit"] == 64
    assert snap.problems["A"]["test_data_count"] == 3
    for _ in range(2):  # non-config rounds rebuild from the snapshot
        asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert snap.problems["A"]["time_limit"] == 0.1
    assert snap.problems["A"]["memory_limit"] == 250
    assert snap.problems["A"]["code_limit"] == 64
    assert snap.problems["A"]["test_data_count"] == 3


# --- print-problem drop (task 20) --------------------------------------------

def test_drop_print_problem_filters_label():
    subs = [{"id": "1", "problem_index": "1-3"},
            {"id": "2", "problem_index": "1-4"}]
    probs = [{"id": "1-3", "label": "1-3"}, {"id": "1-4", "label": "1-4"}]
    kept_subs, kept_probs = drop_print_problem(subs, probs, "1-3")
    assert [r["id"] for r in kept_subs] == ["2"]
    assert [p["id"] for p in kept_probs] == ["1-4"]
    # new lists; inputs untouched
    assert kept_subs is not subs and kept_probs is not probs
    assert len(subs) == 2 and len(probs) == 2


@pytest.mark.parametrize("print_label", [None, ""])
def test_drop_print_problem_none_or_empty_is_noop(print_label):
    subs = [{"id": "1", "problem_index": "1-3"}]
    probs = [{"id": "1-3", "label": "1-3"}]
    kept_subs, kept_probs = drop_print_problem(subs, probs, print_label)
    assert kept_subs is subs and kept_probs is probs  # fast path


def test_drop_print_problem_matches_problem_id_without_label():
    probs = [{"id": "1-3"}, {"id": "1-4", "label": "1-4"}]
    kept_subs, kept_probs = drop_print_problem([], probs, "1-3")
    assert kept_subs == []
    assert [p["id"] for p in kept_probs] == ["1-4"]


def test_drop_print_problem_mixed_labels_unaffected():
    subs = [{"id": "1", "problem_index": "1-1"},
            {"id": "2", "problem_index": "1-31"},  # prefix must not match
            {"id": "3", "problem_index": "1-3"}]
    probs = [{"id": "1-1", "label": "1-1"},
             {"id": "1-4", "label": "1-4"}]
    kept_subs, kept_probs = drop_print_problem(subs, probs, "1-3")
    assert [r["id"] for r in kept_subs] == ["1", "2"]
    assert [p["id"] for p in kept_probs] == ["1-1", "1-4"]


def make_print_poller(print_label: str):
    poller, client, store = make_poller(config_poll_seconds=60.0)
    poller.manual["options"] = {"print_problem": print_label}
    client.problems = [{"id": "1-3", "label": "1-3", "title": "隐藏题"},
                       {"id": "1-4", "label": "1-4", "title": "正常题"}]
    client.subs = [dict(sub_row("1"), problem_index="1-3"),
                   dict(sub_row("2"), problem_index="1-4")]
    return poller, client, store


def test_print_problem_dropped_from_store_and_never_detail_fetched():
    poller, client, store = make_print_poller("1-3")
    asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert "2" in snap.submissions
    assert "1" not in snap.submissions
    assert "1-4" in snap.problems
    assert "1-3" not in snap.problems
    # the dropped sid never even reached _fetch_details' `needed` list
    assert client.detail_calls == ["2"]


def test_print_problem_drop_clears_detail_cache():
    poller, client, store = make_poller(config_poll_seconds=60.0)
    client.subs = [sub_row("1"), dict(sub_row("2"), problem_index="B")]
    asyncio.run(poller.poll_once())
    assert set(poller._detail_cache) == {"1", "2"}
    assert set(poller._detail_status) == {"1", "2"}
    poller.manual["options"] = {"print_problem": "A"}
    asyncio.run(poller.poll_once())  # non-config round: snapshot rebuild
    assert "1" not in poller._detail_cache
    assert "1" not in poller._detail_status
    assert "2" in poller._detail_cache and "2" in poller._detail_status
    snap = store.snapshot()
    assert "1" not in snap.submissions and "2" in snap.submissions
    assert "A" not in snap.problems  # rebuild path drops it too
    # dropped sid never re-fetched, never re-attached
    assert client.detail_calls == ["1", "2"]


# --- print pipeline (task: 扮演 DOMjudge) -----------------------------------

PRINT_SERVER = "http://printer.local/print"


def make_pipeline_poller(tmp_path, *, print_server: str | None = PRINT_SERVER):
    """Poller with a print problem, a manual seat/group for D22, a roster
    carrying D22, and print_sent.json isolated under tmp_path."""
    poller, client, store = make_poller(
        config_poll_seconds=60.0, print_server=print_server,
        print_sent_path=tmp_path / "print_sent.json")
    poller.manual["options"] = {"print_problem": "1-3"}
    poller.manual["groups"] = {"1": {"name": "正式队伍"}}
    poller.manual["teams"] = {"D22": {"group_ids": ["1"], "seat": "A12"}}
    client.problems = [{"id": "1-3", "label": "1-3", "title": "打印题"},
                       {"id": "1-4", "label": "1-4", "title": "正常题"}]
    client.teams = [{"id": "D22", "name": "鸡你太美队", "label": "2022000111",
                     "group_id": "1901545194649161728",
                     "organization_id": "1901545194649161728",
                     "organization_name": "DSA_2024"}]
    client.subs = [dict(sub_row("1"), problem_index="1-3"),
                   dict(sub_row("2"), problem_index="1-4")]
    return poller, client, store


def capture_http(poller, status_code: int = 200) -> list:
    """Replace the print HTTP seam with a MockTransport; returns the list
    of captured requests (one entry per POST)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={})

    poller._print_http_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    return requests


def test_print_pipeline_happy_path(tmp_path):
    poller, client, store = make_pipeline_poller(tmp_path)
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    snap = store.snapshot()
    # print-problem rows never reach the scoreboard
    assert "1" not in snap.submissions and "1" not in snap.sources
    assert "2" in snap.submissions
    assert "1-3" not in snap.problems and "1-4" in snap.problems
    # detail isolation: the print source never enters the scoreboard caches
    assert "1" not in poller._detail_status and "1" not in poller._detail_cache
    # source fetch: normal row first (_fetch_details), then the print row
    assert client.detail_calls == ["2", "1"]
    # multipart job from the fresh snapshot
    assert len(requests) == 1
    parts = parse_multipart(requests[0].content,
                            requests[0].headers["content-type"])
    assert parts["file"][0] == "1-3.cc"
    assert parts["file"][1] == "int main(){}".encode("utf-8")
    assert parts["filename"][1] == b"1-3.cc"
    assert parts["lang"][1] == b"cpp"
    assert parts["tname"][1] == "鸡你太美队".encode("utf-8")
    assert parts["team"][1] == b"D22"
    assert parts["location"][1] == b"A12"          # manual seat -> label
    assert parts["group"][1] == "正式队伍".encode("utf-8")  # manual group name
    # sent set persisted; cached source purged
    assert poller._print_sent == {"1"}
    assert json.loads((tmp_path / "print_sent.json").read_text(
        encoding="utf-8"))["sent"] == ["1"]
    assert poller._print_source == {}


def test_print_pipeline_second_round_never_resends(tmp_path):
    poller, client, store = make_pipeline_poller(tmp_path)
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    assert len(requests) == 1
    asyncio.run(poller.poll_once())  # non-config round (config_poll=60s)
    assert len(requests) == 1  # still one POST total
    # already-sent sid: no source refetch either
    assert client.detail_calls == ["2", "1"]


def test_print_pipeline_respects_preexisting_sent_file(tmp_path):
    (tmp_path / "print_sent.json").write_text(
        json.dumps({"sent": ["1"]}), encoding="utf-8")
    poller, client, store = make_pipeline_poller(tmp_path)
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    assert requests == []                 # never POSTed
    assert "1" not in client.detail_calls  # source never fetched
    assert "1" not in store.snapshot().submissions  # still hidden
    assert "2" in store.snapshot().submissions


def test_print_pipeline_500_failure_retries_next_round(tmp_path, caplog):
    poller, client, store = make_pipeline_poller(tmp_path)
    capture_http(poller, status_code=500)
    with caplog.at_level(logging.WARNING, logger="pta2icpc.printjob"):
        asyncio.run(poller.poll_once())
    assert "1" not in poller._print_sent
    assert poller._print_attempts == {"1": 1}
    assert not (tmp_path / "print_sent.json").exists()  # nothing persisted
    assert any("rejected job" in rec.message for rec in caplog.records)
    # recovery: next round POSTs the cached source successfully
    requests = capture_http(poller, status_code=200)
    asyncio.run(poller.poll_once())
    assert len(requests) == 1
    assert poller._print_sent == {"1"}
    assert json.loads((tmp_path / "print_sent.json").read_text(
        encoding="utf-8"))["sent"] == ["1"]


def test_print_pipeline_gives_up_after_max_attempts(tmp_path, caplog):
    poller, client, store = make_pipeline_poller(tmp_path)
    for i in range(PRINT_MAX_ATTEMPTS):
        capture_http(poller, status_code=500)
        asyncio.run(poller.poll_once())
        assert "1" not in poller._print_sent
        assert poller._print_attempts["1"] == i + 1
    with caplog.at_level(logging.ERROR, logger="pta2icpc.poller"):
        capture_http(poller, status_code=500)
        asyncio.run(poller.poll_once())  # attempt 11 -> permanent skip
    assert "1" in poller._print_sent
    assert poller._print_attempts["1"] == PRINT_MAX_ATTEMPTS + 1
    assert any("permanently skipped" in rec.message for rec in caplog.records)
    assert json.loads((tmp_path / "print_sent.json").read_text(
        encoding="utf-8"))["sent"] == ["1"]
    # stays skipped - no further POSTs
    requests = capture_http(poller, status_code=200)
    asyncio.run(poller.poll_once())
    assert requests == []


def test_print_source_fetch_budget_per_round(tmp_path):
    poller, client, store = make_pipeline_poller(tmp_path)
    client.subs = [dict(sub_row(str(i)), problem_index="1-3")
                   for i in range(PRINT_FETCH_PER_ROUND + 2)]  # 7 rows
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    # budget: PRINT_FETCH_PER_ROUND sources fetched and sent this round
    assert len(client.detail_calls) == PRINT_FETCH_PER_ROUND
    assert len(requests) == PRINT_FETCH_PER_ROUND
    # backlog drained on the next round
    asyncio.run(poller.poll_once())
    assert len(client.detail_calls) == PRINT_FETCH_PER_ROUND + 2
    assert len(requests) == PRINT_FETCH_PER_ROUND + 2
    assert poller._print_sent == {str(i)
                                  for i in range(PRINT_FETCH_PER_ROUND + 2)}
    sent = json.loads((tmp_path / "print_sent.json").read_text(
        encoding="utf-8"))["sent"]
    assert sent == sorted(str(i) for i in range(PRINT_FETCH_PER_ROUND + 2))


def test_print_pipeline_empty_source_counts_attempt(tmp_path):
    poller, client, store = make_pipeline_poller(tmp_path)
    client.detail_program = ""  # fetch succeeds but the source is empty
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    assert requests == []  # never POSTed an empty program
    assert poller._print_attempts == {"1": 1}
    assert "1" not in poller._print_sent


def test_print_pipeline_detail_failure_counts_attempt(tmp_path, caplog):
    poller, client, store = make_pipeline_poller(tmp_path)
    client.detail_fail_ids = {"1"}
    requests = capture_http(poller)
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        asyncio.run(poller.poll_once())
    assert requests == []  # source fetch failed -> nothing to send
    assert poller._print_attempts == {"1": 1}
    assert any("print source fetch failed" in rec.message
               and "1" in rec.message for rec in caplog.records)
    assert "int main(){}" not in caplog.text  # program never logged


def test_print_sent_file_corrupt_starts_empty(tmp_path, caplog):
    (tmp_path / "print_sent.json").write_text("{not json",
                                              encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller, client, store = make_pipeline_poller(tmp_path)
    assert poller._print_sent == set()
    assert any("print sent set starts empty" in rec.message
               for rec in caplog.records)
    # ...and the pipeline still works
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    assert len(requests) == 1


def test_without_print_server_keeps_old_drop_path(tmp_path):
    poller, client, store = make_pipeline_poller(tmp_path, print_server=None)

    def boom():
        raise AssertionError("print HTTP client must not be constructed")

    poller._print_http_client = boom  # type: ignore[method-assign]
    asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert "1" not in snap.submissions  # dropped as before
    assert "2" in snap.submissions
    # print row never detail-fetched on the old path
    assert client.detail_calls == ["2"]
    assert not (tmp_path / "print_sent.json").exists()  # no state written


def test_print_pipeline_team_absent_falls_back_to_row_fields(tmp_path):
    """Print submission whose team is not in the roster → snapshot team
    lookup misses, so tname comes from row.team_name and location/group
    are omitted entirely."""
    poller, client, store = make_pipeline_poller(tmp_path)
    client.subs = [dict(sub_row("1"), problem_index="1-3", team_id="X99"),
                   dict(sub_row("2"), problem_index="1-4")]
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    assert len(requests) == 1
    parts = parse_multipart(requests[0].content,
                            requests[0].headers["content-type"])
    assert parts["tname"][1] == "队".encode("utf-8")   # row.team_name fallback
    assert parts["team"][1] == b"X99"
    assert "location" not in parts      # no team → no seat
    assert "group" not in parts         # no team → no group


def test_print_pipeline_empty_seat_falls_back_to_roster_label(tmp_path):
    """Manual team with no seat → team.label stays the PTA roster label
    (学号), which is sent as the print location."""
    poller, client, store = make_pipeline_poller(tmp_path)
    poller.manual["teams"] = {"D22": {"group_ids": ["1"]}}  # no seat
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    parts = parse_multipart(requests[0].content,
                            requests[0].headers["content-type"])
    assert parts["location"][1] == b"2022000111"  # roster label


def test_print_source_fetch_429_cooldown_without_attempt_burn(tmp_path):
    """A 429 while fetching print sources sets the poller-wide cooldown,
    keeps the round alive, and must NOT count a failure attempt for a
    submission that was never fetched (review finding #1)."""
    poller, client, store = make_pipeline_poller(tmp_path)
    client.detail_rate_limit_ids = {"1"}
    capture_http(poller)
    asyncio.run(poller.poll_once())
    assert poller._rate_limit_until is not None   # cooldown armed
    assert poller._print_source == {}             # source never cached
    assert poller._print_attempts == {}           # no false attempt burn
    assert "1" not in poller._print_sent
    # once the cooldown clears, the next round fetches and sends normally
    poller._rate_limit_until = None
    client.detail_rate_limit_ids = set()
    requests = capture_http(poller)
    asyncio.run(poller.poll_once())
    assert len(requests) == 1
    assert poller._print_sent == {"1"}


# --- pending-verdict warning dedupe (task 22 review) ----------------------

class _FakeData:
    def __init__(self, pending: int, *, ended=True, eou=None):
        self.state = {"ended": "2023-05-21T20:00:00+08:00" if ended else None,
                      "end_of_updates": eou}
        self.judgements = {str(i): {"judgement_type_id": None}
                           for i in range(pending)}


def test_pending_verdict_warning_deduped(caplog):
    poller, _, _ = make_poller()
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        for _ in range(3):
            poller._warn_pending_verdicts(_FakeData(2))
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1                      # N polls, unchanged count
    # the warning is informational only: finalization is time-based (end +
    # 5:00) regardless of pending verdicts, which are omitted when it fires
    msg = warns[0].getMessage()
    assert "unresolved" in msg and "end + 5:00" in msg and "omitted" in msg
    assert "not finalizing" not in msg


def test_pending_verdict_warning_relogs_on_count_change(caplog):
    poller, _, _ = make_poller()
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller._warn_pending_verdicts(_FakeData(2))
        poller._warn_pending_verdicts(_FakeData(2))
        poller._warn_pending_verdicts(_FakeData(3))
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2


def test_pending_verdict_warning_resets_when_clear(caplog):
    poller, _, _ = make_poller()
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller._warn_pending_verdicts(_FakeData(2))          # logs
        poller._warn_pending_verdicts(_FakeData(0))          # clears
        poller._warn_pending_verdicts(_FakeData(2))          # logs again
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2


def test_pending_verdict_no_warning_after_finalized(caplog):
    poller, _, _ = make_poller()
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller._warn_pending_verdicts(_FakeData(2, eou="2023-05-21T20:00:01+08:00"))
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
