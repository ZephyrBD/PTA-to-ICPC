from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from .models import sanitize_id
from .pta.verdicts import PTA_STATUS_VERDICT_MAP, PTA_VERDICT_MAP
from .timefmt import parse_pta_time, parse_reltime, to_iso_time, to_reltime

log = logging.getLogger(__name__)

PALETTE = [
    ("red", "#f00"), ("green", "#00c000"), ("blue", "#00f"), ("yellow", "#ff0"),
    ("purple", "#800080"), ("orange", "#ff8000"), ("cyan", "#0ff"), ("magenta", "#f0f"),
    ("teal", "#008080"), ("lime", "#80ff00"), ("brown", "#8b4513"), ("pink", "#ffc0cb"),
]

BUILTIN_JUDGEMENT_TYPES = {
    "AC": {"name": "Accepted", "penalty": False, "solved": True},
    "WA": {"name": "Wrong Answer", "penalty": True, "solved": False},
    "PE": {"name": "Presentation Error", "penalty": True, "solved": False},
    "TLE": {"name": "Time Limit Exceeded", "penalty": True, "solved": False},
    "MLE": {"name": "Memory Limit Exceeded", "penalty": True, "solved": False},
    "OLE": {"name": "Output Limit Exceeded", "penalty": True, "solved": False},
    "CE": {"name": "Compile Error", "penalty": False, "solved": False},
    "RE": {"name": "Run-Time Error", "penalty": True, "solved": False},
    "SE": {"name": "Submission Error", "penalty": False, "solved": False},
}

USER_RE = re.compile(r"^([A-Za-z]\d{1,2})\s+(\S.*)$")


@dataclass
class ContestData:
    contest: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    groups: dict[str, dict] = field(default_factory=dict)
    organizations: dict[str, dict] = field(default_factory=dict)
    teams: dict[str, dict] = field(default_factory=dict)
    problems: dict[str, dict] = field(default_factory=dict)
    languages: dict[str, dict] = field(default_factory=dict)
    judgement_types: dict[str, dict] = field(default_factory=dict)
    submissions: dict[str, dict] = field(default_factory=dict)
    judgements: dict[str, dict] = field(default_factory=dict)


def _build_contest(contest_id: str, manual_contest: dict | None) -> dict:
    mc = dict(manual_contest or {})
    return {
        "id": contest_id,
        "name": mc.get("name", "contest"),
        "formal_name": mc.get("formal_name", mc.get("name", "contest")),
        "start_time": mc.get("start_time"),
        "duration": mc.get("duration", "5:00:00"),
        "scoreboard_freeze_duration": mc.get("scoreboard_freeze_duration", "0:00:00"),
        "scoreboard_type": "pass-fail",
        "penalty_time": mc.get("penalty_time", "0:20:00"),
    }


def _build_state(contest: dict, now: dt.datetime) -> dict:
    state = {"started": None, "ended": None, "frozen": None,
             "thawed": None, "finalized": None, "end_of_updates": None}
    start_s = contest.get("start_time")
    if not start_s:
        return state
    start = dt.datetime.fromisoformat(start_s)
    if now >= start:
        state["started"] = to_iso_time(start)
    end = start + parse_reltime(contest["duration"])
    if now >= end:
        state["ended"] = to_iso_time(end)
    freeze = parse_reltime(contest.get("scoreboard_freeze_duration", "0:00:00"))
    if freeze.total_seconds() > 0:
        state["frozen"] = to_iso_time(end - freeze)
    return state


def _parse_user(user: str) -> tuple[str, str] | None:
    m = USER_RE.match(user.strip())
    if not m:
        return None
    return sanitize_id(m.group(1).upper()), m.group(2)


def translate(contest_id: str, manual: dict, raw_submissions: list[dict],
              raw_problems: list[dict], raw_teams: list[dict] | None,
              now: dt.datetime) -> ContestData:
    data = ContestData()
    mc = manual.get("contest") or {}
    tz = mc.get("pta_timezone", "+08:00")
    data.contest = _build_contest(contest_id, mc)
    data.state = _build_state(data.contest, now)

    for key in ("groups", "organizations", "teams", "problems", "languages"):
        for k, v in (manual.get(key) or {}).items():
            entry = dict(v)
            entry["id"] = k
            data.__dict__[key][k] = entry

    data.judgement_types = {k: dict(v, id=k) for k, v in BUILTIN_JUDGEMENT_TYPES.items()}
    for k, v in (manual.get("judgement_types_overrides") or {}).items():
        if k in data.judgement_types:
            data.judgement_types[k].update(v)

    for i, p in enumerate(raw_problems):
        pid = sanitize_id(str(p.get("id", i)))
        color, rgb = PALETTE[i % len(PALETTE)]
        entry = data.problems.get(pid, {})
        entry.update({
            "id": pid,
            "label": p.get("label") or chr(ord("A") + i),
            "name": p.get("title", p.get("name", pid)),
            "ordinal": p.get("ordinal", i + 1),
            "color": p.get("color", entry.get("color", color)),
            "rgb": p.get("rgb", entry.get("rgb", rgb)),
        })
        data.problems[pid] = entry

    for lang in dict.fromkeys(str(s.get("language", "unknown")) for s in raw_submissions):
        lid = sanitize_id(lang)
        if lid not in data.languages:
            data.languages[lid] = {"id": lid, "name": lang}

    for row in raw_teams or []:
        tid = sanitize_id(str(row["id"]))
        data.teams[tid] = {
            "id": tid, "name": row.get("name", tid), "label": row.get("label", tid),
            "group_id": row.get("group_id"), "organization_id": row.get("organization_id"),
            "icpc_id": row.get("icpc_id"),
        }
        room = str(row.get("group_id") or tid[:1])
        data.groups.setdefault(room, {"id": room, "name": room})

    start = dt.datetime.fromisoformat(data.contest["start_time"]) if data.contest["start_time"] else None
    for row in raw_submissions:
        try:
            ts = parse_pta_time(row["submit_time"], tz)
        except (KeyError, ValueError, TypeError) as e:
            log.warning("skip submission with bad time: %s (%s)", row.get("id"), e)
            continue
        parsed = _parse_user(str(row.get("user", "")))
        if parsed is None:
            log.warning("skip submission with unparsable user: %s (unparsable user field)", row.get("id"))
            continue
        team_id, team_name = parsed
        room = team_id[0]
        team = data.teams.get(team_id)
        if team is None:
            data.teams[team_id] = {
                "id": team_id, "name": team_name, "label": team_id,
                "group_id": room, "organization_id": None, "icpc_id": None,
            }
        else:
            # manual-defined team: fill fields the submission row can derive (manual wins)
            team.setdefault("name", team_name)
            team.setdefault("label", team_id)
            team.setdefault("group_id", room)
            team.setdefault("organization_id", None)
            team.setdefault("icpc_id", None)
        data.groups.setdefault(room, {"id": room, "name": room})
        sid = sanitize_id(str(row["id"]))
        problem_id = sanitize_id(str(row.get("problem_index", "")))
        language_id = sanitize_id(str(row.get("language", "unknown")))
        contest_time = to_reltime(ts - start) if start else "0:00:00"
        data.submissions[sid] = {
            "id": sid, "team_id": team_id, "problem_id": problem_id,
            "language_id": language_id, "time": to_iso_time(ts),
            "contest_time": contest_time,
        }
        status = str(row.get("status", ""))
        # status may be a Chinese label (PTA_VERDICT_MAP) or a raw API enum (PTA_STATUS_VERDICT_MAP)
        if status in PTA_VERDICT_MAP:
            jtype, _solved, _penalty = PTA_VERDICT_MAP[status]
        else:
            jtype = PTA_STATUS_VERDICT_MAP.get(status, (None, False, False))[0]
        data.judgements[sid] = {
            "id": sid, "submission_id": sid, "judgement_type_id": jtype,
            "current": True, "start_time": to_iso_time(ts),
            "start_contest_time": contest_time,
            "end_time": to_iso_time(ts) if jtype else None,
            "end_contest_time": contest_time if jtype else None,
        }
    return data
