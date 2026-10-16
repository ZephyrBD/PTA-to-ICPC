from __future__ import annotations

import logging
from dataclasses import dataclass

from .translate import ContestData

log = logging.getLogger(__name__)

EVENT_WINDOW = 10000

_STATIC_FIELDS = (
    ("contest", "contest"), ("state", "state"), ("groups", "groups"),
    ("organizations", "organizations"), ("teams", "teams"),
    ("persons", "persons"),
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
        # spec: notification token is type string; Event.token stays int
        return {"type": self.type, "id": self.id, "data": self.data,
                "token": str(self.token)}


def _same_ignoring_state(a: ContestData, b: ContestData) -> bool:
    # ContestData is a dataclass with no custom __eq__; compare every field
    # except `state` (frozen path: only a non-state change is a real
    # divergence worth warning about).
    return all(
        getattr(a, name) == getattr(b, name)
        for name in a.__dataclass_fields__ if name != "state")


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
        old = self._data
        # a finalized contest must not change (spec): once end_of_updates is
        # in the snapshot, ignore every later poll and keep the snapshot.
        # Only non-state fields count as a real divergence — the auto rule
        # re-derives the state times from the advancing clock, so a repeat
        # poll legitimately differs in `state` alone.
        if old is not None and old.state.get("end_of_updates"):
            if not _same_ignoring_state(old, data):
                log.warning("ignoring update to a finalized contest")
            return []
        events: list[Event] = []
        deleted: list[tuple[str, str]] = []
        state_changed = False
        for attr, type_ in _STATIC_FIELDS:
            new_map: dict = getattr(data, attr)
            old_map: dict = getattr(old, attr) if old else {}
            if attr == "state":
                # the state event (end_of_updates) must be the LAST line of
                # the batch — never _emit it here (that would also order it
                # in Store._events / the wire before the object diffs)
                state_changed = old_map != new_map
                continue
            if attr == "contest":
                if old_map != new_map:
                    events.append(self._emit(type_, None, new_map))
                continue
            for k, v in new_map.items():
                if old_map.get(k) != v:
                    events.append(self._emit(type_, k, v))
            # id-keyed collections only: keys gone from the new snapshot are
            # deletes (spec: data null = deleted). old is None → old_map empty.
            deleted += [(type_, k) for k in old_map if k not in new_map]
        for sid, sub in data.submissions.items():
            if old is None or old.submissions.get(sid) != sub:
                events.append(self._emit("submissions", sid, sub))
        for sid, jud in data.judgements.items():
            if old is None or old.judgements.get(sid) != jud:
                events.append(self._emit("judgements", sid, jud))
        if old is not None:
            deleted += [("submissions", sid) for sid in old.submissions
                        if sid not in data.submissions]
            deleted += [("judgements", sid) for sid in old.judgements
                        if sid not in data.judgements]
        for type_, k in deleted:
            events.append(self._emit(type_, k, None))
        if state_changed:
            events.append(self._emit("state", None, data.state))
        self._data = data
        return events
