import re

ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,36}$")


def sanitize_id(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)[:36]
    cleaned = cleaned.lstrip("-.")
    cleaned = cleaned.rstrip(".")
    return cleaned or "x"


def error_body(code: int, message: str) -> dict:
    return {"code": code, "message": message}


def make_api_info() -> dict:
    return {
        "version": "2026-01",
        "version_url": "https://ccs-specs.icpc.io/2026-01/contest_api",
        "provider": {"name": "pta2ccs"},
    }


ACCESS_ENDPOINTS = [
    {"type": "contests", "properties": [
        "id", "name", "formal_name", "start_time", "duration",
        "scoreboard_freeze_duration", "scoreboard_type", "penalty_time"]},
    {"type": "state", "properties": [
        "started", "ended", "frozen", "thawed", "finalized", "end_of_updates"]},
    {"type": "groups", "properties": ["id", "name"]},
    {"type": "organizations", "properties": ["id", "name"]},
    {"type": "teams", "properties": [
        "id", "name", "label", "group_id", "organization_id", "icpc_id"]},
    {"type": "problems", "properties": [
        "id", "label", "name", "ordinal", "color", "rgb"]},
    {"type": "languages", "properties": ["id", "name"]},
    {"type": "judgement-types", "properties": ["id", "name", "penalty", "solved"]},
    {"type": "submissions", "properties": [
        "id", "team_id", "problem_id", "language_id", "time", "contest_time"]},
    {"type": "judgements", "properties": [
        "id", "submission_id", "judgement_type_id", "current",
        "start_time", "start_contest_time", "end_time", "end_contest_time"]},
    {"type": "scoreboard", "properties": ["time", "contest_time", "state", "rows"]},
    {"type": "event-feed", "properties": ["type", "id", "data", "token"]},
]


def make_access() -> dict:
    return {"capabilities": [], "endpoints": [dict(e) for e in ACCESS_ENDPOINTS]}
