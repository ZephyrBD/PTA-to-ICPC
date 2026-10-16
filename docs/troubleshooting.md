# 常见问题

[← 返回 README](../README.md)

## 日志排查（`manual/pta2icpc.log`）

**用途**：服务与 setup 入口的运行日志（含 uvicorn 启动与访问日志）统一写到 `manual/pta2icpc.log`，同时打到 stderr。排查问题时先看这个文件。（`pta2icpc-diag` 是 stdlib-only 的只读诊断，报表走 stdout，**不**写日志文件。）

**格式**：`dd hh:mm:ss 短名 [X] message`（例 `16 15:12:33    poller [I] restored 42 submissions from cache`）。控制台按级别给 `[X]` 标签上色（`I` 青 / `W` 黄 / `E` 红 / `C` 亮红加粗 / `D` 灰，时间戳灰），**日志文件为纯文本、无颜色**（同一布局）。短名见 `docs/configuration.md`（`pta2icpc.poller`→`poller`、`uvicorn.*`→`server`/`access`）。

**轮转**：单文件 5 MB、保留 3 份备份（`pta2icpc.log.1` …）；级别固定 `INFO`（无 CLI / 环境变量开关；`NO_COLOR` / `PTA2ICPC_NO_COLOR` 仅关闭控制台颜色）。日志文件绝不写入 PTA cookie 或 `api_password`。

## cookie 过期

**现象**：启动 / 轮询时 stderr 出现 `connection not configured — run 'python -m pta2icpc.setup' first`；或运行中 `/healthz` 的 `cookie_ok` 变 `false`、日志出现 `PTA auth failed (cookie expired?)`；setup GUI 弹「**cookie 失效**」。

**原因**：PTA 的 `PTASession` cookie 有时效，且只对该场比赛有效。

**处理**：重新获取 cookie，运行 `uv run pta2icpc-setup`，在「比赛连接」对话框更新。确认 cookie 覆盖整场比赛。

## `/api/v4/` 返回 401 / 认证失败

**现象**：CDS / xcpc-tools 访问 `/api/v4/…` 返回 `401`，响应头 `WWW-Authenticate: Basic realm="pta2icpc"`、错误体 `{"code": 401, "message": "authentication required"}`。

**原因**：`/api/v4/*` 需 HTTP Basic 认证（用户名固定 `admin`，密码为 `manual/config/config.json` 的 `api_password`）；消费者没带凭据或凭据不对。

**处理**：给消费者补凭据——CDS 在 `ccsConfig.xml` 的 `<ccs user="admin" password="…"/>`，xcpc-tools 用 `username` / `password`（`type: domjudge`）。密码可在 setup「比赛设置」tab 的「接口认证」区查看 / 复制。

## 启动报 `api_password not configured`

**现象**：启动时 stderr 出现 `api_password not configured — run 'python -m pta2icpc.setup' first (manual/config/config.json)` 并以退出码 1 退出。

**原因**：`manual/config/config.json` 里没有 `api_password`（认证**不可关闭**，必须有密码）。

**处理**：运行 `uv run pta2icpc-setup`，在「比赛设置」tab 的「接口认证」区保存密码（首次运行 setup 会自动生成一个密码）。

## 忘记接口密码

**现象**：不知道 `/api/v4/` 的 Basic 密码。

**处理**：重跑 `uv run pta2icpc-setup`，在「比赛设置」tab 点「随机重置」并保存，然后**重启服务**；或直接查看 `manual/config/config.json` 的 `api_password`。启动服务时控制台也会打印 `Server View User Info: admin / <密码>`。消费者侧同步更新凭据。

## PTA 429 限流

**现象**：日志出现 `poll rate limited; sleeping …` 或 `detail fetch paused: rate limited, retry_after=…`；`/healthz` 的 `rate_limited_until` 非空。

**原因**：短时间内请求过多（例如重启瞬间大量详情拉取）触发 PTA 限流。

**处理**：程序自动冷却，无需干预。可降低 `pta_requests_per_second` 或 `detail_fetch_per_round`（见 `docs/configuration.md`），或临时设 `fetch_details: false`。冷却期间不发起任何 PTA 请求，也不计为失败。

## 必须在 `pta2icpc/` 目录下运行

**现象**：读不到配置 / 连接，或写出了意外的 `manual/`。

**原因**：所有路径（`manual/`、`pta2icpc/paths.py` 的常量）都是**相对路径**。

**处理**：始终在 `pta2icpc/` 目录下运行命令（`uv run pta2icpc`、`uv run pta2icpc-setup`、`uv run python -B -m pytest -q -p no:cacheprovider`）。

## 不能用 `uvicorn pta2icpc.app:app`

**现象**：`uvicorn pta2icpc.app:app` 报找不到 `app` 或启动后行为异常。

**原因**：模块级没有 `app` 对象，只有工厂 `create_app`；启动时还要读连接文件、构建 poller。

**处理**：用 `uv run pta2icpc`。

## `manual/balloons.json` 损坏 / 清空

**现象**：日志出现 `cannot read … balloon state starts empty` / `… has the wrong shape …`，或清空文件后气球从空状态重新编号。

**原因**：`BalloonState` 刻意容错——缺失 / 损坏 / 形状不对都从空状态开始（只 warning，不抛异常）。

**处理**：一般无需处理；若确实需要干净重来，删掉 `manual/balloons.json` 即可。注意 `balloonid` 一旦重置，消费者（xcpc-tools）里旧的 `balloonid` 行会成为孤儿——见 `docs/api.md`。

## 重复打气球 / 打印重复

**现象**：同一提交被打印两次。

**原因**：去重只靠 `manual/print_sent.json`（已发送 id 集合）。

**处理**：正常重启**不会**重发（文件持久化）。只有当你**删除** `print_sent.json`、或它损坏导致以空集启动时才会重发——除非确实要重打，别删它。

## 「xcpc-tools 面板 Balloon New 一直涨」

**现象**：xcpc-tools 面板上的 `Balloon New` 指标只增不减。

**原因**：这是**对方**的 Prometheus 计数器，每次同步累加整批响应条数；`Sent` / `Done` 只在志愿者客户端领取 / 完成时才增加。**不是 PTA to ICPC 的 bug。**

**处理**：忽略，或按 xcpc-tools 自己的文档理解其语义。
