"""One-time tkinter setup GUI: problem colors + custom team groups.

Run before the server starts (``python -m pta2ccs.setup``). Fetches problems
and teams once from PTA, opens the GUI, writes the operator's choices into
``manual/setup-*.json`` (auto-loaded by ``config.load_manual``), then exits.
The server process never imports tkinter.

Pure, testable logic lives at module level; all tkinter usage is confined to
``launch_gui()`` (tkinter imported lazily) so importing this module is
headless-safe.

Data semantics (see README / docs):
- PTA userGroups feed ONLY ``organization_id`` + ``organizations``.
- ``group_ids`` (打星 / 正式 / 女队 ...) is operator-defined here and emitted
  as a spec-compliant array on teams; ``groups`` come only from this config.
- Problem ``color``/``rgb`` set here win over the PALETTE default.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from .config import Settings, load_settings
from .pta.client import PtaAuthError, PtaClient
from .translate import PALETTE

SETUP_PROBLEMS_FILE = "setup-problems.json"
SETUP_GROUPS_FILE = "setup-groups.json"
SETUP_TEAMS_FILE = "setup-teams.json"

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
    "org": lambda r: str(r.get("organization_name", "")).casefold(),
    "groups": lambda r: "，".join(r.get("group_names") or []),
}


def sort_team_rows(rows, key, reverse=False) -> list[dict]:
    """Return a new list of team rows sorted by the named display column.

    ``key`` is one of ``_TEAM_SORT_KEYS`` ("label", "name", "org", "groups").
    Unknown keys fall back to 学号 (the default initial order).
    """
    return sorted(rows, key=_TEAM_SORT_KEYS.get(key, _label_sort_key),
                  reverse=reverse)


def build_problem_rows(problems, saved_colors) -> list[dict]:
    """One display row per problem, in ordinal order.

    Prefill color/rgb from ``saved_colors`` (label → {color, rgb}) when
    present, else the PALETTE default (same index rule as translate). A row
    carrying a saved value is marked ``changed`` so it is written back.
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
        })
    rows.sort(key=lambda r: r["ordinal"])
    return rows


def build_team_rows(teams, groups, saved_assignments) -> list[dict]:
    """Team rows for the group tab, sorted by 学号.

    ``group_ids`` prefill from ``saved_assignments`` (team_id → {group_ids});
    ``group_names`` is the comma-joinable display form resolved via ``groups``
    (unknown ids fall back to the id itself). Read-only org columns come from
    the PTA roster.
    """
    rows: list[dict] = []
    for t in teams or []:
        tid = str(t["id"])
        gids = list(((saved_assignments or {}).get(tid) or {}).get("group_ids") or [])
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
        })
    return sort_teams_by_label(rows)


async def fetch_setup_data(client) -> tuple[list[dict], list[dict]]:
    """One-shot fetch of (problems, teams) from PTA."""
    problems = await client.fetch_problems()
    teams = await client.fetch_teams()
    return problems, teams


def load_manual_setup(manual_dir) -> dict:
    """Read the three ``setup-*.json`` files if present.

    Returns ``{"problems": {...}, "groups": {...}, "teams": {...}}`` with empty
    dicts for missing/short files (a malformed JSON file is ignored — the
    server's ``config.load_manual`` performs the strict validation).
    """
    out = {"problems": {}, "groups": {}, "teams": {}}
    manual_dir = Path(manual_dir)
    for key, fname in (("problems", SETUP_PROBLEMS_FILE),
                       ("groups", SETUP_GROUPS_FILE),
                       ("teams", SETUP_TEAMS_FILE)):
        f = manual_dir / fname
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        section = data.get(key) if isinstance(data, dict) else None
        if isinstance(section, dict):
            out[key] = section
    return out


def write_manual_setup(manual_dir, problems_colors, groups, assignments) -> None:
    """Write the three setup files (UTF-8, no ASCII escaping, indent 2).

    ``assignments`` maps team_id → {group_ids: [...]}; the caller omits teams
    with an empty assignment.
    """
    manual_dir = Path(manual_dir)
    manual_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        SETUP_PROBLEMS_FILE: {"problems": problems_colors},
        SETUP_GROUPS_FILE: {"groups": groups},
        SETUP_TEAMS_FILE: {"teams": assignments},
    }
    for fname, payload in payloads.items():
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        (manual_dir / fname).write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# networking (shared by main() and the GUI refetch button)
# --------------------------------------------------------------------------- #

def _client(settings: Settings) -> PtaClient:
    return PtaClient("https://pintia.cn", settings.contest_id,
                     settings.pta_cookie,
                     requests_per_second=settings.pta_requests_per_second)


def _fetch_remote(settings: Settings) -> tuple[list[dict], list[dict]]:
    async def run():
        client = _client(settings)
        try:
            return await fetch_setup_data(client)
        finally:
            await client.aclose()
    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# GUI (tkinter imported lazily; never needed for tests)
# --------------------------------------------------------------------------- #

def launch_gui(settings: Settings, manual_dir, problems, teams, saved) -> None:
    """Build and run the setup window. Blocking (tkinter mainloop)."""
    import tkinter as tk
    from tkinter import colorchooser, messagebox, simpledialog, ttk

    manual_dir = Path(manual_dir)

    problem_rows = build_problem_rows(problems, saved.get("problems") or {})
    group_defs = {str(gid): {"name": str((g or {}).get("name") or "")}
                  for gid, g in (saved.get("groups") or {}).items()}
    team_rows = build_team_rows(teams, group_defs, saved.get("teams") or {})
    assignments = {r["id"]: set(r["group_ids"]) for r in team_rows}

    state = {"dirty": False}
    ui = {"combo_map": {}}

    root = tk.Tk()
    root.title("pta2ccs setup — 首次运行配置")
    root.geometry("1000x640")

    def mark_dirty() -> None:
        state["dirty"] = True
        root.title("pta2ccs setup — 首次运行配置 *")

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
    tree = ttk.Treeview(pframe, columns=("ordinal", "label", "name", "color"),
                        show="headings", height=18)
    for col, text, width in (("ordinal", "#", 40), ("label", "题号", 100),
                             ("name", "名称", 320), ("color", "颜色", 200)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w")
    tree.pack(side="left", fill="both", expand=True)

    def refresh_problems() -> None:
        tree.delete(*tree.get_children())
        for i, row in enumerate(problem_rows):
            tag = f"pcol{i}"
            tree.tag_configure(tag, foreground=row["rgb"])
            star = "*" if row["changed"] else " "
            tree.insert("", "end", iid=str(i), tags=(tag,),
                        values=(row["ordinal"], row["label"], star + row["name"],
                                "■ " + row["color"]))

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

    # ---------------- Tab 2: team groups ----------------
    tframe = ttk.Frame(notebook)
    left = ttk.Frame(tframe)
    left.pack(side="left", fill="both", expand=True)
    right = ttk.Frame(tframe, width=300)
    right.pack(side="right", fill="y", padx=8, pady=8)

    ttree = ttk.Treeview(
        left, show="headings", selectmode="extended",
        columns=("ordinal", "label", "name", "org", "groups"))
    for col, text, width in (("ordinal", "#", 40), ("label", "学号", 110),
                             ("name", "姓名", 120), ("org", "学校", 160),
                             ("groups", "分组", 180)):
        ttree.heading(col, text=text)
        ttree.column(col, width=width, anchor="w")
    ttree.pack(side="left", fill="both", expand=True)

    # sortable header columns (Treeview #2..#5); "#" (ordinal) is a plain
    # position counter, not a sort target. Clicking a heading sorts asc,
    # clicking again toggles to desc; clicking a different column resets
    # to that column ascending. Arrows ▲/▼ mark the active column.
    _HEADER_KEYS = ("label", "name", "org", "groups")
    _HEADER_NAMES = {"label": "学号", "name": "姓名", "org": "学校", "groups": "分组"}
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
        col = ttree.identify_column(event.x)  # "#2".."#5"
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
                                 r["organization_name"], names))

    ttk.Label(right, text="已定义分组").pack(anchor="w")
    gtree = ttk.Treeview(right, columns=("id", "name"), show="headings",
                         height=8, selectmode="browse")
    for col, text, width in (("id", "ID", 50), ("name", "名称", 200)):
        gtree.heading(col, text=text)
        gtree.column(col, width=width, anchor="w")
    gtree.pack(fill="x", pady=4)

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

    # ---------------- bottom bar buttons ----------------
    def refetch() -> None:
        nonlocal problem_rows, team_rows
        if state["dirty"] and not messagebox.askyesno(
                "重新拉取数据", "有未保存的修改，重新拉取将丢弃它们（颜色除外）。继续？",
                parent=root):
            return
        try:
            problems2, teams2 = _fetch_remote(settings)
        except PtaAuthError:
            messagebox.showerror("错误", "cookie 失效，请更新 config.json 的 pta_cookie",
                                 parent=root)
            return
        except Exception as e:  # noqa: BLE001 - surface any fetch failure in-GUI
            messagebox.showerror("错误", f"拉取失败：{e}", parent=root)
            return
        old_colors = {r["label"]: {"color": r["color"], "rgb": r["rgb"]}
                      for r in problem_rows if r["changed"]}
        problem_rows = build_problem_rows(problems2, old_colors)
        saved_teams = {tid: {"group_ids": sorted(gs)}
                       for tid, gs in assignments.items() if gs}
        team_rows = build_team_rows(teams2, group_defs, saved_teams)
        for r in team_rows:
            assignments.setdefault(r["id"], set(r["group_ids"]))
        # refetch rebuilds team_rows in 学号 order — reset the view sort
        sort_state["col"] = None
        sort_state["reverse"] = False
        set_team_headings()
        refresh_problems()
        refresh_teams()
        refresh_groups()

    def save() -> None:
        problems_colors = {
            r["label"]: {"color": r["color"], "rgb": r["rgb"]}
            for r in problem_rows if r["changed"]}
        groups_out = {gid: {"name": g["name"]} for gid, g in group_defs.items()}
        teams_out = {tid: {"group_ids": sorted(gs)}
                     for tid, gs in assignments.items() if gs}
        write_manual_setup(manual_dir, problems_colors, groups_out, teams_out)
        state["dirty"] = False
        root.title("pta2ccs setup — 首次运行配置")
        messagebox.showinfo("保存到 manual", f"已写入 {manual_dir}", parent=root)

    def on_close() -> None:
        if state["dirty"] and not messagebox.askyesno(
                "退出", "有未保存的修改，确定退出？", parent=root):
            return
        root.destroy()

    ttk.Button(bar, text="↺ 重新拉取数据", command=refetch).pack(
        side="left", padx=4, pady=4)
    ttk.Button(bar, text="保存到 manual", command=save).pack(
        side="left", padx=4, pady=4)
    ttk.Button(bar, text="退出", command=on_close).pack(
        side="right", padx=4, pady=4)

    # ensure sizes are laid out before the initial refresh so the Treeviews
    # compute their geometry against the real (non-zero) parent
    root.update_idletasks()
    refresh_problems()
    refresh_groups()
    refresh_teams()
    set_team_headings()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main() -> None:
    settings = load_settings(Path("config.json"))
    saved = load_manual_setup(settings.manual_dir)
    try:
        problems, teams = _fetch_remote(settings)
    except PtaAuthError:
        print("cookie 失效，请更新 config.json 的 pta_cookie", file=sys.stderr)
        raise SystemExit(1)
    launch_gui(settings, settings.manual_dir, problems, teams, saved)


if __name__ == "__main__":
    main()
