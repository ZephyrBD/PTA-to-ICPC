# Design: /api/v4/ 的 HTTP Basic 认证（admin + setup 生成密码）

状态：用户已批准（2026-10-16，build）。实现子 agent 的执行规格。

## 背景与目标

- 服务的全部接口现在**无认证**（`docs/api.md`：「只读、本机无认证」）。CDS 的
  `cdsConfig.xml` 里 `<contest>` 的子元素 `<ccs url="…" user="…" password="…"/>` 本就是
  「登进 CCS 的账号/密码」（见 ICPC CDS 文档与 DOMjudge 对接 wiki），Consent API 2026-01
  规范也**要求必须支持 HTTP Basic Auth**。
- 目标：给 `/api/v4/*` 加 **HTTP Basic 认证**——用户名固定 `admin`，密码由 setup 随机生成、
  可在「比赛设置」tab 修改；使 CDS（以及 `type: domjudge` 的 xcpc-tools，支持
  `username`/`password`）能带凭据消费。
- 纯只读语义不变：认证只做**访问控制**，不引入任何账号/角色体系，`capabilities` 仍为 `[]`。

## 已定决策（用户裁定）

1. 密码存 `manual/config/config.json` 的 `api_password`（服务级设置，与 `host`/`port` 同源），
   凭证**不进**比赛数据 / `translate` 通路。
2. **必须有密码**：未配置则服务**拒绝启动**；「比赛设置」tab 保存空密码报错。认证不可关闭。
3. 只保护 `/api/v4/*`；`/healthz` 与 `/contests*` 重定向**保持开放**（本机自检）。
4. GUI：密码**明文** Entry + 「复制」+「随机重置」；用户名 `admin` 只读显示。
5. 版本 `0.3.0 → 0.4.0`；功能拆 **2 个提交**（代码+设计文档 / 文档）。

## 认证模型

- 用户名常量 `API_USERNAME = "admin"`（`pta2icpc/auth.py`），**不落文件、不可配置**。
- 密码源：`Settings.api_password`（`manual/config/config.json`）。
- `create_app(store, poller, contest_id, tz=…, balloon_state=…, api_password=None)`：
  用 **app 级依赖** `FastAPI(..., dependencies=[Depends(require_api_auth)])` 统一拦截，
  依赖内只对 `request.url.path == "/api/v4"` 或 `startswith("/api/v4/")` 生效。
  - **选依赖而非中间件**：不包裹响应体，不干扰 `event-feed` 的 NDJSON 流式响应。
- 校验：解析 `Authorization: Basic base64(user:pass)`；`hmac.compare_digest` 比较 user 与 pass
  （两者都常数时间比较）。失败 → `401` + `WWW-Authenticate: Basic realm="pta2icpc"` +
  规范错误体 `{"code": 401, "message": "authentication required"}`（经既有 exception handler）。
- `api_password=None` 或 `""` 传给 `create_app` → **完全开放**（现有测试与 `TestClient` 不受影响；
  `create_app` 本身不承担「必须有密码」的强制）。

## 「必须有密码」的落地

- `Settings.api_password: str = ""`：模型层**可选**（默认空），使 `setup.py` 首次运行可先
  `load_settings()` 而不死锁；字段注释说明服务入口会强制。
- **强制点在服务入口** `__main__.main()`：取到 `conn` 之后，
  `if not s.api_password:` → 向 stderr 打印
  `api_password not configured — run 'python -m pta2icpc.setup' first
  (manual/config/config.json)` + `raise SystemExit(1)`。
- `setup.py` 自动保证自洽：`main()` 取到 `conn` 之后，若 `load_settings()` 的密码为空 →
  **立即 `generate_api_password()` 并 `write_api_password()`**（不依赖用户点保存）再进 GUI；
  ⇒ 只要跑过 setup（写过 `setup-connection.json`）就一定有一个密码，服务可启动。
- 「比赛设置」tab 的 `save()`：密码为空 → `messagebox.showerror` 拒绝；非空 → `write_api_password()`。
- 文档不变量改写：`manual/config/config.json`「缺失 = 除 `api_password` 外全默认；缺密码服务拒绝启动」。

## 新模块 `pta2icpc/auth.py`（纯函数，全部可单测）

- `API_USERNAME = "admin"`
- `generate_api_password() -> str`：8 位，字符集 = 英文字母 + 数字 **剔除易混 `0O1lI`**
  （≈ 47 bit；无 `&`/引号 ⇒ 可直接贴进 `cdsConfig.xml`）。
- `check_basic_auth(header: str | None, password: str) -> bool`：解析并常数时间比较；任何异常 → False。
- `require_api_auth(password)` 依赖工厂：`password` 为空 → 直接放行；非 `/api/v4` 路径 → 放行；
  否则校验失败即 `raise HTTPException(401, error_body(401, "authentication required"),
  headers={"WWW-Authenticate": 'Basic realm="pta2icpc"'})`。

## 其它代码改动

- `config.py`：`Settings` 增 `api_password: str = ""`（含注释）。
- `app.py`：`create_app` 增参数 `api_password`；`_build_default_app` 透传 `settings.api_password`。
- `__main__.py`：见上「必须有密码」。
- `setup.py`：
  - 纯函数 `merge_api_password(existing_text: str | None, password: str) -> dict`：解析既有
    JSON（`None`/空 → `{}`），仅在顶层 set `api_password`，其余键原样保留。
  - `write_api_password(password: str, *, config_file=paths.CONFIG_FILE) -> None`：读既有文本 →
    `merge_api_password` → 原子式写出（UTF-8, indent 2）；既有文件 JSON 非法 → 抛
    `ValueError`（GUI 弹错、**不覆盖**）。
  - 「比赛设置」tab 新增「接口认证」区：`admin` 只读 Label + 密码明文 Entry（宽度同其它字段）+
    「随机重置」按钮（`generate_api_password()` 填回 + `mark_dirty()`）+「复制」按钮
    （`root.clipboard_clear/append`）+ 灰字提示「供 CDS / xcpc-tools 使用；保存后重启服务生效」。
  - `save()`：空密码拒绝；否则 `write_api_password(api_pass_var.get().strip())`。
  - `main()`：见上。

## CDS / xcpc-tools 接入（文档片段）

```xml
<!-- CDS cdsConfig.xml -->
<ccs url="http://127.0.0.1:8000/api/v4/contests/<contest_id>"
     user="admin" password="<setup 生成的密码>"/>
```

```yaml
# xcpc-tools config.server.yaml（type: domjudge）
server: http://127.0.0.1:8000
username: admin
password: <setup 生成的密码>
```

## 文档改动

- `README.md`：特性「Windows 单机可跑」的「无认证」→「可选 HTTP Basic 认证（`admin` + 随机密码）」；
  快速开始的 CDS / xcpc-tools 片段补凭据；静态测试数徽章更新。
- `docs/api.md`：第 3 行「本机无认证」改写；新增「认证」小节（401 形状、`WWW-Authenticate`、
  只保护 `/api/v4/*`；Basic over HTTP 为明文，仅限本机/可信网络，规范要求 HTTPS）。
- `docs/configuration.md`：字段表 + `api_password` 行；不变量改写；「密钥」段补密码。
- `docs/data-and-setup.md`：比赛设置 tab 说明补「接口认证」。
- `docs/troubleshooting.md`：401 / 认证失败 / 忘记密码（重跑 setup）。
- `pta2icpc/AGENTS.md`：硬约束补「`api_password` 是密钥，不得写文档/日志/提交」；
  关键契约补「无 `api_password` 服务拒绝启动」。

## 测试

- 新 `tests/test_auth.py`：`generate_api_password` 长度/字符集/无易混字符；`check_basic_auth`
  正/误/坏 base64/缺头/未知用户名；`create_app(api_password="p")` 经 `TestClient` —
  401 体 `{"code":401,…}` + `WWW-Authenticate`、正确凭据 200、`/healthz` 免认证、非 `/api/v4` 免认证。
- `tests/test_config.py`：`api_password` 默认 `""`、从文件解析。
- `tests/test_setup.py`：`generate_api_password` 形态；`merge_api_password` 保留既有键 / 空文本 /
  非法 JSON 抛错；`write_api_password` 落盘 + 不破坏其它键。
- 现有测试**零改动**（`create_app` 默认 `api_password=None` 仍开放）。
- 预计 466 → ~485 passed。

## 风险 / 注意

- 认证启用后，**同一端口的 xcpc-tools 必须补 `username`/`password`**，否则 401 —— 文档明示。
- Basic over HTTP 是明文；本机（`127.0.0.1`）可接受，跨网部署须 HTTPS（文档 caveat）。
- 不得把 `api_password` 写入任何日志 / 文档 / 提交；`manual/` 已 gitignore。
- 子代理不得提交/推送、不得跑 setup GUI、不得启停服务；测试仅 `tmp_path` + `monkeypatch.chdir`，
  绝不触碰打印路径。
