from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path

import httpx

from .config import Settings
from .printjob import build_job, send_job
from .pta.client import PtaAuthError, PtaClient, PtaRateLimitError
from .pta.langs import primary_extension, std_language_id
from .store import Store
from .timefmt import parse_reltime
from .translate import translate

log = logging.getLogger(__name__)

# per-submission detail fetch concurrency; the per-round budget comes from
# settings.detail_fetch_per_round (task 14 — default 5, was a 50/round
# module constant that triggered PTA's 429 rate limiting on restart)
DETAIL_CONCURRENCY = 2

# print pipeline (task: 扮演 DOMjudge): separate, capped budgets — source
# fetches for print-problem submissions are sequential and capped per
# round; a submission whose send keeps failing is abandoned after
# PRINT_MAX_ATTEMPTS failed attempts (logged as an error, never retried).
PRINT_FETCH_PER_ROUND = 5
PRINT_MAX_ATTEMPTS = 10


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _rel_to_ms(rel) -> int | None:
    """Snapshot time_limit → ms for the non-config raw_problems rebuild
    (translate expects milliseconds). The snapshot value is Contest API
    seconds (number) since the spec-alignment change; a manual override
    may still be a reltime string — accept both."""
    if rel in (None, ""):
        return None
    if isinstance(rel, (int, float)) and not isinstance(rel, bool):
        return round(float(rel) * 1000)
    try:
        return round(parse_reltime(str(rel)).total_seconds() * 1000)
    except (ValueError, TypeError, IndexError):
        return None


def _mib_to_kb(mem) -> int | None:
    """Snapshot memory_limit (Contest API MiB) → KB for the rebuild
    (translate re-derives MiB via `// 1024`)."""
    if isinstance(mem, (int, float)) and not isinstance(mem, bool):
        return int(mem) * 1024
    return None


def drop_print_problem(raw_subs: list[dict], raw_problems: list[dict],
                       print_label: str | None
                       ) -> tuple[list[dict], list[dict]]:
    """Drop every submission and problem belonging to the manual
    print_problem (task 20: that problem's data never reaches the
    scoreboard — its balloon slip is handled separately).

    Pure: returns new lists, inputs untouched; None/empty print_label is
    a fast path returning the inputs unchanged.
    """
    if not print_label:
        return raw_subs, raw_problems
    subs = [row for row in raw_subs
            if row.get("problem_index") != print_label]
    problems = [p for p in raw_problems
                if p.get("label") != print_label and p.get("id") != print_label]
    return subs, problems


class Poller:
    def __init__(self, client: PtaClient, store: Store, settings: Settings,
                 manual: dict, *, contest_id: str,
                 print_server: str | None = None,
                 print_sent_path: Path | str = Path("print_sent.json")):
        self.client = client
        self.store = store
        self.settings = settings
        self.manual = manual
        # connection identity lives in manual/setup-connection.json (task
        # 17), not Settings — passed straight through to translate()
        self.contest_id = contest_id
        # PRINT_SERVER (optional): when unset, print-problem submissions
        # are simply dropped (old behaviour); when set, their source is
        # fetched and POSTed to the print server each round.
        self.print_server = print_server or None
        # already-printed submission ids, persisted to print_sent.json
        # (cwd-relative like config.json / manual/); loaded leniently so a
        # missing or corrupt file just starts from an empty set.
        self._print_sent_path = Path(print_sent_path)
        self._print_sent: set[str] = self._load_print_sent()
        # sid → failed-attempt count (source fetch / empty source / POST)
        self._print_attempts: dict[str, int] = {}
        # sid → fetched source text; NOT the scoreboard detail cache —
        # entries are purged once the job is sent (or abandoned).
        self._print_source: dict[str, str] = {}
        self.status: dict = {
            "ready": False, "last_success": None, "cookie_ok": True,
            "consecutive_failures": 0, "rate_limited_until": None,
        }
        self._stop = asyncio.Event()
        # monotonic deadline until which ALL PTA requests are skipped
        # (set on any 429: list/config rounds re-raise, detail rounds
        # swallow and push it forward); healthz shows the ISO form via
        # status["rate_limited_until"]
        self._rate_limit_until: float | None = None
        self._last_config_poll = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        # last successful roster; re-passed to translate() on every
        # non-config round so zero-submission teams don't vanish (and
        # re-create/delete in event storms) between config refreshes.
        self._last_teams: list[dict] | None = None
        # last successful contest meta (freeze/compilers/times); same
        # caching pattern as the roster — fetched on config rounds only.
        self._last_meta: dict | None = None
        # submission_id → status at last successful detail fetch; rows
        # whose status changed since then get re-fetched (in-memory only,
        # no cache files — by design)
        self._detail_status: dict[str, str] = {}
        # submission_id → last successful detail payload (program /
        # compiler / run_results). fetch_submissions returns fresh row
        # dicts every round, so fetched details must be re-attached from
        # memory on rounds where nothing is re-fetched — otherwise
        # runs/files/sources would vanish from the rebuilt snapshot.
        self._detail_cache: dict[str, dict] = {}
        # pending-verdict count at the last warning (None = condition not
        # active). The contest finalizes at end + 5:00 regardless, but a
        # still-unresolved verdict is worth surfacing once — dedupe on the
        # count so the message fires on entry and again only when it changes.
        self._pending_warned: int | None = None

    def _set_rate_limit(self, retry_after: float) -> None:
        """Start a no-requests cooldown of `retry_after` seconds (task 14)."""
        self._rate_limit_until = time.monotonic() + retry_after
        self.status["rate_limited_until"] = (
            dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=retry_after)).isoformat()

    async def _fetch_details(self, raw_subs: list[dict]) -> None:
        """Attach source/run detail onto rows. Fetch (at most
        settings.detail_fetch_per_round per round, DETAIL_CONCURRENCY in
        flight) only for ids whose status is new or changed since the last
        successful fetch; then re-attach the cached payload for every row
        whose cached status still matches. A per-id non-429 failure logs
        the id only, leaves the stale cache unattached (status mismatch)
        and retries next round; it never fails the whole poll cycle.

        A detail 429 is swallowed here (after pushing the poller-wide
        cooldown forward): the submissions list already succeeded — the
        real-time scoreboard must not die because details got throttled.
        """
        by_id = {str(row["id"]): row for row in raw_subs}
        budget = self.settings.detail_fetch_per_round
        if not self.settings.fetch_details or budget <= 0:
            needed: list[tuple[str, str | None]] = []
        else:
            needed = [
                (sid, row.get("status"))
                for sid, row in by_id.items()
                if self._detail_status.get(sid) != row.get("status")
            ][:budget]
        if needed:
            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def one(sid: str, status) -> None:
                async with sem:
                    try:
                        detail = await self.client.fetch_submission_detail(sid)
                    except PtaRateLimitError:
                        raise  # poller-wide backoff, not a per-id failure
                    except Exception as e:
                        # id only — never the program or raw response
                        log.warning("detail fetch failed for %s: %s", sid, e)
                        return
                self._detail_cache[sid] = {
                    "program": detail.get("program"),
                    "compiler": detail.get("compiler"),
                    "run_results": detail.get("run_results") or [],
                }
                self._detail_status[sid] = status

            # FIRST_EXCEPTION: the only exception `one` lets escape is
            # PtaRateLimitError — cancel the remaining in-flight fetches
            # as soon as one 429s instead of hammering a throttled PTA
            tasks = [asyncio.create_task(one(sid, status))
                     for sid, status in needed]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                exc = t.exception()
                if isinstance(exc, PtaRateLimitError):
                    self._set_rate_limit(exc.retry_after)
                    log.warning(
                        "detail fetch paused: rate limited, "
                        "retry_after=%.1fs", exc.retry_after)
                    break
        for sid, row in by_id.items():
            cached = self._detail_cache.get(sid)
            if (cached is None
                    or self._detail_status.get(sid) != row.get("status")):
                continue
            row["program"] = cached["program"]
            row["compiler_enum"] = cached["compiler"]
            row["run_results"] = cached["run_results"]
            row["detail_fetched"] = True

    # ------------------------------------------------------------------ #
    # print pipeline (task: 扮演 DOMjudge — POST print-problem submissions
    # to PRINT_SERVER in the DOMjudge print script's multipart shape)
    # ------------------------------------------------------------------ #

    def _load_print_sent(self) -> set[str]:
        """print_sent.json → already-sent sid set. Missing file → empty;
        any read/parse problem → empty + warning (never raises)."""
        try:
            data = json.loads(self._print_sent_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError) as e:  # includes JSONDecodeError
            log.warning("cannot read %s (%s); print sent set starts empty",
                        self._print_sent_path, e)
            return set()
        sent = data.get("sent") if isinstance(data, dict) else None
        if not isinstance(sent, list):
            log.warning("%s has no 'sent' list; print sent set starts empty",
                        self._print_sent_path)
            return set()
        return {str(s) for s in sent}

    def _persist_print_sent(self) -> None:
        """Write print_sent.json (tmp + replace; failure only warns — a
        lost write means a duplicate print, never a crashed poll round)."""
        payload = json.dumps({"sent": sorted(self._print_sent)},
                             ensure_ascii=False) + "\n"
        tmp = self._print_sent_path.parent / (
            self._print_sent_path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._print_sent_path)
        except OSError as e:
            log.warning("cannot write %s: %s", self._print_sent_path, e)

    def _print_http_client(self) -> httpx.AsyncClient:
        """HTTP client for print POSTs (test seam: MockTransport)."""
        return httpx.AsyncClient()

    def _count_print_attempt(self, sid: str) -> None:
        """Register one failed attempt for a print submission (non-429
        source-fetch failure or a failed/empty POST). After
        PRINT_MAX_ATTEMPTS failures the sid joins the sent set — a
        permanent skip, logged as an error. Transient conditions that
        keep a source from ever being fetched (per-round budget, a 429
        cooldown) never call this: they must not burn attempts."""
        attempts = self._print_attempts.get(sid, 0) + 1
        self._print_attempts[sid] = attempts
        if attempts > PRINT_MAX_ATTEMPTS:
            log.error(
                "print job for submission %s permanently skipped "
                "after %d failed attempts", sid, attempts)
            self._print_sent.add(sid)
            self._print_source.pop(sid, None)
            self._persist_print_sent()

    async def _fetch_print_sources(self, print_rows: list[dict]) -> None:
        """Fetch source text for unsent print submissions — at most
        PRINT_FETCH_PER_ROUND per round, sequentially (deliberately not
        the concurrent detail path). Results land in self._print_source
        ONLY: print sources never touch _detail_status/_detail_cache, so
        they can never reach the scoreboard. A 429 sets the poller-wide
        cooldown and stops fetching this round; a non-429 failure counts
        one bounded attempt (via _count_print_attempt) — it would
        otherwise retry forever.
        """
        budget = PRINT_FETCH_PER_ROUND
        for row in print_rows:
            if budget <= 0:
                return
            sid = str(row["id"])
            if sid in self._print_sent or sid in self._print_source:
                continue
            try:
                detail = await self.client.fetch_submission_detail(sid)
            except PtaRateLimitError as e:
                self._set_rate_limit(e.retry_after)
                log.warning(
                    "print source fetch paused: rate limited, "
                    "retry_after=%.1fs", e.retry_after)
                return
            except Exception as e:
                # id only — never the program or raw response
                log.warning("print source fetch failed for %s: %s", sid, e)
                self._count_print_attempt(sid)
                continue
            program = detail.get("program")
            self._print_source[sid] = program if isinstance(program, str) else ""
            budget -= 1

    async def _send_print_jobs(self, print_rows: list[dict],
                               print_label: str) -> None:
        """POST every not-yet-sent print submission (spec step 4: runs
        after store.apply_all so tname/location/group come from the
        fresh snapshot). A submission is only attempted when its source
        is cached (i.e. it was actually fetched this/earlier round) —
        rows still awaiting fetch (per-round budget / 429) never count
        an attempt. Empty/missing source or a failed POST counts one
        attempt via _count_print_attempt (after PRINT_MAX_ATTEMPTS
        failures the sid is permanently skipped). At most
        PRINT_FETCH_PER_ROUND sends per round so a slow/dead print
        server cannot stall the poll cycle for long. Success persists
        the sent set immediately and purges the cached source.
        """
        snap = self.store.snapshot()
        if snap is None:
            return
        pending = [r for r in print_rows if str(r["id"]) not in self._print_sent]
        if not pending:
            return
        group_names = {gid: str((g or {}).get("name") or gid)
                       for gid, g in snap.groups.items()}
        async with self._print_http_client() as http:
            for row in pending[:PRINT_FETCH_PER_ROUND]:
                sid = str(row["id"])
                if sid not in self._print_source:
                    # source not fetched yet (budget / 429): wait — this
                    # must never burn a failure attempt.
                    continue
                program = self._print_source.get(sid)
                ok = bool(program)
                if ok:
                    team_id = str(row.get("team_id") or "")
                    team = snap.teams.get(team_id) or {}
                    # tname: snapshot name → row team_name → team_id
                    tname = (str(team.get("name") or "")
                             or str(row.get("team_name") or "") or team_id)
                    location = str(team.get("label") or "") or None
                    group = ",".join(
                        group_names.get(g, g)
                        for g in team.get("group_ids") or []) or None
                    lang = std_language_id(row.get("language"))
                    job = build_job(
                        source=program,
                        filename=f"{print_label}{primary_extension(lang)}",
                        lang=lang, tname=tname, team=team_id,
                        location=location, group=group)
                    ok = await send_job(http, self.print_server, job)
                if ok:
                    self._print_sent.add(sid)
                    self._print_source.pop(sid, None)
                    self._persist_print_sent()
                    continue
                self._count_print_attempt(sid)

    def _warn_pending_verdicts(self, data) -> None:
        """Log once when the contest has ended with unjudged submissions,
        and again only when the pending count changes (surfaces unknown PTA
        statuses that will be omitted — finalization itself is purely
        time-based and does not wait on verdicts)."""
        pending = sum(1 for j in data.judgements.values()
                      if not j.get("judgement_type_id"))
        active = bool(data.state.get("ended")) \
            and data.state.get("end_of_updates") is None and pending > 0
        if active and pending != self._pending_warned:
            log.warning(
                "%d verdict(s) still unresolved; finalizing at end + 5:00 "
                "with them omitted", pending)
            self._pending_warned = pending
        elif not active:
            self._pending_warned = None

    async def poll_once(self) -> None:
        now = time.monotonic()
        if self._rate_limit_until is not None and now < self._rate_limit_until:
            # cooldown from a previous 429: zero PTA requests this round
            log.debug("poll skipped: rate limited, %.1fs left",
                      self._rate_limit_until - now)
            return
        try:
            print_label = (self.manual.get("options") or {}).get("print_problem")
            raw_subs = await self.client.fetch_submissions()
            print_rows: list[dict] = []
            if print_label:
                # drop BEFORE _fetch_details so the hidden problem's sids
                # never enter `needed` — and purge their cached details so
                # a later un-hide doesn't resurrect stale payloads (task 20)
                before_ids = {str(r["id"]) for r in raw_subs}
                if self.print_server:
                    # print pipeline: partition the rows out (they never
                    # reach translate/store) and handle them separately
                    print_rows = [r for r in raw_subs
                                  if r.get("problem_index") == print_label]
                    raw_subs = [r for r in raw_subs
                                if r.get("problem_index") != print_label]
                else:
                    raw_subs, _ = drop_print_problem(raw_subs, [], print_label)
                for sid in before_ids - {str(r["id"]) for r in raw_subs}:
                    self._detail_status.pop(sid, None)
                    self._detail_cache.pop(sid, None)
            await self._fetch_details(raw_subs)
            if print_rows:
                await self._fetch_print_sources(print_rows)
            need_config = (
                self.store.snapshot() is None
                or (_now() - self._last_config_poll).total_seconds()
                >= self.settings.config_poll_seconds
            )
            raw_problems: list[dict] = []
            if need_config:
                raw_problems = await self.client.fetch_problems()
                # failure raises → whole poll cycle retried (never a
                # partial roster)
                fetched = await self.client.fetch_teams()
                if fetched or not self._last_teams:
                    self._last_teams = fetched
                else:
                    # 200-but-empty /members would wipe zero-submission
                    # teams until the next config round: keep last roster
                    log.warning("empty roster from /members; keeping "
                                "previous %d teams", len(self._last_teams))
                fetched_meta = await self.client.fetch_contest_meta()
                if fetched_meta or not self._last_meta:
                    self._last_meta = fetched_meta
                self._last_config_poll = _now()
            else:
                snap = self.store.snapshot()
                if snap is not None:
                    raw_problems = [
                        {"id": p["id"], "title": p.get("name", p["id"]),
                         "label": p.get("label"), "ordinal": p.get("ordinal"),
                         "color": p.get("color"), "rgb": p.get("rgb"),
                         # round-trip limits so they don't vanish between
                         # config refreshes (time_limit is stored as
                         # Contest API seconds; memory_limit as MiB)
                         "time_limit_ms": _rel_to_ms(p.get("time_limit")),
                         "memory_limit_kb": _mib_to_kb(p.get("memory_limit")),
                         "code_limit_kb": p.get("code_limit"),
                         "test_data_count": p.get("test_data_count")}
                        for p in snap.problems.values()
                    ]
            if print_label:
                # both paths (live fetch + snapshot rebuild): manual
                # entries can re-introduce the hidden problem (task 20)
                _, raw_problems = drop_print_problem([], raw_problems,
                                                     print_label)
            raw_teams = list(self._last_teams) if self._last_teams is not None else None
            data = translate(self.contest_id, self.manual,
                             raw_subs, raw_problems, raw_teams, _now(),
                             meta=self._last_meta)
            self._warn_pending_verdicts(data)
            self.store.apply_all(data)
            self.status["ready"] = True
            self.status["cookie_ok"] = True
            self.status["consecutive_failures"] = 0
            self.status["last_success"] = dt.datetime.now(dt.timezone.utc).isoformat()
            if print_rows:
                # print pipeline step 4: snapshot is fresh — send jobs
                await self._send_print_jobs(print_rows, print_label)
            # clear an EXPIRED cooldown (a detail 429 may have set one
            # mid-round — never clear an active one here)
            if (self._rate_limit_until is not None
                    and time.monotonic() >= self._rate_limit_until):
                self._rate_limit_until = None
                self.status["rate_limited_until"] = None
        except PtaRateLimitError as e:
            # rate limiting is NOT a failure: no backoff multiplication,
            # consecutive_failures/last_success untouched; next round is
            # skipped until the cooldown expires
            self._set_rate_limit(e.retry_after)
            log.warning("poll rate limited; sleeping %.1fs", e.retry_after)
        except PtaAuthError as e:
            self.status["cookie_ok"] = False
            self.status["consecutive_failures"] += 1
            log.error("PTA auth failed (cookie expired?): %s", e)
        except Exception as e:
            self.status["consecutive_failures"] += 1
            log.warning("poll failed: %s", e)

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            await self.poll_once()
            failures = self.status["consecutive_failures"]
            delay = min(self.settings.poll_interval_seconds * (2 ** min(failures, 5)),
                        60.0)
            if self._rate_limit_until is not None:
                # wait out an active/pending rate-limit cooldown (min 1s)
                delay = max(delay, self._rate_limit_until - time.monotonic(),
                            1.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
