from __future__ import annotations

import asyncio
import datetime as dt
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)

from .balloons import (BalloonState, build_balloon, build_contestproblem,
                       build_domjudge_contest, build_domjudge_org,
                       build_domjudge_team, first_ac_pairs, time_sort_key)
from .config import Settings, load_connection, load_manual, load_settings
from .models import error_body, make_access, make_api_info
from .paths import BALLOONS_FILE, ensure_dirs
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
    "persons": ("persons", None),
    "problems": ("problems", None),
    "languages": ("languages", None),
    "judgement-types": ("judgement_types", None),
    "submissions": ("submissions", ("team_id", "problem_id", "language_id")),
    "judgements": ("judgements", ("submission_id", "judgement_type_id")),
    "runs": ("runs", ("submission_id", "team_id", "problem_id")),
}
ITEM_SINGULAR = {
    "submissions": "submission", "judgements": "judgement", "runs": "run",
    "teams": "team", "persons": "person", "problems": "problem",
    "groups": "group", "organizations": "organization",
    "languages": "language", "judgement-types": "judgement type",
}


def _now_local(tz_str: str = "+08:00") -> dt.datetime:
    return dt.datetime.now(parse_offset(tz_str))


def create_app(store: Store, poller: Poller | None, contest_id: str,
               tz: str = "+08:00",
               balloon_state: BalloonState | None = None) -> FastAPI:
    if balloon_state is None:
        balloon_state = BalloonState(BALLOONS_FILE)

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

    app = FastAPI(title="PTA to ICPC", lifespan=lifespan)
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

    @app.get("/healthz")
    async def healthz():
        snap = store.snapshot()
        base = {"ready": snap is not None, "last_success": None,
                "cookie_ok": True}
        if poller is not None:
            base.update(poller.status)
        return base

    # --- /api/v4/ explicit routes (all registered before the catch-alls) ---
    # Shared paths (contests/teams/organizations) serve DOMjudge-superset
    # shapes; the remaining collections keep the Contest API shape through
    # the catch-alls at the bottom.

    @app.get("/api/v4/")
    async def api_info():
        return make_api_info()

    @app.get("/api/v4/contests")
    async def v4_contests():
        snap = store.snapshot()
        if snap is None:
            return []
        return [build_domjudge_contest(snap)]

    @app.get("/api/v4/contests/{cid}")
    async def v4_contest(cid: str):
        require_cid(cid)
        return build_domjudge_contest(require_snap())

    @app.get("/api/v4/contests/{cid}/access")
    async def access(cid: str):
        require_cid(cid)
        return make_access()

    @app.get("/api/v4/contests/{cid}/state")
    async def state(cid: str):
        require_cid(cid)
        return require_snap().state

    @app.get("/api/v4/contests/{cid}/scoreboard")
    async def scoreboard(cid: str, group_id: str | None = None):
        require_cid(cid)
        now = _now_local(tz)
        return compute_scoreboard(require_snap(), now, group_id=group_id)

    # registered before the {collection} catch-all so the literal
    # /api/v4/contests/{cid}/event-feed path is not shadowed (matched first)
    from .event_feed import register as _register_feed
    _register_feed(app, store, contest_id)

    # source-file route (also before the catch-all routes): the Contest
    # API files href points here; program text comes from snapshot.sources
    @app.get("/api/v4/contests/{cid}/submissions/{sid}/files/{filename}")
    async def submission_file(cid: str, sid: str, filename: str):
        require_cid(cid)
        snap = require_snap()
        if sid not in snap.submissions:
            raise HTTPException(404, error_body(404, f"submission not found: {sid}"))
        program = snap.sources.get(sid)
        if program is None:
            raise HTTPException(404, error_body(404, f"file not found: {filename}"))
        return PlainTextResponse(program, media_type="text/plain; charset=utf-8")

    # DOMjudge-superset teams/organizations (explicit, shadow the catch-all)
    @app.get("/api/v4/contests/{cid}/teams")
    async def v4_teams(cid: str):
        require_cid(cid)
        snap = require_snap()
        return [build_domjudge_team(snap, tid) for tid in sorted(snap.teams)]

    @app.get("/api/v4/contests/{cid}/organizations")
    async def v4_organizations(cid: str):
        require_cid(cid)
        snap = require_snap()
        return [build_domjudge_org(snap, oid)
                for oid in sorted(snap.organizations)]

    # DOMjudge v4 balloon surface (registered before the {collection}
    # catch-all, like event_feed). GET balloons reconciles BalloonState
    # against the fresh snapshot on every request.
    @app.get("/api/v4/contests/{cid}/balloons")
    async def v4_balloons(cid: str, todo: bool = False):
        require_cid(cid)
        snap = require_snap()
        await balloon_state.sync(snap)
        pairs = first_ac_pairs(snap)
        total_map: dict[str, dict] = {}
        for team_id, problem_id in pairs:
            cp = build_contestproblem(snap, problem_id)
            if cp is None:
                continue
            total_map.setdefault(team_id, {})[cp["label"]] = cp
        rows = []
        # snapshot_entries() already excludes inactive rows (a pair that
        # temporarily left the first-AC set keeps its id but is hidden), so
        # no extra filter is needed here.
        for bid, entry in (await balloon_state.snapshot_entries()).items():
            team_id = entry["team_id"]
            problem_id = entry["problem_id"]
            if team_id not in snap.teams or problem_id not in snap.problems:
                continue  # defensive: team/problem no longer resolve
            info = pairs.get((team_id, problem_id))
            if info is None:
                continue  # pruned by sync above; should not happen
            rows.append(build_balloon(
                snap, dict(entry, time=info["time"]), bid,
                entry["done"], total_map))
        # undone first, then done; within each group first-AC time DESC
        rows.sort(key=lambda r: time_sort_key(r["time"]), reverse=True)
        rows.sort(key=lambda r: r["done"])
        if todo:
            rows = [r for r in rows if not r["done"]]
        return rows

    @app.post("/api/v4/contests/{cid}/balloons/{balloonId}/done")
    async def v4_balloon_done(cid: str, balloonId: int):
        require_cid(cid)
        if not await balloon_state.set_done(balloonId, True):
            raise HTTPException(404, error_body(
                404, f"balloon not found: {balloonId}"))
        return Response(status_code=204)

    @app.post("/api/v4/contests/{cid}/balloons/{balloonId}/undone")
    async def v4_balloon_undone(cid: str, balloonId: int):
        require_cid(cid)
        if not await balloon_state.set_done(balloonId, False):
            raise HTTPException(404, error_body(
                404, f"balloon not found: {balloonId}"))
        return Response(status_code=204)

    # --- catch-alls (Contest API shape for the remaining collections) ---
    # The COLLECTIONS whitelist keeps teams/organizations so the item
    # routes /teams/{id} and /organizations/{id} still serve the Contest
    # API shape; the explicit collection routes above shadow them.

    @app.get("/api/v4/contests/{cid}/{collection}")
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

    @app.get("/api/v4/contests/{cid}/{collection}/{item_id}")
    async def item(cid: str, collection: str, item_id: str):
        require_cid(cid)
        if collection not in COLLECTIONS:
            raise HTTPException(404, error_body(404, f"unknown collection: {collection}"))
        attr, _ = COLLECTIONS[collection]
        obj = getattr(require_snap(), attr).get(item_id)
        if obj is None:
            name = ITEM_SINGULAR.get(collection, collection)
            raise HTTPException(404, error_body(404, f"{name} not found: {item_id}"))
        # Note: the teams/organizations ITEM shape is the raw Contest API
        # object while their collection routes serve the DOMjudge superset
        # (externalid/display_name/affiliation_id/...). No consumer fetches
        # a single team/org; the CDS tolerates the extra properties.
        return obj

    # --- legacy redirects (registered last so they never shadow /api/v4/) ---
    # bare /contests must 301 straight to the list — otherwise Starlette's
    # trailing-slash handling makes it a 307→301→307 triple hop.
    @app.get("/contests")
    async def legacy_contests_redirect(request: Request):
        return RedirectResponse(request.url.replace(path="/api/v4/contests"),
                                status_code=301)

    @app.get("/contests/{rest:path}")
    async def legacy_contest_redirect(request: Request, rest: str):
        new_path = f"/api/v4/contests/{rest}"
        return RedirectResponse(request.url.replace(path=new_path),
                                status_code=301)

    @app.get("/")
    async def legacy_root_redirect(request: Request):
        return RedirectResponse(request.url.replace(path="/api/v4/"),
                                status_code=301)

    return app


def _make_poller(settings: Settings, conn: dict, manual: dict,
                 store: Store) -> tuple[Poller, str]:
    """Build the PTA client/poller pair; tz comes from manual config BEFORE
    the client is constructed so UTC submitAt values convert to the right
    wall clock (client default is +08:00)."""
    tz = ((manual.get("contest") or {}).get("pta_timezone", "+08:00"))
    client = PtaClient("https://pintia.cn", conn["contest_id"],
                       conn["pta_cookie"], tz=tz,
                       requests_per_second=settings.pta_requests_per_second)
    return Poller(client, store, settings, manual,
                  contest_id=conn["contest_id"],
                  print_server=conn.get("print_server")), tz


def _build_default_app(conn: dict | None = None) -> FastAPI:
    ensure_dirs()
    settings = load_settings()
    if conn is None:
        conn = load_connection()
        if conn is None:
            print("connection not configured — run 'python -m pta2icpc.setup' "
                  "first", file=sys.stderr)
            raise SystemExit(1)
    manual = load_manual()
    store = Store()
    poller, tz = _make_poller(settings, conn, manual, store)
    return create_app(store, poller, conn["contest_id"], tz=tz)
