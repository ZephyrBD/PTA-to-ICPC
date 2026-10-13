import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from pta2ccs.pta.client import PtaAuthError, PtaClient

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pta"
BASE_URL = "https://pintia.cn"
CONTEST_ID = "1000000000000000000"
COOKIE = "secret-cookie-value"
TIME_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_client(handler) -> PtaClient:
    return PtaClient(BASE_URL, CONTEST_ID, COOKIE,
                     transport=httpx.MockTransport(handler))


async def _run(client: PtaClient, coro):
    try:
        return await coro
    finally:
        await client.aclose()


def fetch_submissions(client: PtaClient) -> list[dict]:
    return asyncio.run(_run(client, client.fetch_submissions()))


def fetch_problems(client: PtaClient) -> list[dict]:
    return asyncio.run(_run(client, client.fetch_problems()))


def expected_user(payload: dict, row: dict) -> str:
    member = payload["examMemberByUserId"][row["userId"]]
    stu = payload["studentUserById"][member["studentUserId"]]
    return f"{stu['studentNumber']} {stu['name']}"


def test_fetch_submissions_normalizes_rows():
    payload = load("submissions_page1.json")
    raw_rows = payload["submissions"]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/problem-sets/{CONTEST_ID}/submissions"
        return httpx.Response(200, json=payload)

    rows = fetch_submissions(make_client(handle))
    assert len(rows) == len(raw_rows)
    assert [r["id"] for r in rows] == [r["id"] for r in raw_rows]
    assert all(isinstance(r["id"], str) for r in rows)
    assert all(TIME_RE.match(r["submit_time"]) for r in rows)

    first, raw_first = rows[0], raw_rows[0]
    # submitAt is UTC ("2026-10-13T11:52:26Z"); client renders contest-local
    # wall time for parse_pta_time (+08:00 default).
    assert first["submit_time"] == "2026/10/13 19:52:26"
    # enum passthrough, no translation
    assert [r["status"] for r in rows] == [r["status"] for r in raw_rows]
    assert first["status"] == "ACCEPTED"
    assert first["user"] == expected_user(payload, raw_first)
    assert all(r["user"] for r in rows)
    assert first["problem_index"] == \
        payload["problemSetProblemById"][raw_first["problemSetProblemId"]]["label"]
    assert first["problem_index"] == "1-1"
    assert first["language"] == "C++ (g++)"  # GXX compiler table


def test_fetch_submissions_cursor_pagination():
    payload = load("submissions_page1.json")
    all_rows = payload["submissions"]
    page1 = dict(payload, submissions=all_rows[:2], hasBefore=True)
    page2 = dict(payload, submissions=all_rows[2:4], hasBefore=False)
    calls: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        before = request.url.params.get("before")
        calls.append(before)
        if before is None:
            return httpx.Response(200, json=page1)
        assert before == all_rows[1]["id"]  # cursor = last row of previous page
        return httpx.Response(200, json=page2)

    rows = fetch_submissions(make_client(handle))
    assert [r["id"] for r in rows] == [r["id"] for r in all_rows[:4]]
    assert calls == [None, all_rows[1]["id"]]


def test_request_headers_and_filter_encoding():
    payload = load("submissions_page1.json")
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        seen["cookies"] = request.headers.get_list("cookie")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    fetch_submissions(make_client(handle))
    assert seen["accept"] == "application/json;charset=UTF-8"
    assert seen["cookies"] == [f"PTASession={COOKIE}"]  # single cookie, no dup
    assert "filter=%7B%7D" in seen["url"]  # percent-encoded (Tomcat pitfall)
    assert "limit=" in seen["url"]


@pytest.mark.parametrize("code", [401, 403])
def test_auth_error_on_401_403(code: int):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"message": "unauthorized"})

    with pytest.raises(PtaAuthError):
        fetch_submissions(make_client(handle))


def test_server_error_raises_httpx_error():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(httpx.HTTPError):
        fetch_submissions(make_client(handle))


def test_cookie_never_logged(caplog):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(httpx.HTTPError):
            fetch_submissions(make_client(handle))
    assert COOKIE not in caplog.text


def test_fetch_problems_from_labels_endpoint():
    payload = load("problems.json")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/problem-sets/{CONTEST_ID}/problem-types"
        return httpx.Response(200, json=payload)

    problems = fetch_problems(make_client(handle))
    assert len(problems) == len(payload["labels"])
    assert [p["id"] for p in problems] == [l["id"] for l in payload["labels"]]
    assert problems[0]["label"] == payload["labels"][0]["label"] == "1-1"
    assert all("title" in p and p["id"] for p in problems)


def test_fetch_teams_returns_none():
    client = make_client(lambda request: httpx.Response(500))
    assert asyncio.run(_run(client, client.fetch_teams())) is None
