from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..models import sanitize_id
from ..timefmt import parse_offset

log = logging.getLogger(__name__)

# Findings §"Recommended normalization": compiler enum → UI language string.
COMPILER_LANGUAGES: dict[str, str] = {
    "GXX": "C++ (g++)",
    "JAVAC": "Java",
    "PYPY3": "PyPy3",
}

# Findings §4: the /members endpoint needs this filter (percent-encoded by
# httpx — Tomcat rejects raw {}). Pagination is page/limit, NOT the
# before/hasBefore cursor used by /submissions.
MEMBERS_FILTER = (
    '{"studentUserFilter":{"keyword":"","unbindOnly":false,'
    '"field":"STUDENT_NUMBER"}}'
)
MAX_MEMBERS_PAGES = 50


class PtaAuthError(Exception):
    pass


class PtaClient:
    """Async client for the pintia.cn internal API (docs/pta-api-findings.md).

    Deviations from the brief skeleton are documented in
    .superpowers/sdd/task-6-report.md: real /api endpoints, cursor
    pagination for submissions (after/before/hasBefore; total is always 0),
    page/limit pagination for the /members roster, row normalization, and
    optional tz/transport kwargs.
    """

    def __init__(self, base_url: str, contest_id: str, cookie: str, *,
                 tz: str = "+08:00",
                 transport: httpx.AsyncBaseTransport | None = None,
                 page_limit: int = 50):
        self.submissions_url = (
            f"{base_url}/api/problem-sets/{contest_id}/submissions")
        self.problems_url = (
            f"{base_url}/api/problem-sets/{contest_id}/problem-types")
        self.members_url = (
            f"{base_url}/api/problem-sets/{contest_id}/members")
        self._tz = tz
        self._page_limit = page_limit
        # problemSetProblemId → label; refreshed by fetch_problems and by
        # every submissions response (embedded problemSetProblemById map).
        self._labels: dict[str, str] = {}
        # Never log or expose this dict; a single Cookie header avoids the
        # duplicate-anonymous-PTASession pitfall (findings §Authentication).
        self._headers = {
            "Accept": "application/json;charset=UTF-8",
            "Cookie": f"PTASession={cookie}",
        }
        self._client = httpx.AsyncClient(
            headers=self._headers, timeout=10.0, follow_redirects=True,
            transport=transport)

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        resp = await self._client.get(url, params=params)
        if resp.status_code in (401, 403):
            raise PtaAuthError(f"PTA rejected request: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    async def fetch_submissions(self) -> list[dict]:
        """Full snapshot, newest first, via cursor pagination.

        `before=<id>` pages backwards in time while `hasBefore` is true
        (findings §1; `total` is always 0 — never used).
        """
        out: dict[str, dict] = {}
        cursor: str | None = None
        while True:
            # httpx percent-encodes filter={} → %7B%7D (Tomcat pitfall).
            params: dict = {"filter": "{}", "limit": self._page_limit}
            if cursor is not None:
                params["before"] = cursor
            data = await self._get_json(self.submissions_url, params)
            self._remember_labels(data.get("problemSetProblemById") or {})
            rows = data.get("submissions") or []
            for row in rows:
                norm = self._normalize(row, data)
                out.setdefault(norm["id"], norm)
            if not data.get("hasBefore") or not rows:
                break
            nxt = str(rows[-1]["id"])
            if nxt == cursor:  # server echoed cursor: guard against loops
                break
            cursor = nxt
        return list(out.values())

    async def fetch_problems(self) -> list[dict]:
        """Problem list from the labels endpoint the submissions UI uses.

        `id` is the PTA **label** (e.g. "1-3"), not the raw
        `problemSetProblemId` — submissions normalize `problem_index` to the
        label too, so both sides of `submissions[].problem_id` agree
        (live smoke 2026-10-13: raw ids here vs labels on submissions left
        every scoreboard cell at `num_judged: 0`). `ordinal` follows the
        response order: `indexInProblemPool` is per-pool and was 1 for all
        15 problems of the probed set (`problemPoolIndex` is the global
        pool order but collides for multi-problem pools).
        """
        data = await self._get_json(self.problems_url)
        labels = data.get("labels") if isinstance(data, dict) else data
        out: list[dict] = []
        for i, p in enumerate(labels or []):
            raw_id = str(p.get("id", i))
            label = p.get("label")
            if label:
                self._labels[raw_id] = str(label)
            out.append({
                "id": str(label) if label else raw_id,
                "label": label,
                "title": p.get("title", ""),
                "ordinal": i + 1,
            })
        return out

    async def fetch_teams(self) -> list[dict]:
        """Full roster from the /members endpoint (findings §4).

        Page/limit pagination with a real `total` (NOT the submissions
        cursor): loop while a page returns exactly `limit` rows, with a
        sane page-count guard. Every member becomes a team — id =
        studentUserId (sanitized; digits pass through), name = label =
        姓名, group_id = organization_id = raw userGroupId (long numeric
        string). Student numbers are resolved to names but NEVER appear
        in the output.
        """
        members: list[dict] = []
        groups: dict[str, str] = {}
        students: dict[str, dict] = {}
        users: dict[str, dict] = {}
        for page in range(MAX_MEMBERS_PAGES):
            params = {"page": page, "limit": self._page_limit,
                      "filter": MEMBERS_FILTER}
            data = await self._get_json(self.members_url, params)
            groups.update({str(gid): str((g or {}).get("name") or gid)
                           for gid, g in (data.get("userGroups") or {}).items()})
            students.update(data.get("studentUserById") or {})
            users.update(data.get("userById") or {})
            rows = data.get("members") or []
            members.extend(rows)
            if len(rows) < self._page_limit:
                break
        out: dict[str, dict] = {}
        for m in members:
            uid = m.get("userId")
            sid = m.get("studentUserId")
            stu = students.get(sid) or {}
            nick = users.get(uid) or {}
            gid = str(m.get("userGroupId") or "")
            team_id = sanitize_id(str(sid or uid or m.get("id")))
            name = str(stu.get("name") or nick.get("nickname") or "")
            out.setdefault(team_id, {
                "id": team_id, "name": name, "label": name,
                "group_id": gid, "organization_id": gid,
                "organization_name": groups.get(gid, gid),
            })
        return list(out.values())

    async def aclose(self) -> None:
        await self._client.aclose()

    def _remember_labels(self, problem_map: dict) -> None:
        for pid, info in problem_map.items():
            label = (info or {}).get("label")
            if label:
                self._labels[str(pid)] = label

    def _fmt_submit_time(self, submit_at: str | None) -> str:
        """ISO-8601 UTC `...Z` → `%Y/%m/%d %H:%M:%S` contest-local wall time
        (the form parse_pta_time expects before it re-attaches pta_timezone).
        """
        when = dt.datetime.fromisoformat(str(submit_at).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when.astimezone(parse_offset(self._tz)).strftime("%Y/%m/%d %H:%M:%S")

    def _resolve_team(self, row: dict, data: dict) -> tuple[str, str]:
        """(team_id, team_name) for a submission row.

        Chain: userId → examMemberByUserId[userId].studentUserId →
        studentUserById[...].name (team id = studentUserId, matching
        fetch_teams' roster). When any link is broken, fall back to
        sanitize_id(userId) + userById nickname. Student numbers are
        never part of the identity — they must not leak into any API
        output.
        """
        uid = row.get("userId")
        member = (data.get("examMemberByUserId") or {}).get(uid) or {}
        sid = member.get("studentUserId")
        nick = str(((data.get("userById") or {}).get(uid) or {})
                   .get("nickname") or "")
        if sid:
            name = str(((data.get("studentUserById") or {}).get(sid) or {})
                       .get("name") or nick)
            return sanitize_id(str(sid)), name
        return sanitize_id(str(uid)) if uid not in (None, "") else "", nick

    def _resolve_problem_index(self, row: dict, data: dict) -> str:
        pid = row.get("problemSetProblemId")
        info = (data.get("problemSetProblemById") or {}).get(pid) or {}
        label = info.get("label")
        if label:
            return str(label)
        # Same fallback as fetch_problems' id: never emit "" so the row still
        # matches a problems entry when the label map is incomplete.
        return self._labels.get(str(pid)) or str(pid)

    def _normalize(self, row: dict, data: dict) -> dict:
        compiler = row.get("language") or row.get("compiler")
        team_id, team_name = self._resolve_team(row, data)
        return {
            "id": str(row["id"]),
            "submit_time": self._fmt_submit_time(row.get("submitAt")),
            "status": row.get("status"),  # raw enum; translate maps it
            "team_id": team_id,
            "team_name": team_name,
            "problem_index": self._resolve_problem_index(row, data),
            "language": COMPILER_LANGUAGES.get(compiler, compiler or "unknown"),
        }
