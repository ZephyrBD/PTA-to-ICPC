from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .models import error_body
from .store import Store

HEARTBEAT_SECONDS = 60.0
POLL_SECONDS = 0.5


def register(app: FastAPI, store: Store, contest_id: str) -> None:
    @app.get(f"/contests/{contest_id}/event-feed")
    async def event_feed(request: Request, since_token: int | None = None):
        if since_token is not None and not store.is_valid_since_token(since_token):
            raise HTTPException(400, error_body(
                400, f"unknown or expired since_token: {since_token}"))

        async def gen() -> AsyncIterator[str]:
            # Deviation from brief verbatim: the brief used
            # store.latest_token() when since_token was None, which starts
            # the feed AFTER all existing events — a fresh client would only
            # ever see heartbeats and test_initial_feed_contains_snapshot_
            # events could never observe the submissions snapshot (hangs).
            # since_token=None now replays the full buffered window (last=0;
            # tokens start at 1, so events_since(0) == events_since(None)).
            # since_token=N keeps replay-only-newer semantics.
            last = since_token if since_token is not None else 0
            for ev in store.events_since(last):
                last = ev.token
                yield json.dumps(ev.to_json(), ensure_ascii=False) + "\n"
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(POLL_SECONDS)
                idle += POLL_SECONDS
                new_events = store.events_since(last)
                if new_events:
                    idle = 0.0
                    for ev in new_events:
                        last = ev.token
                        yield json.dumps(ev.to_json(), ensure_ascii=False) + "\n"
                elif idle >= HEARTBEAT_SECONDS:
                    idle = 0.0
                    yield "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")
