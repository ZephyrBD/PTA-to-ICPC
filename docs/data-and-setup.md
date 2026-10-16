# 数据来源、覆盖与 setup

[← 返回 README](../README.md)

## 数据来源

以下数据全部按 `manual/contest/setup-connection.json` 里的 `contest_id` 从 PTA API 自动获取（带 `pta_cookie`）：

- 比赛名、开赛 / 结束时间、比赛时长（`GET /api/problem-sets/{id}`）。
- 封榜时长（`hideScoreboard` + `hidingTime`，按「**开赛后** X 分钟」换算）。**始终基于最终生效的 duration 计算**：显式 duration 优先，无显式时用 PTA duration，两者都缺时用内置默认 `5:00:00`。因此 manual 覆盖 duration 后封榜时刻仍正确。
- 全部可用编译器 → `/languages`（38 种，随 PTA 配置变化）。
- 题目列表、全量选手花名册 → `/teams`；PTA 用户组只映射到 `/organizations`（学校），**不**生成 `/groups`。

## 覆盖优先级

**manual 显式配置 > PTA API > 内置默认**。`manual/contest/*.json` 仅用于**覆盖**。改完需要**重启服务**才生效（manual 在启动时加载一次）。典型用途：改比赛罚时、把用户组 / 队伍名改成中文显示名。

`config.load_manual` 只 glob 并合并 `manual/contest/*.json`；`manual/config/config.json` **不参与** manual 覆盖合并。

## setup GUI（`python -m pta2ccs.setup`）

一次性 tkinter GUI，可重复运行；写出 `manual/contest/setup-*.json` 后退出。GUI 本身不自动化测试（tkinter 逻辑抽成纯函数单独测试）。

### 比赛连接对话框

- 首次运行弹出「**比赛连接**」对话框，含三个输入框：`比赛 ID`、`PTA Cookie`、`打印服务器 URL（可选，PRINT_SERVER）`。
- 「连接并拉取」用一次网络往返校验 cookie 与 `contest_id` 后写入 `setup-connection.json`；cookie 失效 → 弹「cookie 失效」；打印服务器 URL 非空时必须 `http://` 或 `https://` 开头。
- 比赛中要改连接可点顶栏「**🔗 比赛连接…**」。换比赛 ID 时会询问是否清空分组指派与打印题选择（颜色和分组定义保留）。

### 三个 tab 写出的概念

**题目颜色**（`setup-problems.json`）：按题号（PTA label，如 `1-1`）设置 `color` / `rgb`，覆盖内置 `PALETTE` 默认色。

**自定义分组**（`setup-groups.json` + `setup-teams.json` 的队伍 `group_ids`）：打星 / 正式 / 女队 …，与 PTA 用户组（学校）完全独立，由操作者定义；`group_ids` 为空数组表示不属于任何自定义分组。可用于 `GET /api/v4/contests/{id}/scoreboard?group_id=g1` 只看某分组。

**打印题**（`setup-problems.json` 顶层 `print_problem`）：题目侧栏（快捷色板下方的「打印题」单选面板）标一题；该题的**全部**数据（题目、submissions、judgements、runs、sources）都不进 CDS。未配置打印服务器时其提交不做详情拉取；配置了 `PRINT_SERVER` 则走打印流水线（见下）。

**座位号 / 队员 / 教练**（`setup-teams.json` 的 `teams[id]` 上的 `seat` / `members` / `coaches`）：在**队伍分组** tab 中双击队伍行、回车或选中后点「**编辑队伍…**」打开对话框编辑（表格有**座位号**列）。`seat` 覆盖队伍 `label`；`members` / `coaches` 不出现在队伍对象上，而是生成 `/persons`（`role` = `contestant` / `coach`，id 形如 `D22-m0` / `D22-c0`）。空值省略不写。

### 比赛设置 tab

覆盖开赛时间 / 时长 / 封榜时长 / 定稿规则，写入 `setup-contest.json` 的 `contest` 段：

- 三个时间输入框（开始时间 / 时长 / 封榜时长）打开时由**刚拉取的 PTA 值**预填（已有 manual 覆盖值优先）；封榜时长按**最终生效**的 duration 计算，故「预填后原样保存」不改变行为。留空 = 自动；「**恢复自动（留空）**」按钮清空三框。
- **重新拉取**（顶栏「重新拉取数据」）会实时读取三个输入框当前内容，只刷新「未被编辑」的框；你手动改过的值不会被覆盖。
- 定稿规则为三选一单选：`自动（比赛结束后 5 分钟）` / `指定时间` / `不定稿`；「指定时间」时 ISO 输入框才可编辑。规则语义见 `docs/api.md`。
- 保存后提示「比赛设置改动需重启服务器后生效」。

## 打印流水线（`PRINT_SERVER`）

`setup-connection.json` 里的可选 `print_server` 开启后，每轮轮询把**打印题**的提交单独分流（扮演 DOMjudge）：

1. 从提交列表分区出来（**不进** translate / store / scoreboard），逐条拉取源码：每轮最多 5 条，**顺序**执行；429 触发全局冷却并停止本轮拉取。
2. `store` 应用新快照后，按 DOMjudge 打印脚本的 multipart 形状 `POST {print_server}`：`file`（源码，UTF-8）、`filename`（`{题目label}.{扩展名}`）、`lang`（Contest API 标准语言 id）、`tname`（snapshot 队伍名 → 提交行队名 → team_id）、`team`（team_id）、`location`（`team.label`，即座位号，缺失则省略）、`group`（分组名以 `,` 连接，空则省略）。
3. 已发送 id 记在 `manual/print_sent.json`（形状 `{"sent": [sid, ...]}`），成功即持久化；**重启不重发**；缺失 / 损坏文件从空集开始（warning）。
4. 失败策略：源码为空 / 拉取失败 / POST 失败各计一次尝试，累计超过 **10** 次**永久跳过**并打 error 日志。因每轮预算或 429 冷却而暂时没轮到的不计尝试。

未配置 `print_server` 时保持旧行为：打印题数据直接丢弃、不写任何状态。
