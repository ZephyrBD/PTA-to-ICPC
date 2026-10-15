from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path

from .timefmt import parse_reltime, to_iso_time
from .translate import ContestData

log = logging.getLogger(__name__)


def first_ac_pairs(data: ContestData) -> dict[tuple[str, str], dict]:
    """(team_id, problem_id) → {"time": first-AC absolute ISO}.

    Verdict convention identical to scoreboard.py: judgements keyed by
    submission id → judgement_type_id → judgement_types.solved. Per-team
    submissions are sorted by contest_time; the first solved judgement per
    problem wins. Missing/pending judgements (jtype None) never count.
    """
    by_team: dict[str, list[dict]] = {}
    for sub in data.submissions.values():
        by_team.setdefault(sub["team_id"], []).append(sub)
    out: dict[tuple[str, str], dict] = {}
    for subs in by_team.values():
        for sub in sorted(subs, key=lambda s: parse_reltime(s["contest_time"])):
            jtype = data.judgements.get(sub["id"], {}).get("judgement_type_id")
            if jtype is None:
                continue
            if not data.judgement_types.get(jtype, {}).get("solved"):
                continue
            key = (sub["team_id"], sub["problem_id"])
            if key in out:
                continue
            out[key] = {"time": sub["time"]}
    return out


def _to_epoch(iso) -> int:
    """ISO timestamp → Unix epoch seconds; an unparseable/missing value
    (defensive) becomes 0 so downstream numeric math never NaNs. A naive
    (tz-less) input is treated as unusable → 0 rather than interpreted in
    the host's local timezone."""
    try:
        d = dt.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return 0
    if d.tzinfo is None:
        return 0
    return int(d.timestamp())


def build_domjudge_team(data: ContestData, team_id: str) -> dict:
    """DOMjudge v4 team object (superset of the Contest API team shape).
    public_description packs the team's members/coaches from data.persons
    (role contestant/coach, team_ids contains the team id); None when the
    team has none. The team is looked up defensively — a missing team
    resolves to id fallbacks, never an exception."""
    tid = str(team_id)
    team = data.teams.get(tid) or {}
    name = str(team.get("name") or "") or tid
    label = team.get("label")
    label = str(label) if label not in (None, "") else None
    org_id = team.get("organization_id")
    org_id = str(org_id) if org_id not in (None, "") else None
    icpc_id = team.get("icpc_id")
    icpc_id = str(icpc_id) if icpc_id not in (None, "") else None
    members = [str(p["name"]) for p in data.persons.values()
               if tid in (p.get("team_ids") or [])
               and p.get("role") == "contestant"]
    coaches = [str(p["name"]) for p in data.persons.values()
               if tid in (p.get("team_ids") or [])
               and p.get("role") == "coach"]
    description_parts = []
    if members:
        description_parts.append("队员：" + "、".join(members))
    if coaches:
        description_parts.append("教练：" + "、".join(coaches))
    return {
        "id": tid,
        "externalid": tid,
        "name": name,
        "display_name": name,
        "organization_id": org_id,
        "group_ids": list(team.get("group_ids") or []),
        "affiliation_id": org_id,
        "label": label,
        "icpc_id": icpc_id,
        "public_description": ("；".join(description_parts)
                               if description_parts else None),
        "hidden": False,
        "location": label,
    }


def build_domjudge_org(data: ContestData, org_id: str) -> dict:
    """DOMjudge v4 organization object (superset shape). The organization
    is looked up defensively — a missing org resolves to id fallbacks."""
    oid = str(org_id)
    org = data.organizations.get(oid) or {}
    name = str(org.get("name") or "") or oid
    return {
        "id": oid,
        "externalid": oid,
        "name": name,
        "formal_name": name,
        "shortname": name,
        "country": None,
        "country_flag": [],
        "logo": [],
        "affiliation_id": oid,
        "icpc_id": None,
    }


def build_contestproblem(data: ContestData, problem_id: str) -> dict | None:
    """DOMjudge v4 contestproblem object; time_limit as a number of seconds
    (spec) from the stored value (legacy reltime strings still accepted;
    absent → None). Returns None when the problem id does not resolve."""
    problem = data.problems.get(problem_id)
    if problem is None:
        return None
    label = str(problem.get("label") or problem_id)
    time_limit = None
    rel = problem.get("time_limit")
    if rel not in (None, ""):
        if isinstance(rel, (int, float)) and not isinstance(rel, bool):
            time_limit = rel
        else:
            try:
                time_limit = round(parse_reltime(str(rel)).total_seconds(), 3)
            except (ValueError, TypeError, IndexError):
                time_limit = None
    return {
        "id": problem_id,
        "label": label,
        "name": str(problem.get("name") or label),
        "short_name": label,
        "rgb": problem.get("rgb"),
        "color": problem.get("color"),
        "time_limit": time_limit,
        "statement": [],
        "attachments": [],
    }


def build_balloon(data: ContestData, entry: dict, balloonid: int, done: bool,
                  total_map: dict[str, dict]) -> dict:
    """DOMjudge v4 balloon object. `entry` is a BalloonState entry
    (team_id/problem_id, optionally pre-resolved "time" — the route passes
    it from first_ac_pairs; when absent the first-AC time is derived from
    `data`). `total_map` maps team_id → {label: contestproblem} for the
    team's full first-AC set."""
    team_id = str(entry["team_id"])
    problem_id = str(entry["problem_id"])
    team = data.teams.get(team_id) or {}
    problem = data.problems.get(problem_id) or {}
    label = str(problem.get("label") or problem_id)
    team_name = str(team.get("name") or "") or team_id
    sub_time = entry.get("time")
    if sub_time is None:
        sub_time = first_ac_pairs(data).get((team_id, problem_id), {}).get("time")
    org_id = team.get("organization_id")
    org = None
    if org_id and org_id in data.organizations:
        org = str(data.organizations[org_id].get("name") or "") or None
    else:
        # affiliationid must not be set while affiliation is None
        org_id = None
    group_ids = team.get("group_ids") or []
    category = category_id = None
    if group_ids:
        gid = str(group_ids[0])
        category_id = gid
        category = str(data.groups.get(gid, {}).get("name") or "") or gid
    total = dict(sorted((total_map.get(team_id) or {}).items()))
    return {
        "balloonid": int(balloonid),
        "time": _to_epoch(sub_time),
        "problem": label,
        "contestproblem": build_contestproblem(data, problem_id),
        "team": f"{team_id}: {team_name}",
        "teamid": team_id,
        "location": str(team.get("label") or "") or None,
        "affiliation": org,
        "affiliationid": org_id,
        "category": category,
        "categoryid": category_id,
        "total": total,
        "done": bool(done),
    }


def build_domjudge_contest(data: ContestData) -> dict:
    """DOMjudge v4 contest object for the snapshot's single contest
    (shortname = name by user decision). end_time = start + duration so the
    planned end is advertised even while the contest is still running
    (start missing/unparseable → None, never the state.ended gate)."""
    contest = data.contest
    end_time = None
    start_s = contest.get("start_time")
    if start_s:
        try:
            end_time = to_iso_time(
                dt.datetime.fromisoformat(start_s)
                + parse_reltime(contest.get("duration")))
        except (TypeError, ValueError, AttributeError):
            # AttributeError: parse_reltime(None) raises on .startswith
            end_time = None
    return {
        "id": contest["id"],
        "name": contest["name"],
        "formal_name": contest["formal_name"],
        "shortname": contest["name"],
        "start_time": contest["start_time"],
        "end_time": end_time,
        "duration": contest["duration"],
        "scoreboard_freeze_duration": contest["scoreboard_freeze_duration"],
        "scoreboard_thaw_time": None,
        "scoreboard_type": contest["scoreboard_type"],
        "penalty_time": contest["penalty_time"],
        "banner": [],
        "problemset": [],
        "allow_submit": True,
        "runtime_as_score_tiebreaker": False,
        "warning_message": "",
    }


def time_sort_key(iso) -> dt.datetime:
    """ISO timestamp OR numeric Unix epoch → aware datetime for ordering;
    an unparseable value (defensive) sorts as the earliest possible time.
    Accepting both keeps first_ac_pairs (ISO) and rendered balloons (epoch
    seconds) on the same ordering path."""
    if isinstance(iso, (int, float)) and not isinstance(iso, bool):
        try:
            return dt.datetime.fromtimestamp(iso, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        d = dt.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    # never mix naive (host-local) and aware (UTC) datetimes in one sort
    if d.tzinfo is None:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return d


class BalloonState:
    """Persisted (balloonid, team_id, problem_id, done) rows in
    balloons.json (cwd, same convention as print_sent.json). Shape:
    {"next_id": int, "entries": {balloonid: {"team_id", "problem_id",
    "done"}}}. Loads tolerantly — a missing/corrupt file is an empty
    state with a warning, never an exception."""

    def __init__(self, path: Path | str = Path("balloons.json")):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._entries: dict[int, dict] = {}
        # (team_id, problem_id) → balloonid reverse index for O(1)
        # add/prune during sync; built at load, maintained by sync.
        self._by_pair: dict[tuple[str, str], int] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:  # includes JSONDecodeError
            log.warning("cannot read %s (%s); balloon state starts empty",
                        self._path, e)
            return
        if not isinstance(raw, dict):
            log.warning("%s is not an object; balloon state starts empty",
                        self._path)
            return
        next_id = raw.get("next_id")
        raw_entries = raw.get("entries")
        if not isinstance(next_id, int) or not isinstance(raw_entries, dict):
            log.warning("%s has the wrong shape; balloon state starts empty",
                        self._path)
            return
        entries: dict[int, dict] = {}
        for bid, entry in raw_entries.items():
            if not isinstance(entry, dict):
                continue
            team_id = entry.get("team_id")
            problem_id = entry.get("problem_id")
            if not isinstance(team_id, str) or not isinstance(problem_id, str):
                continue
            try:
                bid_int = int(bid)
            except (TypeError, ValueError):
                continue
            entries[bid_int] = {
                "team_id": team_id,
                "problem_id": problem_id,
                "done": bool(entry.get("done", False)),
            }
        self._entries = entries
        # next_id must never collide with an existing balloon id
        self._next_id = max(next_id, (max(entries) + 1 if entries else 1))
        # first-wins reverse index; a duplicate (team, problem) pair in a
        # hand-edited/corrupt file is dropped from _entries so it can never
        # be rendered as a second balloon row (review F1).
        self._by_pair: dict[tuple[str, str], int] = {}
        for bid, e in list(entries.items()):
            pair = (e["team_id"], e["problem_id"])
            if pair in self._by_pair:
                del self._entries[bid]
                continue
            self._by_pair[pair] = bid

    def _persist(self) -> None:
        """Write balloons.json (tmp + replace atomic write; failure only
        warns — a lost write means re-numbering on restart, never a
        crashed request)."""
        payload = json.dumps({"next_id": self._next_id,
                              "entries": self._entries},
                             ensure_ascii=False) + "\n"
        tmp = self._path.parent / (self._path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("cannot write %s: %s", self._path, e)

    async def sync(self, data: ContestData) -> None:
        """Reconcile entries with the snapshot's first-AC set: prune pairs
        that are no longer first-AC and mint balloons (done False) for
        missing pairs in submission-time ascending order. Persists only
        when something changed."""
        pairs = first_ac_pairs(data)
        async with self._lock:
            changed = False
            for (team_id, problem_id), bid in list(self._by_pair.items()):
                if (team_id, problem_id) not in pairs:
                    del self._entries[bid]
                    del self._by_pair[(team_id, problem_id)]
                    changed = True
            missing = [(key, val) for key, val in pairs.items()
                       if key not in self._by_pair
                       # skip pairs whose team/problem don't resolve yet
                       # (roster/problem-list lag): minting them now would
                       # leave an invisible phantom entry consuming an id
                       # (review F2).
                       and key[0] in data.teams and key[1] in data.problems]
            missing.sort(key=lambda kv: time_sort_key(kv[1]["time"]))
            for (team_id, problem_id), _info in missing:
                bid = self._next_id
                self._next_id += 1
                self._entries[bid] = {"team_id": team_id,
                                      "problem_id": problem_id,
                                      "done": False}
                self._by_pair[(team_id, problem_id)] = bid
                changed = True
            if changed:
                self._persist()

    async def set_done(self, balloonid: int, done: bool) -> bool:
        """Mark a balloon done/undone and persist. Returns False for an
        unknown id (nothing written)."""
        async with self._lock:
            entry = self._entries.get(int(balloonid))
            if entry is None:
                return False
            if entry["done"] == bool(done):
                return True
            entry["done"] = bool(done)
            self._persist()
            return True

    async def snapshot_entries(self) -> dict[int, dict]:
        """Copy of the current entries (balloonid → entry) for route
        rendering, taken under the lock so it never observes a torn state."""
        async with self._lock:
            return {bid: dict(e) for bid, e in self._entries.items()}
