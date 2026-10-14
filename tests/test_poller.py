import asyncio
import logging
import time

import pytest

from pta2ccs.config import Settings
from pta2ccs.poller import Poller
from pta2ccs.pta.client import PtaRateLimitError
from pta2ccs.store import Store

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
        # live-shaped meta: start-relative freeze (111600 − 360×60 = 90000
        # = 25h) + contest name + two compilers; times None so the manual
        # contest block keeps providing start/duration
        self.meta = {
            "name": "示例比赛",
            "startAt": None, "endAt": None, "durationSeconds": None,
            "compilers": ["GXX", "JAVAC"], "scoreboardFreezeSeconds": 90000,
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


def make_poller(config_poll_seconds: float = 0.01,
                **settings_overrides) -> tuple[Poller, FakeClient, Store]:
    settings = Settings(contest_id="1", pta_cookie="c",
                        poll_interval_seconds=0.01,
                        config_poll_seconds=config_poll_seconds,
                        **settings_overrides)
    client = FakeClient()
    store = Store()
    manual = {"contest": {"start_time": "2023-05-21T09:00:00+08:00",
                          "duration": "5:00:00", "pta_timezone": "+08:00"}}
    return Poller(client, store, settings, manual), client, store


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
        from pta2ccs.pta.client import PtaAuthError
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
    assert snap.groups["1901545194649161728"]["name"] == "DSA_2024"
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
    with caplog.at_level(logging.WARNING, logger="pta2ccs.poller"):
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
    # manual has no scoreboard_freeze_duration → meta's 25h freeze applies
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
    with caplog.at_level(logging.WARNING, logger="pta2ccs.poller"):
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
    with caplog.at_level(logging.WARNING, logger="pta2ccs.poller"):
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
    with caplog.at_level(logging.WARNING, logger="pta2ccs.poller"):
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
    # through (time_limit round-trips reltime → ms).
    poller, client, store = make_poller(config_poll_seconds=60.0)
    client.problems = [{"id": "A", "title": "题A", "time_limit_ms": 100,
                        "memory_limit_kb": 256000, "code_limit_kb": 64}]
    asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert snap.problems["A"]["time_limit"] == "0:00:00.100"
    assert snap.problems["A"]["memory_limit"] == 256000
    assert snap.problems["A"]["code_limit"] == 64
    for _ in range(2):  # non-config rounds rebuild from the snapshot
        asyncio.run(poller.poll_once())
    snap = store.snapshot()
    assert snap.problems["A"]["time_limit"] == "0:00:00.100"
    assert snap.problems["A"]["memory_limit"] == 256000
    assert snap.problems["A"]["code_limit"] == 64
