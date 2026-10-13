from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..timefmt import parse_offset

log = logging.getLogger(__name__)

# Findings §"Recommended normalization": compiler enum → UI language string.
COMPILER_LANGUAGES: dict[str, str] = {
    "GXX": "C++ (g++)",
    "JAVAC": "Java",
    "PYPY3": "PyPy3",
}


class PtaAuthError(Exception):
    pass


class PtaClient:
    """Async client for the pintia.cn internal API (docs/pta-api-findings.md).

    Deviations from the brief skeleton are documented in
    .superpowers/sdd/task-6-report.md: real /api endpoints, cursor
    pagination (after/before/hasBefore; total is always 0), row
    normalization, and optional tz/transport kwargs.
    """

    def __init__(self, base_url: str, contest_id: str, cookie: str, *,
                 tz: str = "+08:00",
                 transport: httpx.AsyncBaseTransport | None = None,
                 page_limit: int = 50):
        self.submissions_url = (
            f"{base_url}/api/problem-sets/{contest_id}/submissions")
        self.problems_url = (
            f"{base_url}/api/problem-sets/{contest_id}/problem-types")
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
        """Problem list from the labels endpoint the submissions UI uses."""
        data = await self._get_json(self.problems_url)
        labels = data.get("labels") if isinstance(data, dict) else data
        out: list[dict] = []
        for i, p in enumerate(labels or []):
            pid = str(p.get("id", i))
            label = p.get("label")
            if label:
                self._labels[pid] = label
            out.append({
                "id": pid,
                "label": label,
                "title": p.get("title", ""),
                "ordinal": p.get("indexInProblemPool", i + 1),
            })
        return out

    async def fetch_teams(self) -> list[dict] | None:
        # No standalone team endpoint: names are embedded in every
        # submissions response (userById/studentUserById maps) and
        # reverse-derived in translate.py — see findings §4.
        return None

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

    def _resolve_user(self, row: dict, data: dict) -> str:
        uid = row.get("userId")
        member = (data.get("examMemberByUserId") or {}).get(uid) or {}
        stu = (data.get("studentUserById") or {}).get(
            member.get("studentUserId")) or {}
        if stu.get("studentNumber") and stu.get("name"):
            return f"{stu['studentNumber']} {stu['name']}"
        nickname = (data.get("userById") or {}).get(uid) or {}
        return str(nickname.get("nickname") or "")

    def _resolve_problem_index(self, row: dict, data: dict) -> str:
        pid = row.get("problemSetProblemId")
        info = (data.get("problemSetProblemById") or {}).get(pid) or {}
        label = info.get("label")
        if label:
            return str(label)
        return self._labels.get(str(pid), "")

    def _normalize(self, row: dict, data: dict) -> dict:
        compiler = row.get("language") or row.get("compiler")
        return {
            "id": str(row["id"]),
            "submit_time": self._fmt_submit_time(row.get("submitAt")),
            "status": row.get("status"),  # raw enum; translate maps it
            "user": self._resolve_user(row, data),
            "problem_index": self._resolve_problem_index(row, data),
            "language": COMPILER_LANGUAGES.get(compiler, compiler or "unknown"),
        }
