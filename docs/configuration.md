# 配置与目录布局

[← 返回 README](../README.md)

## 程序设置 `manual/config/config.json`

程序设置只有一个文件：`manual/config/config.json`。**文件缺失 → 全部使用默认值**；**文件存在但非法（JSON 解析失败或字段校验失败）→ 打印 `config error: …` 并以退出码 1 退出**。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `poll_interval_seconds` | `5.0` | 轮询间隔（秒），必须 > 0 |
| `config_poll_seconds` | `60.0` | 花名册 / 题目 / 比赛元数据等低频数据刷新间隔（秒），必须 > 0 |
| `host` | `127.0.0.1` | 监听地址 |
| `port` | `8000` | 监听端口 |
| `pta_requests_per_second` | `2.0` | PTA 全局请求限速（每秒），必须 > 0 |
| `detail_fetch_per_round` | `5` | 每轮提交详情拉取条数上限，必须 ≥ 0（`0` = 不拉详情） |
| `fetch_details` | `true` | 是否拉取提交详情（`false` 是比 `detail_fetch_per_round=0` 更粗的总开关） |

`cookie` 与 `contest_id` **不在**这个文件里，由 setup GUI 写入 `manual/contest/setup-connection.json`。

## `manual/` 目录布局

| 路径 | 内容 | 写入者 | 是否 gitignore |
|---|---|---|---|
| `manual/config/config.json` | 程序设置（上表） | 手工 | 是（整个 `manual/`） |
| `manual/contest/setup-connection.json` | `contest_id` / `pta_cookie` / 可选 `print_server` | 连接对话框 | 是 |
| `manual/contest/setup-contest.json` | 比赛时间覆盖 + 定稿规则 | 比赛设置 tab | 是 |
| `manual/contest/setup-problems.json` | 题目颜色 + 顶层 `print_problem` | 题目颜色 tab | 是 |
| `manual/contest/setup-groups.json` | 自定义分组定义 | 队伍分组 tab | 是 |
| `manual/contest/setup-teams.json` | 队伍分组指派 + `seat` / `members` / `coaches` | 队伍分组 tab | 是 |
| `manual/balloons.json` | 气球发放状态（运行状态） | 运行时自动 | 是 |
| `manual/print_sent.json` | 已发送打印 id（运行状态） | 运行时自动 | 是 |

`manual/balloons.json.tmp`、`manual/print_sent.json.tmp` 是原子写临时文件（写入失败时可能残留）。

启动时自动创建 `manual/`、`manual/config/`、`manual/contest/`（`pta2icpc/paths.py:ensure_dirs`）。

## 路径不可配置

所有路径常量硬编码在 `pta2icpc/paths.py`，**不可配置**：`Settings` 没有 `manual_dir` 之类的字段，也没有 CLI / 环境变量覆盖。设置只从固定的 `manual/config/config.json` 读取；`manual/contest/*.json` 只用于比赛覆盖合并。

## 运行目录要求

路径都是**相对路径**，因此所有命令必须在 `pta2icpc/` 目录下运行。在别处运行会读写错误的 `manual/`。

## 密钥

- `manual/contest/setup-connection.json` 含 PTA cookie（`pta_cookie`）与可选打印服务器 URL，是唯一需要保密的运行文件；整个 `manual/` 已 gitignore。
- 不要把 cookie 复制进任何文档、日志或提交。
