from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import httpx

from ..models import sanitize_id
from ..timefmt import parse_offset

log = logging.getLogger(__name__)

# Findings §"Recommended normalization": compiler enum → UI language string
# (网页设置页显示名; live-verified 2026-10-14 against all 38 enabled
# compilers). MODERN_GCC is "C (gcc c11)", not a C++ variant.
COMPILER_LANGUAGES: dict[str, str] = {
    "GCC": "C (gcc)",
    "MODERN_GCC": "C (gcc c11)",
    "GXX": "C++ (g++)",
    "CLANG": "C (clang)",
    "CLANGXX": "C++ (clang++)",
    "JAVAC": "Java (javac)",
    "PYTHON2": "Python (python2)",
    "PYTHON3": "Python (python3)",
    "RUBY": "Ruby (ruby)",
    "BASH": "Bash (bash)",
    "CAT": "Plaintext (cat)",
    "CLISP": "Common Lisp (sbcl)",
    "FPC": "Pascal (fpc)",
    "GO": "Go (go)",
    "GHC": "Haskell (ghc)",
    "LUA": "Lua (lua)",
    "LUAJIT": "Lua (luajit)",
    "MCS": "C# (dotnet)",
    "NODE": "JavaScript (node)",
    "OCAMLC": "OCaml (ocamlc)",
    "PHP": "PHP (php)",
    "PERL": "Perl (perl)",
    "AWK": "AWK (awk)",
    "DMD": "D (dmd)",
    "RKT": "Racket (racket)",
    "VALAC": "Vala (valac)",
    "VBNC": "Visual Basic (dotnet)",
    "KOTLIN": "Kotlin (kotlinc)",
    "SWIFT": "Swift (swiftc)",
    "OBJC": "Objective-C (clang)",
    "FORTRAN": "Fortran95 (gfortran)",
    "OCTAVE": "Octave (octave-cli)",
    "RLANG": "R (R)",
    "ASM": "ASM (nasm.sh)",
    "RUST": "Rust (rustc)",
    "SCALA": "Scala (scalac)",
    "PYPY3": "Python (pypy3)",
    "CANGJIE": "仓颉 (cjc)",
}

# Findings §4: the /members endpoint needs this filter (percent-encoded by
# httpx — Tomcat rejects raw {}). Pagination is page/limit, NOT the
# before/hasBefore cursor used by /submissions.
MEMBERS_FILTER = (
    '{"studentUserFilter":{"keyword":"","unbindOnly":false,'
    '"field":"STUDENT_NUMBER"}}'
)
MAX_MEMBERS_PAGES = 50
MAX_PROBLEMS_PAGES = 50


def _to_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class PtaAuthError(Exception):
    pass


class PtaRateLimitError(Exception):
    """HTTP 429 from PTA; poller backs off `retry_after` seconds.

    Live-observed 2026-10-14: a restart burst (~50 detail requests in <2 s)
    triggered a 429 storm on /api/submissions/{id} and /problems. The
    client never auto-retries — the poller owns the backoff decision.
    """

    def __init__(self, message: str = "PTA rate limited",
                 *, retry_after: float = 5.0):
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float:
    """Retry-After header (seconds) → float, clamped to [1.0, 60.0];
    missing/invalid → 5.0."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 5.0
    return min(60.0, max(1.0, seconds))


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
                 page_limit: int = 50,
                 requests_per_second: float = 2.0):
        self.submissions_url = (
            f"{base_url}/api/problem-sets/{contest_id}/submissions")
        # per-submission detail (source code + per-case runs; findings §2)
        self.submissions_api_url = f"{base_url}/api/submissions"
        # full problems list with programmingProblemConfig limits
        # (findings §3; replaces the labels-only problem-types endpoint)
        self.problems_url = (
            f"{base_url}/api/problem-sets/{contest_id}/problems")
        self.members_url = (
            f"{base_url}/api/problem-sets/{contest_id}/members")
        self.contest_meta_url = (
            f"{base_url}/api/problem-sets/{contest_id}")
        self._tz = tz
        self._page_limit = page_limit
        # Global rate gate: every HTTP request (list pages, details, config)
        # passes through; minimum spacing = 1/rps via monotonic clock under
        # an asyncio.Lock (concurrency is serialized at the gate, so e.g.
        # 2 concurrent detail fetches are still 0.5 s apart at 2 rps).
        # Steady-state budget ≪ cap: list ~0.6 rps + details ≤1 rps
        # (detail_fetch_per_round=5, DETAIL_CONCURRENCY=2) + config
        # negligible — all well under the default 2.0 rps cap (task 14).
        self._min_interval = 1.0 / requests_per_second
        self._gate_lock = asyncio.Lock()
        self._next_request_at = 0.0  # time.monotonic()
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

    async def _rate_gate(self) -> None:
        """Wait until 1/rps seconds since the previous request started.

        Serialized under a lock so concurrent callers are spaced, not
        batched (concurrency 4 still fires one request every interval).
        """
        async with self._gate_lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_request_at = now + self._min_interval

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        await self._rate_gate()
        resp = await self._client.get(url, params=params)
        if resp.status_code == 429:
            raise PtaRateLimitError(
                retry_after=_parse_retry_after(resp.headers.get("Retry-After")))
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
        """Full problems list from GET /problems (findings §3).

        page/limit pagination like /members (loop while a page returns
        exactly `limit` rows). `id` is the PTA **label** (e.g. "1-3"), not
        the raw `problemSetProblemId` — submissions normalize
        `problem_index` to the label too, so both sides of
        `submissions[].problem_id` agree (live smoke 2026-10-13: raw ids
        here vs labels on submissions left every scoreboard cell at
        `num_judged: 0`). `ordinal` follows the response order.

        Limits come from `problemConfig.programmingProblemConfig`:
        timeLimit is MILLISECONDS (live 100–1000), memoryLimit and
        codeSizeLimit are KB. Only label/title/limits are extracted —
        the response also embeds content/description/solution/judgeConfig
        (题面 + reference answer), which must NEVER be stored or returned.
        """
        rows: list[dict] = []
        for page in range(MAX_PROBLEMS_PAGES):
            params = {"page": page, "limit": self._page_limit,
                      "problemType": "PROGRAMMING"}
            data = await self._get_json(self.problems_url, params)
            batch = data.get("problemSetProblems") or []
            rows.extend(batch)
            if len(batch) < self._page_limit:
                break
        out: list[dict] = []
        for i, p in enumerate(rows):
            raw_id = str(p.get("problemId", p.get("id", i)))
            label = p.get("label")
            if label:
                self._labels[raw_id] = str(label)
            cfg = ((p.get("problemConfig") or {})
                   .get("programmingProblemConfig") or {})
            cases = cfg.get("cases")
            out.append({
                "id": str(label) if label else raw_id,
                "label": label,
                "title": p.get("title", ""),
                "ordinal": i + 1,
                "time_limit_ms": _to_int(cfg.get("timeLimit")),
                "memory_limit_kb": _to_int(cfg.get("memoryLimit")),
                "code_limit_kb": _to_int(cfg.get("codeSizeLimit")),
                # CCS problem.test_data_count = number of test data sets.
                # Only the LENGTH is emitted — the `cases` content (per-case
                # hints / sample I/O) must never be stored or returned. Live
                # PTA returns `cases` as a dict keyed by case name (e.g.
                # "data00"), not a list; both shapes count the same way.
                "test_data_count": (len(cases)
                                    if isinstance(cases, (list, dict)) else None),
            })
        return out

    async def fetch_contest_meta(self) -> dict:
        """Contest metadata from GET /api/problem-sets/{id} (findings §5).

        `name` is the raw problemSet.name — the contest-name source for
        translate when manual/00-contest.json sets no explicit name.

        Freeze: problemSetConfig.hideScoreboard + hidingTime (minutes AFTER
        the contest STARTS — user-confirmed 2026-10-14, correcting the
        earlier end-relative reading). The raw minutes pass through as
        `hidingTimeMinutes` (None when hiding is off): the Contest API's
        scoreboard_freeze_duration is end-relative and must be computed
        against the FINAL duration in translate — the manual override may
        shorten the contest (live 2026-10-15: PTA window 79h vs manual
        31h), so computing it here against PTA's duration froze the
        scoreboard relative to the wrong end (start − 42h). Compilers are
        raw enums — translate maps them through COMPILER_LANGUAGES.
        startAt/endAt stay in PTA's ISO-8601 UTC form; translate attaches
        pta_timezone.
        """
        data = await self._get_json(self.contest_meta_url)
        ps = data.get("problemSet") or {}
        cfg = ps.get("problemSetConfig") or {}
        duration = ps.get("duration")
        duration_s = int(duration) if duration is not None else None
        hiding_minutes: int | None = None
        if cfg.get("hideScoreboard"):
            try:
                hiding_minutes = max(0, int(cfg.get("hidingTime") or 0))
            except (TypeError, ValueError):
                hiding_minutes = None
        return {
            "name": ps.get("name"),
            "startAt": ps.get("startAt"),
            "endAt": ps.get("endAt"),
            "durationSeconds": duration_s,
            "compilers": [str(c) for c in (cfg.get("compilers") or [])],
            "hidingTimeMinutes": hiding_minutes,
        }

    async def fetch_submission_detail(self, sub_id: str) -> dict:
        """Per-submission detail from GET /api/submissions/{id}
        (findings §2). Returns only the safe subset: program (source),
        compiler enum, and per-case run results. Never log the program or
        the raw response — it also embeds stdout/stderr/checkerOutput
        (privacy) and compilationResult.log (CE log deliberately
        unexposed: no Contest API field for it)."""
        data = await self._get_json(f"{self.submissions_api_url}/{sub_id}")
        sub = data.get("submission") or {}
        program: str | None = None
        compiler: str | None = None
        for detail in sub.get("submissionDetails") or []:
            p = (detail or {}).get("programmingSubmissionDetail") or {}
            if p.get("program") is not None:
                program = p.get("program")
            if p.get("compiler"):
                compiler = str(p["compiler"])
        run_results: list[dict] = []
        for jrc in sub.get("judgeResponseContents") or []:
            # cases live under the programming-specific wrapper
            content = (jrc or {}).get("programmingJudgeResponseContent") or {}
            cases = content.get("testcaseJudgeResults") or {}
            if not isinstance(cases, dict):
                continue
            for case_key, r in cases.items():
                r = r or {}
                run_results.append({
                    "case": str(case_key),
                    "result": r.get("result"),
                    "time": _to_float(r.get("time")),
                    "memory": _to_int(r.get("memory")),
                    "exitcode": _to_int(r.get("exitcode")),
                })
        return {"program": program, "compiler": compiler,
                "run_results": run_results}

    async def fetch_teams(self) -> list[dict]:
        """Full roster from the /members endpoint (findings §4).

        Page/limit pagination with a real `total` (NOT the submissions
        cursor): loop while a page returns exactly `limit` rows, with a
        sane page-count guard; warn when `total` says rows were missed.
        Every member becomes a team — id = studentUserId (sanitized;
        digits pass through), name = 姓名, label = 学号 (falls back to
        姓名 when the student number is missing; user decision 2026-10-14),
        group_id = organization_id = sanitized userGroupId (None when the
        member has none).
        """
        members: list[dict] = []
        groups: dict[str, str] = {}
        students: dict[str, dict] = {}
        users: dict[str, dict] = {}
        total = 0
        for page in range(MAX_MEMBERS_PAGES):
            params = {"page": page, "limit": self._page_limit,
                      "filter": MEMBERS_FILTER}
            data = await self._get_json(self.members_url, params)
            total = int(data.get("total") or 0)
            groups.update({sanitize_id(str(gid)): str((g or {}).get("name") or gid)
                           for gid, g in (data.get("userGroups") or {}).items()})
            students.update(data.get("studentUserById") or {})
            users.update(data.get("userById") or {})
            rows = data.get("members") or []
            members.extend(rows)
            if len(rows) < self._page_limit:
                break
        if total > 0 and len(members) < total:
            log.warning("roster truncated: fetched %d of %d members "
                        "reported by /members total", len(members), total)
        out: dict[str, dict] = {}
        for m in members:
            uid = m.get("userId")
            sid = m.get("studentUserId")
            stu = students.get(sid) or {}
            nick = users.get(uid) or {}
            raw_gid = m.get("userGroupId")
            gid = sanitize_id(str(raw_gid)) if raw_gid not in (None, "") else None
            raw_team_id = sid or uid or m.get("id")
            if raw_team_id in (None, ""):
                log.warning("skip member row with no studentUserId/userId/id")
                continue
            team_id = sanitize_id(str(raw_team_id))
            name = str(stu.get("name") or nick.get("nickname") or "")
            label = str(stu.get("studentNumber") or name)
            out.setdefault(team_id, {
                "id": team_id, "name": name, "label": label,
                "group_id": gid, "organization_id": gid,
                "organization_name": groups.get(gid, gid) if gid else None,
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
        sanitize_id(userId) + userById nickname. Team identity (id/name)
        never includes the student number — only roster labels do.
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
            # list rows carry time (seconds float) and score (float);
            # None when absent or non-numeric (pending/judging rows)
            "run_time": _to_float(row.get("time")),
            "score": _to_float(row.get("score")),
        }
