from __future__ import annotations

import datetime as dt

from .timefmt import parse_reltime, to_iso_time, to_reltime
from .translate import ContestData


def _parse_time(rel: str) -> dt.timedelta:
    return parse_reltime(rel)


def compute_scoreboard(data: ContestData, now: dt.datetime,
                       group_id: str | None = None) -> dict:
    penalty = _parse_time(data.contest.get("penalty_time", "0:20:00"))
    start = (dt.datetime.fromisoformat(data.contest["start_time"])
             if data.contest.get("start_time") else None)
    problem_ids = sorted(
        data.problems.values(), key=lambda p: p.get("ordinal", 0))
    problem_ids = [p["id"] for p in problem_ids]

    teams = [t for t in data.teams.values()
             if group_id is None or t.get("group_id") == group_id]

    by_team: dict[str, list] = {t["id"]: [] for t in teams}
    for sub in data.submissions.values():
        if sub["team_id"] in by_team:
            by_team[sub["team_id"]].append(sub)

    rows = []
    for team in teams:
        per_problem: dict[str, dict] = {
            pid: {"problem_id": pid, "num_judged": 0, "num_pending": 0,
                  "solved": False, "time": None}
            for pid in problem_ids
        }
        num_solved = 0
        total_penalty = dt.timedelta(0)
        failed_eligible: dict[str, int] = {}
        subs = sorted(by_team[team["id"]], key=lambda s: _parse_time(s["contest_time"]))
        for sub in subs:
            pid = sub["problem_id"]
            if pid not in per_problem:
                per_problem[pid] = {"problem_id": pid, "num_judged": 0,
                                    "num_pending": 0, "solved": False, "time": None}
            cell = per_problem[pid]
            jud = data.judgements.get(sub["id"], {})
            jtype = jud.get("judgement_type_id")
            if jtype is None:
                cell["num_pending"] += 1
                continue
            jt = data.judgement_types.get(jtype, {})
            cell["num_judged"] += 1
            if jt.get("solved") and not cell["solved"]:
                cell["solved"] = True
                cell["time"] = sub["contest_time"]
                num_solved += 1
                # ICPC: penalty per penalty-eligible failed attempt before the
                # first AC; non-penalty verdicts (CE/SE) are not counted.
                total_penalty += (_parse_time(sub["contest_time"])
                                  + penalty * failed_eligible.get(pid, 0))
            elif not jt.get("solved"):
                if jt.get("penalty"):
                    failed_eligible[pid] = failed_eligible.get(pid, 0) + 1
        rows.append({
            "rank": 0,
            "team_id": team["id"],
            "score": {"num_solved": num_solved, "total_time": to_reltime(total_penalty)},
            "problems": [per_problem[pid] for pid in problem_ids],
        })

    rows.sort(key=lambda r: (-r["score"]["num_solved"],
                             _parse_time(r["score"]["total_time"]), r["team_id"]))
    rank = 0
    prev_key = None
    for i, r in enumerate(rows):
        key = (r["score"]["num_solved"], r["score"]["total_time"])
        if key != prev_key:
            rank = i + 1
            prev_key = key
        r["rank"] = rank

    contest_time = "0:00:00"
    if start and now >= start:
        contest_time = to_reltime(now - start)
    return {
        "time": to_iso_time(now),
        "contest_time": contest_time,
        "state": dict(data.state),
        "rows": rows,
    }
