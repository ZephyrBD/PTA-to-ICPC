# pta2ccs

把 PTA (pintia.cn) Web UI 内部接口转成 ICPC Contest API 2026-01 只读服务。

用法：
1. `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`
2. 复制 `config.example.json` 为 `config.json` 并填写；复制 `manual.example/` 为 `manual/` 并按需编辑
3. 在 `pta2ccs/` 目录下 `.venv/Scripts/python -m pta2ccs`（从 `config.json` 构建 app 并启动 uvicorn；没有模块级 `app`，不能用 `uvicorn pta2ccs.app:app`）
4. `GET http://127.0.0.1:8000/` → api info；`GET /healthz` → 就绪状态

## 数据来源与手动覆盖

以下数据全部按 `config.json` 里的 `contest_id` 从 PTA API 自动获取，无需手工维护：

- 比赛名、开赛/结束时间、比赛时长（`GET /api/problem-sets/{id}`）
- 封榜时长（`hideScoreboard` + `hidingTime`，按"开赛后 X 分钟封榜"换算）
- 全部可用编译器 → `/languages`（38 种，随 PTA 配置变化）
- 题目列表、全量选手花名册 → `/teams`（含用户组 → `/organizations`、`/groups`）

`manual/*.json`（模板见 `manual.example/`）仅用于**覆盖**，优先级：**manual 显式配置 > PTA API > 内置默认**。改完重启生效。典型用途：改比赛罚时、把用户组/队伍名改成中文显示名。

设计文档：`../docs/superpowers/specs/2026-10-13-pta2ccs-design.md`
