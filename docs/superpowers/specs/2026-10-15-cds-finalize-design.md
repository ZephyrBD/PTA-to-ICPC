# Design: contest finalization (`finalized_at`) + CDS download fix + Contest API spec alignment + setup 比赛设置 tab

Date: 2026-10-15
Status: approved (plan reviewed with the user; decisions locked)

## Problem

Two field bugs, one shared root cause:

- **bug A — CDS event-feed download never terminates.** CDS (workspace
  `wlp.CDS-2.7.1412`, :8080) mirrors our event feed over one persistent
  `RESTContestSource` connection. Its `ContestFeed` only `cancel`s its heartbeat
  and `close`s the feed when `ContestState.isDoneUpdating()` becomes true, i.e.
  once it has seen a `state` event with `end_of_updates` (and `finalized`) set.
  Our `/state` never sets `thawed`/`finalized`/`end_of_updates` (always `null`),
  so CDS mirrors `null`s and its feed never closes → the browser download in the
  CDS UI hangs forever.
- **bug B — the ICPC resolver refuses to run without `--test`.** Non-test mode
  waits for `end_of_updates != null` (`waitForContest`) and requires
  `finalized != null` (`ContestState.isFinal()`), else
  `Contest is not over. Use --test...` + `System.exit(1)`.

PTA has **no** "results finalized" concept: the raw `GET /api/problem-sets/{id}`
response exposes only `startAt`, `endAt`, `duration`, `stage` (`NORMAL`),
`archived`, `hide` — none of which flip at contest end (verified live
2026-10-15, contest already ended). PTA auto-judges instantly and has no
post-contest finalize step. So the `finalized` assertion must be derived by us.

### Spec constraints (Contest API 2026-01, verbatim)

- `thawed`: "Required iff `scoreboard_freeze_duration` is present in the contest
  endpoint. Must not be set if frozen is `null`."
- `finalized`: "Must not be set if ended is `null`."
- Strict inequalities "must hold":
  `started < frozen < ended < thawed < end_of_updates` and
  `ended < finalized < end_of_updates`.
- "A contest that has ended, been thawed (or was never frozen) and is finalized
  must not change. Thus, `end_of_updates` can be set once both `finalized` is
  set and `thawed` is set if the contest was frozen."
- Event feed: "the notification for the state endpoint setting
  `end_of_updates` must be the last event in the feed."
- Notification `token` is type `string ?`.

## Decisions (locked with the user)

1. **Auto-derive finalization** (option A): once `now >= ended + 1s` and every
   submission has a resolved verdict, finalize automatically. No manual value
   needed for the E2E run.
2. Times live in **`manual/setup-contest.json`** → `{"contest": {...}}`
   (single source of truth). `config.load_manual` already merges any manual
   file's `contest` section, and `translate()` already reads that section, so
   `config.py` / `config.example.json` / `poller.py` / `app.py` / `Settings`
   are untouched.
3. Three pre-existing contest time overrides are exposed in the GUI too:
   `start_time`, `duration`, `scoreboard_freeze_duration`. Blank = automatic
   (PTA-derived).
4. **No CDS-side change**: no `CDS.war` patch, no `cdsConfig.xml` edit, no
   `?download=1` endpoint. Fixing our `state` output is the whole fix for A.
5. `finalized_at` tri-state: absent/`"auto"`/`null` = auto rule; `"never"` =
   never finalize; ISO-8601 = explicit timestamp.

## Behavior specification

### `_build_state(contest, now, *, finalized_at=None)`

Ordering/strictness is satisfied without future timestamps:

- **auto**: guard `now >= ended + 1s`, `judgements` non-empty and all carry a
  non-null `judgement_type_id`; then `base = now - 1s`,
  `thawed = finalized = base` (set `thawed` only when `frozen` is set),
  `end_of_updates = now`.
- **explicit ISO**: guard `now >= ended`; `T = max(iso, ended + 1s)`;
  `thawed = finalized = T` (thawed only when frozen set); `end_of_updates = T + 1s`.
- **never / not ended / pending verdicts**: everything stays `null`; when the
  contest has ended but some submission is unjudged, log one warning
  (a wrong verdict → not the verdict's own fault: surfaces unknown PTA statuses
  that block auto-finalization).
- No submissions at all → no auto finalization (explicit config is the escape
  hatch), to make vacuous "all judged" impossible.

Invalid `finalized_at` (e.g. garbage ISO) → log warning, behave as auto.

### Store

- `state` events are emitted **last** inside `apply_all` (holds the state diff
  and emits it after object diffs and deletes), so `end_of_updates` is always
  the last line of the batch.
- Once the applied snapshot has `state.end_of_updates` set, `apply_all` is a
  no-op: returns `[]` and does not replace the snapshot (spec: a finalized
  contest must not change). A divergence in incoming data logs a warning
  (expected never).
- `Event.to_json` emits `token` as a **string**; internal counter stays int.

### Spec alignment

| field | before | after | spec |
|---|---|---|---|
| event `type` for the contest | `"contests"` | `"contest"` | `contest` |
| `access.endpoints[].type` for contest | `"contests"` | `"contest"` | `contest` |
| `token` | int | string | `string ?` |
| `?since_token` | int | string (parsed to int; invalid → 400) | string token |
| `problems.time_limit` | RELTIME `"0:00:00.100"` | number `0.1` | number seconds |
| `problems.memory_limit` | KB (`262144`) | MiB (`256`) | integer MiB |

`code_limit` (KiB), `ordinal`, `test_data_count`, `rgb` audited: already correct.

## Setup GUI

New third tab **比赛设置** (`ttk.Notebook`, after 题目颜色 / 队伍分组) with four
labeled entries plus format hints:

- 开始时间 `start_time` — `2026-10-13T16:00:00+08:00` (offset required)
- 时长 `duration` — `51:00:00`
- 封榜时长 `scoreboard_freeze_duration` — `45:00:00`
- 定稿时间 `finalized_at` — radio 自动（留空）/ 指定时间 / 不定稿; ISO entry
  enabled only for 指定时间

Validation is a pure function; save writes `manual/setup-contest.json` and warns
that the server must be restarted (manual is loaded once at boot).

## Verification

Unit tests for every rule above; then live: `/state` strict ordering,
`/problems` types, CDS `/state` mirror, CDS `event-feed` ending with EOF
(exit code 0, not curl timeout 28) as the machine proof of bug A, then a fresh
CDS download feeding the resolver without `--test`.
