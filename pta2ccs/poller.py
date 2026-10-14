from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .config import Settings
from .pta.client import PtaAuthError, PtaClient
from .store import Store
from .timefmt import parse_reltime
from .translate import translate

log = logging.getLogger(__name__)

# per-submission detail (source + runs) budget per poll round; no
# persistence by design — first poll after restart fetches everything
# again (capped here so a large backlog cannot stall the cycle)
DETAIL_FETCH_LIMIT = 50
DETAIL_CONCURRENCY = 4


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
            "consecutive_failures": 0,
        }
        self._stop = asyncio.Event()
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

    async def _fetch_details(self, raw_subs: list[dict]) -> None:
        """Attach source/run detail onto rows. Fetch (at most
        DETAIL_FETCH_LIMIT per round, DETAIL_CONCURRENCY in flight) only
        for ids whose status is new or changed since the last successful
        fetch; then re-attach the cached payload for every row whose
        cached status still matches. A per-id failure logs the id only,
        leaves the stale cache unattached (status mismatch) and retries
        next round; it never fails the whole poll cycle."""
        by_id = {str(row["id"]): row for row in raw_subs}
        needed = [
            (sid, row.get("status"))
            for sid, row in by_id.items()
            if self._detail_status.get(sid) != row.get("status")
        ][:DETAIL_FETCH_LIMIT]
        if needed:
            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def one(sid: str, status) -> None:
                async with sem:
                    try:
                        detail = await self.client.fetch_submission_detail(sid)
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

            await asyncio.gather(*(one(sid, status) for sid, status in needed))
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
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
