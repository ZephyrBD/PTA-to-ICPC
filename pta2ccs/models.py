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
    {"type": "contest", "properties": [
        "id", "name", "formal_name", "start_time", "duration",
        "scoreboard_freeze_duration", "scoreboard_type", "penalty_time"]},
    {"type": "state", "properties": [
        "started", "ended", "frozen", "thawed", "finalized", "end_of_updates"]},
    {"type": "groups", "properties": ["id", "name"]},
    {"type": "organizations", "properties": [
        "id", "name", "externalid", "formal_name", "shortname", "country",
        "country_flag", "logo", "affiliation_id", "icpc_id"]},
    {"type": "teams", "properties": [
        "id", "name", "label", "group_ids", "organization_id", "icpc_id",
        "externalid", "display_name", "affiliation_id", "public_description",
        "hidden", "location"]},
    {"type": "persons", "properties": ["id", "name", "role", "team_ids"]},
    {"type": "problems", "properties": [
        "id", "label", "name", "ordinal", "color", "rgb",
        "time_limit", "memory_limit", "code_limit", "test_data_count"]},
    {"type": "languages", "properties": ["id", "name", "extensions"]},
    {"type": "judgement-types", "properties": ["id", "name", "penalty", "solved"]},
    {"type": "submissions", "properties": [
        "id", "team_id", "problem_id", "language_id", "time", "contest_time",
        "files"]},
    {"type": "judgements", "properties": [
        "id", "submission_id", "judgement_type_id", "current",
        "start_time", "start_contest_time", "end_time", "end_contest_time",
        "max_run_time", "score"]},
    {"type": "runs", "properties": [
        "id", "judgement_id", "ordinal", "judgement_type_id", "time",
        "contest_time", "submission_id", "team_id", "problem_id", "run_time",
        "memory", "exit_code", "judge_time"]},
    {"type": "scoreboard", "properties": ["time", "contest_time", "state", "rows"]},
    {"type": "event-feed", "properties": ["type", "id", "data", "token"]},
]


def make_access() -> dict:
    return {"capabilities": [], "endpoints": [dict(e) for e in ACCESS_ENDPOINTS]}
