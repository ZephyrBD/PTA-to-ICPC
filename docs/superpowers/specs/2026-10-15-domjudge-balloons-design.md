# Design: DOMjudge v4 兼容端点（气球 API，/api/v4/ 下）

状态：用户已批准（2026-10-15，build 模式）。实现子 agent 的执行规格。

## 背景

pta2ccs 增加 DOMjudge v4 兼容表面，供气球发放前端（DOMjudge 风格）消费：
- `GET  /api/v4/contests` —— 比赛列表（DOMjudge contest 对象）
- `GET  /api/v4/contests/{cid}/balloons?todo=` —— 气球列表
- `POST /api/v4/contests/{cid}/balloons/{balloonId}/done` —— 标记已发（204）
- `POST /api/v4/contests/{cid}/balloons/{balloonId}/undone` —— 标记未发（204）

**与 Contest API 表面隔离**：现有 `/contests/{cid}` 等（Contest API 2026-01，8443 CDS 依赖）
**保持不变**；新接口一律挂 `/api/v4/` 前缀（用户明确：`/api/v4/` 是 DOMjudge 专属空间）。

气球语义（DOMjudge 源码 BalloonService/BalloonController 确认）：
- 每 (队伍, 题目) **首次 AC** 生成一个气球，行上有 `done`（持久化）。
- `?todo=true` 只返回未发放；排序 = 未发放在前、已发放在后，各组内按提交时间**倒序**。
- `team` = `"<teamid>: <队名>"`；`total` = 该队全部已发题目 `{label: contestproblem}`，按 label 排序。

## 1. 新模块 `pta2ccs/balloons.py`

### 1.1 纯函数：推导与构造

- `first_ac_pairs(data: ContestData) -> dict[tuple[str, str], dict]`
  判 AC 惯例与 `scoreboard.py` 完全一致：`judgements` 以 sid 为键 →
  `judgement_type_id` → `judgement_types.get(jtype, {}).get("solved")`。
  每个 team 的提交按 `contest_time` 排序（`parse_reltime`），对每 (team, problem)
  记录**首次** AC 的提交，返回 `{(team_id, problem_id): {"time": sub["time"]}}`
  （`sub["time"]` 是绝对 ISO，translate 已产 `to_iso_time`）。
  忽略 judgement 缺失/pending（jtype None → 不算 AC）。

- `build_contestproblem(data, problem_id) -> dict`
  DOMjudge 问题对象，字段严格：
  `{"id", "label", "name", "short_name" (=label), "rgb", "color",
   "time_limit" (数值**秒**，取自快照 `time_limit`；旧版 reltime 字符串仍兼容换算，缺省 None→null),
   "statement": [], "attachments": []}`。
  problem 不存在时返回 None（调用方处理）。

- `build_balloon(data, entry, balloonid, done, total_map) -> dict`
  字段严格（DOMjudge 形状）：
  ```
  {"balloonid": int, "time": sub time(ISO), "problem": label(shortname),
   "contestproblem": <contestproblem>, "team": f"{teamid}: {team_name}",
   "teamid": team_id, "location": team.label or None,
   "affiliation": org name or None, "affiliationid": org id or None,
   "category": 第一个分组名 or None, "categoryid": 第一个分组 id or None,
   "total": {label: contestproblem, ...}, "done": bool}
  ```
  - `team_name` 缺省回退 team_id；`total` = 该队全部 first-AC 题目，键为 label，
    值 `build_contestproblem`，`sorted` 后重建 dict。
  - category 取 team.group_ids 第一个（组在 data.groups 查到 name；找不到用 gid）。
  - org 取 team.organization_id → data.organizations 的 name。

- `build_domjudge_contest(data) -> dict`
  字段严格（用户 schema）：
  ```
  {"id": data.contest["id"], "name": contest.name, "formal_name": contest.formal_name,
   "shortname": contest.name,                 # 用户决定：shortname = name
   "start_time": contest.start_time,          # 原样（ISO，带时区）
   "end_time": data.state["ended"] or None,   # start + duration（已由 _build_state 算好）
   "duration": contest.duration, "scoreboard_freeze_duration": ...,
   "scoreboard_thaw_time": None, "scoreboard_type": ...,
   "penalty_time": ..., "banner": [], "problemset": [],
   "allow_submit": True, "runtime_as_score_tiebreaker": False,
   "warning_message": ""}
  ```

### 1.2 `BalloonState`（持久化 done）

- 文件 `balloons.json`（**cwd**，与 `print_sent.json` 同约定；`.gitignore` 加）。
  形状：`{"next_id": int, "entries": {balloonid: {"team_id", "problem_id", "done": bool}}}`。
- 构造 `BalloonState(path: Path | str = Path("balloons.json"))`。
- 加载：缺失/坏 JSON/形状不对 → 空状态 + `log.warning`（绝不抛）。
- `async def sync(data) -> None`：与 `first_ac_pairs(data)` 对比——
  - 现有 entries 中 (team, problem) 已不在当前 first-AC 集合 → **剪除**（记分牌实时重算，保持一致）；
  - 缺的 (team, problem) 按提交时间升序补发 `next_id`（`done: False`）；
  - 有变化才写盘（tmp + replace 原子写，同 `_persist_print_sent` 模式）；用 `asyncio.Lock` 串行化。
- `async def set_done(balloonid: int, done: bool) -> bool`：改 + 写盘；id 不存在 → False。
- 逆索引 (team, problem) → balloonid（加载时构建，sync 维护），供 O(1) 补号/剪除。

## 2. app.py 路由（全部在 `{collection}` catch-all 之前注册，同 event_feed）

- `create_app(...)` 增加参数 `balloon_state: BalloonState | None = None`（默认 `BalloonState()`）；
  `_build_default_app` 无需改（默认 cwd 文件即可），但保持可注入以便测试。
- `GET /api/v4/contests` → `require_snap()` 未就绪（None）返回 `[]`；否则
  `[build_domjudge_contest(snap)]`。
- `GET /api/v4/contests/{cid}/balloons`，query `todo: bool = False`：
  - `require_cid(cid)`（未知 → 404 error_body）；`require_snap()`。
  - `await balloon_state.sync(snap)` → 遍历 entries，join 快照构造 balloon 列表；
    跳过 (team, problem) 当前解析不到的行（防御）。
  - 排序：done False 在前、True 在后；组内按提交时间倒序（用 first-AC time 比较；
    time 是 ISO 字符串，直接字符串比较在相同时区下成立，或用解析后的比较——用 `parse_pta_time`/`datetime.fromisoformat` 稳妥）。
  - `todo=True` 过滤 done。
- `POST /api/v4/contests/{cid}/balloons/{balloonId}/done` →
  `set_done(balloonid, True)`；False（未知）→ 404 `error_body(404, f"balloon not found: {balloonid}")`；
  成功 → `Response(status_code=204)`。
- `POST /api/v4/contests/{cid}/balloons/{balloonId}/undone` → 同上，False。
- 注意 `{balloonId}` 是 int 路径参数（`balloonId: int`），非数字 FastAPI 自动 422（可接受）。

## 3. 测试

- `tests/test_balloons.py`（新）：
  - 推导：每 (team, problem) 首次 AC；同一队多题各自一个；时间排序正确；pending/非 AC 不算。
  - `build_contestproblem`：time_limit 秒（数值）、short_name=label、statement/attachments 空。
  - `build_balloon`：team="id: 队名"、location=座位号、category/affiliation 解析、total 按 label 排序、缺 label/org/group → null。
  - `build_domjudge_contest`：字段齐、shortname=name、end_time、thaw null、banner/problemset 空。
  - `BalloonState`：sync 幂等（重复 sync 不写盘/不重号）、补号按时间序、剪除、done/undone 持久化（重新 new BalloonState 读回）、坏/缺文件不抛、404 未知 id。
- `tests/test_api.py`（扩展）：4 条路由——GET /api/v4/contests 形状（snap None → []）；GET balloons 排序 + todo 过滤；POST done → 204 + todo 后隐藏；POST undone → 204；未知 balloonid/cid → 404。用现有 TestClient 模式 + 注入 `balloon_state`（用 tmp_path）。

## 4. 文档

- README：新增 `/api/v4/` DOMjudge 兼容端点说明（balloons + done/undone + balloons.json 状态文件）。
- `.gitignore`：`balloons.json`。

## 约束

- 测试唯一命令：仓库根 `.venv/Scripts/python -m pytest -q`（基线 301 passed，全部必须绿）。
- **不碰** `config.json`、`manual/`、`参考项目/`；不 commit；不重启 8000 现有进程（子 agent）。
- 无新依赖；风格对齐现有代码（英文 docstring，无 emoji）。
- Contest API 表面（`/contests/{cid}` 等）**零改动**。
