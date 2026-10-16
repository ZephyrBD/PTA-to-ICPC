# Design: 全局统一日志（控制台 + `manual/pta2icpc.log`）与 uv 启动

状态：用户已批准（2026-10-16，build）。实现子 agent 的执行规格。

## 背景与目标

1. **日志不统一**：8 个模块各自 `logging.getLogger(__name__)`，但全仓没有任何
   `basicConfig` / handler，WARNING+ 靠 Python `lastResort` 打成**裸文本**（无时间、级别、
   来源），`log.info` / `log.debug`（如「从缓存恢复 N 条提交」「限流跳过」）**完全看不到**；
   `uvicorn.run(...)` 又用自带格式（`INFO:     Started server process`）；另有若干裸 `print`。
2. **启动不优雅**：靠 `.venv/Scripts/python -m pta2icpc`。改用 Astral `uv`。

目标：所有运行日志（自有 logger + uvicorn）走**同一格式**，同时写 **stderr** 与
**`manual/pta2icpc.log`**（大小轮转）；启动改用 `uv run pta2icpc` 等控制台入口。

## 已定决策（用户裁定）

1. 统一格式：`%(asctime)s %(levelname)-8s %(name)s: %(message)s`，时间 `%Y-%m-%d %H:%M:%S`。
   示例：`2026-10-16 15:12:33 INFO     pta2icpc.poller: restored 42 submissions from cache`
2. 默认级别 **INFO**；**保留** uvicorn 访问日志（控制台与文件都写，每条请求一行）。
3. 日志文件：**`manual/pta2icpc.log`**（`manual/` 根部，与 `balloons.json` 同级）；
   **按大小轮转**：单文件 5 MB、保留 3 份备份。
4. 三个入口（服务 / setup / diag）**共用**同一日志文件。
5. uv：Astral uv + `[project.scripts]` 控制台入口；**提交 `uv.lock`**；文档只留 uv 流程
   （移除 pip / `.venv` 说明）。
6. 版本 `0.4.0 → 0.5.0`；**2 个提交**（日志统一 / uv 启动）。

## 日志格式与 handler（`pta2icpc/logging_config.py`）

```
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3

def configure_logging(level=logging.INFO, log_file=None) -> None: ...
```

- `log_file=None` 时在**调用时**解析 `paths.LOG_FILE`（默认参数不得绑定常量，否则
  `monkeypatch` 不生效）。
- 幂等：给自建 handler 打标记（如 `handler._pta2icpc = True`），重复调用先移除旧的再加，
  绝不叠加。
- 两个 handler 用**同一个** `logging.Formatter(LOG_FORMAT, DATE_FORMAT)`：
  - `StreamHandler()`（默认 stderr）
  - `RotatingFileHandler(log_file, maxBytes=LOG_FILE_MAX_BYTES,
    backupCount=LOG_FILE_BACKUPS, encoding="utf-8")`；父目录 `mkdir(parents=True, exist_ok=True)`
- `logging.getLogger().setLevel(level)`，并确保 `uvicorn` / `uvicorn.error` /
  `uvicorn.access` **传播到 root**（不给自己加 handler、不清 propagate）。
- **不得**在 import 期调用（只有入口 `main()` 调），以免污染测试。

## 路径（`pta2icpc/paths.py`）

- 新增 `LOG_FILE = MANUAL_DIR / "pta2icpc.log"`。
- `ensure_dirs()` **无需改**：`LOG_FILE` 直接在已创建的 `MANUAL_DIR` 下。
  （轮转产生的 `pta2icpc.log.1` 亦在 `manual/` 根部；`manual/` 整目录已 gitignore。）

## 入口接线

顺序统一为 `ensure_dirs()` → `configure_logging()` → 其余（保证 `config error:` 也进文件）。

- `pta2icpc/__main__.py::main()`：
  - 在 `load_settings()` **之前** `configure_logging()`
  - 两条致命提示（connection / api_password not configured）改为 `log.error(...)`，
    **保留原文子串**（`manual/contest/setup-connection.json`、`manual/config/config.json`）
  - 启动横幅 → `print(credentials_banner(...), flush=True)` **保持不变**（见下）
  - `uvicorn.run(_build_default_app(conn), host=s.host, port=s.port, log_config=None)`
    —— `log_config=None` 让 uvicorn 不装自己的 handler，其 logger 传播到我们的 root handler。
- `pta2icpc/setup.py::main()`：开头加 `ensure_dirs()` + `configure_logging()`。
- `pta2icpc/diag_balloons.py`：**不改**。它是「stdlib only、never writes files」的只读诊断，
  没有 logger、报表是 `print` 到 stdout（命令输出，不是日志）。加 handler 会违反其文件契约。

## 消息归属（`config.py` / `app.py`）

- `config.py::_fail`：`print(f"config error: {msg}", file=sys.stderr)` →
  `log.error("config error: %s", msg)`（**保留 `config error:` 子串**；`raise SystemExit(1)`
  不变）。加 `log = logging.getLogger(__name__)`；若 `sys` 不再被引用则删 `import sys`。
- `app.py::_build_default_app` 的重复 print →
  `log.error("connection not configured — run 'python -m pta2icpc.setup' first")`；
  加 `log = logging.getLogger(__name__)`；若 `sys` 不再被引用则删 `import sys`。
- 各模块既有 `log.*` 调用**文本一律不动**（统一 formatter 生效后自然改观）。
- 未配置 logging 时（如测试直接 import）`log.error` 回落到 `lastResort`，仍打到 stderr。

## 启动横幅（密钥处理）

`Server View User Info: admin / <密码>`（`auth.credentials_banner`）**保持纯 `print`**：
**只进控制台、绝不写日志文件**——避免把 `api_password` 持久化。
`docs/configuration.md` 的「唯一例外」措辞明确「仅控制台输出」。

## 测试（`tests/test_logging_config.py` 新增）

- `configure_logging(log_file=tmp_path/"x.log")` 后：root 有且仅有一个流 handler + 一个
  `RotatingFileHandler`，formatter 的 `_fmt` 等于 `LOG_FORMAT`。
- 用某模块 logger 发一条 record → 文件中出现含 日期/级别/`pta2icpc.<name>` 的行。
- **幂等**：连续调用两次，root handler 数不增加。
- `uvicorn.access` 无自带 handler（配置后仍传播到 root）。
- 中文消息经 UTF-8 往返无损。
- 测试结束清理自建 handler（或 fixture 内 `logging.getLogger().handlers.clear()`）。
- `tests/test_paths.py`：加 `LOG_FILE == Path("manual/pta2icpc.log")` 断言。
- **不得**让测试在仓库根创建真实 `manual/pta2icpc.log`；任何调用入口 `main()` 的测试
  必须 `monkeypatch.chdir(tmp_path)`（`setup.main()` 如无测试则不受影响）。
- `tests/test_config.py` 既有 `SystemExit` 断言（`pytest.raises(SystemExit)`，无 capsys）不受影响。

## uv 启动

`pyproject.toml`：

```toml
[project]
version = "0.5.0"

[project.scripts]
pta2icpc = "pta2icpc.__main__:main"
pta2icpc-setup = "pta2icpc.setup:main"
pta2icpc-diag = "pta2icpc.diag_balloons:main"

[dependency-groups]
dev = ["pytest>=8.3"]
```

- 删除 `[project.optional-dependencies] dev`（改由 uv 原生 `[dependency-groups]` 承载）。
- `uv.lock` 由 `uv lock` 生成并提交。
- 文档改为：`uv sync` → `uv run pta2icpc` / `uv run pta2icpc-setup` / `uv run pta2icpc-diag`；
  测试 `uv run python -B -m pytest -q -p no:cacheprovider`；setup GUI / 服务启停仍只由用户操作。

## 文档改动

- `docs/configuration.md`：布局表加 `manual/pta2icpc.log`（自动生成、gitignore）；
  「密钥」段明确横幅仅控制台、绝不入日志文件；可加「日志」小节说明格式/级别/轮转。
- `docs/troubleshooting.md`：加「用 `manual/pta2icpc.log` 排查」。
- `README.md`：安装/快速开始改 uv；徽章测试数更新；日志一句话提及。
- `AGENTS.md`：命令段改 uv；布局不变量补 `LOG_FILE`；「日志统一由 `logging_config`
  配置，模块内不得 `basicConfig` / `print` 记日志（`config error` 等经 `log.error`）」；
  测试数徽章提示保留。

## 提交拆分

1. `feat: unify logging and write logs to manual/pta2icpc.log`
   —— `logging_config.py`、`paths.py`、`config.py`、`app.py`、`__main__.py`、`setup.py`、
   新测试、`docs/configuration.md`、`docs/troubleshooting.md`、README 日志句。
2. `feat: run via uv with console entry points`
   —— `pyproject.toml`（scripts / dependency-groups / version）、`uv.lock`、README 安装段、
   AGENTS.md 命令段。

## 风险 / 注意

- 第一次 `uv sync` 需联网解析依赖并接管现有 `.venv`。
- 文件日志设 INFO 且含访问日志 → 增长较快，靠 5 MB × 3 轮转兜底。
- 转换 `print` → `log.error` 时务必保留文档/契约所引用的原文子串。
