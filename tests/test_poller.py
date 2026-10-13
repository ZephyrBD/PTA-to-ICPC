import asyncio
import datetime as dt

from pta2ccs.config import Settings
from pta2ccs.poller import Poller
from pta2ccs.store import Store


class FakeClient:
    def __init__(self):
        self.subs = []
        self.problems = [{"id": "A", "title": "题A"}]

    async def fetch_submissions(self):
        return list(self.subs)

    async def fetch_problems(self):
        return list(self.problems)

    async def fetch_teams(self):
        return None


def make_poller() -> tuple[Poller, FakeClient, Store]:
    settings = Settings(contest_id="1", pta_cookie="c",
                        poll_interval_seconds=0.01, config_poll_seconds=0.01)
    client = FakeClient()
    store = Store()
    manual = {"contest": {"start_time": "2023-05-21T09:00:00+08:00",
                          "duration": "5:00:00", "pta_timezone": "+08:00"}}
    return Poller(client, store, settings, manual), client, store


def test_poll_once_populates_store():
    poller, client, store = make_poller()
    client.subs = [{"id": "1", "submit_time": "2023/05/21 09:10:00",
                    "status": "答案正确", "user": "D22 队", "problem_index": "A",
                    "language": "cpp"}]
    asyncio.run(poller.poll_once())
    assert store.snapshot() is not None
    assert "1" in store.snapshot().submissions
    assert poller.status["ready"] is True
    assert poller.status["cookie_ok"] is True


def test_poll_auth_error_sets_cookie_flag():
    poller, client, store = make_poller()

    async def boom():
        from pta2ccs.pta.client import PtaAuthError
        raise PtaAuthError("expired")

    client.fetch_submissions = boom  # type: ignore[method-assign]
    asyncio.run(poller.poll_once())
    assert poller.status["cookie_ok"] is False
    assert poller.status["consecutive_failures"] == 1
    assert poller.status["ready"] is False


def test_poll_once_bad_row_does_not_crash():
    poller, client, store = make_poller()
    client.subs = [
        {"id": "1", "submit_time": "2023/05/21 09:10:00", "status": "答案正确",
         "user": "D22 队", "problem_index": "A", "language": "cpp"},
        {"id": "2", "submit_time": "???bad", "status": "答案正确",
         "user": "D23 队", "problem_index": "A", "language": "cpp"},
    ]
    asyncio.run(poller.poll_once())
    assert "1" in store.snapshot().submissions
    assert "2" not in store.snapshot().submissions
