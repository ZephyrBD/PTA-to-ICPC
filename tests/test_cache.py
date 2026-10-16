import asyncio
import datetime as dt
import json
import logging
import time
import types

import pytest

from pta2icpc.config import Settings
from pta2icpc.pta import client as client_module
from pta2icpc.pta.client import PtaClient
from pta2icpc.poller import CACHE_MIN_WRITE_INTERVAL, Poller
from pta2icpc.store import Store
from tests.test_poller import FakeClient, sub_row

BASE_URL = "https://pintia.cn"
CONTEST_ID = "1000000000000000000"
COOKIE = "secret-cookie-value"

MANUAL = {"contest": {"start_time": "2023-05-21T09:00:00+08:00",
                      "duration": "31:00:00", "pta_timezone": "+08:00",
                      "finalized_at": "never"}}


def make_cache_poller(tmp_path, **overrides) -> tuple[Poller, FakeClient, Store]:
    settings = Settings(poll_interval_seconds=0.01, config_poll_seconds=60.0,
                        **overrides)
    client = FakeClient()
    store = Store()
    poller = Poller(client, store, settings, dict(MANUAL), contest_id="c1",
                    cache_path=tmp_path / "cache" / "state.json",
                    print_sent_path=tmp_path / "print_sent.json")
    return poller, client, store


def write_cache(tmp_path, *, saved_at=None, contest_id="c1", **overrides):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": saved_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        "contest_id": contest_id,
        "raw_subs": [], "raw_problems": [], "raw_teams": [], "meta": {},
        "details": {},
    }
    payload.update(overrides)
    (cache_dir / "state.json").write_text(json.dumps(payload),
                                          encoding="utf-8")
    return cache_dir / "state.json"


# --- save -> load roundtrip ------------------------------------------------

def test_save_then_load_roundtrip(tmp_path):
    poller, client, store = make_cache_poller(tmp_path)
    client.subs = [sub_row("1"), sub_row("2")]
    asyncio.run(poller.poll_once())
    cache_file = tmp_path / "cache" / "state.json"
    assert cache_file.exists()
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["contest_id"] == "c1"
    assert raw["saved_at"]
    # raw_subs are persisted un-detailed
    assert {r["id"] for r in raw["raw_subs"]} == {"1", "2"}
    for row in raw["raw_subs"]:
        assert not ({"program", "compiler_enum", "run_results",
                     "detail_fetched"} & set(row))
    assert raw["details"]["1"]["status"] == "答案正确"
    assert raw["details"]["1"]["program"] == "int main(){}"
    assert raw["details"]["1"]["compiler"] == "GXX"
    # a fresh poller sharing the cache restores everything (no calls)
    poller2, client2, store2 = make_cache_poller(tmp_path)
    assert client2.detail_calls == []
    assert set(poller2._detail_status) == {"1", "2"}
    assert poller2._detail_cache["1"]["program"] == "int main(){}"
    assert poller2._last_teams is not None
    assert poller2._cache_raw_subs is not None


def test_cache_write_is_atomic_no_tmp_left(tmp_path):
    poller, client, store = make_cache_poller(tmp_path)
    client.subs = [sub_row("1")]
    asyncio.run(poller.poll_once())
    assert (tmp_path / "cache" / "state.json").exists()
    assert not (tmp_path / "cache" / "state.json.tmp").exists()


# --- tolerant failure modes -------------------------------------------------

def test_cache_ttl_expired_ignored(tmp_path, caplog):
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(hours=2)).isoformat()
    write_cache(tmp_path, saved_at=old)
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller, client, store = make_cache_poller(tmp_path,
                                                  cache_ttl_seconds=3600.0)
    assert poller._cache_raw_subs is None
    assert poller._detail_cache == {}
    assert any("old" in rec.message for rec in caplog.records)


def test_cache_contest_id_mismatch_ignored(tmp_path, caplog):
    write_cache(tmp_path, contest_id="other-contest")
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller, client, store = make_cache_poller(tmp_path)
    assert poller._cache_raw_subs is None
    assert poller._detail_cache == {}
    assert any("mismatch" in rec.message for rec in caplog.records)


def test_cache_corrupt_json_ignored_not_raised(tmp_path, caplog):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "state.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller, client, store = make_cache_poller(tmp_path)
    assert poller._cache_raw_subs is None
    assert poller._detail_cache == {}
    assert any("unreadable cache" in rec.message for rec in caplog.records)
    # ...and the poller still polls normally
    client.subs = [sub_row("1")]
    asyncio.run(poller.poll_once())
    assert "1" in store.snapshot().submissions


@pytest.mark.parametrize("bad", [
    {"details": None},
    {"raw_subs": "not-a-list"},
    {"saved_at": "not-a-timestamp"},
])
def test_cache_bad_shape_ignored(tmp_path, bad, caplog):
    write_cache(tmp_path, **bad)
    with caplog.at_level(logging.WARNING, logger="pta2icpc.poller"):
        poller, client, store = make_cache_poller(tmp_path)
    assert poller._cache_raw_subs is None
    assert poller._detail_cache == {}
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_cache_disabled_never_reads_or_writes(tmp_path):
    saved_at = dt.datetime.now(dt.timezone.utc).isoformat()
    cache_file = write_cache(
        tmp_path, saved_at=saved_at, raw_subs=[sub_row("1")],
        details={"1": {"status": "答案正确", "program": "x",
                       "compiler": "GXX", "run_results": []}})
    poller, client, store = make_cache_poller(tmp_path, cache_enabled=False)
    assert poller._cache_raw_subs is None
    assert poller._detail_cache == {}
    client.subs = [sub_row("1")]
    asyncio.run(poller.poll_once())
    # a disabled cache is neither read nor written: the file is untouched
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["saved_at"] == saved_at


# --- details are reused, not re-fetched ------------------------------------

def test_cached_snapshot_serves_without_detail_fetch(tmp_path):
    poller, client, store = make_cache_poller(tmp_path)
    client.subs = [sub_row("1"), sub_row("2")]
    asyncio.run(poller.poll_once())
    assert sorted(client.detail_calls) == ["1", "2"]
    poller2, client2, store2 = make_cache_poller(tmp_path)
    asyncio.run(poller2._apply_cached_snapshot())
    assert client2.detail_calls == []          # served straight from cache
    snap = store2.snapshot()
    assert snap is not None
    assert {"1", "2"} <= set(snap.submissions)
    assert snap.sources["1"] == "int main(){}"
    assert "1-1" in snap.runs
    assert poller2.status["ready"] is True


def test_run_serves_cache_before_first_poll(tmp_path, monkeypatch):
    poller, client, store = make_cache_poller(tmp_path)
    client.subs = [sub_row("1")]
    asyncio.run(poller.poll_once())
    poller2, client2, store2 = make_cache_poller(tmp_path)
    client2.subs = [sub_row("1")]

    applied: list[bool] = []
    real = poller2._apply_cached_snapshot

    async def spy():
        applied.append(True)
        await real()

    monkeypatch.setattr(poller2, "_apply_cached_snapshot", spy)

    async def go():
        task = asyncio.ensure_future(poller2.run())
        await asyncio.sleep(0)
        poller2.stop()
        await task

    asyncio.run(go())
    assert applied == [True]           # run() published the cache first
    assert client2.detail_calls == []  # ...without re-fetching details


# --- throttled writes -------------------------------------------------------

def test_cache_write_throttled_to_interval(tmp_path):
    poller, client, store = make_cache_poller(tmp_path)
    client.subs = [sub_row("1", "正在评测")]
    asyncio.run(poller.poll_once())
    cache_file = tmp_path / "cache" / "state.json"
    first = cache_file.read_text(encoding="utf-8")
    # a second round inside the throttle window: no rewrite
    client.subs = [sub_row("1", "答案正确")]
    asyncio.run(poller.poll_once())
    assert cache_file.read_text(encoding="utf-8") == first
    # once the window has passed a fresh snapshot is written
    poller._cache_last_write = (time.monotonic()
                                - CACHE_MIN_WRITE_INTERVAL - 1.0)
    asyncio.run(poller.poll_once())
    assert cache_file.read_text(encoding="utf-8") != first
    assert json.loads(cache_file.read_text(
        encoding="utf-8"))["details"]["1"]["status"] == "答案正确"


# --- PTA request jitter -----------------------------------------------------

class _FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


def _run_gates(monkeypatch, *, requests_per_second: float, jitter: float,
               uniform) -> list[float]:
    client = PtaClient(BASE_URL, CONTEST_ID, COOKIE,
                       requests_per_second=requests_per_second,
                       request_jitter=jitter)
    clock = _FakeClock()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(client_module, "time", clock)
    monkeypatch.setattr(client_module, "asyncio",
                        types.SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(client_module, "random",
                        types.SimpleNamespace(uniform=uniform))

    async def go() -> None:
        await client._rate_gate()
        await client._rate_gate()

    asyncio.run(go())
    asyncio.run(client.aclose())
    return slept


def test_rate_gate_jitter_upper_bound(monkeypatch):
    # requests_per_second=2 → min_interval 0.5 s; jitter 0.25 adds up to
    # 0.125 s (uniform → the max), so the gate interval is 0.625 s
    slept = _run_gates(monkeypatch, requests_per_second=2.0, jitter=0.25,
                       uniform=lambda a, b: b)
    assert slept == [pytest.approx(0.5 * (1 + 0.25))]


def test_rate_gate_jitter_lower_bound(monkeypatch):
    slept = _run_gates(monkeypatch, requests_per_second=2.0, jitter=0.25,
                       uniform=lambda a, b: a)
    assert slept == [pytest.approx(0.5)]


def test_rate_gate_without_jitter_exact_spacing(monkeypatch):
    slept = _run_gates(monkeypatch, requests_per_second=2.0, jitter=0.0,
                       uniform=lambda a, b: pytest.fail("uniform called"))
    assert slept == [pytest.approx(0.5)]


def test_app_passes_jitter_to_client(monkeypatch):
    from pta2icpc import app as app_module

    captured: dict = {}

    def fake_client(base_url, contest_id, cookie, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(app_module, "PtaClient", fake_client)
    monkeypatch.setattr(app_module, "Poller",
                        lambda *a, **k: object())
    settings = Settings(pta_request_jitter=0.4)
    app_module._make_poller(settings, {"contest_id": "c1", "pta_cookie": "x"},
                            {}, Store())
    assert captured["request_jitter"] == 0.4
