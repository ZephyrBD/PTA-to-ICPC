# AGENTS.md — PTA Patch（agent 向）

面向在本仓库（自成一个 git 仓库）里工作的 agent。使用者教程见 `README.md`，细节见 `docs/`。

## 工作目录

- 所有相对路径（`.venv/`、`manual/`、`ptapatch/paths.py` 的常量）都以 `ptapatch/` 为根，命令**必须**在该目录下执行。
- 安装 / 快速开始等步骤见 `README.md`，此处不重复。

## 命令

- 测试：`.venv/Scripts/python -B -m pytest -q -p no:cacheprovider`（`-B` + `-p no:cacheprovider` 避免留下 `__pycache__` / `.pytest_cache`）。
- setup GUI：`.venv/Scripts/python -m ptapatch.setup`（**禁止在自动化里运行**）。
- 服务：`.venv/Scripts/python -m ptapatch`（由用户自行启停，agent 不得擅自启停）。

## 硬约束

- **绝不**提交 / 打印 / 记录 PTA cookie。`manual/contest/setup-connection.json` 与历史 `config.json` 都是密钥载体；`manual/` 整目录已 gitignore。
- 打印流水线会产生**真实硬件**副作用：测试**绝不允许**走打印路径（`manual/print_sent.json`、`printjob.send_job`）。用 `httpx.MockTransport` 等替身。
- 不擅自启停用户的服务；不改真实状态文件（`manual/` 下的运行文件）。
- 子代理不得提交 / 推送；测试只能使用 `tmp_path` + `monkeypatch.chdir`。
- 不修改 `config.json`、`manual/`、工作区根的 `参考项目/`。

## 布局不变量

- `ptapatch/paths.py` 是**唯一**路径来源，路径**不可配置**：不得复活 `Settings.manual_dir`，不得新增 CLI / 环境变量覆盖。
- `load_manual` 只 glob 并合并 `manual/contest/*.json`；`manual/config/config.json` **不参与** manual 覆盖合并。

## 关键契约（改这些代码前必读）

- **气球 id**：只增、永不复用；资格暂时消失只标 `active: false` 并保留号与 `done`（`balloons.py`）。xcpc-tools 侧按 `balloonid` upsert——换号会造成孤儿行与已发放气球复活。
- **自动定稿**：`auto` 规则为 `end + 5:00` 定稿、`end_of_updates = end + 5:00 + 1s`；`state` 事件必须是该批事件流的**最后一条**，`end_of_updates` 是 feed 的最后一行。零提交 / 未判定提交都不阻止定稿。
- **设置校验**：`Settings` 非法必须 `SystemExit(1)`（打印 `config error: …`）。

## 文档职责

- `README.md` 面向使用者，`docs/` 面向细节，本文件面向 agent。
- `README.md` 顶部徽章里的测试数是**静态**的（shields.io），改动测试数后需手动更新。

## 参考材料

- `docs/superpowers/specs/*`（设计文档，5 篇）、`docs/pta-api-findings.md`（PTA 内部接口调研笔记）。
- 工作区根：`docs/contest-api.md`（Contest API 2026-01 摘录，**仓外**）。
- 工作区根：`参考项目/`（上游 PTA_BALL 克隆，只读参考）。
- 致谢 / 参考：[ICPC Contest API 2026-01](https://ccs-specs.icpc.io/2026-01/)、[ICPC Tools](https://github.com/icpctools/icpctools)（[CDS](https://tools.icpc.global/cds/)）、[DOMjudge](https://github.com/DOMjudge/domjudge)（[官网](https://www.domjudge.org/)）、[Hydro/xcpc-tools](https://github.com/hydro-dev/xcpc-tools)、[laobuzhang/PTA_BALL](https://github.com/laobuzhang/PTA_BALL)。

## 工作流

规模改动 = brief（`.superpowers/sdd/task-N-brief.md`）→ 实现子代理 → 独立 review 子代理 → pytest 绿 → 主代理提交 → 追加 `.superpowers/sdd/progress.md`。
