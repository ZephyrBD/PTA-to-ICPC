# board-pusher

把 `pta2icpc` 服务（Contest API 2026-01 形状）的实时数据转换成 CUIT Board
（xcpcio 分支，`cuitboard.pages.dev`）的榜单格式，并周期性推送到 Cloudflare
Pages KV。纯 Python 标准库，无第三方依赖（Python 3.13+ 即可，无需 uv）。

## 数据流

```
pta2icpc /api/v4 (HTTP Basic: admin / api_password)
    │  拉取 contest / problems / submissions / judgements / teams
    ▼
转换（本脚本）
    │  config.json / team.json / run.json（xcpcio 榜单形状）
    ▼
PUT https://cuitboard.pages.dev/api/live/data/data/<比赛id>/*.json
    （Bearer <LIVE_PUSH_TOKEN>，写入 KV；前端 /data/<...> 先查 KV 再回落静态文件）
```

- `run.json`：提交记录。`contest_time`（`H:MM:SS[.mmm]`）解析成毫秒；
  judgement-type id 按 `STATUS_MAP` 映射为 xcpcio 状态，未匹配的兜底
  `WRONG_ANSWER`，拿不到判定的提交标 `PENDING`。
- `config.json`：由比赛元信息生成；封榜时长、罚时、奖项可用参数覆盖。
- `team.json`：Contest API teams → xcpcio Team 形状。

## Token

推送 token 是 Pages 项目环境变量 `LIVE_PUSH_TOKEN` 的值（由部署者通过
`openssl rand -hex 32` 生成后提供）。**本目录任何文件不得写入真实 token /
api_password**，一律走环境变量或命令行参数。

## 赛前清单（按顺序做一遍）

1. **拿 token**：向 board 部署者索取 `LIVE_PUSH_TOKEN`，导出环境变量：
   ```bash
   export PTA2ICPC_PASSWORD=<pta2icpc manual/config/config.json 里的 api_password>
   export CUITBOARD_TOKEN=<LIVE_PUSH_TOKEN>
   ```
2. **pta2icpc 就绪**：`uv sync` 后用 `uv run pta2icpc-setup` 配好 PTA
   连接、比赛设置、队伍名单；`uv run pta2icpc` 启动服务（比赛期间保持
   运行）。自检：
   ```bash
   curl -u admin:<api_password> http://127.0.0.1:8000/api/v4/contests
   ```
3. **生成登记/归档文件**（在 `board-pusher/` 目录下，比赛 id 全程一致）：
   ```bash
   python push_board.py --contest-id <比赛id> --teams-only --emit-index
   python push_board.py --contest-id <比赛id> --teams-only --dump-data
   ```
4. **合入 xcpcio 仓库并 push**（静态构建，与 KV 无关）：
   - `contest_list.json` 的条目 → `packages/apps/board/public/data/index/contest_list.json`
   - `config.json` / `team.json` → `packages/apps/board/public/data/icpc/<年份>/<比赛id>/`
5. **验证空榜**：Pages 构建完成后打开
   `https://cuitboard.pages.dev/icpc/<年份>/<比赛id>`，应看到全部队伍 0 题。
6. **清残留 run.json**（KV 里如有旧数据会让空榜不空）：
   ```bash
   python push_board.py --contest-id <比赛id> --once
   ```
   此时会推一轮全量（run.json 为空数组），config/team 也随首轮进 KV。
7. 比赛开始后进入[实时推送](#用法)循环（步骤 2）。

## 用法

```bash
export PTA2ICPC_PASSWORD=<pta2icpc manual/config/config.json 里的 api_password>
export CUITBOARD_TOKEN=<LIVE_PUSH_TOKEN>

# 0) 赛前一次性登记/归档（都要在 xcpcio 仓库手动合入 + git push 走静态构建）：
#    contest_list.json：孤立条目，手动合入 public/data/index/contest_list.json
python push_board.py --contest-id <比赛id> --teams-only --emit-index
#    config.json / team.json：归档进 public/data/icpc/<年份>/<比赛id>/
python push_board.py --contest-id <比赛id> --teams-only --dump-data

# 1) 赛前预推一轮 config/team + 空 run（清单第 6 步）
python push_board.py --contest-id <比赛id> --teams-only

# 2) 比赛期间实时推送（默认 10 秒一轮，站点要求 >=5 秒）
python push_board.py --contest-id <比赛id>

# 3) 比赛结束：最终数据归档进 xcpcio 仓库 public/data/ 并 push 之后，清理 KV
python push_board.py --contest-id <比赛id> --delete-only
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--contest-id` | 必填 | 榜单比赛 id，即推送 key `data/<比赛id>/*.json` |
| `--pta2icpc` | `http://127.0.0.1:8000` | pta2icpc 服务地址 |
| `--pta2icpc-contest` | `0` | pta2icpc 里的 contest id |
| `--frozen-time` | `3600` | 封榜时长（秒） |
| `--penalty` | `1200` | 罚时（毫秒，1200 = 20 分钟） |
| `--medal` | 上届正式队配置 | 奖项 JSON |
| `--interval` | `10` | 推送间隔（秒，最低 5） |
| `--once` | — | 只推一轮就退出（测试用） |
| `--emit-index` | — | 在当前目录生成孤立的 contest_list.json（登记用） |
| `--dump-data` | — | 在当前目录生成 config.json / team.json（归档用） |
| `--teams-only` | — | 只推 config/team |
| `--delete-only` | — | 删除 KV 里的三个文件（归档清理用） |

行为细节：config/team 视为赛前定稿的不变量，推送循环里只在首轮推一次，
之后每轮只推 run.json；401（token / 密码错误）直接退出；其它 HTTP /
网络错误打印后等下一轮重试。

## 本地调试

不碰线上 board 也可以验证转换结果：

```bash
# xcpcio 仓库根下起一个本地 Pages dev（随便设个 token）
npx wrangler pages dev dist --kv DATA_KV --binding LIVE_PUSH_TOKEN=devtoken
# 然后指向它推一轮
CUITBOARD_TOKEN=devtoken python push_board.py --contest-id test --once --board-base http://127.0.0.1:8788
```
