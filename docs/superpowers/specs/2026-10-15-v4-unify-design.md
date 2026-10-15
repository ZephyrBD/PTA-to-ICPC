# Design: 全部 API 统一到 /api/v4/（DOMjudge 超集合并 + 缺陷修复）

状态：用户已批准（2026-10-15，build）。实现子 agent 的执行规格。

## 背景与目标

- 用户要求全部接口统一挂 `/api/v4/` 前缀；共享路径（contests/teams/organizations）用
  **DOMjudge 形状的超集** 同时满足 xcpctools（DOMjudge 消费者）与 CDS（Contest API 消费者，
  规范要求客户端容忍未知字段）。
- 保留 PTA 比赛名（不手动覆盖）。
- team 的 `public_description` **塞队员/教练**（来自 persons：role contestant/coach）。
- 旧 `/contests/...` 与 `/` 保留为 **301 重定向** 到 `/api/v4/...`（CDS 安全；用户可随时要求移除）。
- `/healthz` 保持 `/healthz`（操作性接口，不动）。

## 缺陷修复（顺带）

1. `build_domjudge_contest` 的 `end_time` = **start + duration**（计划结束，未结束也填），
   不再取 `state.ended`（比赛未结束时为 null → xcpctools 的 Ends/Freeze 显示错乱）。
   实现：`dt.datetime.fromisoformat(start) + parse_reltime(duration)` → `to_iso_time`；
   start 缺失/解析失败 → None。
2. 气球 `time` 改 **数值 Unix 秒**（`int(epoch_seconds)`）。xcpctools 做 `o.time*1e3`
   数值运算：ISO 字符串得 NaN → freeze 逻辑把**所有气球自动标 done**。
   内部 `first_ac_pairs` 仍存 ISO（排序用），route 渲染时经 `int(fromisoformat(iso).timestamp())` 转秒；
   转换失败 → 0（防御）。

## 新模块/函数

### balloons.py 增补

- `_to_epoch(iso) -> int`：ISO → 秒；解析失败 → 0。
- `build_domjudge_team(data, team_id) -> dict`：
  ```
  {"id": tid, "externalid": tid, "name": name or tid, "display_name": name or tid,
   "organization_id": org_id or None, "group_ids": [...],
   "affiliation_id": org_id or None, "label": label or None,
   "icpc_id": icpc_id or None,
   "public_description": "队员：…、…；教练：…、…" or None,
   "hidden": False, "location": label or None}
  ```
  public_description 由 `data.persons` 推导：`tid in p["team_ids"]` 且
  `role == "contestant"` → 队员列表；`role == "coach"` → 教练列表；中文顿号 `、` 连接、
  两部分用 `；` 连接；都空 → None。
- `build_domjudge_org(data, org_id) -> dict`：
  ```
  {"id": oid, "externalid": oid, "name": name or oid, "formal_name": name or oid,
   "shortname": name or oid, "country": None, "country_flag": [],
   "logo": [], "affiliation_id": oid, "icpc_id": None}
  ```
- `build_balloon`：`"time"` 字段改为 `_to_epoch(sub_time)`（数值）。

### models.py ACCESS_ENDPOINTS 增补
- teams properties 加：`externalid, display_name, affiliation_id, public_description, hidden, location`。
- organizations properties 加：`externalid, formal_name, shortname, country, country_flag, logo, affiliation_id, icpc_id`。

## app.py 路由重排（全部 /api/v4/）

显式路由（注册顺序：先显式后 catch-all，同现 event_feed 模式）：
```
GET  /api/v4/                            → make_api_info()
GET  /api/v4/contests                    → DOMjudge 列表（已存在；忽略 onlyActive 查询参数）
GET  /api/v4/contests/{cid}              → build_domjudge_contest(snap)（合并：替代原 Contest API contest 路由）
GET  /api/v4/contests/{cid}/access       → make_access()
GET  /api/v4/contests/{cid}/state        → snap.state
GET  /api/v4/contests/{cid}/scoreboard   → compute_scoreboard（保留 group_id 查询参数）
GET  /api/v4/contests/{cid}/event-feed   → event_feed.register 改为该路径
GET  /api/v4/contests/{cid}/submissions/{sid}/files/{filename} → 源码文件
GET  /api/v4/contests/{cid}/teams        → [build_domjudge_team(...)]（DOMjudge 超集，显式，遮蔽 catch-all）
GET  /api/v4/contests/{cid}/organizations→ [build_domjudge_org(...)]（同上）
GET  /api/v4/contests/{cid}/balloons?todo=（已存在）
POST /api/v4/contests/{cid}/balloons/{balloonId}/done|undone（已存在）
GET  /api/v4/contests/{cid}/{collection}         → Contest API 形状（其余集合，catch-all）
GET  /api/v4/contests/{cid}/{collection}/{item_id}→ 同上单对象
```
旧路径重定向（放在所有 /api/v4/ 路由之后注册，避免遮蔽）：
```
GET  /contests/{rest:path} → 301 → /api/v4/contests/{rest}（保留 query：request.url.replace(path=…)）
GET  /                      → 301 → /api/v4/
GET  /healthz               → 不变
```
注意：catch-all `{collection}` 白名单保留 teams/organizations（item 路由 `/teams/{id}` 返回 Contest API 形状，
无消费者，可接受）；`require_cid`/`require_snap` 沿用。

## 测试

- 更新既有路由测试前缀：test_api.py / test_event_feed.py 全部 `/contests/...` → `/api/v4/contests/...`；
  重定向断言：GET `/contests/{cid}` → 301 `location` 以 `/api/v4/contests/{cid}` 开头（带 query 保留）。
- 新增：
  - 单 contest 端点：DOMjudge 形状；**比赛未结束时 end_time 非 null**（= start+duration）。
  - teams：DOMjudge 形状（externalid/affiliation_id/public_description/location）、public_description
    含队员/教练、无 persons 时 None。
  - organizations：DOMjudge 形状。
  - balloon `time` 为数值 epoch 秒（与 ISO 起点一致）。
  - access：teams/organizations properties 含新增字段。
- 保持全绿：`.venv/Scripts/python -m pytest -q`（基线 326）。

## 文档

- README：统一 /api/v4/ 布局说明（DOMjudge 超集 + Contest API 其余集合 + 旧路径重定向）。
- `docs/superpowers/specs/2026-10-15-domjudge-balloons-design.md` 已在库内，本规格为增量。

## 约束

- 测试唯一命令：仓库根 `.venv/Scripts/python -m pytest -q`；全部绿（326 → 预期略增）。
- 不碰 `config.json`、`manual/`、`参考项目/`；不 commit；不重启 8000 进程（子 agent 内）。
- 无新依赖；风格对齐（英文 docstring、无 emoji）。
- 旧路径仅 301 重定向，不再提供服务端逻辑。
