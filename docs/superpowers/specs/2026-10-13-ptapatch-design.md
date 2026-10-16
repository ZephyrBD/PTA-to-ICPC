# PTA Patch — PTA 内部接口转 ICPC Contest API 设计文档

日期：2026-10-13（设计已经用户逐节确认）
Spec 依据：ICPC CCS **Contest API 2026-01**（https://ccs-specs.icpc.io/2026-01/ ），速记见 `docs/contest-api.md`

## 目标

做一个新服务 `ptapatch/`：逆向 pintia.cn Web UI 的内部 JSON 接口取数，在本机暴露一个**完整只读子集**的 Contest API 2026-01 服务器，供气球打印工具等客户端消费。替代参考项目（`参考项目/`）的 Selenium 爬 HTML 方案；参考项目保持不动，本期不改气球打印客户端。

## 已确认的约束

- 数据来源：PTA Web UI 内部 JSON 接口（带 PTASession cookie 的纯 HTTP），不用 Selenium
- 输出范围：只读 Contest API 完整子集 —— `api`、`access`、`contests`、`state`、`groups`、`organizations`、`teams`、`problems`、`languages`、`judgement-types`、`submissions`、`judgements`、`scoreboard`、`event-feed`；不做任何 POST/PUT/PATCH/DELETE
- 部署：单场比赛，绑 `127.0.0.1`，无认证；PTA cookie 存在服务端配置
- 技术栈：Python 3.13 + FastAPI + uvicorn + httpx + pydantic；测试 pytest
- 取数：后台轮询 + 内存缓存
- 实测条件：用户可提供有效 PTASession cookie 用于逆向探测

## 架构（方案 A：分层翻译器 + 内存权威状态）

```
PTA 内部接口 ──httpx──▶ pta/client.py ──原始JSON──▶ translate.py ◀── manual/*.json
                                                              │
                                          ┌───────────────────┤
                                          ▼                   ▼
                                    store.py (权威状态     scoreboard.py
                                     + 事件日志+token)        (现算榜)
                                          ▲
                     poller.py (asyncio 循环: 拉取→diff→写 store→发事件)
                                          │
                              FastAPI app.py / event_feed.py
                                          │
                                    消费者 (GET)
```

- **pta/client.py**：唯一知道 PTA 接口 URL/参数/分页的模块；httpx 异步客户端；401/403 视为 cookie 失效
- **translate.py**：PTA 原始快照 → 规范化 Contest API 对象；合并 `manual/` 补充数据
- **store.py**：内存权威状态（各对象表）+ 事件日志（`{"type","id","data","token"}`，token 单调递增整数，保留最近 ~10000 条）；GET 无锁读
- **scoreboard.py**：pass-fail 榜现算（唯一解、罚时、pending 计数）
- **poller.py**：asyncio 后台任务，唯一写 store 的地方，负责产生事件
- **app.py / event_feed.py**：FastAPI 只读路由 + NDJSON 流

## 目录结构

```
ptapatch/
  pyproject.toml              # fastapi, uvicorn, httpx, pydantic; dev: pytest
  config.example.json
  manual.example/             # 手动补充 JSON 示例（实际 manual/ 不入库）
  ptapatch/
    config.py                 # 加载/校验配置，fail fast
    models.py                 # Contest API 对象的 Pydantic 模型
    pta/client.py
    pta/verdicts.py           # PTA 判决字符串 → AC/WA/TLE/... 映射表
    translate.py
    scoreboard.py
    store.py
    poller.py
    app.py
    event_feed.py
  tests/
    fixtures/                 # 逆向阶段录下的 PTA 真实 JSON + 手写边界用例
    test_translate.py  test_scoreboard.py  test_api.py  test_event_feed.py
  docs/pta-api-findings.md    # 逆向探测结论（实现 Phase 1 产出）
```

## 配置

`config.json`（gitignore）+ `config.example.json`（入库）：

```json
{
  "contest_id": "<PTA problem set id>",
  "pta_cookie": "<PTASession 值>",
  "manual_dir": "manual",
  "poll_interval_seconds": 5,
  "config_poll_seconds": 60,
  "host": "127.0.0.1",
  "port": 8000
}
```

`start_time`/`duration`/`penalty_time`/`contest_name` 等比赛元数据放 `manual/` 补充 JSON（见下），不硬编码进配置。

## 手动补充数据（manual/）

- 配置 `manual_dir`（默认 `manual/`）；启动时加载目录下所有 `*.json`，按文件名顺序合并，后加载者优先（按对象 `id` 深合并；`contest` 单例后者覆盖前者）
- PTA 拿不到的数据从这里人工提供：`contest`（start_time、duration、penalty_time、name、formal_name…）、`groups`、`organizations`、`teams`（修正/补全）、`problems`（调色板覆盖）、`languages`、`judgement_types.overrides`
- 三级来源优先级：**PTA 实时数据 > manual/ > 内置默认**
- 队伍、题目按 `id` 合并（PTA 反推的 `D22` 与 manual 中 `D22` 合并，manual 字段可覆盖/补充）
- **实时数据（submissions/judgements）永远不接受手工编辑**
- 不预设需要哪些文件，缺什么补什么；example 文件先给 `00-contest.json`、`10-groups.json`、`20-organizations.json`

## 数据映射

| Contest API 对象 | PTA 能拿 | 兜底 |
|---|---|---|
| `contests` | — | manual 提供 start_time/duration/penalty_time/name 等；`scoreboard_type: "pass-fail"`；id = 配置 contest_id |
| `state` | — | 墙钟 + manual 配置推导（started/ended/frozen） |
| `problems` | id/标题/序号 | `label` 按序号 A、B、C…；color/rgb 内置调色板，可被 manual 覆盖 |
| `teams` | 反推 `id`=`label`=`{赛场字母}{座位号}`（如 `D22`）、`name`=队名、`group_id`=赛场字母 | manual 补全 icpc_id、organization_id、改名 |
| `groups` | 赛场字母集合 | manual 给字母→名称 |
| `organizations` | — | manual 提供；没有则从 access 隐藏 |
| `languages` | 有则映射 | 占位 `unknown` |
| `judgement-types` | 文案→AC/WA/TLE/CE/PE/RTE/MLE/OLE…（`verdicts.py`，可被 manual 覆盖） | 标准表含 `solved`/`penalty` |
| `submissions` | PTA 提交 id 原样作 `id`；PTA 时间戳作 `time`；`contest_time` 由 start_time 算 | `files` 不暴露（拿不到源码） |
| `judgements` | 每提交一条；“正在评测”时 `judgement_type_id` 为 null，判完更新 | `end_time` 尽力而为，拿不到用提交时间近似（记录在 findings） |

不支持的端点（`persons`/`accounts`/`runs`/`awards`/`clarifications`/`commentary`/`webhooks`）不进 `access`——access 即真实能力清单。`capabilities: []`（纯只读）。

## poller 与事件

- 每 `poll_interval_seconds`：分页拉全量提交列表 → 与 store diff → 新提交写 store 并产生 `submissions`(create) + `judgements`(create, verdict null) 事件；判决翻转产生 `judgements`(update) 事件
- 每 `config_poll_seconds`（默认 60s）：拉题目/队伍等低频数据，变了才写 store 并发事件
- scoreboard 是聚合数据，**不进事件流**（规范如此），GET 现算
- 事件只覆盖 submissions/judgements/配置类对象；PTA 无删除概念，不模拟删除
- 拉取失败：指数退避（1s 起，封顶 60s），旧状态继续服务；401/403 → cookie 失效，退避 + 日志警告 + `/healthz` 显示 `cookie_ok: false`

## API 路由

- `GET /` → api info（`version: "2026-01"`）
- `GET /contests/{cid}/access` → 静态生成（实际暴露的端点+属性）
- `GET /contests/{cid}/{collection}`、`/{collection}/{id}` → 读内存；过滤：ID 类属性可作 query 参数；404 用规范错误体 `{"code","message"}`
- `GET /contests/{cid}/scoreboard` → 现算，支持 `?group_id=`
- `GET /contests/{cid}/event-feed?since_token=` → NDJSON：先回放窗口内事件，再推新事件；60s 无事件发空行保活；token 窗口外/非法 → 400
- `GET /healthz` → 非规范便捷端点（ready、上次成功拉取时间、cookie_ok）

## 错误处理

- 单条提交解析失败 → 跳过 + warning，不拖垮整轮
- 错误响应统一 `{"code": <int>, "message": "<str>"}`，未知路径同格式 404
- 启动时配置/manual 非法 → fail fast 退出，指明出错键
- 首轮拉取完成前：collection 返回 `[]`，`/healthz` `ready: false`

## 测试与验收

- `test_translate.py`：fixture 驱动——正常提交、正在评测→翻转、队名反推、脏行跳过、manual 合并覆盖
- `test_scoreboard.py`：唯一解、罚时累加、错误提交罚时规则、pending 计数、`?group_id=`
- `test_api.py`：TestClient 全端点，字段与 access 一致、404 错误体
- `test_event_feed.py`：事件顺序与 token 递增、since_token 回放、窗口外 400、心跳空行
- 全部 PTA 交互走 fixture，不发真网络请求
- 验收：pytest 全绿；真 cookie 实测 submissions/event-feed；可选 check-api.sh 校验

## 实施顺序

1. 项目脚手架 + 配置加载 + manual 合并
2. **Phase 1 逆向探测**（需用户 cookie）：抓 PTA 内部接口，产出 `docs/pta-api-findings.md` + fixtures，定稿 `verdicts.py` 映射
3. models + translate + scoreboard（fixture 驱动开发）
4. store + poller
5. app.py 全路由 + event_feed
6. 测试补全 + 真 cookie 联调

## 明确不做（本期）

POST/写端点、多场比赛、认证/角色、冻结的角色化隐藏、气球打印客户端、event-feed 跨重启恢复（重启后从头重放，规范允许）、`persons`/`accounts`/`runs`/`awards`/`clarifications`/`commentary`。
