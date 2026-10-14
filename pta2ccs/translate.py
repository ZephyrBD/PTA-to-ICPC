from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from .models import sanitize_id
from .pta.client import COMPILER_LANGUAGES
from .pta.verdicts import PTA_STATUS_VERDICT_MAP, PTA_VERDICT_MAP
from .timefmt import parse_offset, parse_pta_time, parse_reltime, to_iso_time, to_reltime

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
    "PA": {"name": "Partial Accepted", "penalty": True, "solved": False},
}


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


def _meta_start_time(meta: dict, tz: str) -> str | None:
    """meta.startAt (ISO-8601 UTC `...Z`, or naive → UTC) → CCS start_time
    in the contest's pta_timezone (same wall-clock form parse_pta_time
    expects before it re-attaches the offset)."""
    raw = meta.get("startAt")
    if not raw:
        return None
    when = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return to_iso_time(when.astimezone(parse_offset(tz)))


def _build_contest(contest_id: str, manual_contest: dict | None,
                   meta: dict | None = None) -> dict:
    """manual explicit > contest meta (fetch_contest_meta) > built-in default
    for name / formal_name / start_time / duration / scoreboard_freeze_duration.
    `or` chains so an empty manual string does not mask the meta value; a
    manual "0:00:00" freeze is explicit and wins (freeze stays off until the
    file is edited). The name falls through manual → meta (problemSet.name)
    → "contest"; formal_name falls through manual → the resolved name.
    """
    mc = dict(manual_contest or {})
    meta = meta or {}
    tz = mc.get("pta_timezone", "+08:00")
    start_time = mc.get("start_time") or _meta_start_time(meta, tz)
    duration = mc.get("duration")
    if not duration:
        seconds = meta.get("durationSeconds")
        duration = (to_reltime(dt.timedelta(seconds=int(seconds)))
                    if seconds is not None else "5:00:00")
    freeze = mc.get("scoreboard_freeze_duration")
    if not freeze:
        f_seconds = meta.get("scoreboardFreezeSeconds")
        freeze = (to_reltime(dt.timedelta(seconds=int(f_seconds)))
                  if f_seconds is not None else "0:00:00")
    name = mc.get("name") or meta.get("name") or "contest"
    return {
        "id": contest_id,
        "name": name,
        "formal_name": mc.get("formal_name") or name,
        "start_time": start_time,
        "duration": duration,
        "scoreboard_freeze_duration": freeze,
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


def translate(contest_id: str, manual: dict, raw_submissions: list[dict],
              raw_problems: list[dict], raw_teams: list[dict] | None,
              now: dt.datetime, meta: dict | None = None) -> ContestData:
    data = ContestData()
    mc = manual.get("contest") or {}
    tz = mc.get("pta_timezone", "+08:00")
    data.contest = _build_contest(contest_id, mc, meta)
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

    # enabled compilers from contest meta → advertised languages even with
    # zero submissions; manual entries (above) and observed submissions
    # (below) still win via setdefault semantics.
    for enum in (meta or {}).get("compilers") or []:
        name = COMPILER_LANGUAGES.get(enum, enum)
        lid = sanitize_id(name)
        data.languages.setdefault(lid, {"id": lid, "name": name})

    for lang in dict.fromkeys(str(s.get("language", "unknown")) for s in raw_submissions):
        lid = sanitize_id(lang)
        if lid not in data.languages:
            data.languages[lid] = {"id": lid, "name": lang}

    # roster (raw_teams from PtaClient.fetch_teams): every member is a team,
    # even with zero submissions. Manual entries were applied above and win —
    # setdefault only fills what manual omitted.
    for row in raw_teams or []:
        tid = sanitize_id(str(row["id"]))
        team = data.teams.setdefault(tid, {"id": tid})
        team.setdefault("name", str(row.get("name") or tid))
        team.setdefault("label", str(row.get("label") or tid))
        # ids sanitized once here so team refs and organizations/groups
        # keys always agree (client normalizes too; sanitize is idempotent)
        gid = row.get("group_id")
        org = row.get("organization_id")
        gid = sanitize_id(str(gid)) if gid not in (None, "") else None
        org = sanitize_id(str(org)) if org not in (None, "") else None
        team.setdefault("group_id", gid)
        team.setdefault("organization_id", org)
        team.setdefault("icpc_id", row.get("icpc_id"))
        # organizations (and mirrored groups) from the PTA user groups
        oid = org or gid
        if oid:
            oname = str(row.get("organization_name") or oid)
            data.organizations.setdefault(oid, {"id": oid, "name": oname})
            data.groups.setdefault(oid, {"id": oid, "name": oname})

    start = dt.datetime.fromisoformat(data.contest["start_time"]) if data.contest["start_time"] else None
    for row in raw_submissions:
        try:
            ts = parse_pta_time(row["submit_time"], tz)
        except (KeyError, ValueError, TypeError) as e:
            log.warning("skip submission with bad time: %s (%s)", row.get("id"), e)
            continue
        raw_team_id = str(row.get("team_id") or "")
        if not raw_team_id:
            # malformed row: no client-normalized identity to attach it to
            # (log the id only — never the team name)
            log.warning("skip submission with missing team_id: %s", row.get("id"))
            continue
        team_id = sanitize_id(raw_team_id)
        team_name = str(row.get("team_name") or "")
        team = data.teams.get(team_id)
        if team is None:
            # submission whose team is not in the roster: still create the
            # team entry — never drop the submission
            data.teams[team_id] = {
                "id": team_id, "name": team_name or team_id, "label": team_id,
                "group_id": None, "organization_id": None, "icpc_id": None,
            }
        else:
            # roster/manual-defined team: fill fields the submission row can
            # derive (existing values — roster and manual — win)
            team.setdefault("name", team_name or team_id)
            team.setdefault("label", team_id)
            team.setdefault("group_id", None)
            team.setdefault("organization_id", None)
            team.setdefault("icpc_id", None)
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
