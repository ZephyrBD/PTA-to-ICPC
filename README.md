# pta2ccs

把 PTA (pintia.cn) Web UI 内部接口转成 ICPC Contest API 2026-01 只读服务。

用法：
1. `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"`
2. 复制 `config.example.json` 为 `config.json` 并填写；复制 `manual.example/` 为 `manual/` 并按需编辑
3. 在 `pta2ccs/` 目录下 `.venv/Scripts/python -m pta2ccs`（从 `config.json` 构建 app 并启动 uvicorn；没有模块级 `app`，不能用 `uvicorn pta2ccs.app:app`）
4. `GET http://127.0.0.1:8000/` → api info；`GET /healthz` → 就绪状态

设计文档：`../docs/superpowers/specs/2026-10-13-pta2ccs-design.md`
