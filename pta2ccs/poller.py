from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from .config import Settings
from .pta.client import PtaAuthError, PtaClient, PtaRateLimitError
from .store import Store
from .timefmt import parse_reltime
from .translate import translate

log = logging.getLogger(__name__)

# per-submission detail fetch concurrency; the per-round budget comes from
# settings.detail_fetch_per_round (task 14 — default 5, was a 50/round
# module constant that triggered PTA's 429 rate limiting on restart)
DETAIL_CONCURRENCY = 2


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _rel_to_ms(rel) -> int | None:
    """Snapshot time_limit reltime ("0:00:01.000") → ms for the
    non-config raw_problems rebuild (translate expects milliseconds)."""
    if rel in (None, ""):
        return None
    try:
        return round(parse_reltime(str(rel)).total_seconds() * 1000)
    except (ValueError, TypeError):
        return None


class Poller:
    def __init__(self, client: PtaClient, store: Store, settings: Settings,
                 manual: dict):
        self.client = client
        self.store = store
        self.settings = settings
        self.manual = manual
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

    async def poll_once(self) -> None:
        now = time.monotonic()
        if self._rate_limit_until is not None and now < self._rate_limit_until:
            # cooldown from a previous 429: zero PTA requests this round
            log.debug("poll skipped: rate limited, %.1fs left",
                      self._rate_limit_until - now)
            return
        try:
            raw_subs = await self.client.fetch_submissions()
            await self._fetch_details(raw_subs)
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
                         # config refreshes (time_limit is stored as a
                         # reltime string; translate expects ms)
                         "time_limit_ms": _rel_to_ms(p.get("time_limit")),
                         "memory_limit_kb": p.get("memory_limit"),
                         "code_limit_kb": p.get("code_limit")}
                        for p in snap.problems.values()
                    ]
            raw_teams = list(self._last_teams) if self._last_teams is not None else None
            data = translate(self.settings.contest_id, self.manual,
                             raw_subs, raw_problems, raw_teams, _now(),
                             meta=self._last_meta)
            self.store.apply_all(data)
            self.status["ready"] = True
            self.status["cookie_ok"] = True
            self.status["consecutive_failures"] = 0
            self.status["last_success"] = dt.datetime.now(dt.timezone.utc).isoformat()
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
