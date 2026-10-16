# 常见问题

[← 返回 README](../README.md)

## cookie 过期

**现象**：启动 / 轮询时 stderr 出现 `connection not configured — run 'python -m pta2ccs.setup' first`；或运行中 `/healthz` 的 `cookie_ok` 变 `false`、日志出现 `PTA auth failed (cookie expired?)`；setup GUI 弹「**cookie 失效**」。

**原因**：PTA 的 `PTASession` cookie 有时效，且只对该场比赛有效。

**处理**：重新获取 cookie，运行 `.venv/Scripts/python -m pta2ccs.setup`，在「比赛连接」对话框更新。确认 cookie 覆盖整场比赛。

## PTA 429 限流

**现象**：日志出现 `poll rate limited; sleeping …` 或 `detail fetch paused: rate limited, retry_after=…`；`/healthz` 的 `rate_limited_until` 非空。

**原因**：短时间内请求过多（例如重启瞬间大量详情拉取）触发 PTA 限流。

**处理**：程序自动冷却，无需干预。可降低 `pta_requests_per_second` 或 `detail_fetch_per_round`（见 `docs/configuration.md`），或临时设 `fetch_details: false`。冷却期间不发起任何 PTA 请求，也不计为失败。

## 必须在 `pta2ccs/` 目录下运行

**现象**：读不到配置 / 连接，或写出了意外的 `manual/`。

**原因**：所有路径（`manual/`、`pta2ccs/paths.py` 的常量）都是**相对路径**。

**处理**：始终在 `pta2ccs/` 目录下运行命令（`.venv/Scripts/python -m pta2ccs`、`-m pta2ccs.setup`、pytest）。

## 不能用 `uvicorn pta2ccs.app:app`

**现象**：`uvicorn pta2ccs.app:app` 报找不到 `app` 或启动后行为异常。

**原因**：模块级没有 `app` 对象，只有工厂 `create_app`；启动时还要读连接文件、构建 poller。

**处理**：用 `.venv/Scripts/python -m pta2ccs`。

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

**原因**：这是**对方**的 Prometheus 计数器，每次同步累加整批响应条数；`Sent` / `Done` 只在志愿者客户端领取 / 完成时才增加。**不是 pta2ccs 的 bug。**

**处理**：忽略，或按 xcpc-tools 自己的文档理解其语义。
