# pta2ccs

把 PTA (pintia.cn) Web UI 内部接口转成 ICPC Contest API 2026-01 只读服务。

用法：
1. `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`
2. 复制 `config.example.json` 为 `config.json`（**可选**，仅服务参数：host/port/轮询/限流，缺省即默认值）；复制 `manual.example/` 为 `manual/` 并按需编辑。cookie 与 contest_id 不在 `config.json` 里，由 setup GUI 写入 `manual/setup-connection.json`（已 gitignore）
3. **首次运行**：`.venv/Scripts/python -m pta2ccs.setup` —— 一次性 GUI。首次启动弹出**比赛连接**对话框（填 contest_id + PTA cookie，预填 `config.json` 里残留的旧值；可选填**打印服务器 URL**；之后可随时用顶栏「🔗 比赛连接…」按钮重新打开），然后从 PTA 拉取题目和队伍，设置每题颜色与自定义队伍分组（**比赛设置** tab 可覆盖开赛时间 / 时长 / 封榜时长 / 定稿规则；三个时间输入框默认由刚拉取的 PTA 值预填，保存时显式写入），题目侧栏用**打印题**单选面板标一题（其数据不进 CDS），队伍侧栏的**座位号 / 队员 / 教练**改用弹窗编辑（双击队伍行或点「编辑队伍…」打开，表格有**座位号**列），写出 `manual/setup-*.json` 后退出。可重复运行（再次运行会预填上次的选择，分组 id 稳定复用）
4. 在 `pta2ccs/` 目录下 `.venv/Scripts/python -m pta2ccs`（连接读自 `manual/setup-connection.json`，缺失时打印错误并退出；从 `config.json` 构建 app 并启动 uvicorn；没有模块级 `app`，不能用 `uvicorn pta2ccs.app:app`）
5. `GET http://127.0.0.1:8000/api/v4/` → api info；`GET /healthz` → 就绪状态。旧路径 `/` 与 `/contests/...` 一律 301 重定向到 `/api/v4/...`（见下）

## 数据来源与手动覆盖

以下数据全部按 `manual/setup-connection.json` 里的 `contest_id` 从 PTA API 自动获取，无需手工维护：

- 比赛名、开赛/结束时间、比赛时长（`GET /api/problem-sets/{id}`）
- 封榜时长（`hideScoreboard` + `hidingTime`，按"开赛后 X 分钟封榜"换算；始终基于**最终生效**的 duration 计算 —— 即 显式 duration 优先，无显式时用 PTA duration，两者都缺时用内置默认 5:00:00 —— 因此 manual 覆盖 duration 后封榜时刻仍正确）
- 全部可用编译器 → `/languages`（38 种，随 PTA 配置变化）
- 题目列表、全量选手花名册 → `/teams`；PTA 用户组只映射到 `/organizations`（学校），**不再**生成 `/groups`

事件流 `GET /api/v4/contests/{id}/event-feed`（NDJSON）：对象从快照中消失时按规范发出删除事件（`data: null`）。`token` 为字符串；`?since_token=` 接收字符串。比赛**定稿**后（见下 `finalized_at`）`/state` 的 `end_of_updates` 落定，此后丢弃一切更新（快照冻结），事件流以 EOF 结束（CDS 正常退出，而非超时）。

`manual/*.json`（模板见 `manual.example/`）仅用于**覆盖**，优先级：**manual 显式配置 > PTA API > 内置默认**。改完重启生效。典型用途：改比赛罚时、把用户组/队伍名改成中文显示名。

### 比赛设置（`setup-contest.json`）

`manual/setup-contest.json`（`{"contest": {...}}`，由 setup GUI **比赛设置** tab 写出）覆盖 PTA 推导的比赛参数：`start_time`（ISO-8601，需带时区偏移）、`duration` / `scoreboard_freeze_duration`（`时:分:秒`），以及 `finalized_at` 定稿规则。三个时间框打开时由**刚拉取的 PTA 值**预填（已有 `manual` 覆盖值优先；封榜时长按**最终生效**的 duration 计算，与保存后服务端解析一致 —— 故「预填后原样保存」不会改变行为；解析出的封榜为 0（即开赛即封榜/未封榜）时预填为**空**，表示封榜关闭，保存后服务端仍解析为 `0:00:00`）；重新拉取会**实时读取**三个输入框当前内容，只更新「未被编辑」的框，你手动改过的值不会被覆盖，「恢复自动（留空）」按钮清空三框回到自动。保存时按当前输入**显式写入** `setup-contest.json`：

- 省略 / `""` / `"auto"`（默认）→ **自动定稿**：**纯时间驱动**，比赛结束后 **5 分钟**（`end + 5:00`）再 **1 秒**（即 `now >= end + 5:00 + 1s`）即定稿：`finalized` = `thawed` = `end + 5:00`（`thawed` 仅在封榜时写）、`end_of_updates` = `end + 5:00 + 1s`，保证 `结束 < finalized/thawed < end_of_updates` 严格成立，且发出时间不晚于 `now`（比较用**带时区**的 datetime，与比赛偏移无关）。**零提交、仍有未判定提交都不阻止**（迟到判定在冻结后被丢弃）；已结束但仍有未判定提交时轮询器仍会**去重**打一次 warning（仅提示，不影响定稿）。
- `"never"` → 永不自动定稿（`/state.finalized` 与 `end_of_updates` 为 null）。
- ISO-8601 时间 → 显式定稿：`T = max(该时间, 结束 + 1s)`，`end_of_updates = T + 1s`。

### 题目颜色 / 队伍分组 / 打印题（`setup` GUI 写出）

`manual/setup-problems.json`、`setup-groups.json`、`setup-teams.json`（由 `python -m pta2ccs.setup` 生成，已被 gitignore）负责三个 PTA 没有的概念：

- **题目颜色** `/problems[].color` / `rgb`：按题号（PTA label，如 `1-1`）设置，覆盖内置 PALETTE 默认色。
- **自定义分组** `/groups` 与队伍上的 `group_ids` 数组（打星 / 正式 / 女队 …）：与 PTA 用户组（学校）完全独立，由操作者在 GUI 中定义；`group_ids` 为空数组表示不属于任何自定义分组。可用 `GET /api/v4/contests/{id}/scoreboard?group_id=g1` 只看某分组。
- **打印题** `setup-problems.json` 的 `print_problem`：题目侧栏单选面板标一题。该题的**全部**数据（题目、submissions、judgements、runs、sources）都不进 CDS。未配置打印服务器时其提交不做详情拉取；配置了 `PRINT_SERVER` 则走打印流水线（见下）。
- **座位号 / 队员 / 教练** `setup-teams.json` 的 `teams[id]` 上的 `seat` / `members` / `coaches`：在**队伍分组** tab 里，双击队伍行或选中后点「编辑队伍…」弹出对话框编辑（左侧表格有**座位号**列），确定写回、取消不改动。`seat` 覆盖队伍 `label`；`members` / `coaches` 不出现在队伍对象上，而是生成 `/persons`（contestant / coach，id 形如 `D22-m0` / `D22-c0`）。空值省略不写。

### 打印流水线（`PRINT_SERVER`，扮演 DOMjudge）

`manual/setup-connection.json` 里的可选 `print_server`（连接对话框第三个输入框）开启后，每轮轮询把**打印题**的提交单独分流：

1. 从提交列表分区出来（不进 translate/store/scoreboard），逐条拉取源码（每轮最多 5 条，顺序执行；429 触发全局冷却）。
2. store 应用新快照后，按 DOMjudge 打印脚本的 multipart 形状 `POST {print_server}`（`file` + `filename` / `lang` / `tname` / `team` / `location` / `group` 字段，文件名 `题目号.扩展名`，语言为 Contest API 标准 id）。
3. 已发送 id 记在 `print_sent.json`（cwd 相对路径，gitignore），成功即持久化，重启不重发；缺失 / 损坏文件从空集开始。
4. 源码为空 / 拉取失败 / POST 失败各计一次尝试，累计超过 10 次永久跳过并打 error 日志。

未配置 `print_server` 时保持旧行为：打印题数据直接丢弃、不写任何状态。

### 语言 id（`/languages`）

编译器枚举与显示名统一映射到 Contest API 2026-01 标准语言 id（`GXX`/`CLANGXX` → `cpp`、`JAVAC` → `java`、`PYTHON3` → `python3` …），并带 known-languages 附录的 `extensions` 表；未知值经 `sanitize_id` 原样保留。提交的 `language_id` 同步用标准 id。

### 统一 `/api/v4/` 布局（DOMjudge 超集 + Contest API 其余集合）

全部接口统一挂在 `/api/v4/` 前缀下。共享路径（contests/teams/organizations）返回 **DOMjudge v4 形状的超集**（xcpctools 等 DOMjudge 消费者可直接用；Contest API 消费者容忍未知字段）；其余集合（state/scoreboard/groups/persons/problems/languages/judgement-types/submissions/judgements/runs/event-feed…）保持 **Contest API 2026-01 形状**。事件与 access 的 contest 类型为单数 `contest`；`/problems[].time_limit` 为**秒**（number，可含小数）、`memory_limit` 为 **MiB**、`code_limit` 为 KiB。

- `GET /api/v4/` —— api info。
- `GET /api/v4/contests` —— 比赛列表（DOMjudge contest 对象，`shortname = name`；快照未就绪返回 `[]`）。
- `GET /api/v4/contests/{cid}` —— 单比赛（DOMjudge 对象；`end_time` = **start + duration**，比赛未结束也填，start 缺失/非法则为 null）。
- `GET /api/v4/contests/{cid}/teams` / `/organizations` —— DOMjudge 超集数组（按 id 排序）。team 带 `externalid/display_name/affiliation_id/public_description/hidden/location`；`public_description` 由 persons 推导（`队员：…、…；教练：…、…`，无队员/教练为 null）。organization 带 `formal_name/shortname/country/country_flag/logo/affiliation_id/icpc_id`。
- 其余集合走 catch-all：`GET /api/v4/contests/{cid}/{collection}` 与 `/…/{collection}/{item_id}`（Contest API 形状，支持 query 过滤；`teams`/`organizations` 的**单对象**路由仍走这里）。
- 气球端点（DOMjudge 风格）：
  - `GET /api/v4/contests/{cid}/balloons?todo=` —— 每 (队伍, 题目) **首次 AC** 一个气球；`todo=true` 只返回未发放。排序：未发放在前、已发放在后，各组内按首次 AC 时间倒序；`time` 为**数值 Unix 秒**。
  - `POST /api/v4/contests/{cid}/balloons/{balloonId}/done` / `/undone` —— 标记已发 / 未发，成功 204，未知 id 404。

旧路径保留为 **301 重定向**（CDS 安全）：`/contests/{rest:path}` → `/api/v4/contests/{rest}`（query 原样保留）、`/` → `/api/v4/`；`/healthz` 不变。

`done` 状态持久化在 `balloons.json`（cwd 相对路径，gitignore；形状 `{"next_id": int, "entries": {balloonid: {"team_id", "problem_id", "done"}}}`）。每次请求按快照实时重算首次 AC：新气球按提交时间补号、不再是首次 AC 的剪除、重启不丢 `done`；缺失 / 损坏文件从空状态开始（warning，不抛）。

设计文档：`../docs/superpowers/specs/2026-10-13-pta2ccs-design.md`、`../docs/superpowers/specs/2026-10-15-print-domjudge-design.md`、`../docs/superpowers/specs/2026-10-15-domjudge-balloons-design.md`、`../docs/superpowers/specs/2026-10-15-v4-unify-design.md`
