# Design: 打印流水线（扮演 DOMjudge）+ setup 座位/人员 + 打印题侧边选择

状态：用户已批准方案 A（2026-10-15）。本文档是实现子 agent 的执行规格。

## 背景与目标

- PTA 打印题提交 = 打印请求。PTA Patch **扮演 DOMjudge**：拉取打印题提交的源码，
  按 DOMjudge 打印脚本的 multipart 格式 POST 给 PRINT_SERVER（xcpctools/hydro，
  之后自己跑，是接收方）。脚本契约（发送端参考）：

  ```
  curl --form "file=@$filename" \
       --form-string "filename=.." --form-string "lang=.." \
       --form-string "tname=.." --form-string "team=.." \
       --form-string "location=.." [--form-string "group=.."] "$PRINT_SERVER"
  ```

- 打印题数据照旧**全部不进记分牌/事件流**（维持 drop 行为），新增"发送"步骤。
- 座位号 / 队员 / 教练按 **Contest API 2026-01 标准**落数据：
  - 座位号 = `team.label`（spec: "at WFs normally the team seat number"）；
    manual seat 优先，空则沿用 PTA 原 label（学号）。
  - 队员 = `persons` 对象 `role: "contestant"`；教练 = `role: "coach"`；
    均带 `team_ids: [tid]`。
- 打印题选择从问题表格列改为**侧边面板**，全局单选（含"不设打印题"）。

## 字段映射（POST 各字段取值）

| 脚本字段 | 取值 |
|---|---|
| `file` | 提交源码（PTA 详情 `program`），multipart 文件 |
| `filename` | `{题目label}.{扩展名}`（如 `3-1.py`）；扩展名取标准语言表第一项；未知语言 `.txt` |
| `lang` | **Contest API 标准语言 id**（见下表），未知枚举保持 sanitize 后原值 |
| `tname` | snapshot 中 `team.name`（缺失 → 原始 row team_name，再缺失 → team_id） |
| `team` | `row["team_id"]` |
| `location` | snapshot 中 `team.label`（即座位号，post-override）；缺失则省略该字段 |
| `group` | 队伍 group_ids → groups 名称，`,` 连接；空则**省略**（与脚本条件添加一致） |

### 语言 id 全局映射（`ptapatch/pta/langs.py` 新模块）

- 映射表（PTA 枚举 → 标准 id；合并变体，first-name-wins 作为该 id 的显示名）：
  `GCC/MODERN_GCC/CLANG → c`，`GXX/CLANGXX → cpp`，`JAVAC → java`，
  `PYTHON2 → python2`，`PYTHON3 → python3`，`RUBY → ruby`，`GO → go`，
  `GHC → haskell`，`FPC → pascal`；其余枚举 → `sanitize_id(枚举)` 原样。
- `STD_EXTENSIONS`：spec known-languages 的 extensions 表（cpp →
  `["cc","cpp","cxx","c++","C"]`，c → `["c"]`，java → `["java"]`，
  python3/python2 → `["py"]` 等，照 spec 附录抄）；未知 → `[]`。
- 暴露函数：`std_language_id(raw) -> str`、`extensions_for(std_id) -> list[str]`、
  `primary_extension(std_id) -> str`（`.txt` 兜底）。
- **全局生效**：translate 的 languages 端点（meta compilers + 提交语言兜底）、
  submissions.language_id 全部走 `std_language_id`；languages 对象补 `extensions`
  字段；printjob 用同一函数。display name 保持 `COMPILER_LANGUAGES` 中文/英文名
  （合并 id 时第一个见到的 name 优先）。

## 1. 打印流水线（方案 A）

### 新模块 `ptapatch/printjob.py`

- `build_job(*, source, filename, lang, tname, team, location, group) -> dict`
  纯函数 → `{"files": {"file": (filename, source_bytes, "text/plain")},
  "data": {filename, lang, tname, team, location?, group?}}`；
  `location`/`group` 为 None/空字符串时**不进 data**。source utf-8 编码。
- `async send_job(http_client, print_server, job, timeout=10.0) -> bool`
  httpx POST multipart（`files` + `data`），2xx → True，异常/非 2xx → False（打 warning）。

### poller 改造（`ptapatch/poller.py`）

每轮顺序（print_server 未配置时保持**现状完全不变**）：

1. `fetch_submissions()` 后按 `problem_index == print_label` 分区 → `print_rows`。
   打印题 problem 对象照旧 drop（:232-236 不动）。
2. 普通行走现有 `_fetch_details`；打印行**单独**取源码：
   `fetch_submission_detail(sid)`，每轮至多 5 个，顺序执行；不写入
   `_detail_status/_detail_cache`（独立 `self._print_source` 临时缓存，成功发送后清除）。
3. `translate(...)` 输入仍只含普通行 + drop 后的 problems → store 更新不变。
4. snapshot 就绪后，对每个**不在 sent 集合**的打印行：
   - 源码缺失/空、详情异常、POST 失败 → `self._print_attempts[sid] += 1`，
     超过 10 次 → error 日志 + 计入 sent（永久跳过，不再重试）；
   - 成功 → sid 进 sent 集合（立即落盘）+ 清源码缓存。
5. 打印行永远不进 translate/store（drop 位置等价迁移）。

### sent 集合持久化

- 文件 `print_sent.json`（与 config.json 同目录/同 cwd 约定，跟随
  `load_settings` 的路径基准），形状 `{"sent": ["sid", ...]}`。
- poller `__init__` 读取（文件不存在/坏 JSON → 空集并 warning）；每次成功后
  原子写回（tmp + replace 或直接覆盖，简单为准）。`.gitignore` 加 `print_sent.json`。

### 配置

- `manual/setup-connection.json` 增加可选键 `"print_server"`（URL 字符串，可为空串）。
- `config.load_connection`：透传该键（缺失/空 → `""` 或省略）；现有校验不得因
  新键失败。
- 连接对话框（`setup.py launch_connection_dialog`，:286-345）：加第三个输入框
  “打印服务器 URL（可选，PRINT_SERVER）”；预填 `prefill.get("print_server")`；
  非空且不以 `http://`/`https://` 开头 → `showwarning` 阻止提交；写入 conn。
- app 构建链把 `conn.get("print_server")` 传进 `Poller.__init__` 新参数
  `print_server: str | None = None`（找到 `_build_default_app(conn)` 处接线）。

## 2. setup GUI：打印题侧边面板

- 问题表格**删除** `("print","打印题",70)` 列（:402）与 `on_problem_click` 的
  `#5` 点击处理（:418-428）；`build_problem_rows` 的 `row["print"]` 布尔与
  `toggle_print_problem`（:123-134，单选互斥语义）**保留**，改由面板 radio 驱动。
- problems 标签页右侧新增 `LabelFrame("打印题")`：每个题目一个
  `ttk.Radiobutton`（text=label + name，value=题目 label），最上面一项
  “不设打印题”（value=""）；选中态与 `row["print"]` 双向同步，切换即 mark_dirty。
  布局从现有 refresh_problems/pack 结构出发（表格旁已有配色面板则叠放其下，
  以实际代码布局为准，保持原有 pack/grid 一致性）。
- refetch（:677-679）与换比赛提示（:736-741）的保留/清空逻辑不变（radio 面板
  保存/恢复照 `print_problem` 现有机制）。
- `save()`（:693-706）取 `row["print"]` 的 label → `print_problem`（不变）。

## 3. setup GUI：teams 侧栏 座位号/队员/教练

- `build_team_rows`（:137-160）：行增加 `seat`（str，预填自 saved）、
  `members`（list[str]）、`coaches`（list[str]）。
- teams 表格列不加座位号列（学号列照旧）；右侧分组面板旁新增
  `LabelFrame("座位号 / 成员")`：
  - 座位号：`ttk.Entry`；
  - 队员、教练：各一个列表（Treebox/Listbox）+ “添加/移除” 按钮
    （添加用 `simpledialog.askstring`）；
  - **仅单选一个队伍时启用**（多选/未选 → disable 并清空显示）。
- `load_manual_setup`（:170-203）**必须放行新键**（现在是白名单校验，漏改会把
  seat/members/coaches 静默丢掉——重点检查）；`write_manual_setup`（:204-224）
  写出：`{"teams": {tid: {"group_ids": [...], "seat"?: "A12",
  "members"?: [...], "coaches"?: [...]}}}`（空 seat/空列表省略键；group_ids
  现有“非空才写”规则不变）。
- `config.load_manual`（config.py:71-84 白名单校验）**同步放行新键**。
- `save()`（:693-706）组装 assignments 时带上 seat/members/coaches。

## 4. translate/store/app：persons 集合 + seat→label

- 新数据：`ContestData.persons: dict[str, dict]`（translate.py dataclass）。
- manual teams 应用处（translate.py:151-155 通用循环）特判 teams：
  - `entry.pop("members")/pop("coaches")`（不得泄漏进 team 对象——access 是
    静态属性表）；`seat` 非空 → `entry["label"] = str(seat)`（空串忽略）；
    其余键照旧。
  - members/coaches 收集后生成 persons：
    `id = sanitize_id(f"{tid}-m{i}")` / `f"{tid}-c{i}"`（sanitize_id 若截断需
    保证 ≤36 且合规），`name`，`role` = `contestant`/`coach`，`team_ids=[tid]`；
    仅当该 tid 最终存在于 `data.teams`（引用完整性）。
- `store._STATIC_FIELDS` 加 `("persons", "persons")`（自动获得 create/update/
  delete 事件，删除事件 data:null 沿用既有机制）。
- `app.COLLECTIONS` 加 `"persons": ("persons", None)`；单数表 `ITEM_SINGULAR`
  若有 persons 需补（看现有泛化方式）。
- `models.ACCESS_ENDPOINTS` 加
  `{"type": "persons", "properties": ["id","name","role","team_ids"]}`；
  languages 条目 properties 加 `"extensions"`。
- teams 不得输出 seat/members/coaches 键（access teams 属性表未列）。

## 5. 测试（新增/更新，全部 `.venv/Scripts/python -m pytest -q` 必须绿）

- `test_printjob.py`（新）：build_job 字段（location/group 缺省省略、filename、
  utf-8 bytes）；send_job 用 `httpx.MockTransport` 断言 multipart 收到
  file/filename/lang/tname/team/location/group 与2xx/非2xx/异常。
- `test_poller.py`：happy path（发送一次、成功后落盘、提交与 problem 仍 drop、
  源码不进 store）；无 print_server → 不取源码不发送（保持旧行为）；失败重试、
  10 次封顶跳过；sent 文件存在时（重启模拟）不重发；翻译后 snapshot 驱动字段
  （tname/location/group 取 snapshot 值）。
- `test_translate.py`：seat 覆盖 label（空 seat 不覆盖）；persons 构建
  （角色/id/team_ids）；未知 team 不产 person；team 对象无 seat/members/coaches。
- `test_api.py` / `test_event_feed.py`：persons 端点 GET、单条、access 含
  persons；delete 事件 data:null。
- `test_config.py`：load_connection 含/不含 print_server；load_manual_setup/
  config.load_manual 放行 seat/members/coaches（round-trip）。
- `test_langs.py`（新）：映射表（GXX→cpp、GCC→c、未知→原样）、extensions、
  primary_extension `.txt` 兜底。
- 既有断言 `language_id == "GXX"` 等改为标准 id；raw PTA fixture 不动。
- `tests/test_setup.py`（新，可选但鼓励）：`build_team_rows` seat/members 预填、
  `load_manual_setup`/`write_manual_setup` round-trip、`toggle_print_problem`
  单选互斥。

## 6. 文档

- README：打印流程章节（触发、字段映射、PRINT_SERVER 配置入口、sent 文件、
  座位/成员/教练 setup 说明、标准语言 id）。
- `.gitignore`：`print_sent.json`。

## 约束（必须遵守）

- 测试命令唯一：`.venv/Scripts/python -m pytest -q`（workdir = 仓库根）。
- **不碰** `config.json`、`manual/`（用户数据，gitignored）、`参考项目/`。
- **不 commit**（评审后由主 agent 提交）；不重启/不杀 8000 端口现有服务。
- 遵循现有代码风格（无新依赖；中文注释仅在既有注释风格需要处）。
- GUI 无法自动化测试：纯函数逻辑抽出来测（与现有 setup 纯函数测试思路一致），
  GUI 接线代码人工审查。
