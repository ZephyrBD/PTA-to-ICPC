# Design: 彩色控制台日志（xcpc-tools 风格）

状态：用户已批准（2026-10-16，build）。实现子 agent 的执行规格。

## 背景与目标

当前日志已是统一格式 `%(asctime)s %(levelname)-8s %(name)s: %(message)s`（Task 30），但
**没有颜色**，用户觉得难看，要求做成消费端 xcpc-tools 那种「短名 + 级别标签」风格，颜色按
**方案 B（级别色标签 + 标准色）**。

参考对象：xcpc-tools 用 Cordis 的 Console exporter，配置为
`showTime:"dd hh:mm:ss"`、`label:{align:"right",width:9,margin:1}`，格式为
`{dd} {hh:mm:ss} {name 右对齐 9} [{X}] {msg}`；`X` = 级别首字母大写（`S`uccess/`E`rror/
`I`nfo/`W`arn/`D`ebug）。xcpc-tools 里时间戳灰、名字粗体哈希色、**标签不上色**；我们**采用方案 B**：
标签按级别上色（比 xcpc 更实用）。

## 已定决策（用户裁定）

1. **方案 B**：时间戳灰、**名字不上色**、**标签按级别上色**、消息不上色。
2. 颜色用**标准 16 色**（兼容性最好）。
3. 时间格式：**控制台与文件都用 `dd hh:mm:ss`**（`%d %H:%M:%S`，`%d` 零填充）。
4. 启动横幅 `Server View User Info: admin / <密码>` **变成彩色 `[I]` 日志行，但仅控制台、
   绝不写入日志文件**（保持「密钥不落盘」）。
5. Windows：**零依赖 `ctypes` 打开 VT**；**非 TTY / `NO_COLOR` 自动去色**。

## 格式

```
{dd} {hh:mm:ss} {name:>9} [{X}] {消息}
```

- 时间：`time.strftime("%d %H:%M:%S", time.localtime(record.created))` → 例 `16 16:52:50`。
- 名字：短名映射后**右对齐到宽度 9**；超过 9 字符不截断（原样，会顶出去）——因此映射表
  必须保证常见名字 ≤ 9（见下）。
- 标签：`[{X}]`，`X` 见颜色表。
- 多行消息：续行缩进到**消息起始列**（即已渲染前缀的可见宽度）。
- **控制台与文件同布局**；文件**不带任何 ANSI**。

## 颜色表（方案 B，标准 16 色）

| 级别 | 标签 | 颜色 | SGR |
|---|---|---|---|
| DEBUG | `[D]` | 灰 | `\x1b[90m` |
| INFO | `[I]` | 青 | `\x1b[36m` |
| WARNING | `[W]` | 黄 | `\x1b[33m` |
| ERROR | `[E]` | 红 | `\x1b[31m` |
| CRITICAL | `[C]` | 亮红加粗 | `\x1b[1;91m` |

- 时间戳：灰 `\x1b[90m`。
- 名字、消息：不上色。
- 每个着色片段以 `\x1b[0m` 收尾。

## 短名映射（集中在 `logging_config.py`）

| logger 名 | 显示 |
|---|---|
| `pta2icpc.poller` | `poller` |
| `pta2icpc.pta.client` | `pta` |
| `pta2icpc.logging_config` | `logging` |
| `pta2icpc.__main__` | `main` |
| `pta2icpc.init`（横幅专用） | `init` |
| `pta2icpc.<x>`（其余） | `<x>`（如 `balloons`/`store`/`translate`/`printjob`/`config`/`app`/`setup`） |
| `uvicorn` / `uvicorn.error` | `server` |
| `uvicorn.access` | `access` |
| 其它未匹配 | 原名 |

实现：先按显式 dict 查表，否则剥掉 `pta2icpc.` 前缀；未加前缀的名字原样返回。

## `pta2icpc/logging_config.py` 改动

```python
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024     # 不变
LOG_FILE_BACKUPS = 3                     # 不变
_NAME_WIDTH = 9
_LEVEL_TAG = {DEBUG:"D", INFO:"I", WARNING:"W", ERROR:"E", CRITICAL:"C"}
_LEVEL_COLOR = {DEBUG:"\x1b[90m", INFO:"\x1b[36m", WARNING:"\x1b[33m",
                ERROR:"\x1b[31m", CRITICAL:"\x1b[1;91m"}
_TIMESTAMP_COLOR = "\x1b[90m"
_RESET = "\x1b[0m"

def short_name(name: str) -> str: ...
class LogFormatter(logging.Formatter):  # __init__(self, *, use_color: bool)
    def format(self, record) -> str: ...
def _enable_windows_vt() -> bool: ...     # ctypes SetConsoleMode, 仅 win32
def _file_only_filter(record) -> bool: return not getattr(record, "console_only", False)
def configure_logging(level=logging.INFO, log_file=None, color=None) -> None: ...
def banner(text: str) -> None:  # getLogger("pta2icpc.init").info(text, extra={"console_only": True})
```

- `configure_logging`：`color=None` 时自动判定 ——
  `stream.isatty() and not os.environ.get("NO_COLOR") and not os.environ.get("PTA2ICPC_NO_COLOR")`，
  且 Windows 上要求 `_enable_windows_vt()` 成功；否则 `color=False`。`color=True/False` 仅供测试注入。
- 控制台 handler 用 `LogFormatter(use_color=color)`；文件 handler 用
  `LogFormatter(use_color=False)` 并 `addFilter(_file_only_filter)`。
- 保留 Task 30 的幂等（自标记 `_pta2icpc` 的 handler 先移除再加）、`log_file` 调用时解析
  `paths.LOG_FILE`、`root.setLevel(level)`、不碰 uvicorn logger（`log_config=None` 传播）。
- 删除旧的字符串常量 `LOG_FORMAT` / `DATE_FORMAT`（被 formatter 取代）；如测试仍 import 需同步。
- `Formatter.format` 若抛异常会丢日志：整体用 try/except 兜底，异常时退回
  `logging.Formatter` 风格的简单输出。

## 入口改动

- `pta2icpc/__main__.py`：`print(credentials_banner(s.api_password), flush=True)` →
  `banner(credentials_banner(s.api_password))`（仍在校验通过后、`uvicorn.run` 前）。
  注意：`api_password` **仍然绝不进日志文件**（靠 `console_only` filter）。

## 文档改动

- `docs/configuration.md` 日志段：新布局、颜色表、短名映射说明、「文件为纯文本 / 无颜色」、
  两边 `dd hh:mm:ss`。
- `docs/troubleshooting.md` 日志排查段：更新格式示例与「控制台彩色、文件纯文本」。
- `README.md` 统一日志 bullet：加「彩色控制台（xcpc-tools 风格）」；徽章测试数同步。
- `AGENTS.md` 日志不变量：改成「formatter 在 `logging_config`（控制台彩色、文件纯文本、短名映射集中）；
  横幅经 `banner()` 仅控制台」。
- 新设计文档即本文件；README/AGENTS 的设计文档数 7 → 8。

## 测试（`tests/test_logging_config.py` 改写）

- 控制台（`color=True`）：输出含 ANSI 转义、含 `[{X}]` 标签、名字右对齐宽度 9、时间戳匹配
  `^\d\d \d\d:\d\d:\d\d`。
- 文件（同一 `configure_logging`）：**无** `\x1b`，且剥离可见内容后布局与控制台一致。
- `color=False` / `NO_COLOR=1` / `PTA2ICPC_NO_COLOR=1`：输出纯文本、无 ANSI。
- 短名映射逐条断言（`pta2icpc.pta.client`→`pta`、`uvicorn.access`→`access` …）。
- 多行消息：续行缩进宽度 == 消息起始列。
- 横幅：控制台出现 `Server View User Info: admin / <pw>`，**日志文件不含密码**
  （改/复用 Task 30 的 `test_main_*`，入口 `main()` 仍 `monkeypatch.chdir(tmp_path)`）。
- `configure_logging()` 幂等、`log_file` 调用时解析（保留 Task 30 的断言，只改格式化相关内容）。
- 测试不得在仓库根创建真实 `manual/pta2icpc.log`。

## 提交

1 个提交：`feat: colorize console logs in xcpc-tools style`（代码 + 测试 + 设计文档 + 文档 +
徽章）。如需拆「代码 / 文档」两个由主代理决定。

## 风险

- Windows 老 conhost 不支持 VT 时，若 `SetConsoleMode` 失败应**禁用颜色**（本设计已要求），
  否则会出现裸转义码。
- 文件日志改为 `dd hh:mm:ss`（无年月），跨天轮转文件内时间有歧义 —— 用户已明确选择，文档注明。
