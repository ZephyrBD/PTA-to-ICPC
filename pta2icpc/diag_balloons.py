"""Read-only balloon-state diagnostic (stdlib only, never writes files).

Compares our ``balloons.json`` (the PTA to ICPC ``BalloonState``) with
xcpc-tools' NeDB ``balloon.db`` to surface the symptoms of balloon-id
churn: orphan rows (their ids we no longer know), missing rows (our active
ids they lack), pair→id disagreements and ``done`` mismatches.

Run: ``python -m pta2icpc.diag_balloons`` — default paths are relative to
the repo root. The exit code is always 0 (a diagnostic, not a gate).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_local_state(path) -> dict:
    """Parse our ``balloons.json`` → ``{"exists", "next_id", "entries"}``.

    ``entries`` maps int balloonid → ``{"team_id", "problem_id", "done",
    "active"}``; a row missing ``active`` counts as active (pre-active
    files upgrade losslessly). A missing/corrupt file is an empty state,
    never an exception.
    """
    out: dict = {"exists": False, "next_id": 1, "entries": {}}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    out["exists"] = True
    next_id = raw.get("next_id")
    if isinstance(next_id, int) and not isinstance(next_id, bool):
        out["next_id"] = next_id
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, dict):
        return out
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
            "active": bool(entry.get("active", True)),
        }
    out["entries"] = entries
    return out


def live_docs(lines) -> dict[str, dict]:
    """NeDB append-only NDJSON → live documents keyed by ``_id``.

    A later line for the same ``_id`` overwrites the earlier one;
    ``{"$$deleted": true, "_id": ...}`` removes a document;
    ``$$indexCreated`` lines and malformed/non-object lines are ignored.
    Only documents with a string ``_id`` are kept.
    """
    out: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        if "$$indexCreated" in doc:
            continue
        _id = doc.get("_id")
        if not isinstance(_id, str):
            continue
        if doc.get("$$deleted"):
            out.pop(_id, None)
            continue
        out[_id] = doc
    return out


def load_xcpc_state(path) -> dict:
    """Read an xcpc-tools NeDB file → ``{"exists", "docs"}``. A missing
    file is not an error (``{"exists": False, "docs": {}}``); bad lines are
    skipped, never raised."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"exists": False, "docs": {}}
    return {"exists": True, "docs": live_docs(text.splitlines())}


def _our_pair(entry) -> tuple[str, str]:
    return (str(entry.get("team_id", "")), str(entry.get("problem_id", "")))


def _their_pair(doc) -> tuple[str, str]:
    return (str(doc.get("teamid", "")), str(doc.get("problem", "")))


def _their_id(doc) -> int | None:
    raw = doc.get("balloonid")
    if isinstance(raw, bool) or raw is None:  # bool is an int subclass
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def compare(ours: dict, theirs: dict) -> dict:
    """Compare our entries (balloonid → entry) with their live docs
    (``_id`` → doc). Pure, no I/O.

    Returns ``{"orphans", "missing", "pair_mismatch", "done_mismatch"}``:
    orphan rows (their balloonid unknown to us — the id-churn symptom);
    missing rows (our active ids they lack); pair→id disagreements for a
    pair present on both sides; and ``done`` disagreements for an id both
    sides know.
    """
    our_ids = set(ours)
    their_by_balloon: dict[int, dict] = {}
    for doc in theirs.values():
        bid = _their_id(doc)
        if bid is not None:
            their_by_balloon[bid] = doc

    orphans = [{"balloonid": bid, "teamid": doc.get("teamid"),
                "problem": doc.get("problem"),
                "done": bool(doc.get("done", False)), "_id": doc.get("_id")}
               for bid, doc in sorted(their_by_balloon.items())
               if bid not in our_ids]

    missing = sorted(bid for bid, e in ours.items()
                     if e.get("active", True) and bid not in their_by_balloon)

    our_pairs: dict[tuple, set] = {}
    for bid, e in ours.items():
        our_pairs.setdefault(_our_pair(e), set()).add(bid)
    their_pairs: dict[tuple, set] = {}
    for bid, doc in their_by_balloon.items():
        their_pairs.setdefault(_their_pair(doc), set()).add(bid)
    pair_mismatch = [{"pair": list(pair), "ours": sorted(our_pairs[pair]),
                      "theirs": sorted(their_pairs[pair])}
                     for pair in sorted(set(our_pairs) & set(their_pairs))
                     if our_pairs[pair] != their_pairs[pair]]

    done_mismatch = [{"balloonid": bid, "ours": bool(ours[bid].get("done")),
                      "theirs": bool(their_by_balloon[bid].get("done"))}
                     for bid in sorted(our_ids & set(their_by_balloon))
                     if bool(ours[bid].get("done"))
                     != bool(their_by_balloon[bid].get("done"))]

    return {"orphans": orphans, "missing": missing,
            "pair_mismatch": pair_mismatch, "done_mismatch": done_mismatch}


def build_report(local: dict, xcpc: dict) -> dict:
    """Assemble the report dict from ``load_local_state`` +
    ``load_xcpc_state`` outputs. Keys are ASCII so ``--json`` is safe."""
    entries = local["entries"]
    docs = xcpc["docs"]
    active = sum(1 for e in entries.values() if e.get("active", True))
    inactive = len(entries) - active
    done = sum(1 for e in entries.values() if e.get("done", False))
    diff = compare(entries, docs)
    return {
        "local_exists": local["exists"],
        "xcpc_exists": xcpc["exists"],
        "local": {"entries": len(entries), "active": active,
                  "inactive": inactive, "done": done,
                  "next_id": local["next_id"]},
        "xcpc": {"live_rows": len(docs),
                 "unique_pairs": len({_their_pair(d) for d in docs.values()})},
        "orphans": diff["orphans"],
        "missing": diff["missing"],
        "pair_mismatch": diff["pair_mismatch"],
        "done_mismatch": diff["done_mismatch"],
    }


def print_report(report: dict) -> None:
    """Human-readable, Chinese-labelled rendering of ``build_report``."""
    loc = report["local"]
    print("== 我方 balloons.json ==")
    if not report["local_exists"]:
        print("  (文件不存在或无法解析，视为空状态)")
    print(f"  entry 总数: {loc['entries']}")
    print(f"  active: {loc['active']}   inactive: {loc['inactive']}")
    print(f"  done: {loc['done']}")
    print(f"  next_id: {loc['next_id']}")
    print("== 对方 balloon.db ==")
    if not report["xcpc_exists"]:
        print(f"  (数据库不存在: {report.get('xcpc_path', '?')})")
    print(f"  live 行数: {report['xcpc']['live_rows']}")
    print(f"  唯一 (team, problem) 对数: {report['xcpc']['unique_pairs']}")
    print(f"== 孤儿行 (对方有、我方无对应 id): {len(report['orphans'])} ==")
    for o in report["orphans"]:
        print(f"  balloonid={o['balloonid']} team={o['teamid']} "
              f"problem={o['problem']} done={o['done']}")
    print(f"== 缺失行 (我方 active、对方缺): {len(report['missing'])} ==")
    for bid in report["missing"]:
        print(f"  balloonid={bid}")
    print(f"== pair-id 不一致: {len(report['pair_mismatch'])} ==")
    for m in report["pair_mismatch"]:
        print(f"  pair={tuple(m['pair'])} ours={m['ours']} "
              f"theirs={m['theirs']}")
    print(f"== done 不一致: {len(report['done_mismatch'])} ==")
    for d in report["done_mismatch"]:
        print(f"  balloonid={d['balloonid']} ours={d['ours']} "
              f"theirs={d['theirs']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="只读诊断：对比 PTA to ICPC 的 balloons.json 与 "
                    "xcpc-tools 的 balloon.db（NeDB）")
    parser.add_argument("--balloons-json", default="manual/balloons.json",
                        help="我方气球状态文件 (默认 manual/balloons.json)")
    parser.add_argument("--xcpc-db",
                        default="../xcpctools/data/.db/balloon.db",
                        help="xcpc-tools NeDB 气球库 "
                             "(默认 ../xcpctools/data/.db/balloon.db)")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON（键为 ASCII）")
    args = parser.parse_args(argv)

    local = load_local_state(args.balloons_json)
    xcpc = load_xcpc_state(args.xcpc_db)
    report = build_report(local, xcpc)
    report["local_path"] = str(args.balloons_json)
    report["xcpc_path"] = str(args.xcpc_db)
    try:
        # 中文标签可能无法被老控制台编码表示；退化为替换字符而非崩溃
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
