from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings, load_manual, load_settings
from .models import error_body, make_access, make_api_info
from .poller import Poller
from .pta.client import PtaClient
from .scoreboard import compute_scoreboard
from .store import Store
from .timefmt import parse_offset
from .translate import ContestData

COLLECTIONS = {
    "groups": ("groups", None),
    "organizations": ("organizations", None),
    "teams": ("teams", ("organization_id",)),
    "problems": ("problems", None),
    "languages": ("languages", None),
    "judgement-types": ("judgement_types", None),
    "submissions": ("submissions", ("team_id", "problem_id", "language_id")),
    "judgements": ("judgements", ("submission_id", "judgement_type_id")),
    "runs": ("runs", ("submission_id", "team_id", "problem_id")),
}
ITEM_SINGULAR = {
    "submissions": "submission", "judgements": "judgement", "runs": "run",
    "teams": "team", "problems": "problem", "groups": "group",
    "organizations": "organization", "languages": "language",
    "judgement-types": "judgement type",
}


def _now_local(tz_str: str = "+08:00") -> dt.datetime:
    return dt.datetime.now(parse_offset(tz_str))


def create_app(store: Store, poller: Poller | None, contest_id: str,
               tz: str = "+08:00") -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if poller is not None:
            task = asyncio.create_task(poller.run())
        yield
        if poller is not None:
            poller.stop()
        if task is not None:
            await task
        if poller is not None:
            try:
                await poller.client.aclose()
            except AttributeError:
                pass  # client without aclose (e.g. test fakes)

    app = FastAPI(title="pta2ccs", lifespan=lifespan)
    app.state.store = store
    app.state.poller = poller
    app.state.contest_id = contest_id

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException):
        # Class-level (not status-404): Starlette checks status handlers
        # first, so a 404-only handler left other statuses (e.g. the event
        # feed's 400) to FastAPI's default, which nests error_body under
        # {"detail": ...}. Flattening every error_body here keeps one error
        # shape; the 404 fallback for plain-string details is unchanged.
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail,
                                headers=exc.headers)
        if exc.status_code == 404:
            return JSONResponse(status_code=404,
                                content=error_body(404, "not found"))
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": detail}, headers=exc.headers)

    def require_cid(cid: str) -> None:
        if cid != contest_id:
            raise HTTPException(404, error_body(404, f"contest not found: {cid}"))

    def require_snap() -> ContestData:
        snap = store.snapshot()
        if snap is None:
            raise HTTPException(404, error_body(404, "contest data not ready"))
        return snap

    @app.get("/")
    async def api_info():
        return make_api_info()

    @app.get("/healthz")
    async def healthz():
        snap = store.snapshot()
        base = {"ready": snap is not None, "last_success": None,
                "cookie_ok": True}
        if poller is not None:
            base.update(poller.status)
        return base

    @app.get("/contests/{cid}/access")
    async def access(cid: str):
        require_cid(cid)
        return make_access()

    @app.get("/contests/{cid}")
    async def contest(cid: str):
        require_cid(cid)
        return require_snap().contest

    @app.get("/contests/{cid}/state")
    async def state(cid: str):
        require_cid(cid)
        return require_snap().state

    @app.get("/contests/{cid}/scoreboard")
    async def scoreboard(cid: str, group_id: str | None = None):
        require_cid(cid)
        now = _now_local(tz)
        return compute_scoreboard(require_snap(), now, group_id=group_id)

    # registered before the {collection} catch-all so the literal
    # /contests/{cid}/event-feed path is not shadowed (matched first)
    from .event_feed import register as _register_feed
    _register_feed(app, store, contest_id)

    # source-file route (also before the catch-all routes): the Contest
    # API files href points here; program text comes from snapshot.sources
    @app.get("/contests/{cid}/submissions/{sid}/files/{filename}")
    async def submission_file(cid: str, sid: str, filename: str):
        require_cid(cid)
        snap = require_snap()
        if sid not in snap.submissions:
            raise HTTPException(404, error_body(404, f"submission not found: {sid}"))
        program = snap.sources.get(sid)
        if program is None:
            raise HTTPException(404, error_body(404, f"file not found: {filename}"))
        return PlainTextResponse(program, media_type="text/plain; charset=utf-8")

    @app.get("/contests/{cid}/{collection}")
    async def collection(cid: str, collection: str, request: Request):
        require_cid(cid)
        if collection not in COLLECTIONS:
            raise HTTPException(404, error_body(404, f"unknown collection: {collection}"))
        attr, filters = COLLECTIONS[collection]
        items = list(getattr(require_snap(), attr).values())
        if filters:
            for fname in filters:
                val = request.query_params.get(fname)
                if val is not None:
                    # Contest API: empty-string query value matches null attributes
                    want = val if val != "" else None
                    items = [i for i in items if i.get(fname) == want]
        return items

    @app.get("/contests/{cid}/{collection}/{item_id}")
    async def item(cid: str, collection: str, item_id: str):
        require_cid(cid)
        if collection not in COLLECTIONS:
            raise HTTPException(404, error_body(404, f"unknown collection: {collection}"))
        attr, _ = COLLECTIONS[collection]
        obj = getattr(require_snap(), attr).get(item_id)
        if obj is None:
            name = ITEM_SINGULAR.get(collection, collection)
            raise HTTPException(404, error_body(404, f"{name} not found: {item_id}"))
        return obj

    return app


def _make_poller(settings: Settings, manual: dict, store: Store) -> tuple[Poller, str]:
    """Build the PTA client/poller pair; tz comes from manual config BEFORE
    the client is constructed so UTC submitAt values convert to the right
    wall clock (client default is +08:00)."""
    tz = ((manual.get("contest") or {}).get("pta_timezone", "+08:00"))
    client = PtaClient("https://pintia.cn", settings.contest_id,
                       settings.pta_cookie, tz=tz,
                       requests_per_second=settings.pta_requests_per_second)
    return Poller(client, store, settings, manual), tz


def _build_default_app() -> FastAPI:
    settings = load_settings(Path("config.json"))
    manual = load_manual(settings.manual_dir)
    store = Store()
    poller, tz = _make_poller(settings, manual, store)
    return create_app(store, poller, settings.contest_id, tz=tz)
