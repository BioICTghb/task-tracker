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
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

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
- **Extra** (`is_extra`, cyan) / **Rent** (`is_rent`, blue; button gated to `RENT_USERNAMES`)
  are operator-created jobs. **On-hold** hides/blocks tasks for operators. **Report** is
  keyed on completion date, has an in-progress page with photo cards, and a status pie.
- Roles: `manager` + `developer` are manager-level (`MANAGER_ROLES`); `technician` = operator.
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
- **Shift colours**: `SCHEDULE_SHIFT_COLORS` maps a cell colour to a shift by *nearest*
  match (Excel pastes vary): green Early, amber Day, blue **Late**, red **Night**
  (both `#ff0000` and `#c00000` anchor Night — they are >tolerance apart), pale-yellow
  Drive. Any other colour is not a shift and is left out of the report summary.
  `SCHEDULE_TEXT_LABELS` rewrites cell text for display only (`DRIJFVEER` → `pro-rate`);
  the grid keeps whatever the manager pasted.
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
