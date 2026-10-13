import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    contest_id: str
    pta_cookie: str
    manual_dir: Path = Path("manual")
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    config_poll_seconds: float = Field(default=60.0, gt=0)
    host: str = "127.0.0.1"
    port: int = 8000


def _fail(msg: str) -> None:
    print(f"config error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_settings(path: Path = Path("config.json")) -> Settings:
    if not path.exists():
        _fail(f"config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _fail(f"invalid JSON in {path}: {e}")
        raise
    try:
        return Settings.model_validate(raw)
    except Exception as e:
        _fail(f"invalid config in {path}: {e}")
        raise


_ID_SECTIONS = ("groups", "organizations", "teams", "problems", "languages")


def load_manual(manual_dir: Path) -> dict:
    merged: dict = {
        "contest": None,
        "groups": {},
        "organizations": {},
        "teams": {},
        "problems": {},
        "languages": {},
        "judgement_types_overrides": {},
    }
    if not manual_dir.exists():
        return merged
    for f in sorted(manual_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            _fail(f"invalid JSON in {f}: {e}")
        if not isinstance(data, dict):
            _fail(f"manual file {f} must contain a JSON object")
        contest = data.get("contest")
        if isinstance(contest, dict):
            if merged["contest"] is None:
                merged["contest"] = {}
            merged["contest"].update(contest)
        jt = data.get("judgement_types")
        if isinstance(jt, dict):
            overrides = jt.get("overrides", {})
            if not isinstance(overrides, dict):
                _fail(f"judgement_types.overrides in {f} must be an object")
            merged["judgement_types_overrides"].update(overrides)
        for key in _ID_SECTIONS:
            section = data.get(key)
            if section is None:
                continue
            if isinstance(section, dict):
                for k, v in section.items():
                    if not isinstance(v, dict):
                        _fail(f"{key}.{k} in {f} must be an object")
                    if not isinstance(merged[key].get(k), (dict, type(None))):
                        _fail(
                            f"{key}.{k} in {f} conflicts with a non-object value "
                            "set by an earlier manual file"
                        )
                    merged[key].setdefault(k, {}).update(v)
            elif isinstance(section, list):
                for item in section:
                    if not isinstance(item, dict) or "id" not in item:
                        _fail(f"{key} list items in {f} must be objects with an id")
                    iid = item["id"]
                    merged[key].setdefault(iid, {}).update(
                        {k: v for k, v in item.items() if k != "id"}
                    )
            else:
                _fail(f"{key} in {f} must be an object or a list")
    return merged
