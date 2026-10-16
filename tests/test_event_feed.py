import asyncio
import contextlib
import json
import datetime as dt

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ptapatch.app import create_app
from ptapatch.event_feed import register  # noqa: F401  (used via create_app)
from ptapatch.store import Store
from ptapatch.translate import translate

NOW = dt.datetime(2023, 5, 21, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
ENDED = dt.datetime(2023, 5, 21, 15, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
MANUAL = {"contest": {"start_time": "2023-05-21T09:00:00+08:00",
                      "duration": "5:00:00", "pta_timezone": "+08:00"}}
PROBLEMS = [{"id": "A", "title": "A"}]


def sub(sid: str) -> dict:
    return {"id": sid, "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
            "team_id": "D22", "team_name": "队", "problem_index": "A", "language": "cpp"}


def make(store: Store) -> TestClient:
    app = create_app(store, poller=None, contest_id="c1")
    return TestClient(app)


def app_for(store: Store) -> FastAPI:
    return create_app(store, poller=None, contest_id="c1")


async def _stream(app: FastAPI, url: str, *, until_blank: bool = False,
                  quiet: float = 0.2, timeout: float = 5.0) -> tuple[list[dict], bool]:
    """Drive the ASGI app directly and read http.response.body messages as
    they are sent. starlette 1.8's TestClient and httpx's ASGITransport both
    await the app to completion and buffer the whole body before returning,
    which deadlocks on the never-ending feed — so the brief's c.stream()
    tests cannot run against these versions; the assertions themselves are
    unchanged. spec_version 2.4 tells StreamingResponse not to spawn its
    disconnect listener (nothing here ever sends http.disconnect).

    Replay events are yielded back-to-back; once the feed enters its poll
    loop, message gaps are >= POLL_SECONDS (0.5s), so a `quiet` gap shorter
    than that means the replay is exhausted and the feed went idle."""
    path, _, query = url.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    queue: asyncio.Queue = asyncio.Queue()

    async def send(message):
        await queue.put(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": True}

    task = asyncio.create_task(app(scope, receive, send))
    events: list[dict] = []
    saw_blank = False
    try:
        async with asyncio.timeout(timeout):
            start = await queue.get()
            assert start["type"] == "http.response.start"
            assert start["status"] == 200, start["status"]
            headers = {k.decode().lower(): v.decode()
                       for k, v in start.get("headers", [])}
            assert "ndjson" in headers.get("content-type", "")
            buf = b""
            while True:
                if until_blank and saw_blank:
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=quiet)
                except TimeoutError:
                    break
                if message["type"] != "http.response.body":
                    continue
                buf += message.get("body", b"")
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        events.append(json.loads(line))
                    else:
                        saw_blank = True
                if not message.get("more_body", False):
                    break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return events, saw_blank


def test_initial_feed_contains_snapshot_events():
    store = Store()
    store.apply_all(translate("c1", MANUAL, [sub("1")], [{"id": "A", "title": "A"}], None, NOW))
    # brief read "the first 5 lines", but the snapshot replays 17 events and
    # "submissions" is #16 — the driver collects the whole replay instead
    events, _ = asyncio.run(_stream(app_for(store), "/api/v4/contests/c1/event-feed"))
    types = {e["type"] for e in events}
    assert "submissions" in types
    assert "contest" in types and "contests" not in types
    assert all("token" in e for e in events)
    # spec: notification token is a string
    assert all(isinstance(e["token"], str) for e in events)


def test_since_token_replay_only_newer():
    store = Store()
    store.apply_all(translate("c1", MANUAL, [sub("1")], [{"id": "A", "title": "A"}], None, NOW))
    checkpoint = store.latest_token()
    store.apply_all(translate("c1", MANUAL, [sub("1"), sub("2")],
                              [{"id": "A", "title": "A"}], None, NOW))
    # the second apply_all emits exactly 2 newer events (submissions "2" +
    # judgements "2"); the brief's ">=3 lines" cap could never be satisfied
    # on an idle stream without waiting out the 60s heartbeat
    events, _ = asyncio.run(_stream(app_for(store),
                                    f"/api/v4/contests/c1/event-feed?since_token={checkpoint}"))
    assert events, "expected replay of events newer than checkpoint"
    assert all(int(e["token"]) > checkpoint for e in events)
    assert any(e.get("id") == "2" for e in events)


def test_invalid_since_token_400():
    store = Store()
    store.apply_all(translate("c1", MANUAL, [sub("1")], [{"id": "A", "title": "A"}], None, NOW))
    c = make(store)
    r = c.get("/api/v4/contests/c1/event-feed?since_token=999999999")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_non_numeric_since_token_400():
    store = Store()
    store.apply_all(translate("c1", MANUAL, [sub("1")], [{"id": "A", "title": "A"}], None, NOW))
    c = make(store)
    r = c.get("/api/v4/contests/c1/event-feed?since_token=abc")
    assert r.status_code == 400
    assert r.json()["code"] == 400
    assert "abc" in r.json()["message"]


def test_heartbeat_on_idle_stream(monkeypatch):
    monkeypatch.setattr("ptapatch.event_feed.HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr("ptapatch.event_feed.POLL_SECONDS", 0.01)
    _, saw_blank = asyncio.run(_stream(app_for(Store()),
                                       "/api/v4/contests/c1/event-feed",
                                       until_blank=True, timeout=2.0))
    assert saw_blank


# --- persons events (task: 扮演 DOMjudge) -----------------------------------

def persons_manual() -> dict:
    return {"contest": dict(MANUAL["contest"]),
            "teams": {"D22": {"members": ["甲"], "coaches": ["丙"]}}}


def test_initial_feed_contains_persons_create_events():
    store = Store()
    store.apply_all(translate("c1", persons_manual(), [sub("1")],
                              [{"id": "A", "title": "A"}], None, NOW))
    events, _ = asyncio.run(_stream(app_for(store),
                                    "/api/v4/contests/c1/event-feed"))
    persons = [e for e in events if e["type"] == "persons"]
    assert {e["id"] for e in persons} == {"D22-m0", "D22-c0"}
    created = next(e for e in persons if e["id"] == "D22-m0")
    assert created["data"]["name"] == "甲"
    assert created["data"]["role"] == "contestant"
    assert created["data"]["team_ids"] == ["D22"]
    assert int(created["token"]) > 0

def test_removed_person_emits_delete_event_with_null_data():
    store = Store()
    store.apply_all(translate("c1", persons_manual(), [sub("1")],
                              [{"id": "A", "title": "A"}], None, NOW))
    checkpoint = store.latest_token()
    # next snapshot: members/coaches gone from the manual team
    store.apply_all(translate("c1", {"contest": dict(MANUAL["contest"]),
                                     "teams": {"D22": {}}},
                              [sub("1")], [{"id": "A", "title": "A"}],
                              None, NOW))
    events, _ = asyncio.run(_stream(
        app_for(store),
        f"/api/v4/contests/c1/event-feed?since_token={checkpoint}"))
    persons = [e for e in events if e["type"] == "persons"]
    assert {e["id"] for e in persons} == {"D22-m0", "D22-c0"}
    assert all(e["data"] is None for e in persons)  # delete: data null
    assert all(int(e["token"]) > checkpoint for e in persons)


# --- state ordering on the wire (task 22 review blocker) -------------------

def test_full_replay_ends_with_state_event():
    store = Store()
    store.apply_all(translate("c1", MANUAL, [sub("1")], PROBLEMS, None, ENDED))
    events, _ = asyncio.run(_stream(app_for(store),
                                    "/api/v4/contests/c1/event-feed"))
    assert any(e["type"] == "submissions" for e in events)
    # the end_of_updates state event must be the last thing on the wire: a
    # direct check of the final element fails if any event follows it
    assert events[-1]["type"] == "state"
    assert events[-1]["data"]["end_of_updates"] is not None


def test_finalizing_poll_state_event_is_last_line():
    store = Store()
    before = dt.datetime(2023, 5, 21, 10, 0, 0,
                         tzinfo=dt.timezone(dt.timedelta(hours=8)))
    store.apply_all(translate("c1", MANUAL, [sub("1")], PROBLEMS, None, before))
    checkpoint = store.latest_token()
    # finalizing poll also introduces a submission + judgement: those must
    # stream BEFORE the state event, never after
    store.apply_all(translate("c1", MANUAL, [sub("1"), sub("2")], PROBLEMS,
                              None, ENDED))
    assert store._events[-1].type == "state"
    assert store._events[-1].data["end_of_updates"] is not None
    events, _ = asyncio.run(_stream(
        app_for(store),
        f"/api/v4/contests/c1/event-feed?since_token={checkpoint}"))
    assert any(e["type"] == "submissions" for e in events)
    assert events[-1]["type"] == "state"
    assert events[-1]["data"]["end_of_updates"] is not None
