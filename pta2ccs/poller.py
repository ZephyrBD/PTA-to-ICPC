from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .config import Settings
from .pta.client import PtaAuthError, PtaClient
from .store import Store
from .translate import translate

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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

    async def poll_once(self) -> None:
        try:
            raw_subs = await self.client.fetch_submissions()
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
                self._last_config_poll = _now()
            else:
                snap = self.store.snapshot()
                if snap is not None:
                    raw_problems = [
                        {"id": p["id"], "title": p.get("name", p["id"]),
                         "label": p.get("label"), "ordinal": p.get("ordinal"),
                         "color": p.get("color"), "rgb": p.get("rgb")}
                        for p in snap.problems.values()
                    ]
            raw_teams = list(self._last_teams) if self._last_teams is not None else None
            data = translate(self.settings.contest_id, self.manual,
                             raw_subs, raw_problems, raw_teams, _now())
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
