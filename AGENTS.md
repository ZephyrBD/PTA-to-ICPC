# AGENTS.md — PTA to ICPC（agent 向）

面向在本仓库里工作的 agent。**本目录 `pta2icpc/` 自成一个 git 仓库**；上层工作区根 `PTA Helper/` 不是仓库。使用者教程见 `README.md`，细节见 `docs/`。

## 环境与工作目录

- Python **3.13+**，用 **uv** 管理（`pyproject.toml` + `uv.lock`；首次 `uv sync`）。
- 所有相对路径（`manual/`、`pta2icpc/paths.py` 的常量）都相对**本仓库根**，命令**必须**在本目录下执行，否则读写错误的 `manual/`。

## 命令

- 测试：`uv run python -B -m pytest -q -p no:cacheprovider`（`-B` + `-p no:cacheprovider` 避免留下 `__pycache__` / `.pytest_cache`）。
- 服务：`uv run pta2icpc`（由用户自行启停，agent 不得擅自启停）。
- setup GUI：`uv run pta2icpc-setup`（**禁止在自动化 / 无头环境里运行**）。
- 只读诊断：`uv run pta2icpc-diag`（气球状态对账；纯 stdlib、只读、退出码恒 0）。

## 架构

- 唯一服务入口 `pta2icpc.__main__:main`（`uv run pta2icpc`）→ `app._build_default_app` → `create_app` 工厂。**没有模块级 `app`**，不能用 `uvicorn pta2icpc.app:app`。
- 数据流：`PtaClient` 轮询 PTA → `Poller`（限流 / 详情预算 / 打印流水线）→ `translate` 产出 `ContestData` → `Store`（diff 成事件）→ FastAPI 只读路由 + `event-feed`。
- `setup.py` 是 tkinter GUI（单文件、较大）；可测逻辑抽成纯函数单独测试，GUI 本身不自动化测试。

## 硬约束

- **绝不**提交 / 打印 / 记录 PTA cookie。`manual/contest/setup-connection.json` 是密钥载体；`manual/` 整目录已 gitignore。
- **绝不**提交 / 记录 `api_password`（`manual/config/config.json`）——`/api/v4/` 的 HTTP Basic 密钥，与 PTA cookie 同级。**唯一例外**：服务入口启动时打印 `credentials_banner`（`Server View User Info: admin / <密码>`），供操作者从控制台读取——**仅控制台输出，绝不写入 `manual/pta2icpc.log`**。
- 打印流水线会产生**真实硬件**副作用：测试**绝不允许**走打印路径（`manual/print_sent.json`、`printjob.send_job`），用 `httpx.MockTransport` 等替身。
- 不擅自启停用户的服务；不改真实状态文件（`manual/` 下的运行文件）；不动工作区根的 `参考项目/`。
- 子代理不得提交 / 推送；测试只能使用 `tmp_path` + `monkeypatch.chdir`，不得在仓库根创建 `manual/` 残留。
- **许可证**：本仓库为 `AGPL-3.0-or-later`（`LICENSE` + `pyproject.toml` 的 `license`）。**禁止**引入 CDS 等非商业 / 不兼容许可的源码；引入 MIT / BSD 等宽松许可第三方代码时须保留其版权与许可声明（不得删除或改写）。
- 源码注释**刻意密集**（记录设计决策、任务编号、踩坑与取舍），编辑时保留并沿用，不要自作主张清理。

## 布局不变量

- `pta2icpc/paths.py` 是**唯一**路径来源，路径**不可配置**：不得复活 `Settings.manual_dir`，不得新增 CLI / 环境变量覆盖。
- 日志统一由 `pta2icpc/logging_config.py` 配置（formatter 集中在 `LogFormatter`：控制台彩色、文件纯文本、短名映射集中于此；stderr + `manual/pta2icpc.log` 轮转），只有入口 `main()` 调用 `configure_logging`；启动横幅经 `banner()` 走 `console_only`（仅控制台、绝不落盘）；模块内**不得** `basicConfig` / 用 `print` 记日志（`config error:` / `connection not configured …` 等经 `log.error`），也不得新增日志路径 / 级别开关（`LOG_FILE` 固定为 `paths.LOG_FILE`；`diag_balloons.py` 例外：stdlib-only、无 logger、报表走 stdout）。
- `load_manual` 只 glob 并合并 `manual/contest/*.json`；`manual/config/config.json` **不参与** manual 覆盖合并。
- README 顶部徽章里的测试数是**静态**的（shields.io），改动测试数后需手动更新。

## 关键契约（改这些代码前必读）

- **气球 id**：只增、永不复用；资格暂时消失只标 `active: false` 并保留号与 `done`（`balloons.py`）。xcpc-tools 侧按 `balloonid` upsert——换号会造成孤儿行与已发放气球复活。
- **自动定稿**：`auto` 规则为 `end + 5:00` 定稿、`end_of_updates = end + 5:00 + 1s`；`state` 事件必须是该批事件流的**最后一条**，`end_of_updates` 是 feed 的最后一行。零提交 / 未判定提交都不阻止定稿。
- **设置校验**：`Settings` 非法必须 `SystemExit(1)`（打印 `config error: …`）。
- **接口认证**：`/api/v4/*` 需 HTTP Basic（用户名固定 `admin`，密码为 `manual/config/config.json` 的 `api_password`，app 级依赖拦截）。无 `api_password`（空 / 缺失）服务入口**拒绝启动**（`SystemExit(1)`）；`/healthz` 与 `/contests*` 重定向保持开放；`create_app(api_password=None)` 仍完全开放（既有测试零改动）。

## 文档职责

- `README.md` 面向使用者，`docs/` 面向细节，本文件面向 agent。
- 详细端点 / 契约见 `docs/api.md`，目录布局与设置字段见 `docs/configuration.md`，数据来源与 setup 见 `docs/data-and-setup.md`。

## 参考材料

- `docs/superpowers/specs/*`（设计文档，8 篇；部分已过时，以源码为准）。
- 工作区根：`docs/contest-api.md`（Contest API 2026-01 摘录，**仓外**）。
- 工作区根：`参考项目/`（上游 PTA_BALL 克隆，只读参考）。
- 致谢 / 参考：[ICPC Contest API 2026-01](https://ccs-specs.icpc.io/2026-01/)、[ICPC Tools](https://github.com/icpctools/icpctools)（[CDS](https://tools.icpc.global/cds/)）、[DOMjudge](https://github.com/DOMjudge/domjudge)（[官网](https://www.domjudge.org/)）、[Hydro/xcpc-tools](https://github.com/hydro-dev/xcpc-tools)、[laobuzhang/PTA_BALL](https://github.com/laobuzhang/PTA_BALL)。

## 工作流

规模改动 = brief（`.superpowers/sdd/task-N-brief.md`）→ 实现子代理 → 独立 review 子代理 → pytest 绿 → 主代理提交 → 追加 `.superpowers/sdd/progress.md`。
