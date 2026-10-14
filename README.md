# pta2ccs

把 PTA (pintia.cn) Web UI 内部接口转成 ICPC Contest API 2026-01 只读服务。

用法：
1. `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`
2. 复制 `config.example.json` 为 `config.json` 并填写；复制 `manual.example/` 为 `manual/` 并按需编辑
3. **首次运行**：`.venv/Scripts/python -m pta2ccs.setup` —— 一次性 GUI，从 PTA 拉取题目和队伍，设置每题颜色与自定义队伍分组，写出 `manual/setup-*.json` 后退出。可重复运行（再次运行会预填上次的选择，分组 id 稳定复用）
4. 在 `pta2ccs/` 目录下 `.venv/Scripts/python -m pta2ccs`（从 `config.json` 构建 app 并启动 uvicorn；没有模块级 `app`，不能用 `uvicorn pta2ccs.app:app`）
5. `GET http://127.0.0.1:8000/` → api info；`GET /healthz` → 就绪状态

## 数据来源与手动覆盖

以下数据全部按 `config.json` 里的 `contest_id` 从 PTA API 自动获取，无需手工维护：

- 比赛名、开赛/结束时间、比赛时长（`GET /api/problem-sets/{id}`）
- 封榜时长（`hideScoreboard` + `hidingTime`，按"开赛后 X 分钟封榜"换算）
- 全部可用编译器 → `/languages`（38 种，随 PTA 配置变化）
- 题目列表、全量选手花名册 → `/teams`；PTA 用户组只映射到 `/organizations`（学校），**不再**生成 `/groups`

`manual/*.json`（模板见 `manual.example/`）仅用于**覆盖**，优先级：**manual 显式配置 > PTA API > 内置默认**。改完重启生效。典型用途：改比赛罚时、把用户组/队伍名改成中文显示名。

### 题目颜色 / 队伍分组（`setup` GUI 写出）

`manual/setup-problems.json`、`setup-groups.json`、`setup-teams.json`（由 `python -m pta2ccs.setup` 生成，已被 gitignore）负责两个 PTA 没有的概念：

- **题目颜色** `/problems[].color` / `rgb`：按题号（PTA label，如 `1-1`）设置，覆盖内置 PALETTE 默认色。
- **自定义分组** `/groups` 与队伍上的 `group_ids` 数组（打星 / 正式 / 女队 …）：与 PTA 用户组（学校）完全独立，由操作者在 GUI 中定义；`group_ids` 为空数组表示不属于任何自定义分组。可用 `GET /contests/{id}/scoreboard?group_id=g1` 只看某分组。

设计文档：`../docs/superpowers/specs/2026-10-13-pta2ccs-design.md`
