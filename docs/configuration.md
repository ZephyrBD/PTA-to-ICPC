# 配置与目录布局

[← 返回 README](../README.md)

## 程序设置 `manual/config/config.json`

程序设置只有一个文件：`manual/config/config.json`。**文件缺失 → 除 `api_password` 外全部使用默认值**；**缺少 `api_password`（键缺失或空字符串）→ 服务入口打印 `api_password not configured …` 并以退出码 1 退出**（先跑 setup）；**文件存在但非法（JSON 解析失败或字段校验失败）→ 打印 `config error: …` 并以退出码 1 退出**。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `poll_interval_seconds` | `5.0` | 轮询间隔（秒），必须 > 0 |
| `config_poll_seconds` | `60.0` | 花名册 / 题目 / 比赛元数据等低频数据刷新间隔（秒），必须 > 0 |
| `host` | `127.0.0.1` | 监听地址 |
| `port` | `8000` | 监听端口 |
| `api_password` | （空） | `/api/v4/` HTTP Basic 密码（用户名固定 `admin`）；由 setup 随机生成、可在「比赛设置」tab 改。**必填**：缺失服务拒绝启动 |
| `pta_requests_per_second` | `2.0` | PTA 全局请求限速（每秒），必须 > 0 |
| `detail_fetch_per_round` | `5` | 每轮提交详情拉取条数上限，必须 ≥ 0（`0` = 不拉详情） |
| `fetch_details` | `true` | 是否拉取提交详情（`false` 是比 `detail_fetch_per_round=0` 更粗的总开关） |
| `cache_enabled` | `true` | 是否读写持久化快照缓存（`false` = 既不读也不写） |
| `cache_ttl_seconds` | `3600.0` | 启动时缓存超过该时长即视为过期并忽略，必须 > 0 |
| `pta_request_jitter` | `0.25` | PTA 请求间隔抖动比例（每个间隔追加 `uniform(0, 比例 × 基础间隔)`，只增不减），取值 ∈ [0, 1] |

`cookie` 与 `contest_id` **不在**这个文件里，由 setup GUI 写入 `manual/contest/setup-connection.json`。

## `manual/` 目录布局

| 路径 | 内容 | 写入者 | 是否 gitignore |
|---|---|---|---|
| `manual/config/config.json` | 程序设置（上表，含 `api_password`） | 手工 / setup（接口密码） | 是（整个 `manual/`） |
| `manual/contest/setup-connection.json` | `contest_id` / `pta_cookie` / 可选 `print_server` | 连接对话框 | 是 |
| `manual/contest/setup-contest.json` | 比赛时间覆盖 + 定稿规则 | 比赛设置 tab | 是 |
| `manual/contest/setup-problems.json` | 题目颜色 + 顶层 `print_problem` | 题目颜色 tab | 是 |
| `manual/contest/setup-groups.json` | 自定义分组定义 | 队伍分组 tab | 是 |
| `manual/contest/setup-teams.json` | 队伍分组指派 + `seat` / `members` / `coaches` | 队伍分组 tab | 是 |
| `manual/balloons.json` | 气球发放状态（运行状态） | 运行时自动 | 是 |
| `manual/print_sent.json` | 已发送打印 id（运行状态） | 运行时自动 | 是 |
| `manual/cache/state.json` | 持久化 PTA 快照缓存（运行状态） | 运行时自动 | 是 |
| `manual/pta2icpc.log` | 运行日志（自动生成、大小轮转） | 运行时自动 | 是 |

`manual/balloons.json.tmp`、`manual/print_sent.json.tmp`、`manual/cache/state.json.tmp`、`manual/config/config.json.tmp` 是原子写临时文件（写入失败时可能残留）。`manual/pta2icpc.log.1` / `.2` / `.3` 是日志轮转的备份文件。

启动时自动创建 `manual/`、`manual/config/`、`manual/contest/`、`manual/cache/`（`pta2icpc/paths.py:ensure_dirs`）。

`manual/cache/state.json` 在每轮成功轮询后节流写入（≥ 10s 一次，原子写）：内容为提交列表、配置类数据与 `(提交, 状态)` 详情。重启时若缓存未过 TTL（`cache_ttl_seconds`）且 `contest_id` 匹配，会先服务该快照再增量刷新——避免重启瞬间大量详情拉取触发 429；缓存缺失 / 损坏 / 过期 / 比赛不匹配 / `cache_enabled=false` 一律忽略（warning，不报错）。

## 路径不可配置

所有路径常量硬编码在 `pta2icpc/paths.py`，**不可配置**：`Settings` 没有 `manual_dir` 之类的字段，也没有 CLI / 环境变量覆盖。设置只从固定的 `manual/config/config.json` 读取；`manual/contest/*.json` 只用于比赛覆盖合并。

## 运行目录要求

路径都是**相对路径**，因此所有命令必须在 `pta2icpc/` 目录下运行。在别处运行会读写错误的 `manual/`。

## 日志

服务与 setup GUI 两个入口共用同一套日志配置（`pta2icpc/logging_config.py`），同一格式写到 **stderr** 与 **`manual/pta2icpc.log`**（`pta2icpc-diag` 是 stdlib-only 只读诊断，报表走 stdout，不写日志文件）：

- 布局：`{dd} {hh:mm:ss} {短名:>9} [{X}] {消息}`，时间 `%d %H:%M:%S`（例：`16 15:12:33    poller [I] restored 42 submissions from cache`）。`X` 是级别首字母：`D`ebug / `I`nfo / `W`arning / `E`rror / `C`ritical。
- **控制台彩色**（xcpc-tools 风格，方案 B）：时间戳灰、名字与消息不上色、级别标签按级别上色（DEBUG 灰、INFO 青、WARNING 黄、ERROR 红、CRITICAL 亮红加粗）。非 TTY、设置 `NO_COLOR` / `PTA2ICPC_NO_COLOR`、或 Windows 控制台不支持 VT 时自动去色。
- **日志文件为纯文本**：与控制台同一布局，但**绝不含任何 ANSI 颜色码**——文件 formatter 从不上色，且消息里的裸 `ESC` 控制字符会被清除（防止含控制字节的请求路径污染日志文件或控制台）。
- **短名映射**（集中在 `logging_config.py`）：`pta2icpc.pta.client`→`pta`、`pta2icpc.logging_config`→`logging`、`pta2icpc.__main__`→`main`、`pta2icpc.init`→`init`、`uvicorn`/`uvicorn.error`→`server`、`uvicorn.access`→`access`，其余 `pta2icpc.<x>` 显示为 `<x>`（如 `poller`/`balloons`），未匹配的名字原样显示。
- 名字右对齐到 9 列（超过 9 字符不截断）；多行消息按可见前缀宽度续行缩进。
- 默认级别 `INFO`；uvicorn 的启动日志与访问日志（每条请求一行）也走同一格式与同一文件。
- 轮转：单文件 5 MB、保留 3 份备份（`pta2icpc.log` → `pta2icpc.log.1` …）。

## 密钥

- `manual/contest/setup-connection.json` 含 PTA cookie（`pta_cookie`）与可选打印服务器 URL；`manual/config/config.json` 含接口密码（`api_password`）。这两者都是密钥载体；整个 `manual/` 已 gitignore。
- 不要把 cookie 或 `api_password` 复制进任何文档、日志或提交。唯一例外：服务启动时把接口凭据打印到控制台（`Server View User Info: admin / <密码>`），方便操作者直接读取——这一行**仅输出到控制台，绝不写入日志文件**（`manual/pta2icpc.log`）。
