import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx
import pytest

from pta2ccs.models import sanitize_id
from pta2ccs.pta.client import (
    COMPILER_LANGUAGES,
    PtaAuthError,
    PtaClient,
    PtaRateLimitError,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pta"
MEMBERS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "members.json"
BASE_URL = "https://pintia.cn"
CONTEST_ID = "1000000000000000000"
COOKIE = "secret-cookie-value"
TIME_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")
MEMBERS_FILTER = (
    '{"studentUserFilter":{"keyword":"","unbindOnly":false,'
    '"field":"STUDENT_NUMBER"}}'
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_members() -> dict:
    return json.loads(MEMBERS_FIXTURE.read_text(encoding="utf-8"))


def make_client(handler, **kwargs) -> PtaClient:
    # tests must not be slowed by the production rate gate (task 14)
    kwargs.setdefault("requests_per_second", 1000.0)
    return PtaClient(BASE_URL, CONTEST_ID, COOKIE,
                     transport=httpx.MockTransport(handler), **kwargs)


async def _run(client: PtaClient, coro):
    try:
        return await coro
    finally:
        await client.aclose()


def fetch_submissions(client: PtaClient) -> list[dict]:
    return asyncio.run(_run(client, client.fetch_submissions()))


def fetch_problems(client: PtaClient) -> list[dict]:
    return asyncio.run(_run(client, client.fetch_problems()))


def fetch_teams(client: PtaClient) -> list[dict]:
    return asyncio.run(_run(client, client.fetch_teams()))


def fetch_contest_meta(client: PtaClient) -> dict:
    return asyncio.run(_run(client, client.fetch_contest_meta()))


def fetch_submission_detail(client: PtaClient, sub_id: str) -> dict:
    return asyncio.run(_run(client, client.fetch_submission_detail(sub_id)))


def expected_team(payload: dict, row: dict) -> tuple[str, str]:
    member = payload["examMemberByUserId"][row["userId"]]
    stu = payload["studentUserById"][member["studentUserId"]]
    return sanitize_id(member["studentUserId"]), stu["name"]


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
    # team identity comes from the studentUserById chain, never a student number
    team_id, team_name = expected_team(payload, raw_first)
    assert first["team_id"] == team_id
    assert first["team_name"] == team_name
    assert "user" not in first
    assert all(r["team_id"] for r in rows)
    assert [r["team_name"] for r in rows] == [
        expected_team(payload, raw)[1] for raw in raw_rows]  # studentUserById names
    dump = json.dumps(rows, ensure_ascii=False)
    for stu in payload["studentUserById"].values():
        assert stu["studentNumber"] not in dump
    assert first["problem_index"] == \
        payload["problemSetProblemById"][raw_first["problemSetProblemId"]]["label"]
    assert first["problem_index"] == "1-1"
    assert first["language"] == "C++ (g++)"  # GXX compiler table
    # list rows carry run time (seconds float) and score (float)
    assert first["run_time"] == 0.003
    assert first["score"] == 5.0
    ce_row = next(r for r in rows
                  if r["id"] == "2109955327070257152")  # COMPILE_ERROR
    assert ce_row["run_time"] == 0.0
    assert ce_row["score"] == 0.0


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


def test_fetch_problems_from_list_endpoint_with_limits():
    payload = load("problems_full.json")
    rows = payload["problemSetProblems"]
    # 2-page mock: page 0 returns exactly `limit` rows → page 1 requested
    page_size = 2
    calls: list[tuple[str | None, str | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/problem-sets/{CONTEST_ID}/problems"
        assert request.url.params.get("problemType") == "PROGRAMMING"
        page, limit = request.url.params.get("page"), request.url.params.get("limit")
        calls.append((page, limit))
        start = int(page) * page_size
        return httpx.Response(200, json=dict(
            payload, problemSetProblems=rows[start:start + page_size]))

    problems = fetch_problems(make_client(handle, page_limit=page_size))
    assert calls == [("0", "2"), ("1", "2")]  # stop when page is short
    assert [p["id"] for p in problems] == ["1-1", "1-2", "1-3"]
    assert [p["label"] for p in problems] == ["1-1", "1-2", "1-3"]
    assert [p["ordinal"] for p in problems] == [1, 2, 3]
    assert problems[0]["time_limit_ms"] == 1000   # milliseconds
    assert problems[0]["memory_limit_kb"] == 128000  # KB
    assert problems[0]["code_limit_kb"] == 64
    assert problems[1]["time_limit_ms"] == 100    # live minimum
    assert problems[2]["time_limit_ms"] is None   # no problemConfig
    assert problems[2]["memory_limit_kb"] is None
    assert problems[2]["code_limit_kb"] is None
    # dangerous fields must never appear in the normalized output
    dump = json.dumps(problems, ensure_ascii=False)
    for banned in ("content", "description", "solution", "judgeConfig",
                   "exampleTestDatas", "cases", "answer"):
        assert f'"{banned}"' not in dump


def test_fetch_problems_updates_label_map():
    """raw problemId → label mapping must survive so submissions'
    _resolve_problem_index fallback keeps working."""
    payload = load("problems_full.json")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = make_client(handle)
    try:
        asyncio.run(client.fetch_problems())
        assert client._labels["2109924519349395456"] == "1-1"
        assert client._labels["2109924519349395457"] == "1-2"
    finally:
        asyncio.run(client.aclose())


def test_fetch_submission_detail():
    payload = load("submission_detail.json")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/submissions/2109975546404745216"
        return httpx.Response(200, json=payload)

    detail = fetch_submission_detail(make_client(handle),
                                     "2109975546404745216")
    assert detail["program"].startswith("#include <iostream>")
    assert detail["compiler"] == "GXX"
    cases = {r["case"]: r for r in detail["run_results"]}
    assert set(cases) == {"1", "data00"}
    assert cases["1"]["result"] == "ACCEPTED"
    assert cases["1"]["time"] == 0.003
    assert cases["1"]["memory"] == 598016
    assert cases["1"]["exitcode"] == 0
    assert cases["data00"]["result"] == "SKIPPED"  # pending-ish cases kept
    assert cases["data00"]["time"] is None
    # privacy/CE-log policy: stdout/stderr/checkerOutput/error/log never
    # appear in the normalized output
    dump = json.dumps(detail, ensure_ascii=False)
    for banned in ("stdout", "stderr", "checkerOutput", '"error"', '"log"',
                   "fake compiler log"):
        assert banned not in dump


def test_fetch_submission_detail_defensive_shapes():
    """No submissionDetails / no judgeResponseContents → Nones/empties."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"submission": {"id": "x"}})

    detail = fetch_submission_detail(make_client(handle), "x")
    assert detail == {"program": None, "compiler": None, "run_results": []}


def test_fetch_contest_meta():
    """GET /api/problem-sets/{id} → name/freeze/compilers/times (findings §5,
    live-verified 2026-10-14: hidingTime 360 min AFTER start, duration 111600
    → 111600 − 21600 = 90000 s = 25:00:00 freeze)."""
    payload = load("contest_meta.json")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/problem-sets/{CONTEST_ID}"
        return httpx.Response(200, json=payload)

    meta = fetch_contest_meta(make_client(handle))
    ps = payload["problemSet"]
    assert meta["name"] == ps["name"]  # contest-name passthrough
    assert meta["startAt"] == ps["startAt"] == "2026-10-13T08:00:00Z"
    assert meta["endAt"] == ps["endAt"] == "2026-10-14T15:00:00Z"
    assert meta["durationSeconds"] == 111600
    assert isinstance(meta["durationSeconds"], int)
    # start-relative freeze: duration − hidingTime×60 (NOT hidingTime×60)
    assert meta["scoreboardFreezeSeconds"] == 111600 - 360 * 60 == 90000
    assert meta["compilers"] == ps["problemSetConfig"]["compilers"]
    assert all(isinstance(c, str) for c in meta["compilers"])


def test_fetch_contest_meta_scoreboard_not_hidden():
    """hideScoreboard false → freeze 0 even with a stale hidingTime."""
    payload = load("contest_meta.json")
    cfg = payload["problemSet"]["problemSetConfig"]
    cfg["hideScoreboard"] = False
    cfg["hidingTime"] = 120  # must be ignored while hiding is off

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    meta = fetch_contest_meta(make_client(handle))
    assert meta["scoreboardFreezeSeconds"] == 0


@pytest.mark.parametrize("hiding_time", [111600, 120000])
def test_fetch_contest_meta_hiding_time_at_or_over_duration(hiding_time: int):
    """hidingTime ≥ duration → freeze 0 (max(0, duration − hidingTime×60))."""
    payload = load("contest_meta.json")
    payload["problemSet"]["problemSetConfig"]["hidingTime"] = hiding_time

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    meta = fetch_contest_meta(make_client(handle))
    assert meta["scoreboardFreezeSeconds"] == 0


def test_fetch_contest_meta_hidden_but_duration_unknown():
    """hideScoreboard true + no duration → cannot compute → freeze 0."""
    payload = {"problemSet": {"problemSetConfig": {
        "hideScoreboard": True, "hidingTime": 360}}}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    meta = fetch_contest_meta(make_client(handle))
    assert meta["durationSeconds"] is None
    assert meta["scoreboardFreezeSeconds"] == 0


def test_fetch_contest_meta_absent_duration_and_config():
    """Defensive shape: missing duration/problemSetConfig → Nones/empties."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"problemSet": {"id": "x"}})

    meta = fetch_contest_meta(make_client(handle))
    assert meta == {
        "name": None, "startAt": None, "endAt": None, "durationSeconds": None,
        "compilers": [], "scoreboardFreezeSeconds": 0,
    }


# The 38 enums the API returns for the live 2026 contest, in API order
# (same order as the PTA web UI compiler list).
LIVE_COMPILER_ENUMS = [
    "GCC", "MODERN_GCC", "GXX", "CLANG", "CLANGXX", "JAVAC", "PYTHON2",
    "PYTHON3", "RUBY", "BASH", "CAT", "CLISP", "FPC", "GO", "GHC", "LUA",
    "LUAJIT", "MCS", "NODE", "OCAMLC", "PHP", "PERL", "AWK", "DMD", "RKT",
    "VALAC", "VBNC", "KOTLIN", "SWIFT", "OBJC", "FORTRAN", "OCTAVE",
    "RLANG", "ASM", "RUST", "SCALA", "PYPY3", "CANGJIE",
]


def test_compilers_table_covers_all_api_enums():
    assert len(LIVE_COMPILER_ENUMS) == 38
    missing = [e for e in LIVE_COMPILER_ENUMS if e not in COMPILER_LANGUAGES]
    assert missing == []
    # fixture, table and the live API agree on the enabled set
    fixture = load("contest_meta.json")
    assert fixture["problemSet"]["problemSetConfig"]["compilers"] == \
        LIVE_COMPILER_ENUMS
    # exact UI display strings (live-verified)
    assert COMPILER_LANGUAGES["CANGJIE"] == "仓颉 (cjc)"
    assert COMPILER_LANGUAGES["MODERN_GCC"] == "C (gcc c11)"
    assert COMPILER_LANGUAGES["GXX"] == "C++ (g++)"
    assert COMPILER_LANGUAGES["JAVAC"] == "Java (javac)"
    assert COMPILER_LANGUAGES["PYPY3"] == "Python (pypy3)"


def test_fetch_teams_merges_pages():
    """Members use page/limit pagination (NOT the submissions cursor):
    loop while a page returns exactly `limit` rows, then stop."""
    payload = load_members()
    members = payload["members"]
    page_size = 20
    calls: list[tuple[str | None, str | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/problem-sets/{CONTEST_ID}/members"
        assert request.url.params.get("filter") == MEMBERS_FILTER
        assert "%7B" in str(request.url)  # percent-encoded filter (Tomcat)
        page, limit = request.url.params.get("page"), request.url.params.get("limit")
        calls.append((page, limit))
        start = int(page) * page_size
        return httpx.Response(200, json=dict(
            payload, members=members[start:start + page_size]))

    teams = fetch_teams(make_client(handle, page_limit=page_size))
    # 49 members / 20 per page → 3 pages, stop when a page is short
    assert calls == [("0", "20"), ("1", "20"), ("2", "20")]
    assert len(teams) == len(members) == 49
    assert len({t["id"] for t in teams}) == 49  # no dupes across pages


def test_fetch_teams_normalizes_roster():
    payload = load_members()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    teams = fetch_teams(make_client(handle))
    assert len(teams) == payload["total"] == 49
    by_id = {t["id"]: t for t in teams}
    for m in payload["members"]:
        sid = m["studentUserId"]
        stu = payload["studentUserById"][sid]
        gid = m["userGroupId"]
        team = by_id[sanitize_id(sid)]
        # id = studentUserId, name = 姓名, label = 学号 (user decision
        # 2026-10-14), group = org = raw group id
        assert team["name"] == stu["name"]
        assert team["label"] == stu["studentNumber"]
        assert team["group_id"] == gid
        assert team["organization_id"] == gid
        assert team["organization_name"] == payload["userGroups"][gid]["name"]
    # student numbers are emitted ONLY as label values — never in id/name
    # (or any other field of the team row)
    numbers = {stu["studentNumber"] for stu in payload["studentUserById"].values()}
    assert numbers <= {t["label"] for t in teams}  # every number is a label
    for t in teams:
        for key, value in t.items():
            if key != "label":
                assert not any(sn in str(value) for sn in numbers)
    org_names = {t["organization_name"] for t in teams}
    assert org_names == {g["name"] for g in payload["userGroups"].values()}


def test_fetch_teams_warns_on_truncated_roster(caplog):
    """total=60 but pages stop early → roster truncation warning
    (logger pta2ccs.pta.client)."""
    payload = load_members()
    pool = payload["members"][:40]
    page_size = 20

    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page"))
        start = page * page_size
        return httpx.Response(200, json=dict(
            payload, total=60, members=pool[start:start + page_size]))

    with caplog.at_level(logging.WARNING, logger="pta2ccs.pta.client"):
        teams = fetch_teams(make_client(handle, page_limit=page_size))
    assert len(teams) == 40
    assert any("truncated" in rec.message and "40" in rec.message
               and "60" in rec.message for rec in caplog.records)


def test_fetch_teams_sanitizes_group_id_with_space():
    payload = {
        "total": 1,
        "members": [{"id": "m1", "userGroupId": "G 1",
                     "userId": "u1", "studentUserId": "s1"}],
        "userGroups": {"G 1": {"name": "机房 1"}},
        "userById": {"u1": {"nickname": "n"}},
        "studentUserById": {"s1": {"name": "张三"}},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    teams = fetch_teams(make_client(handle))
    assert teams[0]["group_id"] == "G-1"
    assert teams[0]["organization_id"] == "G-1"
    assert teams[0]["organization_name"] == "机房 1"


def test_fetch_teams_missing_group_id_is_none():
    payload = {
        "total": 2,
        "members": [
            {"id": "m1", "userId": "u1", "studentUserId": "s1"},
            {"id": "m2", "userGroupId": "1901", "userId": "u2",
             "studentUserId": "s2"},
        ],
        "userGroups": {"1901": {"name": "DSA"}},
        "userById": {},
        "studentUserById": {"s1": {"name": "甲"}, "s2": {"name": "乙"}},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    by_id = {t["id"]: t for t in fetch_teams(make_client(handle))}
    assert by_id["s1"]["group_id"] is None
    assert by_id["s1"]["organization_id"] is None
    assert by_id["s1"]["organization_name"] is None
    assert by_id["s2"]["organization_id"] == "1901"
    assert by_id["s2"]["organization_name"] == "DSA"


def test_fetch_teams_skips_member_without_any_id(caplog):
    payload = {
        "total": 2,
        "members": [
            {"userGroupId": "1901"},  # no studentUserId/userId/id
            {"id": "m2", "userGroupId": "1901", "userId": "u2",
             "studentUserId": "s2"},
        ],
        "userGroups": {"1901": {"name": "DSA"}},
        "userById": {},
        "studentUserById": {"s2": {"name": "乙"}},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with caplog.at_level(logging.WARNING, logger="pta2ccs.pta.client"):
        teams = fetch_teams(make_client(handle))
    assert [t["id"] for t in teams] == ["s2"]
    assert not any(t["id"] == "None" for t in teams)
    assert any("skip member" in rec.message for rec in caplog.records)


def test_submission_team_fallback_when_chain_broken():
    """No examMemberByUserId entry → team from userId + nickname, never a
    student number; missing nickname → empty team_name."""
    subs_payload = {
        "submissions": [
            {"id": "1", "userId": "u1", "problemSetProblemId": "424242",
             "submitAt": "2026-10-13T11:00:00Z", "status": "ACCEPTED",
             "compiler": "GXX"},
            {"id": "2", "userId": "u2", "problemSetProblemId": "424242",
             "submitAt": "2026-10-13T11:01:00Z", "status": "ACCEPTED",
             "compiler": "GXX"},
        ],
        "hasBefore": False, "hasAfter": False,
        "examMemberByUserId": {},  # broken chain
        "studentUserById": {},
        "userById": {"u1": {"nickname": "nick-u1"}},
        "problemSetProblemById": {"424242": {"id": "424242", "label": "1-1"}},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=subs_payload)

    rows = fetch_submissions(make_client(handle))
    assert rows[0]["team_id"] == sanitize_id("u1") == "u1"
    assert rows[0]["team_name"] == "nick-u1"
    assert rows[1]["team_id"] == sanitize_id("u2") == "u2"
    assert rows[1]["team_name"] == ""


def test_problem_ids_agree_with_submission_problem_index():
    """problems[].id must be a member of the ids submissions reference.

    Live smoke (2026-10-13) showed raw problemSetProblemId on problems but
    labels on submissions → scoreboard cells all `num_judged: 0`.
    """
    subs_payload = load("submissions_page1.json")
    # problems rows built from the submissions' embedded label map so the
    # two sides of problem_id agree by construction
    probs_payload = {"total": 1, "problemSetProblems": [
        {"problemId": pid, "label": info["label"], "title": ""}
        for pid, info in subs_payload["problemSetProblemById"].items()]}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/problems"):
            return httpx.Response(200, json=probs_payload)
        return httpx.Response(200, json=subs_payload)

    async def both(client: PtaClient):
        try:
            return (await client.fetch_problems(),
                    await client.fetch_submissions())
        finally:
            await client.aclose()

    problems, rows = asyncio.run(both(make_client(handle)))
    ids = {p["id"] for p in problems}
    assert ids == {i["label"] for i in subs_payload["problemSetProblemById"].values()}
    used = {r["problem_index"] for r in rows}
    assert used and used <= ids


def test_missing_label_falls_back_to_raw_problem_id_on_both_sides():
    probs_payload = {"total": 1, "problemSetProblems": [
        {"problemId": "424242", "title": "T"}]}
    subs_payload = {
        "submissions": [{
            "id": "9", "userId": "u1", "problemSetProblemId": "424242",
            "submitAt": "2026-10-13T11:00:00Z", "status": "ACCEPTED",
            "compiler": "GXX",
        }],
        "hasBefore": False, "hasAfter": False,
        "examMemberByUserId": {"u1": {"studentUserId": "s1"}},
        "studentUserById": {"s1": {"studentNumber": "2025000001", "name": "N"}},
        "problemSetProblemById": {"424242": {"id": "424242"}},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/problems"):
            return httpx.Response(200, json=probs_payload)
        return httpx.Response(200, json=subs_payload)

    async def both(client: PtaClient):
        try:
            return (await client.fetch_problems(),
                    await client.fetch_submissions())
        finally:
            await client.aclose()

    problems, rows = asyncio.run(both(make_client(handle)))
    assert problems[0]["id"] == "424242"
    assert rows[0]["problem_index"] == "424242"


# --- rate limiting (task 14: live-observed 429 storm 2026-10-14) ----------

def _handle_429(retry_after: str | None) -> object:
    def handle(request: httpx.Request) -> httpx.Response:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return httpx.Response(429, headers=headers, json={"message": "busy"})
    return handle


def test_429_raises_rate_limit_error_with_retry_after():
    client = make_client(_handle_429("7"))
    try:
        with pytest.raises(PtaRateLimitError) as excinfo:
            asyncio.run(client.fetch_submissions())
        assert excinfo.value.retry_after == 7.0
    finally:
        asyncio.run(client.aclose())


def test_429_retry_after_clamped_to_60():
    client = make_client(_handle_429("999"))
    try:
        with pytest.raises(PtaRateLimitError) as excinfo:
            asyncio.run(client.fetch_submissions())
        assert excinfo.value.retry_after == 60.0
    finally:
        asyncio.run(client.aclose())


def test_429_missing_retry_after_defaults_to_5():
    client = make_client(_handle_429(None))
    try:
        with pytest.raises(PtaRateLimitError) as excinfo:
            asyncio.run(client.fetch_submissions())
        assert excinfo.value.retry_after == 5.0
    finally:
        asyncio.run(client.aclose())


def test_429_invalid_retry_after_defaults_to_5():
    client = make_client(_handle_429("soon"))
    try:
        with pytest.raises(PtaRateLimitError) as excinfo:
            asyncio.run(client.fetch_submissions())
        assert excinfo.value.retry_after == 5.0
    finally:
        asyncio.run(client.aclose())


def _ok_subs_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"submissions": [], "hasBefore": False})


def test_rate_gate_spaces_sequential_requests():
    # requests_per_second=10 → 0.1 s minimum spacing between requests
    client = PtaClient(BASE_URL, CONTEST_ID, COOKIE,
                       transport=httpx.MockTransport(_ok_subs_handler),
                       requests_per_second=10)

    async def go() -> float:
        try:
            t0 = time.monotonic()
            await client.fetch_submissions()
            await client.fetch_submissions()
            return time.monotonic() - t0
        finally:
            await client.aclose()

    elapsed = asyncio.run(go())
    assert elapsed >= 0.09  # one 0.1 s gap between the two requests


def test_rate_gate_serializes_concurrent_callers():
    # requests_per_second=20 → 0.05 s spacing; 3 concurrent callers must
    # still be spaced (gate serializes acquisition), ~0.1 s total
    client = PtaClient(BASE_URL, CONTEST_ID, COOKIE,
                       transport=httpx.MockTransport(_ok_subs_handler),
                       requests_per_second=20)

    async def go() -> float:
        try:
            t0 = time.monotonic()
            await asyncio.gather(*(client.fetch_submissions()
                                   for _ in range(3)))
            return time.monotonic() - t0
        finally:
            await client.aclose()

    elapsed = asyncio.run(go())
    assert elapsed >= 0.09  # 2 gaps of 0.05 s
