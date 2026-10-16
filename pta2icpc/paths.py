from __future__ import annotations

from pathlib import Path

MANUAL_DIR = Path("manual")
CONFIG_DIR = MANUAL_DIR / "config"
CONTEST_DIR = MANUAL_DIR / "contest"
CONFIG_FILE = CONFIG_DIR / "config.json"
CONNECTION_FILE = CONTEST_DIR / "setup-connection.json"
SETUP_CONTEST_FILE = CONTEST_DIR / "setup-contest.json"
SETUP_PROBLEMS_FILE = CONTEST_DIR / "setup-problems.json"
SETUP_GROUPS_FILE = CONTEST_DIR / "setup-groups.json"
SETUP_TEAMS_FILE = CONTEST_DIR / "setup-teams.json"
BALLOONS_FILE = MANUAL_DIR / "balloons.json"
PRINT_SENT_FILE = MANUAL_DIR / "print_sent.json"
CACHE_DIR = MANUAL_DIR / "cache"
CACHE_FILE = CACHE_DIR / "state.json"
LOG_FILE = MANUAL_DIR / "pta2icpc.log"


def ensure_dirs() -> None:
    for directory in (MANUAL_DIR, CONFIG_DIR, CONTEST_DIR, CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
