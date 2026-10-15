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

# compiler enum → source-file extension for the submissions files route
# (only enums with a conventional extension; everything else → ".txt")
COMPILER_EXTENSIONS = {
    "GXX": ".cpp", "MODERN_GCC": ".cpp", "CLANGXX": ".cpp",
    "GCC": ".c", "CLANG": ".c",
    "OBJC": ".m", "JAVAC": ".java",
    "PYTHON2": ".py", "PYTHON3": ".py", "PYPY3": ".py",
    "MCS": ".cs", "GO": ".go",
}


def _file_ext(compiler_enum: str | None) -> str:
    return COMPILER_EXTENSIONS.get(str(compiler_enum or ""), ".txt")


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
    # per-case runs (CCS "runs" collection); keyed by sanitized
    # f"{submission_id}-{case}". sources maps submission_id → program text
    # for the files route — never in any JSON collection, never diffed.
    runs: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


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

    # manual print-problem hide (task 20): the poller already filters, but
    # manual JSON can re-introduce the problem — skip defensively. Truthy
    # guard first: manual_print None must never match a missing label/id.
    manual_print = (manual.get("options") or {}).get("print_problem")

    for i, p in enumerate(raw_problems):
        if manual_print and (p.get("label") == manual_print
                             or p.get("id") == manual_print):
            continue
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
        # limits from PTA programmingProblemConfig (time in ms → reltime,
        # memory/code in KB); manual entries win via setdefault — a manual
        # time_limit/memory_limit/code_limit is never overwritten
        if p.get("time_limit_ms") is not None:
            entry.setdefault("time_limit",
                             to_reltime(dt.timedelta(milliseconds=p["time_limit_ms"])))
        if p.get("memory_limit_kb") is not None:
            entry.setdefault("memory_limit", p["memory_limit_kb"])
        if p.get("code_limit_kb") is not None:
            entry.setdefault("code_limit", p["code_limit_kb"])
        # CCS problem.test_data_count (required integer) from the PTA cases
        # count; skip when the raw problem has no cases (None) so manual
        # entries are never overwritten by a null.
        if p.get("test_data_count") is not None:
            entry.setdefault("test_data_count", p["test_data_count"])
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
        # ids sanitized once here so team refs and organizations keys always
        # agree (client normalizes too; sanitize is idempotent). PTA user
        # groups feed organization_id + organizations ONLY; `groups` is a
        # separate operator-defined classification and is never mirrored here.
        org = row.get("organization_id")
        if org in (None, ""):
            org = row.get("group_id")  # legacy roster shape fallback
        org = sanitize_id(str(org)) if org not in (None, "") else None
        team.setdefault("organization_id", org)
        # manual-provided group_ids win (applied above); the raw roster never
        # introduces group_ids, so a team with no manual entry gets []
        team.setdefault("group_ids", [])
        team.setdefault("icpc_id", row.get("icpc_id"))
        if org:
            oname = str(row.get("organization_name") or org)
            data.organizations.setdefault(org, {"id": org, "name": oname})

    start = dt.datetime.fromisoformat(data.contest["start_time"]) if data.contest["start_time"] else None
    for row in raw_submissions:
        if manual_print and row.get("problem_index") == manual_print:
            continue  # print-problem defensive skip (task 20)
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
                "group_ids": [], "organization_id": None, "icpc_id": None,
            }
        else:
            # roster/manual-defined team: fill fields the submission row can
            # derive (existing values — roster and manual — win)
            team.setdefault("name", team_name or team_id)
            team.setdefault("label", team_id)
            team.setdefault("group_ids", [])
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
            # list-row enrichment (time = seconds float, score = float);
            # None when the row lacks them (pending / defensive)
            "max_run_time": row.get("run_time"),
            "score": row.get("score"),
        }
        if row.get("detail_fetched"):
            # per-submission detail (fetch_submission_detail): source
            # text + per-case runs. sources is never in any JSON
            # collection (files route reads it directly); runs is a
            # first-class collection, diffed per-key by the store.
            program = row.get("program") or ""
            data.sources[sid] = program
            if program:
                ext = _file_ext(row.get("compiler_enum"))
                data.submissions[sid]["files"] = [{
                    "href": f"contests/{contest_id}/submissions/{sid}/files/main{ext}",
                    "filename": f"main{ext}",
                    "mime": "text/plain",
                }]
            # CCS runs carry exactly the 7 spec-required fields
            # (id/judgement_id/ordinal/judgement_type_id/time/contest_time/
            # run_time) plus our extras. Cases whose PTA result maps to no
            # judgement type (SKIPPED/WAITING/JUDGING/NEUTRAL/OVERRIDDEN…)
            # are omitted entirely — CDS rejects a run with a null
            # judgement_type_id ("Invalid judgement type null").
            ordinal = 0
            for r in row.get("run_results") or []:
                jtype = PTA_STATUS_VERDICT_MAP.get(
                    str(r.get("result")), (None, False, False))[0]
                if jtype is None:
                    continue
                # ordinal: 1-based position among emitted runs, in case
                # order — unique within the judgement, within
                # 1..problem.test_data_count
                ordinal += 1
                case = str(r.get("case", ""))
                rid = sanitize_id(f"{sid}-{case}")
                mem = r.get("memory")
                data.runs[rid] = {
                    "id": rid,
                    "judgement_id": sid,
                    "ordinal": ordinal,
                    "judgement_type_id": jtype,
                    "time": to_iso_time(ts),
                    "contest_time": contest_time,
                    "run_time": r.get("time"),
                    "submission_id": sid,
                    "team_id": team_id,
                    "problem_id": problem_id,
                    # PTA reports bytes; the Contest API uses KB
                    "memory": (mem // 1024) if isinstance(mem, int) else None,
                    "exit_code": r.get("exitcode"),
                    "judge_time": to_iso_time(ts),
                }
    return data
