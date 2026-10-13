from fastapi import FastAPI

from .store import Store


def register(app: FastAPI, store: Store, contest_id: str) -> None:
    @app.get(f"/contests/{contest_id}/event-feed")
    async def event_feed_placeholder():
        from fastapi import HTTPException
        from .models import error_body
        raise HTTPException(501, error_body(501, "event feed not implemented yet"))
