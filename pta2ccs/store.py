from __future__ import annotations

from dataclasses import dataclass

from .translate import ContestData

EVENT_WINDOW = 10000

_STATIC_FIELDS = (
    ("contest", "contests"), ("state", "state"), ("groups", "groups"),
    ("organizations", "organizations"), ("teams", "teams"),
    ("problems", "problems"), ("languages", "languages"),
    ("judgement_types", "judgement-types"), ("runs", "runs"),
    # ContestData.sources (submission_id → program text) is deliberately
    # NOT here: the files route reads it directly from the snapshot and
    # source code must never enter the event feed.
)


@dataclass
class Event:
    type: str
    id: str | None
    data: dict | list | None
    token: int

    def to_json(self) -> dict:
        return {"type": self.type, "id": self.id, "data": self.data, "token": self.token}


class Store:
    def __init__(self, window: int = EVENT_WINDOW):
        self._data: ContestData | None = None
        self._events: list[Event] = []
        self._token = 0
        self._window = window

    def snapshot(self) -> ContestData | None:
        return self._data

    def latest_token(self) -> int:
        return self._token

    def is_valid_since_token(self, token: int) -> bool:
        if token < 0:
            return False
        if token == 0:
            return True
        oldest = self._events[0].token if self._events else 0
        return oldest <= token <= self._token

    def events_since(self, token: int | None) -> list[Event]:
        if token is None:
            return list(self._events)
        return [e for e in self._events if e.token > token]

    def _emit(self, type_: str, id_: str | None, data) -> Event:
        self._token += 1
        ev = Event(type=type_, id=id_, data=data, token=self._token)
        self._events.append(ev)
        if len(self._events) > self._window:
            del self._events[: len(self._events) - self._window]
        return ev

    def apply_all(self, data: ContestData) -> list[Event]:
        events: list[Event] = []
        old = self._data
        for attr, type_ in _STATIC_FIELDS:
            new_map: dict = getattr(data, attr)
            old_map: dict = getattr(old, attr) if old else {}
            if attr in ("contest", "state"):
                if old_map != new_map:
                    events.append(self._emit(type_, None, new_map))
                continue
            for k, v in new_map.items():
                if old_map.get(k) != v:
                    events.append(self._emit(type_, k, v))
        for sid, sub in data.submissions.items():
            if old is None or old.submissions.get(sid) != sub:
                events.append(self._emit("submissions", sid, sub))
        for sid, jud in data.judgements.items():
            if old is None or old.judgements.get(sid) != jud:
                events.append(self._emit("judgements", sid, jud))
        self._data = data
        return events
