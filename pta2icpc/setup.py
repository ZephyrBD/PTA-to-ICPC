"""One-time tkinter setup GUI: problem colors + custom team groups.

Run before the server starts (``python -m pta2icpc.setup``). Fetches problems
and teams once from PTA, opens the GUI, writes the operator's choices into
``manual/contest/setup-*.json`` (auto-loaded by ``config.load_manual``), then exits.
The server process never imports tkinter.

Pure, testable logic lives at module level; all tkinter usage is confined to
``show_compliance_warning()`` / ``launch_gui()`` / ``launch_connection_dialog()``
(tkinter imported lazily) so importing this module is headless-safe.

Data semantics (see README / docs):
- PTA userGroups feed ONLY ``organization_id`` + ``organizations``.
- ``group_ids`` (打星 / 正式 / 女队 ...) is operator-defined here and emitted
  as a spec-compliant array on teams; ``groups`` come only from this config.
- Problem ``color``/``rgb`` set here win over the PALETTE default.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path

from . import paths
from .auth import generate_api_password
from .config import Settings, load_connection, load_settings
from .logging_config import configure_logging
from .pta.client import PtaAuthError, PtaClient
from .timefmt import parse_finalized_at, parse_reltime, to_iso_time
from .translate import PALETTE, resolve_contest_times

log = logging.getLogger(__name__)

SETUP_PROBLEMS_FILE = paths.SETUP_PROBLEMS_FILE.name
SETUP_GROUPS_FILE = paths.SETUP_GROUPS_FILE.name
SETUP_TEAMS_FILE = paths.SETUP_TEAMS_FILE.name
SETUP_CONNECTION_FILE = paths.CONNECTION_FILE.name
SETUP_CONTEST_FILE = paths.SETUP_CONTEST_FILE.name

_GROUP_ID_RE = re.compile(r"g(\d+)\Z")


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #

def allocate_group_id(existing_groups) -> str:
    """Next stable group id ``gN`` (N = max existing N + 1; ``g1`` when none).

    Only ids matching ``g<digits>`` count toward the max, so unrelated ids in
    the same mapping never bump the counter.
    """
    highest = 0
    for gid in existing_groups or ():
        m = _GROUP_ID_RE.fullmatch(str(gid))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"g{highest + 1}"


def _label_sort_key(row):
    """Numeric-aware 学号 comparator: numeric labels sort as numbers (so
    "20230010" comes after "2023002"); mixed batches fall back to a stable
    string sort via a (is_numeric, value) tuple key."""
    lb = str(row.get("label", ""))
    return (0, int(lb)) if lb.isdigit() else (1, lb)


def sort_teams_by_label(rows):
    """Sort team rows ascending by ``label`` (学号).

    Numeric-aware via ``_label_sort_key``: each label maps to an
    ``(is_numeric, value)`` tuple, so in a mixed batch every numeric label
    comes first (in numeric order, "20230010" after "2023002"), followed by
    the non-numeric labels in string order.
    """
    return sorted(rows, key=_label_sort_key)


# sortable header columns of the teams Treeview ("#" is positional — not a
# sort target). Value key per column; name/org/groups are case-folded string
# sorts (deterministic; Chinese names sort by Unicode codepoint order, not
# pinyin — documented limitation).
_TEAM_SORT_KEYS = {
    "label": _label_sort_key,
    "name": lambda r: str(r.get("name", "")).casefold(),
    "seat": lambda r: str(r.get("seat", "")).casefold(),
    "org": lambda r: str(r.get("organization_name", "")).casefold(),
    "groups": lambda r: "，".join(r.get("group_names") or []),
}


def sort_team_rows(rows, key, reverse=False) -> list[dict]:
    """Return a new list of team rows sorted by the named display column.

    ``key`` is one of ``_TEAM_SORT_KEYS`` ("label", "name", "seat", "org",
    "groups"). Unknown keys fall back to 学号 (the default initial order).
    """
    return sorted(rows, key=_TEAM_SORT_KEYS.get(key, _label_sort_key),
                  reverse=reverse)


def build_problem_rows(problems, saved_colors, print_problem=None) -> list[dict]:
    """One display row per problem, in ordinal order.

    Prefill color/rgb from ``saved_colors`` (label → {color, rgb}) when
    present, else the PALETTE default (same index rule as translate). A row
    carrying a saved value is marked ``changed`` so it is written back.
    ``print_problem`` (label or None) prefills the single-select 打印题 flag.
    """
    rows: list[dict] = []
    for i, p in enumerate(problems or []):
        label = str(p.get("label") or p.get("id") or (i + 1))
        saved = (saved_colors or {}).get(label) or {}
        default_color, default_rgb = PALETTE[i % len(PALETTE)]
        rows.append({
            "ordinal": p.get("ordinal", i + 1),
            "label": label,
            "name": str(p.get("title", p.get("name", label))),
            "color": saved.get("color") or default_color,
            "rgb": saved.get("rgb") or default_rgb,
            "changed": bool(saved),
            "print": print_problem is not None and label == str(print_problem),
        })
    rows.sort(key=lambda r: r["ordinal"])
    return rows


def toggle_print_problem(problem_rows, idx) -> None:
    """Single-select toggle for the 打印题 column (mutates in place).

    Sets ``problem_rows[idx]["print"]`` to the opposite of its current
    value and clears every other row, so at most one row is ever True.
    An out-of-range ``idx`` (empty table) is a no-op.
    """
    if not 0 <= idx < len(problem_rows):
        return
    new_val = not problem_rows[idx]["print"]
    for i, row in enumerate(problem_rows):
        row["print"] = new_val and i == idx


def build_team_rows(teams, groups, saved_assignments) -> list[dict]:
    """Team rows for the group tab, sorted by 学号.

    ``group_ids`` prefill from ``saved_assignments`` (team_id → {group_ids});
    ``group_names`` is the comma-joinable display form resolved via ``groups``
    (unknown ids fall back to the id itself). ``seat``/``members``/``coaches``
    also prefill from the same saved entry (defaults ""/[] when absent).
    Read-only org columns come from the PTA roster.
    """
    rows: list[dict] = []
    for t in teams or []:
        tid = str(t["id"])
        saved = (saved_assignments or {}).get(tid) or {}
        gids = list(saved.get("group_ids") or [])
        names = [str((groups.get(g) or {}).get("name") or g) for g in gids]
        rows.append({
            "id": tid,
            "label": str(t.get("label") or tid),
            "name": str(t.get("name") or tid),
            "organization_id": t.get("organization_id"),
            "organization_name": str(
                t.get("organization_name") or t.get("organization_id") or ""),
            "group_ids": gids,
            "group_names": names,
            "seat": str(saved.get("seat") or ""),
            "members": [str(m) for m in saved.get("members") or []],
            "coaches": [str(c) for c in saved.get("coaches") or []],
        })
    return sort_teams_by_label(rows)


def build_teams_setup(assignments, team_meta) -> dict:
    """teams section for setup-teams.json (pure; empty values omitted).

    ``assignments`` maps team_id → set/list of group ids, ``team_meta``
    maps team_id → {seat, members, coaches}. Per team the output entry
    carries ``group_ids`` (non-empty only — existing rule) plus ``seat`` /
    ``members`` / ``coaches`` (non-empty only); a team with nothing to
    write is omitted entirely.
    """
    out: dict = {}
    for tid, gs in (assignments or {}).items():
        entry: dict = {}
        gids = sorted(gs)
        if gids:
            entry["group_ids"] = gids
        meta = (team_meta or {}).get(tid) or {}
        seat = str(meta.get("seat") or "").strip()
        if seat:
            entry["seat"] = seat
        members = [str(m).strip() for m in meta.get("members") or []
                   if str(m).strip()]
        if members:
            entry["members"] = members
        coaches = [str(c).strip() for c in meta.get("coaches") or []
                   if str(c).strip()]
        if coaches:
            entry["coaches"] = coaches
        if entry:
            out[tid] = entry
    return out


async def fetch_setup_data(client) -> tuple[list[dict], list[dict], dict]:
    """One-shot fetch of (problems, teams, contest meta) from PTA."""
    problems = await client.fetch_problems()
    teams = await client.fetch_teams()
    meta = await client.fetch_contest_meta()
    return problems, teams, meta


def build_contest_setup(contest: dict) -> dict:
    """contest section for setup-contest.json (pure; empty values omitted).

    All four keys are optional. ``start_time`` / ``duration`` /
    ``scoreboard_freeze_duration`` are kept only when non-blank;
    ``finalized_at`` is kept only when it is ``"never"`` or an ISO-8601
    string (blank / ``"auto"`` = the automatic rule).
    """
    contest = contest or {}
    out: dict = {}
    for key in ("start_time", "duration", "scoreboard_freeze_duration"):
        value = str(contest.get(key) or "").strip()
        if value:
            out[key] = value
    fa = str(contest.get("finalized_at") or "").strip()
    if fa == "never":
        out["finalized_at"] = "never"
    elif fa and parse_finalized_at(fa)[0] == "at":
        out["finalized_at"] = fa
    return out


def prefill_contest_times(meta: dict, saved_contest: dict | None = None,
                          tz: str = "+08:00") -> dict:
    """Initial values for the 比赛设置 time boxes ``{"start_time",
    "duration", "scoreboard_freeze_duration"}``.

    Per key: the saved override wins when non-blank, else the PTA-derived
    value, else ``""``. Delegates to :func:`translate.resolve_contest_times`
    so the prefilled freeze is computed against the RESOLVED duration (saved
    override wins) — i.e. "prefill then save unchanged" reproduces exactly
    what the server would resolve. ``tz`` is the saved contest's
    ``pta_timezone``; it only affects the emitted ``start_time`` offset.

    A non-positive resolved freeze (the clamped ``"0:00:00"``) becomes ``""``:
    a blank box means 封榜关闭 on save, whereas ``validate_contest_times``
    rejects a literal ``"0:00:00"`` (freeze must be > 0) — without this the
    operator could not save a prefill untouched. ``_build_contest`` is
    unaffected (it still resolves ``"0:00:00"``).
    """
    resolved = resolve_contest_times(saved_contest or {}, meta or {}, tz)
    freeze = resolved.get("scoreboard_freeze_duration") or ""
    if freeze:
        try:
            if parse_reltime(freeze).total_seconds() <= 0:
                freeze = ""
        except (ValueError, TypeError, IndexError):
            freeze = ""  # unexpected form → treat as 封榜关闭 rather than trap the save
    return {"start_time": resolved.get("start_time") or "",
            "duration": resolved.get("duration") or "",
            "scoreboard_freeze_duration": freeze}


def merge_contest_prefill(current: dict, last_prefill: dict,
                          new_prefill: dict) -> dict:
    """Three-way merge for the 比赛设置 boxes after a refetch.

    One rule: a var is updated to ``new_prefill[k]`` only when its current
    text still equals ``last_prefill[k]`` (the user has not edited it); a
    user edit always wins. Returns ``{"values", "prefill"}`` where
    ``values[k]`` is the next ``StringVar`` text and ``prefill[k]`` is the
    new bookkeeping to remember — so the caller updates both in one step.
    """
    values: dict = {}
    prefill: dict = {}
    for key in ("start_time", "duration", "scoreboard_freeze_duration"):
        untouched = str(current.get(key) or "") == str(last_prefill.get(key) or "")
        values[key] = new_prefill.get(key, "") if untouched else current.get(key, "")
        prefill[key] = new_prefill.get(key, "")
    return {"values": values, "prefill": prefill}


def normalize_team_meta(seat, members, coaches) -> dict:
    """Clean a team dialog result: ``seat`` stripped; blank member/coach
    entries dropped and the rest stripped. Returns ``{"seat", "members",
    "coaches"}`` ready to store in ``team_meta[tid]``."""
    return {
        "seat": str(seat or "").strip(),
        "members": [str(m).strip() for m in members or [] if str(m).strip()],
        "coaches": [str(c).strip() for c in coaches or [] if str(c).strip()],
    }


def apply_team_meta(team_meta: dict, tid: str, result: dict) -> None:
    """Write a dialog result (already ``normalize_team_meta``-normalized, or
    raw — it is normalized again here) into ``team_meta[tid]``."""
    team_meta[tid] = normalize_team_meta(
        result.get("seat"), result.get("members"), result.get("coaches"))


def render_team_seat(team_meta: dict, row: dict) -> str:
    """Seat text to display for a team table ``row``.

    ``team_meta`` is the live source of truth (the dialog writes there); the
    row's own ``seat`` is only the fallback until a dialog has touched it, so
    a freshly edited 座位号 shows immediately instead of waiting for a
    refetch to rebuild ``team_rows``."""
    entry = team_meta.get(row.get("id"))
    if entry is not None and "seat" in entry:
        return str(entry.get("seat") or "")
    return str(row.get("seat") or "")


def apply_team_edit(team_meta: dict, team_rows, tid: str, result: dict) -> None:
    """Apply a confirmed dialog result to BOTH the store and the display rows.

    Writes the normalized entry into ``team_meta[tid]`` and mirrors
    ``seat`` / ``members`` / ``coaches`` into the matching ``team_rows`` entry
    (when present). Without the mirror, the 座位号 sort key (which reads
    ``row["seat"]``) would disagree with the rendered cell (which reads
    ``team_meta``) between an edit and the next refetch, so a 座位号 sort could
    order rows inconsistently with the column shown."""
    apply_team_meta(team_meta, tid, result)
    entry = team_meta[tid]
    for row in team_rows or []:
        if str(row.get("id")) == str(tid):
            row["seat"] = entry["seat"]
            row["members"] = list(entry["members"])
            row["coaches"] = list(entry["coaches"])
            break


def validate_contest_times(start_time, duration, freeze, finalized_at,
                           ) -> tuple[bool, str, dict]:
    """Validate the 比赛设置 tab fields and return the normalized payload.

    Blank field = automatic (no error). A non-blank ``start_time`` must be
    ISO-8601 with an explicit UTC offset (``Z`` counts) — naive input is
    rejected (the Contest API TIME type is absolute). ``duration`` and
    ``freeze`` must parse via ``timefmt.parse_reltime`` and be positive.
    ``finalized_at`` is ""/"auto"/"never" or ISO-8601 (offset optional;
    naive is read as UTC by ``parse_finalized_at``). Returns
    ``(ok, error message, {"contest": {...}})``.
    """
    contest: dict = {}
    st = str(start_time or "").strip()
    if st:
        try:
            when = dt.datetime.fromisoformat(st.replace("Z", "+00:00"))
        except ValueError:
            return False, "开始时间格式不对：应为 ISO-8601 且带时区偏移，例如 2026-10-13T16:00:00+08:00", {}
        if when.tzinfo is None:
            return False, "开始时间必须带时区偏移（如 +08:00 或 Z），例如 2026-10-13T16:00:00+08:00", {}
        contest["start_time"] = to_iso_time(when)
    dur = str(duration or "").strip()
    if dur:
        try:
            td = parse_reltime(dur)
        except (ValueError, TypeError, IndexError):
            return False, "时长格式不对：应为 时:分:秒（如 51:00:00）", {}
        if td.total_seconds() <= 0:
            return False, "时长必须大于 0", {}
        contest["duration"] = dur
    fz = str(freeze or "").strip()
    if fz:
        try:
            td = parse_reltime(fz)
        except (ValueError, TypeError, IndexError):
            return False, "封榜时长格式不对：应为 时:分:秒（如 45:00:00）", {}
        if td.total_seconds() <= 0:
            return False, "封榜时长必须大于 0", {}
        contest["scoreboard_freeze_duration"] = fz
    fa = str(finalized_at or "").strip()
    if fa and fa != "auto":
        mode, when = parse_finalized_at(fa)
        if mode == "never":
            contest["finalized_at"] = "never"
        elif mode == "at":
            contest["finalized_at"] = to_iso_time(when)
        else:
            return False, "定稿时间格式不对：应为空、auto、never 或 ISO-8601 时间", {}
    return True, "", {"contest": contest}


def write_contest_setup(contest: dict, *,
                        contest_dir=paths.CONTEST_DIR) -> None:
    """Write ``contest_dir/setup-contest.json`` (UTF-8, indent 2)."""
    contest_dir = Path(contest_dir)
    contest_dir.mkdir(parents=True, exist_ok=True)
    payload = {"contest": build_contest_setup(contest)}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    (contest_dir / SETUP_CONTEST_FILE).write_text(text, encoding="utf-8")


def load_manual_setup(contest_dir=paths.CONTEST_DIR) -> dict:
    """Read the four ``setup-*.json`` files if present.

    Returns ``{"problems": {...}, "groups": {...}, "teams": {...},
    "contest": {...}, "print_problem": str | None}`` with empty dicts /
    None for missing or short files (a malformed JSON file is ignored —
    the server's ``config.load_manual`` performs the strict validation).
    ``print_problem`` is the top-level sibling of ``"problems"`` in
    setup-problems.json.
    """
    out: dict = {"problems": {}, "groups": {}, "teams": {},
                 "contest": {}, "print_problem": None}
    contest_dir = Path(contest_dir)
    for key, fname in (("problems", SETUP_PROBLEMS_FILE),
                       ("groups", SETUP_GROUPS_FILE),
                       ("teams", SETUP_TEAMS_FILE),
                       ("contest", SETUP_CONTEST_FILE)):
        f = contest_dir / fname
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        section = data.get(key)
        if isinstance(section, dict):
            out[key] = section
        if key == "problems":
            pp = data.get("print_problem")
            if isinstance(pp, str) and pp:
                out["print_problem"] = pp
    return out


def write_manual_setup(problems_colors, groups, assignments,
                       print_problem=None, *,
                       contest_dir=paths.CONTEST_DIR) -> None:
    """Write the three setup files (UTF-8, no ASCII escaping, indent 2).

    ``assignments`` maps team_id → {group_ids: [...], seat?, members?,
    coaches?} (see ``build_teams_setup`` for the omission rules); the
    caller omits teams with an empty assignment. ``print_problem`` (label
    or None) is stored as a top-level sibling of ``"problems"`` in
    setup-problems.json; None is written explicitly as null ("no print
    problem" is meaningful after switching contests).
    """
    contest_dir = Path(contest_dir)
    contest_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        SETUP_PROBLEMS_FILE: {"problems": problems_colors,
                              "print_problem": print_problem},
        SETUP_GROUPS_FILE: {"groups": groups},
        SETUP_TEAMS_FILE: {"teams": assignments},
    }
    for fname, payload in payloads.items():
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        (contest_dir / fname).write_text(text, encoding="utf-8")


def write_connection(conn: dict, *,
                     contest_dir=paths.CONTEST_DIR) -> None:
    """Write ``contest_dir/setup-connection.json`` (UTF-8, indent 2).

    The optional ``print_server`` URL is only written when non-empty.
    """
    contest_dir = Path(contest_dir)
    contest_dir.mkdir(parents=True, exist_ok=True)
    payload = {"contest_id": conn["contest_id"],
               "pta_cookie": conn["pta_cookie"]}
    print_server = conn.get("print_server")
    if isinstance(print_server, str) and print_server:
        payload["print_server"] = print_server
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    (contest_dir / SETUP_CONNECTION_FILE).write_text(text, encoding="utf-8")


def merge_api_password(existing_text: str | None, password: str) -> dict:
    """Set top-level ``api_password``, keeping every other key untouched.

    Missing / blank ``existing_text`` starts from ``{}``; malformed JSON (or
    a non-object root) raises ``ValueError`` so the caller never overwrites a
    file it could not understand.
    """
    if not existing_text or not existing_text.strip():
        return {"api_password": password}
    data = json.loads(existing_text)  # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("config.json must contain a JSON object")
    data["api_password"] = password
    return data


def write_api_password(password: str, *,
                       config_file=paths.CONFIG_FILE) -> None:
    """Write ``api_password`` into ``config.json``, preserving other settings.

    Reads the existing file first (missing → new file); a malformed existing
    file raises ``ValueError`` and is left untouched. Written atomically via a
    ``.tmp`` sibling (UTF-8, indent 2).
    """
    config_file = Path(config_file)
    existing = (config_file.read_text(encoding="utf-8")
                if config_file.exists() else None)
    data = merge_api_password(existing, password)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = config_file.with_name(config_file.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(config_file)


# --------------------------------------------------------------------------- #
# networking (shared by main() and the GUI refetch button)
# --------------------------------------------------------------------------- #

def _client(conn: dict, requests_per_second: float) -> PtaClient:
    return PtaClient("https://pintia.cn", conn["contest_id"],
                     conn["pta_cookie"],
                     requests_per_second=requests_per_second)


def _fetch_remote(conn: dict, requests_per_second: float
                  ) -> tuple[list[dict], list[dict], dict]:
    async def run():
        client = _client(conn, requests_per_second)
        try:
            return await fetch_setup_data(client)
        finally:
            await client.aclose()
    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# GUI (tkinter imported lazily; never needed for tests)
# --------------------------------------------------------------------------- #

COMPLIANCE_WARNING_TEXT = (
    "本项目仅为 ICPC Contest API 协议研究与技术演示工具。\n"
    "它仅在本地读取账号已可见的比赛数据，将其转译为标准化竞赛接口。\n"
    "\n"
    "1. 本工具仅用于技术学习、本地测试。\n"
    "2. 禁止在生产环境，使用本工具举办正式竞赛。\n"
    "3. 使用本工具自动化访问PTA服务，需自行遵守PTA的用户协议。\n"
    "4. 自动化轮询PTA网页接口可能违反平台服务条款，使用者自行承担全部法律与账号风险。\n"
    "5. 本项目不提供任何形式的生产环境可用性承诺，不提供技术支持。\n"
    "\n"
    "PTA 官方提供稳定的 Contest API 的付费服务，正式赛事请直接采购。"
)


def show_compliance_warning(parent=None) -> None:
    """Modal compliance warning shown once, before the setup window appears.

    A single OK button (warning-only), so it never blocks automation beyond
    the user's acknowledgement. Best-effort: when tkinter / messagebox is
    unavailable (headless env), log and return instead of crashing the setup
    flow. The GUI is never part of the test/automation path.
    """
    try:
        from tkinter import messagebox
        messagebox.showwarning("使用须知 / Warning",
                               COMPLIANCE_WARNING_TEXT, parent=parent)
    except Exception:  # noqa: BLE001 - the warning must never break setup
        log.warning("compliance warning dialog unavailable", exc_info=True)


def launch_connection_dialog(parent, settings: Settings,
                             contest_dir=paths.CONTEST_DIR,
                             prefill=None) -> dict | None:
    """Modal dialog to enter/validate the PTA connection.

    Three Entry fields (比赛 ID / PTA Cookie / 打印服务器 URL), prefilled
    from ``prefill``. The print server is optional but, when non-empty,
    must start with http:// or https://. "连接并拉取" validates with one
    ``_fetch_remote`` round-trip (exercises both cookie auth and contest
    id), then writes setup-connection.json and returns the conn dict;
    "取消" / window close returns None. ``parent`` may be None (standalone
    call from ``main()`` — a temporary root is created).
    """
    import tkinter as tk
    from tkinter import messagebox, ttk

    temp_root = None
    if parent is None:
        temp_root = tk.Tk()
        temp_root.withdraw()
        parent = temp_root

    dlg = tk.Toplevel(parent)
    dlg.title("比赛连接")
    dlg.geometry("300x220")
    dlg.resizable(False, False)

    result: dict = {"conn": None}

    ttk.Label(dlg, text="比赛 ID").pack(anchor="w", padx=12, pady=(12, 0))
    cid_entry = ttk.Entry(dlg, width=36)
    cid_entry.pack(fill="x", padx=12)
    ttk.Label(dlg, text="PTA Cookie").pack(anchor="w", padx=12, pady=(8, 0))
    cookie_entry = ttk.Entry(dlg, width=36)
    cookie_entry.pack(fill="x", padx=12)
    ttk.Label(dlg, text="打印服务器 URL（可选，PRINT_SERVER）").pack(
        anchor="w", padx=12, pady=(8, 0))
    print_entry = ttk.Entry(dlg, width=36)
    print_entry.pack(fill="x", padx=12)

    prefill = prefill or {}
    cid_entry.insert(0, str(prefill.get("contest_id") or ""))
    cookie_entry.insert(0, str(prefill.get("pta_cookie") or ""))
    print_entry.insert(0, str(prefill.get("print_server") or ""))

    def on_connect() -> None:
        conn = {"contest_id": cid_entry.get().strip(),
                "pta_cookie": cookie_entry.get().strip(),
                "print_server": print_entry.get().strip()}
        if not conn["contest_id"] or not conn["pta_cookie"]:
            messagebox.showwarning("比赛连接", "请填写比赛 ID 和 PTA Cookie",
                                   parent=dlg)
            return
        if conn["print_server"] and not conn["print_server"].startswith(
                ("http://", "https://")):
            messagebox.showwarning("比赛连接",
                                   "打印服务器 URL 需以 http:// 或 https:// 开头",
                                   parent=dlg)
            return
        try:
            _fetch_remote(conn, settings.pta_requests_per_second)
        except PtaAuthError:
            messagebox.showerror("错误", "cookie 失效", parent=dlg)
            return
        except Exception as e:  # noqa: BLE001 - surface any fetch failure in-GUI
            messagebox.showerror("错误", f"拉取失败：{e}", parent=dlg)
            return
        write_connection(conn, contest_dir=contest_dir)
        result["conn"] = conn
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.pack(fill="x", side="bottom", padx=12, pady=12)
    ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
    ttk.Button(btns, text="连接并拉取", command=on_connect).pack(
        side="right", padx=6)

    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    dlg.grab_set()
    dlg.wait_window()
    if temp_root is not None:
        temp_root.destroy()
    return result["conn"]


def launch_team_dialog(parent, title: str, meta: dict) -> dict | None:
    """Modal dialog to edit one team's 座位号 / 队员 / 教练.

    ``meta`` is the team's existing ``{"seat", "members", "coaches"}``;
    the listboxes' 添加/移除 keep the previous behaviour (removing with
    nothing selected is a no-op). Returns a ``normalize_team_meta``-shaped
    dict on 确定, ``None`` on 取消 / window close (the caller must then
    change nothing).
    """
    import tkinter as tk
    from tkinter import simpledialog, ttk

    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.geometry("360x460")
    dlg.resizable(False, False)
    dlg.columnconfigure(0, weight=1)
    dlg.rowconfigure(3, weight=1)  # members box absorbs slack; buttons stay low

    result: dict = {"meta": None}
    members = list(meta.get("members") or [])
    coaches = list(meta.get("coaches") or [])

    ttk.Label(dlg, text="座位号").grid(row=0, column=0, columnspan=2,
                                       sticky="w", padx=12, pady=(12, 0))
    seat_entry = ttk.Entry(dlg, width=32)
    seat_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12)
    seat_entry.insert(0, str(meta.get("seat") or ""))

    ttk.Label(dlg, text="队员").grid(row=2, column=0, columnspan=2,
                                     sticky="w", padx=12, pady=(8, 0))
    members_list = tk.Listbox(dlg, height=6, exportselection=False)
    members_list.grid(row=3, column=0, sticky="nsew", padx=(12, 0))
    members_btns = ttk.Frame(dlg)
    members_btns.grid(row=3, column=1, sticky="n", padx=(6, 12))

    ttk.Label(dlg, text="教练").grid(row=4, column=0, columnspan=2,
                                     sticky="w", padx=12, pady=(8, 0))
    coaches_list = tk.Listbox(dlg, height=4, exportselection=False)
    coaches_list.grid(row=5, column=0, sticky="nsew", padx=(12, 0))
    coaches_btns = ttk.Frame(dlg)
    coaches_btns.grid(row=5, column=1, sticky="n", padx=(6, 12))

    def refill(lb, names: list) -> None:
        lb.delete(0, "end")
        for name in names:
            lb.insert("end", name)

    def add(kind: str, lb) -> None:
        name = simpledialog.askstring(title, "添加：", parent=dlg)
        if name is None or not name.strip():
            return
        (members if kind == "members" else coaches).append(name.strip())
        refill(lb, members if kind == "members" else coaches)

    def remove(kind: str, lb) -> None:
        sel = lb.curselection()
        if not sel:  # nothing selected → no-op
            return
        idx = sel[0]
        (members if kind == "members" else coaches).pop(idx)
        refill(lb, members if kind == "members" else coaches)

    for kind, lb, box in (("members", members_list, members_btns),
                          ("coaches", coaches_list, coaches_btns)):
        ttk.Button(box, text="＋添加",
                   command=lambda k=kind, b=lb: add(k, b)).pack(
            anchor="w", fill="x")
        ttk.Button(box, text="移除",
                   command=lambda k=kind, b=lb: remove(k, b)).pack(
            anchor="w", fill="x", pady=(4, 0))

    refill(members_list, members)
    refill(coaches_list, coaches)

    def on_ok() -> None:
        result["meta"] = normalize_team_meta(
            seat_entry.get(), members, coaches)
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=12)
    ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
    ttk.Button(btns, text="确定", command=on_ok).pack(side="right", padx=6)

    dlg.bind("<Return>", lambda _e: on_ok())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    dlg.grab_set()
    dlg.wait_window()
    return result["meta"]


def launch_gui(settings: Settings, problems, teams, meta, saved,
               conn: dict) -> None:
    """Build and run the setup window. Blocking (tkinter mainloop)."""
    import tkinter as tk
    from tkinter import colorchooser, messagebox, simpledialog, ttk

    contest_dir = paths.CONTEST_DIR

    problem_rows = build_problem_rows(problems, saved.get("problems") or {},
                                      saved.get("print_problem"))
    group_defs = {str(gid): {"name": str((g or {}).get("name") or "")}
                  for gid, g in (saved.get("groups") or {}).items()}
    team_rows = build_team_rows(teams, group_defs, saved.get("teams") or {})
    assignments = {r["id"]: set(r["group_ids"]) for r in team_rows}
    # seat/members/coaches live in a parallel dict keyed by team id (the
    # Treeview only displays groups); the 座位号 / 成员 panel edits it.
    team_meta = {r["id"]: {"seat": r["seat"], "members": list(r["members"]),
                           "coaches": list(r["coaches"])}
                 for r in team_rows}
    contest_saved = saved.get("contest") or {}

    state = {"dirty": False}
    ui = {"combo_map": {}}

    root = tk.Tk()
    root.title("PTA to ICPC setup — 首次运行配置")
    root.geometry("1000x640")

    def mark_dirty() -> None:
        state["dirty"] = True
        root.title("PTA to ICPC setup — 首次运行配置 *")

    def group_display(gid: str) -> str:
        name = (group_defs.get(gid) or {}).get("name") or ""
        return f"{gid} {name}".strip()

    # ---------------- Tab 1: problem colors ----------------
    # Layout: the bottom bar is packed FIRST (side=bottom) so the Notebook
    # (packed with fill both + expand) gets the remaining central space.
    # Packed before the tabs' widgets are created.
    bar = ttk.Frame(root)
    bar.pack(fill="x", side="bottom")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    pframe = ttk.Frame(notebook)
    # tree + a vertical scrollbar share one box so the 快捷色板 panel (packed
    # side=right in pframe) stays flush with the window edge and the
    # scrollbar hugs the tree
    ptable = ttk.Frame(pframe)
    ptable.pack(side="left", fill="both", expand=True)
    tree = ttk.Treeview(
        ptable, columns=("ordinal", "label", "name", "color"),
        show="headings", height=18)
    for col, text, width in (("ordinal", "#", 40), ("label", "题号", 100),
                             ("name", "名称", 320), ("color", "颜色", 200)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w")
    tree.pack(side="left", fill="both", expand=True)
    pscroll = ttk.Scrollbar(ptable, orient="vertical", command=tree.yview)
    pscroll.pack(side="right", fill="y")
    tree.configure(yscrollcommand=pscroll.set)

    print_var = tk.StringVar(value="")

    def sync_print_panel() -> None:
        # radios read their state from problem_rows (single source of truth)
        print_var.set(next((str(r["label"]) for r in problem_rows if r["print"]),
                           ""))

    def on_print_selected() -> None:
        new_label = print_var.get()
        old_label = next((str(r["label"]) for r in problem_rows if r["print"]),
                         "")
        if old_label == new_label:
            return
        for row in problem_rows:
            row["print"] = new_label != "" and str(row["label"]) == new_label
        mark_dirty()

    def rebuild_print_panel() -> None:
        for child in print_inner.winfo_children():
            child.destroy()
        first = ttk.Radiobutton(print_inner, text="不设打印题", value="",
                                variable=print_var,
                                command=on_print_selected)
        first.pack(anchor="w", padx=8, pady=2)
        _bind_mousewheel(first, print_canvas)
        for row in problem_rows:
            rb = ttk.Radiobutton(print_inner,
                                 text=f"{row['label']} {row['name']}",
                                 value=str(row["label"]), variable=print_var,
                                 command=on_print_selected)
            rb.pack(anchor="w", padx=8, pady=1)
            _bind_mousewheel(rb, print_canvas)
        sync_print_panel()

    def refresh_problems() -> None:
        tree.delete(*tree.get_children())
        for i, row in enumerate(problem_rows):
            tag = f"pcol{i}"
            tree.tag_configure(tag, foreground=row["rgb"])
            star = "*" if row["changed"] else " "
            tree.insert("", "end", iid=str(i), tags=(tag,),
                        values=(row["ordinal"], row["label"], star + row["name"],
                                "■ " + row["color"]))
        sync_print_panel()

    def problem_index() -> int | None:
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def apply_color(name: str, rgb: str) -> None:
        idx = problem_index()
        if idx is None:
            messagebox.showinfo("题目颜色", "请先选择一行题目", parent=root)
            return
        problem_rows[idx]["color"] = name
        problem_rows[idx]["rgb"] = rgb
        problem_rows[idx]["changed"] = True
        mark_dirty()
        refresh_problems()
        tree.selection_set(str(idx))

    def pick_custom_color() -> None:
        idx = problem_index()
        if idx is None:
            messagebox.showinfo("题目颜色", "请先选择一行题目", parent=root)
            return
        _rgb_tuple, hex_rgb = colorchooser.askcolor(
            color=problem_rows[idx]["rgb"], parent=root, title="选择颜色")
        if not hex_rgb:
            return
        name = next((n for n, r in PALETTE
                     if r.lower() == hex_rgb.lower()), hex_rgb)
        apply_color(name, hex_rgb)

    tree.bind("<Double-1>", lambda _e: pick_custom_color())

    pside = ttk.Frame(pframe)
    pside.pack(side="right", fill="y", padx=8, pady=8)
    ttk.Label(pside, text="快捷色板").pack(anchor="w")
    swatch = ttk.Frame(pside)
    swatch.pack(anchor="w", pady=4)
    for ci, (cname, crgb) in enumerate(PALETTE):
        tk.Button(swatch, bg=crgb, width=3, height=1, relief="ridge",
                  command=lambda n=cname, r=crgb: apply_color(n, r)
                  ).grid(row=ci // 4, column=ci % 4, padx=1, pady=1)
    ttk.Button(pside, text="🎨 选择颜色…",
               command=pick_custom_color).pack(anchor="w", pady=8)
    ttk.Label(pside, text="（选中一行后点色板/选择颜色）",
              foreground="#666").pack(anchor="w")

    ttk.Separator(pside, orient="horizontal").pack(fill="x", pady=8)

    def _bind_mousewheel(widget, canvas) -> None:
        # wheel over the canvas or any radio scrolls the radio list. delta
        # is 120-based on Windows; Button-4/5 cover X11 (no-op on Windows).
        widget.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        widget.bind("<Button-4>",
                    lambda _e: canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>",
                    lambda _e: canvas.yview_scroll(1, "units"))

    # print-problem picker: global single-select radio list (replaces the
    # old 打印题 table column); "" = 不设打印题. The radios live in a
    # scrollable canvas because their count grows with the problem count.
    print_frame = ttk.LabelFrame(pside, text="打印题")
    print_frame.pack(fill="x", pady=(4, 0))
    print_row = ttk.Frame(print_frame)
    print_row.pack(fill="both", expand=True)
    print_canvas = tk.Canvas(print_row, height=180, highlightthickness=0)
    print_canvas.pack(side="left", fill="both", expand=True)
    print_scroll = ttk.Scrollbar(print_row, orient="vertical",
                                 command=print_canvas.yview)
    print_scroll.pack(side="right", fill="y")
    print_canvas.configure(yscrollcommand=print_scroll.set)
    print_inner = ttk.Frame(print_canvas)
    print_win = print_canvas.create_window((0, 0), window=print_inner,
                                           anchor="nw")
    print_inner.bind(
        "<Configure>",
        lambda _e: print_canvas.configure(
            scrollregion=print_canvas.bbox("all")))
    print_canvas.bind(
        "<Configure>",
        lambda e: print_canvas.itemconfig(print_win, width=e.width))
    _bind_mousewheel(print_canvas, print_canvas)
    _bind_mousewheel(print_inner, print_canvas)

    # ---------------- Tab 2: team groups ----------------
    tframe = ttk.Frame(notebook)
    left = ttk.Frame(tframe)
    left.pack(side="left", fill="both", expand=True)
    right = ttk.Frame(tframe, width=300)
    right.pack(side="right", fill="y", padx=8, pady=8)

    ttree = ttk.Treeview(
        left, show="headings", selectmode="extended",
        columns=("ordinal", "label", "name", "seat", "org", "groups"))
    for col, text, width in (("ordinal", "#", 40), ("label", "学号", 100),
                             ("name", "姓名", 110), ("seat", "座位号", 80),
                             ("org", "学校", 150), ("groups", "分组", 170)):
        ttree.heading(col, text=text)
        ttree.column(col, width=width, anchor="w")

    # the 座位号 / 队员 / 教练 dialog opener sits below the table (pack it
    # FIRST so it keeps its height while the table expands to fill the rest)
    edit_bar = ttk.Frame(left)
    edit_bar.pack(side="bottom", fill="x", padx=8, pady=6)
    edit_btn = ttk.Button(edit_bar, text="编辑队伍…", state="disabled")
    edit_btn.pack(side="left")
    ttk.Label(edit_bar, text="（双击 / 回车 也可编辑所选队伍）",
              foreground="#666").pack(side="left", padx=8)
    ttree.pack(side="left", fill="both", expand=True)
    tscroll = ttk.Scrollbar(left, orient="vertical", command=ttree.yview)
    tscroll.pack(side="right", fill="y")
    ttree.configure(yscrollcommand=tscroll.set)

    # sortable header columns (Treeview #2..#6); "#" (ordinal) is a plain
    # position counter, not a sort target. Clicking a heading sorts asc,
    # clicking again toggles to desc; clicking a different column resets
    # to that column ascending. Arrows ▲/▼ mark the active column.
    _HEADER_KEYS = ("label", "name", "seat", "org", "groups")
    _HEADER_NAMES = {"label": "学号", "name": "姓名", "seat": "座位号",
                     "org": "学校", "groups": "分组"}
    sort_state = {"col": None, "reverse": False}

    def set_team_headings() -> None:
        for i, key in enumerate(_HEADER_KEYS, start=2):
            mark = ""
            if sort_state["col"] == key:
                mark = " ▼" if sort_state["reverse"] else " ▲"
            ttree.heading(f"#{i}", text=_HEADER_NAMES[key] + mark)

    def on_team_head_click(event) -> None:
        nonlocal team_rows
        if ttree.identify("region", event.x, event.y) != "heading":
            return
        col = ttree.identify_column(event.x)  # "#2".."#6"
        if col == "#1":  # ordinal — not sortable
            return
        key = _HEADER_KEYS[int(col.lstrip("#")) - 2]
        if sort_state["col"] == key:
            sort_state["reverse"] = not sort_state["reverse"]
        else:
            sort_state["col"] = key
            sort_state["reverse"] = False
        team_rows = sort_team_rows(team_rows, key, sort_state["reverse"])
        set_team_headings()
        keep_sel = [iid for iid in ttree.selection() if iid in {r["id"] for r in team_rows}]
        refresh_teams()
        ttree.selection_set(*keep_sel)

    ttree.bind("<Button-1>", on_team_head_click)

    def refresh_teams() -> None:
        ttree.delete(*ttree.get_children())
        for i, r in enumerate(team_rows):
            gids = sorted(assignments.get(r["id"], set()))
            names = "，".join(group_display(g) for g in gids)
            ttree.insert("", "end", iid=r["id"],
                         values=(i + 1, r["label"], r["name"],
                                 render_team_seat(team_meta, r),
                                 r["organization_name"], names))

    def edit_selection() -> list:
        return ttree.selection()

    def sync_team_edit_state(_e=None) -> None:
        edit_btn.configure(
            state="normal" if len(edit_selection()) == 1 else "disabled")

    def on_team_double_click(event) -> None:
        # never treat a heading click (column sort) as "edit this row"
        if ttree.identify_region(event.x, event.y) != "cell":
            return
        row = ttree.identify_row(event.y)
        if row:
            ttree.selection_set(row)
            edit_team()

    def edit_team() -> None:
        sel = edit_selection()
        if len(sel) != 1:
            return
        tid = sel[0]
        current = dict(team_meta.get(
            tid, {"seat": "", "members": [], "coaches": []}))
        result = launch_team_dialog(root, f"编辑队伍 {tid}", current)
        if result is None:
            return  # 取消 → change nothing (team_meta untouched)
        apply_team_edit(team_meta, team_rows, tid, result)
        mark_dirty()
        refresh_teams()

    edit_btn.configure(command=edit_team)
    ttree.bind("<<TreeviewSelect>>", sync_team_edit_state)
    ttree.bind("<Double-1>", on_team_double_click)
    ttree.bind("<Return>", lambda _e: edit_team())

    ttk.Label(right, text="已定义分组").pack(anchor="w")
    gbox = ttk.Frame(right)
    gbox.pack(fill="x", pady=4)
    gtree = ttk.Treeview(gbox, columns=("id", "name"), show="headings",
                         height=8, selectmode="browse")
    for col, text, width in (("id", "ID", 50), ("name", "名称", 200)):
        gtree.heading(col, text=text)
        gtree.column(col, width=width, anchor="w")
    gtree.pack(side="left", fill="both", expand=True)
    gscroll = ttk.Scrollbar(gbox, orient="vertical", command=gtree.yview)
    gscroll.pack(side="right", fill="y")
    gtree.configure(yscrollcommand=gscroll.set)

    # combo must exist BEFORE refresh_groups (refresh_groups writes to
    # combo["values"]); created here to avoid a NameError on the first call
    combo = ttk.Combobox(right, state="readonly")
    combo.pack(fill="x", pady=4)

    def refresh_groups() -> None:
        gtree.delete(*gtree.get_children())
        for gid, g in group_defs.items():
            gtree.insert("", "end", iid=gid, values=(gid, g["name"]))
        values = [group_display(gid) for gid in group_defs]
        ui["combo_map"] = {group_display(gid): gid for gid in group_defs}
        combo["values"] = values
        if values and combo.get() not in values:
            combo.current(0)
        elif not values:
            combo.set("")

    newrow = ttk.Frame(right)
    newrow.pack(fill="x", pady=4)
    new_entry = ttk.Entry(newrow)
    new_entry.pack(side="left", fill="x", expand=True)

    def new_group() -> None:
        name = new_entry.get().strip()
        if not name:
            messagebox.showwarning("新建分组", "请输入分组名称", parent=root)
            return
        gid = allocate_group_id(group_defs)
        group_defs[gid] = {"name": name}
        new_entry.delete(0, "end")
        mark_dirty()
        refresh_groups()
        refresh_teams()

    ttk.Button(newrow, text="＋新建分组", command=new_group).pack(side="left")

    def selected_group() -> str | None:
        sel = gtree.selection()
        return sel[0] if sel else None

    def rename_group() -> None:
        gid = selected_group()
        if gid is None:
            messagebox.showinfo("重命名分组", "请先选择一个分组", parent=root)
            return
        new = simpledialog.askstring(
            "重命名分组", "新名称：",
            initialvalue=group_defs[gid]["name"], parent=root)
        if new is None:
            return
        group_defs[gid]["name"] = new.strip()
        mark_dirty()
        refresh_groups()
        refresh_teams()

    def delete_group() -> None:
        gid = selected_group()
        if gid is None:
            messagebox.showinfo("删除分组", "请先选择一个分组", parent=root)
            return
        if not messagebox.askyesno(
                "删除分组", f"删除分组 {group_display(gid)}？\n将从所有队伍移除。",
                parent=root):
            return
        group_defs.pop(gid, None)
        for tid in assignments:
            assignments[tid].discard(gid)
        mark_dirty()
        refresh_groups()
        refresh_teams()

    grpbtns = ttk.Frame(right)
    grpbtns.pack(fill="x", pady=4)
    ttk.Button(grpbtns, text="✎ 重命名所选",
               command=rename_group).pack(side="left")
    ttk.Button(grpbtns, text="🗑 删除所选分组",
               command=delete_group).pack(side="left", padx=4)

    ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
    ttk.Label(right, text="批量设置（先在左侧多选队伍）").pack(anchor="w")

    def batch_add() -> None:
        sel = ttree.selection()
        if not sel:
            messagebox.showinfo("批量设置", "请先在左侧选择队伍", parent=root)
            return
        gid = ui["combo_map"].get(combo.get())
        if gid is None:
            messagebox.showinfo("批量设置", "请先选择分组", parent=root)
            return
        for tid in sel:
            assignments.setdefault(tid, set()).add(gid)
        mark_dirty()
        refresh_teams()

    def batch_clear() -> None:
        sel = ttree.selection()
        if not sel:
            messagebox.showinfo("批量设置", "请先在左侧选择队伍", parent=root)
            return
        for tid in sel:
            assignments[tid] = set()
        mark_dirty()
        refresh_teams()

    batchbtns = ttk.Frame(right)
    batchbtns.pack(fill="x", pady=4)
    ttk.Button(batchbtns, text="设分组 →", command=batch_add).pack(side="left")
    ttk.Button(batchbtns, text="清除分组", command=batch_clear).pack(
        side="left", padx=4)

    notebook.add(pframe, text="题目颜色")
    notebook.add(tframe, text="队伍分组")

    # ---------------- Tab 3: contest times ----------------
    # manual/contest/setup-contest.json overrides the PTA-derived start/duration/
    # freeze and the finalization rule; the boxes are PREFILLED from the
    # freshly fetched PTA values (saved override wins), and blank fields
    # stay automatic on save (written explicitly like any other edit).
    cframe = ttk.Frame(notebook)
    contest_form = ttk.Frame(cframe)
    contest_form.pack(anchor="nw", padx=12, pady=12)

    contest_tz = str(contest_saved.get("pta_timezone") or "+08:00")
    initial = prefill_contest_times(meta, contest_saved, tz=contest_tz)
    start_var = tk.StringVar(value=initial["start_time"])
    dur_var = tk.StringVar(value=initial["duration"])
    freeze_var = tk.StringVar(value=initial["scoreboard_freeze_duration"])
    # `last_prefill` records what the last prefill/refetch left in the boxes;
    # a box is "untouched" (so a refetch may refresh it) iff its live text
    # still equals this — the live text is read via collect_contest_prefill.
    prefill_state = {
        "last_prefill": {"start_time": start_var.get(),
                         "duration": dur_var.get(),
                         "scoreboard_freeze_duration": freeze_var.get()},
    }
    fa_saved = str(contest_saved.get("finalized_at") or "")
    if fa_saved == "never":
        fa_mode = "never"
    elif fa_saved:
        fa_mode = "at"
    else:
        fa_mode = "auto"
    fa_mode_var = tk.StringVar(value=fa_mode)
    fa_iso_var = tk.StringVar(value=fa_saved if fa_mode == "at" else "")

    def add_contest_field(row: int, label: str, var, hint: str, **kwargs):
        ttk.Label(contest_form, text=label).grid(
            row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(contest_form, textvariable=var, width=32, **kwargs)
        entry.grid(row=row, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(contest_form, text=hint, foreground="#666").grid(
            row=row, column=2, sticky="w")
        entry.bind("<KeyRelease>", lambda _e: mark_dirty())
        return entry

    add_contest_field(0, "开始时间", start_var,
                      "ISO-8601 需带时区，例如 2026-10-13T16:00:00+08:00")
    add_contest_field(1, "时长", dur_var, "时:分:秒，例如 51:00:00")
    add_contest_field(2, "封榜时长", freeze_var, "时:分:秒，例如 45:00:00")

    ttk.Label(contest_form, text="定稿时间").grid(
        row=3, column=0, sticky="nw", pady=4)
    fa_radios = ttk.Frame(contest_form)
    fa_radios.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)
    fa_entry = ttk.Entry(fa_radios, textvariable=fa_iso_var, width=32)

    def sync_fa_entry() -> None:
        fa_entry.configure(
            state="normal" if fa_mode_var.get() == "at" else "disabled")

    def on_fa_mode() -> None:
        mark_dirty()
        sync_fa_entry()

    ttk.Radiobutton(fa_radios, text="自动（比赛结束后 5 分钟）", value="auto",
                    variable=fa_mode_var, command=on_fa_mode).pack(anchor="w")
    ttk.Radiobutton(fa_radios, text="指定时间", value="at",
                    variable=fa_mode_var, command=on_fa_mode).pack(anchor="w")
    ttk.Radiobutton(fa_radios, text="不定稿", value="never",
                    variable=fa_mode_var, command=on_fa_mode).pack(anchor="w")
    fa_entry.pack(anchor="w", padx=24, pady=(2, 0))
    fa_entry.bind("<KeyRelease>", lambda _e: mark_dirty())
    sync_fa_entry()

    def collect_contest_prefill() -> dict:
        """Read the 比赛设置 boxes' CURRENT text straight from their vars, so
        an edit made since the last prefill is visible to merge_contest_prefill
        (a refetch must not clobber a value the user typed)."""
        return {"start_time": start_var.get(),
                "duration": dur_var.get(),
                "scoreboard_freeze_duration": freeze_var.get()}

    def apply_contest_prefill() -> None:
        merged = merge_contest_prefill(
            collect_contest_prefill(), prefill_state["last_prefill"],
            prefill_contest_times(meta, contest_saved, tz=contest_tz))
        start_var.set(merged["values"]["start_time"])
        dur_var.set(merged["values"]["duration"])
        freeze_var.set(merged["values"]["scoreboard_freeze_duration"])
        prefill_state["last_prefill"] = dict(merged["prefill"])

    def reset_contest_times() -> None:
        for var in (start_var, dur_var, freeze_var):
            var.set("")
        prefill_state["last_prefill"] = {"start_time": "", "duration": "",
                                         "scoreboard_freeze_duration": ""}
        mark_dirty()

    ttk.Button(contest_form, text="恢复自动（留空）",
               command=reset_contest_times).grid(
        row=4, column=1, sticky="w", padx=8, pady=(8, 4))

    # --- 接口认证 (/api/v4/ HTTP Basic) ---
    auth_frame = ttk.LabelFrame(cframe, text="接口认证")
    auth_frame.pack(anchor="nw", fill="x", padx=12, pady=(0, 12))
    auth_form = ttk.Frame(auth_frame)
    auth_form.pack(anchor="nw", padx=8, pady=8)
    ttk.Label(auth_form, text="用户名").grid(row=0, column=0, sticky="w",
                                            pady=4)
    ttk.Label(auth_form, text="admin").grid(row=0, column=1, sticky="w",
                                            padx=8)
    ttk.Label(auth_form, text="密码").grid(row=1, column=0, sticky="w",
                                          pady=4)
    api_pass_var = tk.StringVar(
        value=settings.api_password or generate_api_password())
    api_pass_entry = ttk.Entry(auth_form, textvariable=api_pass_var, width=32)
    api_pass_entry.grid(row=1, column=1, sticky="w", padx=8, pady=4)
    api_pass_entry.bind("<KeyRelease>", lambda _e: mark_dirty())

    def reset_api_password() -> None:
        api_pass_var.set(generate_api_password())
        mark_dirty()

    def copy_api_password() -> None:
        root.clipboard_clear()
        root.clipboard_append(api_pass_var.get())

    auth_btns = ttk.Frame(auth_form)
    auth_btns.grid(row=1, column=2, sticky="w")
    ttk.Button(auth_btns, text="随机重置",
               command=reset_api_password).pack(side="left")
    ttk.Button(auth_btns, text="复制",
               command=copy_api_password).pack(side="left", padx=4)
    ttk.Label(auth_form, text="供 CDS / xcpc-tools 使用；保存后重启服务生效",
              foreground="#666").grid(row=2, column=0, columnspan=3,
                                      sticky="w", pady=(4, 0))

    notebook.add(cframe, text="比赛设置")

    # ---------------- bottom bar buttons ----------------
    def refetch() -> None:
        nonlocal problem_rows, team_rows, team_meta, meta
        if state["dirty"] and not messagebox.askyesno(
                "重新拉取数据", "有未保存的修改，重新拉取将丢弃它们（颜色除外）。继续？",
                parent=root):
            return
        try:
            problems2, teams2, meta2 = _fetch_remote(
                conn, settings.pta_requests_per_second)
        except PtaAuthError:
            messagebox.showerror(
                "错误", "cookie 失效，请更新 manual/contest/setup-connection.json",
                parent=root)
            return
        except Exception as e:  # noqa: BLE001 - surface any fetch failure in-GUI
            messagebox.showerror("错误", f"拉取失败：{e}", parent=root)
            return
        meta = meta2
        apply_contest_prefill()
        old_colors = {r["label"]: {"color": r["color"], "rgb": r["rgb"]}
                      for r in problem_rows if r["changed"]}
        current_print = next((r["label"] for r in problem_rows if r["print"]),
                             None)
        problem_rows = build_problem_rows(problems2, old_colors, current_print)
        old_meta = team_meta
        saved_teams = build_teams_setup(assignments, old_meta)
        team_rows = build_team_rows(teams2, group_defs, saved_teams)
        for r in team_rows:
            assignments.setdefault(r["id"], set(r["group_ids"]))
        # seat/members/coaches edits survive the refetch for teams that
        # still exist (same spirit as the preserved group assignments)
        team_meta = {r["id"]: old_meta.get(
            r["id"], {"seat": r["seat"], "members": list(r["members"]),
                      "coaches": list(r["coaches"])})
            for r in team_rows}
        # refetch rebuilds team_rows in 学号 order — reset the view sort
        sort_state["col"] = None
        sort_state["reverse"] = False
        set_team_headings()
        refresh_problems()
        rebuild_print_panel()
        refresh_teams()
        refresh_groups()

    def save() -> None:
        if fa_mode_var.get() == "never":
            finalized_at = "never"
        elif fa_mode_var.get() == "at":
            finalized_at = fa_iso_var.get()
        else:
            finalized_at = ""
        ok, error, payload = validate_contest_times(
            start_var.get(), dur_var.get(), freeze_var.get(), finalized_at)
        if not ok:
            messagebox.showerror("比赛设置", error, parent=root)
            return
        pw = api_pass_var.get().strip()
        if not pw:
            messagebox.showerror("比赛设置", "接口密码不能为空", parent=root)
            return
        problems_colors = {
            r["label"]: {"color": r["color"], "rgb": r["rgb"]}
            for r in problem_rows if r["changed"] and not r["print"]}
        groups_out = {gid: {"name": g["name"]} for gid, g in group_defs.items()}
        teams_out = build_teams_setup(assignments, team_meta)
        print_problem = next((r["label"] for r in problem_rows if r["print"]),
                             None)
        try:
            # password first: its writer is the only one that can reject an
            # existing (unparseable) config.json, so a failure aborts before
            # the other two files are touched
            write_api_password(pw, config_file=paths.CONFIG_FILE)
            write_manual_setup(problems_colors, groups_out, teams_out,
                               print_problem, contest_dir=contest_dir)
            write_contest_setup(payload["contest"], contest_dir=contest_dir)
        except (ValueError, OSError) as e:
            messagebox.showerror("比赛设置", f"保存失败：{e}", parent=root)
            return
        state["dirty"] = False
        root.title("PTA to ICPC setup — 首次运行配置")
        messagebox.showinfo(
            "保存到 manual/contest",
            f"已写入 {contest_dir.as_posix()}\n"
            f"接口密码已写入 {paths.CONFIG_FILE.as_posix()}\n"
            "比赛设置改动需重启服务器后生效。", parent=root)

    def on_close() -> None:
        if state["dirty"] and not messagebox.askyesno(
                "退出", "有未保存的修改，确定退出？", parent=root):
            return
        root.destroy()

    def change_connection() -> None:
        nonlocal problem_rows, team_rows, team_meta, meta
        new_conn = launch_connection_dialog(root, settings, contest_dir,
                                            dict(conn))
        if new_conn is None:
            return
        try:
            problems2, teams2, meta2 = _fetch_remote(
                new_conn, settings.pta_requests_per_second)
        except PtaAuthError:
            messagebox.showerror("错误", "cookie 失效", parent=root)
            return
        except Exception as e:  # noqa: BLE001 - surface any fetch failure in-GUI
            messagebox.showerror("错误", f"拉取失败：{e}", parent=root)
            return
        contest_changed = conn.get("contest_id") != new_conn.get("contest_id")
        conn.clear()
        conn.update(new_conn)
        meta = meta2
        apply_contest_prefill()
        old_colors = {r["label"]: {"color": r["color"], "rgb": r["rgb"]}
                      for r in problem_rows if r["changed"]}
        print_label = next((r["label"] for r in problem_rows if r["print"]),
                           None)
        if contest_changed and messagebox.askyesno(
                "比赛连接",
                "比赛 ID 已变更，是否清空分组指派和打印题选择？"
                "（颜色和分组定义保留）", parent=root):
            assignments.clear()
            team_meta.clear()
            print_label = None
        saved_teams = build_teams_setup(assignments, team_meta)
        problem_rows = build_problem_rows(problems2, old_colors, print_label)
        team_rows = build_team_rows(teams2, group_defs, saved_teams)
        for r in team_rows:
            assignments.setdefault(r["id"], set(r["group_ids"]))
        old_meta = team_meta
        team_meta = {r["id"]: old_meta.get(
            r["id"], {"seat": r["seat"], "members": list(r["members"]),
                      "coaches": list(r["coaches"])})
            for r in team_rows}
        # refetch rebuilds team_rows in 学号 order — reset the view sort
        sort_state["col"] = None
        sort_state["reverse"] = False
        set_team_headings()
        refresh_problems()
        rebuild_print_panel()
        refresh_teams()
        refresh_groups()

    ttk.Button(bar, text="↺ 重新拉取数据", command=refetch).pack(
        side="left", padx=4, pady=4)
    ttk.Button(bar, text="保存到 manual", command=save).pack(
        side="left", padx=4, pady=4)
    ttk.Button(bar, text="🔗 比赛连接…", command=change_connection).pack(
        side="left", padx=4, pady=4)
    ttk.Button(bar, text="退出", command=on_close).pack(
        side="right", padx=4, pady=4)

    # ensure sizes are laid out before the initial refresh so the Treeviews
    # compute their geometry against the real (non-zero) parent
    root.update_idletasks()
    refresh_problems()
    rebuild_print_panel()
    refresh_groups()
    refresh_teams()
    set_team_headings()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main() -> None:
    paths.ensure_dirs()
    configure_logging()
    show_compliance_warning()
    settings = load_settings()
    saved = load_manual_setup()
    conn = load_connection()
    if conn is None:
        # first run / missing connection file — dialog, no prefill
        conn = launch_connection_dialog(None, settings, paths.CONTEST_DIR, None)
        if conn is None:
            raise SystemExit(0)  # user cancelled
        saved = load_manual_setup()
    try:
        problems, teams, meta = _fetch_remote(
            conn, settings.pta_requests_per_second)
    except PtaAuthError:
        # connection may be stale — fall back to the dialog (dict(conn)
        # keeps the optional print_server prefilled too)
        conn = launch_connection_dialog(
            None, settings, paths.CONTEST_DIR, dict(conn))
        if conn is None:
            raise SystemExit(1)
        problems, teams, meta = _fetch_remote(
            conn, settings.pta_requests_per_second)
    if not settings.api_password:
        # self-heal: writing setup-connection.json implies a usable password
        # exists, so the service entry point never refuses to start
        write_api_password(generate_api_password())
        settings = load_settings()
    launch_gui(settings, problems, teams, meta, saved, conn)


if __name__ == "__main__":
    main()
