# API（`/api/v4/`）

[← 返回 README](../README.md)

全部接口统一挂在 `/api/v4/` 前缀下，只读，`/api/v4/*` 需 **HTTP Basic 认证**。应用层错误体为 `{"code": <int>, "message": "<str>"}`；框架级错误（未匹配路由 404 / 方法不允许 405 / 参数校验失败 422）仍是 FastAPI 默认的 `{"detail": ...}`。

## 认证

- 全部 `/api/v4/*` 端点需 HTTP Basic 认证：用户名固定 `admin`，密码为 `manual/config/config.json` 的 `api_password`（由 setup 随机生成，可在「比赛设置」tab 改）。认证**不可关闭**。
- 认证失败 → `401`，响应头 `WWW-Authenticate: Basic realm="pta2icpc"`，错误体 `{"code": 401, "message": "authentication required"}`。
- `/healthz` 与 `/contests*` 旧路径 301 重定向**不**需要认证（本机自检）。
- Basic over HTTP 为**明文**：仅适用于本机（`127.0.0.1`）或可信网络；跨网部署必须置于 HTTPS 之后（Contest API 2026-01 要求 HTTPS）。`api_password` 是服务级密钥，勿写入任何文档 / 日志 / 提交。

## 布局：DOMjudge v4 超集 + Contest API 2026-01

- 共享路径（`contests` / `teams` / `organizations`）返回 **DOMjudge v4 形状的超集**，DOMjudge 消费者（xcpctools）可直接使用；Contest API 消费者容忍未知字段。
- 其余集合保持 **Contest API 2026-01 形状**。
- 事件与 access 的 contest 类型为**单数** `contest`。
- `/problems[].time_limit` 为**秒**（number，可含小数）、`memory_limit` 为 **MiB**、`code_limit` 为 **KiB**。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 便捷就绪端点：`ready`、`last_success`、`cookie_ok`、`consecutive_failures`、`rate_limited_until` |
| GET | `/api/v4/` | api info（`version: "2026-01"`） |
| GET | `/api/v4/contests` | 比赛列表（DOMjudge contest 对象数组；快照未就绪返回 `[]`） |
| GET | `/api/v4/contests/{cid}` | 单比赛（DOMjudge；`end_time` = start + duration，比赛未结束也填） |
| GET | `/api/v4/contests/{cid}/access` | 能力清单（`capabilities: []` + 端点/属性表） |
| GET | `/api/v4/contests/{cid}/state` | 比赛状态时间戳 |
| GET | `/api/v4/contests/{cid}/scoreboard` | 现算榜单，支持 `?group_id=` |
| GET | `/api/v4/contests/{cid}/event-feed` | NDJSON 事件流，支持 `?since_token=` |
| GET | `/api/v4/contests/{cid}/submissions/{sid}/files/{filename}` | 提交源码（`text/plain; charset=utf-8`） |
| GET | `/api/v4/contests/{cid}/teams` | DOMjudge 超集数组（按 id 排序） |
| GET | `/api/v4/contests/{cid}/organizations` | DOMjudge 超集数组（按 id 排序） |
| GET | `/api/v4/contests/{cid}/balloons` | 气球列表，支持 `?todo=` |
| POST | `/api/v4/contests/{cid}/balloons/{balloonId}/done` | 标记已发放 → 204；未知 id → 404 |
| POST | `/api/v4/contests/{cid}/balloons/{balloonId}/undone` | 标记未发放 → 204；未知 id → 404 |
| GET | `/api/v4/contests/{cid}/{collection}` | catch-all 集合（Contest API 形状，支持 query 过滤） |
| GET | `/api/v4/contests/{cid}/{collection}/{item_id}` | catch-all 单对象（Contest API 形状） |

### catch-all 集合与过滤

白名单（`app.py:COLLECTIONS`）：`groups`、`organizations`、`teams`、`persons`、`problems`、`languages`、`judgement-types`、`submissions`、`judgements`、`runs`。未知集合 → 404。

过滤参数（空字符串匹配 `null`）：`teams` → `organization_id`；`submissions` → `team_id` / `problem_id` / `language_id`；`judgements` → `submission_id` / `judgement_type_id`；`runs` → `submission_id` / `team_id` / `problem_id`。

`teams` / `organizations` 的**单对象**路由走 catch-all，返回 Contest API 形状（无消费者依赖）。

### DOMjudge 超集字段

- team 额外带 `externalid` / `display_name` / `affiliation_id` / `public_description` / `hidden` / `location`；`public_description` 由 `persons` 推导（`队员：…、…；教练：…、…`，无队员/教练为 `null`）。
- organization 额外带 `formal_name` / `shortname` / `country` / `country_flag` / `logo` / `affiliation_id` / `icpc_id`。

### 旧路径 301

- `GET /contests` → 301 → `/api/v4/contests`
- `GET /contests/{rest}` → 301 → `/api/v4/contests/{rest}`（query 保留）
- `GET /` → 301 → `/api/v4/`
- `/healthz` 路径不变。

## 气球契约

- 每个 (队伍, 题目) 的**首次 AC** 生成一个气球。判定惯例与榜单一致：`judgements[sid].judgement_type_id` → `judgement_types[...].solved`；缺失 / 未判定的 judgement 不算 AC。
- `GET .../balloons?todo=true` 只返回未发放。
- 排序：**未发放在前、已发放在后**；各组内按**首次 AC 时间倒序**。
- 气球对象字段：`balloonid`（int）、`time`（**数值 Unix 秒**）、`problem`（label）、`contestproblem`、`team`（`"{teamid}: {队名}"`）、`teamid`、`location`（`team.label`）、`affiliation` / `affiliationid`、`category` / `categoryid`（第一个分组）、`total`（该队全部 first-AC 题目 `{label: contestproblem}`，按 label 排序）、`done`。
- `POST .../balloons/{balloonId}/done|undone`：成功 204，未知 id → 404 `balloon not found: {id}`。

### `manual/balloons.json` 状态

形状：`{"next_id": int, "entries": {balloonid: {"team_id", "problem_id", "done", "active"}}}`。

- 记录**只增不删**，`balloonid` **只增、永不复用 / 不改号**。
- 某队某题的首次 AC 资格暂时消失时只标记 `active: false`（保留号与 `done`）；资格恢复时复用原号。这样以 `balloonid` upsert 的消费者（xcpc-tools 写入自己的 `balloon.db`）不会产生孤儿行，也不会让已发放气球复活。
- 文件大小以「曾经出现过的不同 (队伍, 题目) 对」为上限，这是换取稳定 id 的取舍。
- 缺失 / 损坏文件从空状态开始（warning，不抛异常）。

## event-feed

- `application/x-ndjson`，每行一个事件 `{"type", "id", "data", "token"}`；`token` 为**字符串**。
- 从头全量重放缓冲窗口（最近 10000 条）；`?since_token=<字符串>` 只回放更新的；非法 / 过期 → 400。
- 对象从快照中消失时发删除事件（`data: null`，id 保留）。
- 60 秒无事件发一个空行保活；每 0.5 秒轮询新事件。
- 定稿后快照冻结（`store.apply_all` 变 no-op）：`state` 事件（含 `end_of_updates`）是该批**最后一行**。事件流本身不自行结束（只在客户端断开时返回），消费者（CDS）在看到 `end_of_updates` 后主动断开连接，表现为「以 EOF 收尾、正常退出（而非超时）」。

## 自动定稿（`/state`）

`manual/contest/setup-contest.json` 的 `contest.finalized_at` 三态：

- 省略 / `""` / `"auto"`（默认）→ **自动**：纯时间驱动，比赛结束后 **5 分钟**再 1 秒（`now >= end + 5:00 + 1s`）即定稿。此时 `finalized` = `thawed` = `end + 5:00`（`thawed` 仅在封榜时写），`end_of_updates` = `end + 5:00 + 1s`，严格满足 `结束 < finalized/thawed < end_of_updates`。**零提交、仍有未判定提交都不阻止**定稿（迟到判定在冻结后被丢弃）。
- `"never"` → 永不自动定稿（`finalized` 与 `end_of_updates` 为 `null`）。
- ISO-8601 时间 → 显式定稿：`T = max(该时间, 结束 + 1s)`，`end_of_updates = T + 1s`。

已结束但仍存在未判定提交时，poller 会**去重**打印一次 warning（仅提示，不影响定稿）。

## 语言 id

编译器枚举与显示名统一映射到 Contest API 2026-01 标准语言 id：`GCC` / `MODERN_GCC` / `CLANG` → `c`，`GXX` / `CLANGXX` → `cpp`，`JAVAC` → `java`，`PYTHON2` → `python2`，`PYTHON3` → `python3`，`RUBY` → `ruby`，`GO` → `go`，`GHC` → `haskell`，`FPC` → `pascal`；其余枚举经 `sanitize_id` 原样保留。每个 language 对象带 known-languages 附录的 `extensions` 表（未知 id → `[]`）。提交的 `language_id` 同步用标准 id。
