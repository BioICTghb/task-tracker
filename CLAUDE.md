# CLAUDE.md — Biopower Task Tracker

Project context for Claude Code, committed so it loads on any machine that clones this
repo. For detailed maintenance recipes, invoke the **`task-tracker`** skill
(`.claude/skills/task-tracker/SKILL.md`).

## What this is
An offline-first **Flask PWA** for an industrial site: managers publish a task list;
operators document each job with before / progress / after photos (no typing on the
floor). SQLite database, PWA service worker + IndexedDB offline queue, Bootstrap UI.
Public URL: **https://biopower-tasks.org**.

## Where it runs
**Production is a dedicated Ubuntu VM** (Proxmox), app + Cloudflare tunnel as systemd
services (`tasktracker`, `cloudflared`), auto-restart + boot-start. The old Windows-laptop
hosting is retired. Deploy path on the VM: `/home/sebastien/task-tracker/task-tracker`.

## Repo layout
- App code lives in the **`task-tracker/`** subfolder of this repo (so a clone is
  `<clone>/task-tracker/app.py`, etc.).
- Remote: `github.com/BioICTghb/task-tracker`, branch `main`. Commit author:
  `sebastien.poncelet@biopower.be`; end commit messages with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Working on it (any PC)
1. Edit code in `task-tracker/` in your clone.
2. **Test before committing** with an isolated temp DB + Flask test client — never touch
   live data. Seed login: `manager` / `biopower`.
   ```python
   import os, sys, tempfile
   sys.path.insert(0, "<path-to>/task-tracker")
   tmp = tempfile.mkdtemp()
   os.environ.update({"DB_BACKEND":"sqlite","SQLITE_PATH":os.path.join(tmp,"t.db"),
     "UPLOAD_ROOT":os.path.join(tmp,"up"),"SECRET_KEY":"t","SESSION_COOKIE_SECURE":"0","FLASK_DEBUG":"0"})
   import app as m; app=m.app; import db as d
   c=app.test_client(); c.post("/login",data={"username":"manager","password":"biopower"},follow_redirects=True)
   ```
   `node --check` any inline template JS.
3. Commit to `main` and push.
4. **Deploy** on the VM: `cd ~/task-tracker && git pull && sudo systemctl restart tasktracker`.
5. **Never commit**: `.env`, `dev.db`, `uploads/`, `cloudflared.exe`, logs, backups (gitignored).
6. The service-worker cache name is now stamped automatically from a hash of the static
   files (`asset_version()`), so there is no `tt-shell-vN` to bump. Still bump
   `APP_VERSION` in `app.py` on a release.

> Note (primary laptop only): historically this repo lives in a OneDrive "Claude Code"
> folder with a separate live copy at `C:\Users\sebastien.poncelet\TaskTracker` (a
> OneDrive-file-locking workaround). On any other PC just edit the clone directly.

## Live data is on the VM
The database and photos live on the VM, not in the repo. Data edits (delete/reopen tasks,
fix photos, timestamps) must run **on the VM** (`/home/sebastien/task-tracker/task-tracker`,
via SSH) or through the app's in-UI **Delete** button — editing any local `dev.db` does
not affect the live site.

## Domain rules (don't break)
- **Manual timestamps win**: editing a time on `/manage/tasks` sets `started_manual` /
  `completed_manual`; photo uploads must not overwrite a manually-set time.
- **Duration** counts only the 08:00–16:00 shift: single-day = actual elapsed (cap 8h),
  multi-day = per-day shift window; on-hold time is subtracted (shift-bounded).
- **Daily tasks**: a `DAILY …` published line (or the Make-daily toggle) = a hidden
  `is_daily` template; each day an instance `DAILY - <date> - …` (`daily_of`) is generated.
  Operators + edit list show only today's; masters under the "Daily masters" filter.
  **Roll-over** (`sweep_stale_dailies()`, run from `ensure_todays_dailies()` so every
  page load does it — no cron): once a daily's day has passed it can no longer be
  worked, so a *started* one is completed at **23:59:59 of its own day** (not "now",
  which would date the work to whenever a page happened to load) and flagged
  `auto_closed=1`; a *never-started* one is deleted, guarded on having no photos.
  On-hold instances are skipped — a hold is a manager's decision. `auto_closed` is
  deliberately invisible in the UI: these count as ordinary completions.
  The day boundary is the **working** day, not midnight: night shifts run past 00:00, so
  yesterday's *started* dailies stay in the operator and edit lists and are left alone
  by the sweep until `DAILY_GRACE_HOUR` (12:00) — see `daily_grace_day()`.
- **Extra** (`is_extra`, cyan) / **Rent** (`is_rent`, blue; button gated to `RENT_USERNAMES`)
  are operator-created jobs. **On-hold** hides/blocks tasks for operators. **Report** is
  keyed on completion date, has an in-progress page with photo cards, and a status pie.
  The **"Still in progress"** list is deliberately *not* date-filtered — `status='started'`
  is a current state, so it shows every unfinished job whenever it began (oldest first,
  with an "Open for" age, ≥7 days in red). Don't re-add a date range to `fetch_started()`.
- Roles: `manager` + `developer` are manager-level (`MANAGER_ROLES`); `technician` = operator;
  `hr` sees **only** `/hr/rates`. `developer` deliberately has access to everything.
  Wages are limited to `RATE_ROLES` (`hr` + `developer`) — a plain `manager` cannot see
  them, and rates live in their own `pay_rates` table so they never ride along in
  ordinary user queries or appear on any other page. Nav is gated on the injected
  `is_manager` / `can_edit_rates`, never on `role != 'technician'` (that showed HR the
  operational pages).
  `ROLE_LABELS` is the vocabulary — label from it, never `'Manager' if role=='manager'
  else 'Operator'`, which silently demoted developers in the UI. Roles are assignable on
  create and changeable at `/admin/users/<id>/role`; guards refuse to strip your own
  manager access or the last manager-level account (counted across `MANAGER_ROLES`).
- **Shifts page** (`/shifts`, manager-level): who is clocked in right now, shifts in a
  date range keyed on the day the shift *started* (so a night shift stays on its own
  day), hours per operator, and the rostered hours beside the worked ones. Auto-closed
  shifts are badged so those times are read as approximate. "On shift right now" is
  queried separately from the date range — a night shift begun yesterday is still on
  the floor today. `shift_rows()` feeds both this page and the report's **Shifts worked**
  section (shared partial `_shift_table.html`, placed ahead of the photos).
- **@-mention** (`manager.html` publish box): typing `@` opens an operator autocomplete
  anchored at the caret (a hidden mirror `div` computes the `@`'s pixel position — textareas
  expose no caret coords). Filters as you type; ↑/↓ navigate, Tab/Enter or click select,
  Esc/click-outside close. The list is fed by `active_operators()`.
- **Assignment**: the `@` picker inserts only the operator's *name* (the `@` is just the
  trigger — published jobs read cleaner without it). `parse_assignees()` then matches operator
  names in the line **whole-word, longest first**, so `Damian M` is never taken from inside
  `Damian Meczykowski`; a hand-typed `@Name` still parses. A job can be **shared** ("… Ivan
  Dymkar & Taras Dymkar") — all matches are stored in `tasks.assigned_to` as a `", "` list
  (`ASSIGN_SEP`), read back with `assignees_of()`. `/api/tasks` keeps on-hold last and floats
  the viewer's own jobs up **in Python** (a shared list has no portable ORDER BY); those get a
  "FOR YOU" badge + red edge (`.task-mine`). No assignee name is repeated in a badge — it's
  already in the title. Daily instances inherit the template's assignees; editing a title
  re-parses them.
  > Caveat by design: any line containing an operator's full display name assigns them, even
  > if the picker wasn't used. Full names rarely appear incidentally, and a line that does
  > name someone in full is fair to treat as theirs.
- **Shift clock** (operator list, one button): `shifts(user_id, started_at, ended_at,
  auto_closed, client_key)`; open shift = `ended_at IS NULL`. `POST /api/shift` takes an
  explicit `action` (`start`/`end`), never a toggle, plus the phone's tap `timestamp` and
  a `client_key` so a punch replayed from the offline queue can't create a second shift.
  Punches ride the **photo outbox** (`kind:"shift"`, drained in order by `sync.js`), so
  clocking in with no signal records the tap time. **Auto clock-out**: a shift only
  counts as forgotten once `SHIFT_CLOCKOUT_WINDOW_HOURS` (16) have passed — not 12: a
  night plus handover runs past 12 h door to door, and a 12 h trigger rejected those
  operators' clock-outs. `SHIFT_MAX_HOURS` (12) is only the cap on the *recorded* length.
  Rostered hours are *paid*
  hours and people work past them, so they must never be a cutoff (that closed Tomasz
  at 13:26 while he was still working). When a forgotten shift is closed, its end is
  *guessed* from `roster_hours()` (`7,6` / `11,6` / `DRIVE` = 7.6; anything else = 12 h)
  and flagged `auto_closed=1`; a real clock-out inside the 12 h window — including one
  that synced late — overwrites the guess. Real clock-outs are capped at 12 h, not at
  the roster. `reopen_shifts_closed_early()` repairs old-rule records on startup.
  Swept lazily from the shift endpoints, no cron.
- **Schedule columns** live in the DB (`schedule_columns`: `position`, `label`, `user_id`,
  `retired`) and are edited by the manager on `/schedule`. `SCHEDULE_OPERATORS` is now only
  the **seed** for the first run — never read it at runtime. A column pushes shifts to the
  account in `user_id`; `col_for_user()` replaces the old name-guessing, which handed people
  each other's roster whenever staffing changed and silently gave new joiners nothing.
  **`position` is what `schedule_cells.col_index` means, so never renumber**: columns are
  added at the end only, and only the last one can be deleted (with its cells). Take someone
  off the roster with `retired` instead — the column and its history stay, greyed, and it
  drops out of "who's on shift". One account per column (relinking clears the other).
- **Schedule week** is **Mon–Sun** (`SCHEDULE_ROWS = 7`); everything derives from that
  constant, including the grid rows, `/api/my-schedule`, and the save validation, so
  don't hard-code 5 or 7 anywhere. Weeks saved before weekends existed simply have no
  Sat/Sun rows; re-saving one writes them as blank without flagging a change. The
  operator's widget lays the week out in a 5-column grid so Sat/Sun sit under Mon–Fri.
- **Shift colours** live in the DB (`shift_colors`: `hex`, `label`) and are edited on
  `/schedule`; `SCHEDULE_SHIFT_COLORS` is only the seed. `schedule_shift_label(hex, colors)`
  matches by *nearest* colour within tolerance (the same colour pastes slightly differently
  between files), so pass the DB map — green Early, amber Day, blue Late, red Night
  (`#ff0000` and `#c00000` both anchor Night), pale-yellow Drive. A colour that isn't listed
  is not a shift (holiday/ADV/JV) and stays out of the report summary.
  `SCHEDULE_TEXT_LABELS` rewrites cell text for display only (`DRIJFVEER` → `pro-rate`).
- **The roster comes from the master Excel**, which is the single source of truth: the
  `/schedule` grid is **read-only** and there is deliberately no save-the-grid endpoint — a
  second place to edit is how the two copies drift apart. `roster_import.py` runs nightly
  from the operator's crontab (no root: it reads the share over SMB, no mount) and from the
  **Import now** button; `~/.roster.env` (outside the repo, `0600`) holds the share
  credentials and the `{year}` path template, which is resolved per *imported date* so the
  January rollover and a week spanning New Year both work. The file still carries **2022**
  dates, so only month+day are read. Columns are matched by **name** against
  `schedule_columns.label`, never by position; unmatched names are reported and skipped.
  A **safety gate** refuses the whole run if fewer than `MIN_NAMES_MATCHED` names match or
  if it would overwrite more than `MAX_OVERWRITE_FRACTION` of the shifts *already* stored
  (counting overwrites, not changes — the first import legitimately fills everything).
  Every run, including a refusal, is recorded in `import_runs` and shown on `/schedule`.
  Writes go through `schedule_io.apply_week()`, shared with nothing else, so the
  change-detection that drives the operator banner has one implementation.
- **Weekly HR hours export** (`hr_export.py`, Tuesday cron): rebuilds HR's "Weekly Hours
  Tracker" workbook for the week that just ended, from the **roster** — not the shift
  clock, which too few operators use to pay from. Delivery is a **folder on the share**
  (`L:\3_ebo_production\5_hr\Weekly hour exports`, `HR_SHARE_DIR`), reusing the roster's
  SMB credentials in `~/.roster.env`; email exists (`--mail`) but is off by default and
  not the chosen route. A week **already in that folder is never overwritten** — HR type
  overtime into column D by hand, so a re-run writes `… (rebuilt <date>).xlsx` beside it
  and says so. The local copy under `~/hr-exports` is written *first* and always kept, so
  a dead share never costs the week's figures. Column D (overtime ×1.5) is deliberately
  left empty for HR; a paid absence goes to column E at `ABSENCE_DAY_HOURS` with its code
  in column K. A missing rate leaves `Rate` **blank, not 0**, and warns by name.
- **Shift-change warning**: `schedule_cells.updated_at` advances only on a real change
  (text or color), `users.schedule_seen_at` records acknowledgement;
  `/api/schedule-changes` → sticky banner on the operator list, cleared by
  `/api/schedule-ack`. Saving the grid returns `notified` so the manager sees who was
  flagged. `schedule_seen_at` is baselined **when the account is created** (plus a
  startup backfill for existing rows) — do not go back to baselining on first view:
  an operator who hadn't opened the app yet then had their first real change swallowed.
- **Asset version**: `asset_version()` hashes static css/js; it stamps the SW cache name
  (`tt-shell-<hash>`, no manual `vN` bump) and `?v=` on the css/js links. The SW `SHELL`
  entries carry the same `?v=` so precached URLs match what pages request.

## Hosting / tunnel
Tunnel `tasktracker` (id `59f22012-df09-427d-9598-10c75ac735e5`) → `127.0.0.1:5000`.
`1033`/`530` = tunnel down; `502` = app down. Health: `/healthz` → `{"ok":true}`.
Deploy/migration runbook: `task-tracker/deploy/MIGRATION.md`.
