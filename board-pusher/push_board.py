#!/usr/bin/env python3
"""把 pta2icpc（Contest API 2026-01 形状）的实时数据转换成 CUIT Board
（xcpcio 分支）榜单格式，并周期性推送到 Cloudflare Pages KV。

数据流：
  pta2icpc /api/v4 (HTTP Basic) ──► 转换 (config/team/run.json)
      ──► PUT {board}/api/live/data/<key> (Bearer LIVE_PUSH_TOKEN)

用法（在本目录下）：
  python push_board.py --contest-id cuitcpc \
      --pta2icpc http://127.0.0.1:8000 --pta2icpc-contest 0 \
      --api-password <pta2icpc 的 api_password>

环境变量（推荐，避免密码/token 进 shell 历史）：
  PTA2ICPC_PASSWORD   pta2icpc /api/v4/ 的 HTTP Basic 密码
  CUITBOARD_TOKEN     Pages 环境变量 LIVE_PUSH_TOKEN 的值（占位，向部署者索取）

比赛结束后归档清理：
  python push_board.py --contest-id cuitcpc --delete-only
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# xcpcio 状态映射：Contest API judgement-type id → xcpcio SubmissionStatus。
# 未列出的判定一律按 WRONG_ANSWER 处理（榜单上视为未通过，安全兜底）。
STATUS_MAP = {
    "AC": "ACCEPTED",
    "WA": "WRONG_ANSWER",
    "TLE": "TIME_LIMIT_EXCEEDED",
    "MLE": "MEMORY_LIMIT_EXCEEDED",
    "OLE": "OUTPUT_LIMIT_EXCEEDED",
    "RTE": "RUNTIME_ERROR",
    "RE": "RUNTIME_ERROR",
    "CE": "COMPILATION_ERROR",
    "PE": "PRESENTATION_ERROR",
    "JE": "JUDGING_ERROR",
    "PD": "PENDING",
}

DEFAULT_BOARD_BASE = "https://cuitboard.pages.dev"
DEFAULT_PTA2ICPC = "http://127.0.0.1:8000"


def http_json(url: str, *, auth: tuple[str, str] | None = None,
              method: str = "GET", token: str | None = None,
              body: bytes | None = None) -> bytes:
    """极简 HTTP 客户端（stdlib-only，避免给 board-pusher 引依赖）。"""
    req = urllib.request.Request(url, method=method, data=body)
    if auth:
        cred = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def parse_contest_time(value: str) -> int:
    """pta2icpc 的 contest_time 形如 'H:MM:SS' 或 'H:MM:SS.mmm' → 毫秒。"""
    neg = value.startswith("-")
    if neg:
        value = value[1:]
    h_s, _, s_part = value.partition(":")
    m_s, _, sec = s_part.partition(":")
    total = int(h_s) * 3_600_000 + int(m_s) * 60_000
    if "." in sec:
        whole, frac = sec.split(".")
        total += int(whole) * 1000 + int(frac.ljust(3, "0")[:3])
    else:
        total += int(sec) * 1000
    return -total if neg else total


def iso_to_ms(value: str) -> int:
    """Contest API 的 ISO 时间（如 '2026-01-01T08:00:00+08:00'）→ epoch ms。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class Pusher:
    def __init__(self, args) -> None:
        self.args = args
        self.base = args.pta2icpc.rstrip("/")
        self.cid = args.pta2icpc_contest
        self.auth = ("admin", args.api_password)
        self.board_base = args.board_base.rstrip("/")
        self.key_prefix = f"data/{args.contest_id.strip('/')}"
        self.token = args.board_token

    # ---- pta2icpc 拉取 ----------------------------------------------------

    def fetch(self, path: str):
        url = f"{self.base}/api/v4/contests/{self.cid}" + (f"/{path}" if path else "")
        return json.loads(http_json(url, auth=self.auth))

    def collect(self) -> tuple[dict, list[dict]]:
        """拉取并拼装 xcpcio 的 config.json 与 run.json 数据。"""
        contest = self.fetch("")
        problems = self.fetch("problems")
        submissions = self.fetch("submissions")
        judgements = self.fetch("judgements")

        # problem_id（Contest API 字符串 id）→ 榜单上的 1-based 序号
        order: dict[str, int] = {}
        labels: list[str] = []
        for i, p in enumerate(problems):
            order[p["id"]] = i + 1
            # label 兜底：个别集合里 submission.problem_id 可能存 label
            order.setdefault(p.get("label") or p["id"], i + 1)
            labels.append(p.get("label") or chr(ord("A") + i))

        # judgement_type_id → xcpcio status；拿不到 judgement 的提交视为 PENDING
        jstatus: dict[str, str] = {}
        for j in judgements:
            jstatus[j["submission_id"]] = STATUS_MAP.get(
                j.get("judgement_type_id") or "", "PENDING")

        start_ms = iso_to_ms(contest["start_time"])
        runs = []
        for s in submissions:
            pid = order.get(s.get("problem_id"))
            if pid is None:
                continue
            runs.append({
                "id": s["id"],
                "language": s.get("language_id") or "cpp",
                "problem_id": pid,
                "status": jstatus.get(s["id"], "PENDING"),
                "team_id": s["team_id"],
                # 前端 config 里 submission_timestamp_unit=millisecond
                "timestamp": parse_contest_time(s.get("contest_time") or "0:00:00"),
            })
        runs.sort(key=lambda r: (r["timestamp"], str(r["id"])))

        config = {
            "contest_name": contest.get("name") or args_contest_name(self.args),
            "start_time": start_ms,
            "end_time": iso_to_ms(contest["end_time"]),
            "frozen_time": self.args.frozen_time,
            "penalty": self.args.penalty,
            "problem_id": labels,
            "logo": {"preset": "ICPC"},
            "group": {"official": "正式队伍", "unofficial": "打星队伍"},
            "medal": self.args.medal,
            "status_time_display": {
                "correct": True, "incorrect": True, "pending": True},
            "options": {
                "enable_organization": True,
                "submission_timestamp_unit": "millisecond",
            },
        }
        return config, runs

    def fetch_teams(self) -> list[dict]:
        """Contest API teams → xcpcio team.json 形状。"""
        teams = self.fetch("teams")
        out = []
        for t in teams:
            members = [{"name": {"zh-CN": m}} for m in t.get("members") or []]
            out.append({
                "id": t["id"],
                "name": t.get("name") or t["id"],
                "organization": t.get("organization_id") or None,
                "group": ["official"],
                "icpc_id": t.get("icpc_id") or t["id"],
                "members": members,
                "coaches": [],
                "location": t.get("label") or None,
            })
        # organization 存 id；board 侧 enable_organization 时展示 organization 字段
        for t in out:
            if t["organization"] is None:
                t.pop("organization")
        return out

    # ---- 榜单登记（contest_list.json）--------------------------------------

    def index_entry(self, config: dict) -> dict:
        """单个比赛在 contest_list.json 里的条目，形状仿照现有条目。"""
        year = datetime.fromtimestamp(config["start_time"] / 1000,
                                      tz=timezone.utc).year
        return {
            "board_link": f"/icpc/{year}/{self.args.contest_id}",
            "config": {
                "start_time": config["start_time"],
                "frozen_time": config["frozen_time"],
                "contest_name": config["contest_name"],
                "end_time": config["end_time"],
                "logo": {"preset": "ICPC"},
            },
        }, year

    def update_index(self, config: dict) -> None:
        """在当前目录生成孤立的 contest_list.json（只含本比赛的条目）。

        用法：把条目手动拷进 xcpcio 仓库 packages/apps/board/public/data/
        index/contest_list.json 对应位置，再 git push 走静态构建。
        """
        entry, year = self.index_entry(config)
        index = {"icpc": {str(year): {self.args.contest_id: entry}}}
        path = "contest_list.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=4)
            f.write("\n")
        print(f"[index] 已生成 {path}（icpc/{year}/{self.args.contest_id}），"
              f"请手动合入 xcpcio 仓库后 git push 触发静态构建")

    def dump_data(self, config: dict) -> None:
        """在当前目录生成 config.json 与 team.json（赛前归档用）。"""
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
            f.write("\n")
        teams = self.fetch_teams()
        with open("team.json", "w", encoding="utf-8") as f:
            json.dump(teams, f, ensure_ascii=False, indent=4)
            f.write("\n")
        print(f"[dump] config.json（{len(config['problem_id'])} 题）、"
              f"team.json（{len(teams)} 队）已生成到当前目录")

    # ---- 推送 -------------------------------------------------------------

    def push(self, filename: str, payload) -> None:
        key = f"{self.key_prefix}/{filename}"
        url = f"{self.board_base}/api/live/data/{key}"
        body = json.dumps(payload, ensure_ascii=False).encode()
        resp = http_json(url, method="PUT", token=self.token, body=body)
        print(f"[push] {key} -> {resp.decode(errors='replace')}")


def args_contest_name(args) -> str:
    return args.contest_name or args.contest_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contest-id", required=True,
                    help="榜单上的比赛 id，即推送 key：data/<contest-id>/run.json")
    ap.add_argument("--contest-name", help="榜单显示的比赛名，默认用 contest-id")
    ap.add_argument("--pta2icpc", default=DEFAULT_PTA2ICPC,
                    help=f"pta2icpc 服务地址（默认 {DEFAULT_PTA2ICPC}）")
    ap.add_argument("--pta2icpc-contest", default="0",
                    help="pta2icpc 里的 contest id（默认 0）")
    ap.add_argument("--api-password", default=os.environ.get("PTA2ICPC_PASSWORD"),
                    help="pta2icpc /api/v4 的 HTTP Basic 密码（缺省读 PTA2ICPC_PASSWORD）")
    ap.add_argument("--board-base", default=DEFAULT_BOARD_BASE)
    ap.add_argument("--board-token", default=os.environ.get("CUITBOARD_TOKEN"),
                    help="推送 token（缺省读 CUITBOARD_TOKEN，占位值由部署者提供）")
    ap.add_argument("--frozen-time", type=int, default=3600,
                    help="封榜时长（秒），默认 3600")
    ap.add_argument("--penalty", type=int, default=1200,
                    help="罚时（毫秒），默认 1200（=20 分钟）")
    ap.add_argument("--medal", default='{"official":{"gold":6,"silver":12,"bronze":17}}',
                    help="medal JSON，默认按上届正式队伍配置")
    ap.add_argument("--emit-index", action="store_true",
                    help="在当前目录生成孤立的 contest_list.json"
                         "（只含本比赛条目，手动合入 xcpcio 仓库）")
    ap.add_argument("--dump-data", action="store_true",
                    help="在当前目录生成 config.json / team.json"
                         "（赛前归档进 xcpcio 仓库用）")
    ap.add_argument("--interval", type=float, default=10.0,
                    help="推送间隔秒数，默认 10（站点要求 >=5）")
    ap.add_argument("--once", action="store_true", help="只推一轮就退出")
    ap.add_argument("--teams-only", action="store_true",
                    help="只推 config/team（比赛前登记用），不进入循环")
    ap.add_argument("--delete-only", action="store_true",
                    help="比赛归档后删除 KV 里的实时数据（清理用）")
    args = ap.parse_args()

    if args.delete_only:
        if not args.board_token:
            print("缺少 CUITBOARD_TOKEN / --board-token", file=sys.stderr)
            return 1
        for f in ("run.json", "team.json", "config.json"):
            key = f"data/{args.contest_id.strip('/')}/{f}"
            url = f"{args.board_base.rstrip('/')}/api/live/data/{key}"
            try:
                resp = http_json(url, method="DELETE", token=args.board_token)
                print(f"[delete] {key} -> {resp.decode(errors='replace')}")
            except urllib.error.HTTPError as e:
                print(f"[delete] {key} -> HTTP {e.code}", file=sys.stderr)
        return 0

    if not args.api_password:
        print("缺少 PTA2ICPC_PASSWORD / --api-password", file=sys.stderr)
        return 1
    if not args.board_token:
        print("缺少 CUITBOARD_TOKEN / --board-token（占位 token 向部署者索取）",
              file=sys.stderr)
        return 1

    pusher = Pusher(args)
    interval = max(args.interval, 5.0)
    first = True

    while True:
        try:
            config, runs = pusher.collect()
            # 以下三类都是赛前一次性动作：
            # contest_list.json 登记、config/team 归档文件生成
            if args.emit_index and first:
                pusher.update_index(config)
            if args.dump_data:
                pusher.dump_data(config)
            if not args.teams_only:
                pusher.push("run.json", runs)
            # config/team 比赛期间不变，只在首轮推一次；
            # 归档版走 --dump-data（队名补录等变化由归档 push 覆盖）
            if first:
                pusher.push("team.json", pusher.fetch_teams())
                pusher.push("config.json", config)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}",
                  file=sys.stderr)
            if e.code == 401:
                return 1  # token / 密码错，重试无意义
        except Exception as e:  # 网络抖动：下一轮再试
            print(f"{type(e).__name__}: {e}", file=sys.stderr)

        if args.once or args.teams_only:
            return 0
        first = False
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
