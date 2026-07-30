---
name: task-tracker
description: >-
  Operate and maintain the Biopower Task Tracker — an offline-first Flask PWA where
  managers publish tasks and operators document jobs with before/after/progress photos.
  Use for routine data edits (delete/reopen tasks, fix photos/timestamps), feature work,
  the commit-to-GitHub flow, the Cloudflare tunnel/hosting, and deployment. Triggers on
  "task tracker", "biopower-tasks", a job/task number to edit, tunnel/502/1033 issues,
  or the daily/rent/extra/on-hold/report features.
---

# Task Tracker — operating manual

Offline-first Flask PWA (SQLite, PWA service worker, IndexedDB offline queue). Public URL
**https://biopower-tasks.org**. **Production runs on a dedicated Ubuntu VM** (Proxmox, user
`sebastien`, app + tunnel as systemd services) — the laptop is retired (migrated 2026-07).

## Where things live (read this first)

| What | Path |
|---|---|
| **Production app + LIVE data** | VM `/home/sebastien/task-tracker/task-tracker` (`dev.db`, `uploads/`) |
| Local dev copy (edit code here) | `C:\Users\sebastien.poncelet\TaskTracker` |
| Local venv python (for tests) | `C:\Users\sebastien.poncelet\TaskTracker\.venv\Scripts\python.exe` |
| Git repo (commit from here) | `C:\Users\sebastien.poncelet\OneDrive - Biopower NV\Bureaublad\Claude Code` |
| App files inside the repo | `<repo>\task-tracker\...` |
| GitHub remote | `https://github.com/BioICTghb/task-tracker.git` (branch `main`) |

**Golden rules:**
- **Edit code** in the local `TaskTracker` folder (never the OneDrive copy — file-locking).
  Commit by copying changed files into the OneDrive repo, then push.
- **Deploy** = on the VM: `cd ~/task-tracker && git pull && sudo systemctl restart tasktracker`.
- **LIVE DATA is on the VM.** The laptop's `dev.db`/`uploads` are a stale snapshot. Data
  edits (delete/reopen tasks, fix photos) must run on the VM (SSH, or the app's in-UI
  Delete button) — editing the laptop's local `dev.db` no longer affects the live site.

## Running data / DB operations

> The recipe below runs against the **local dev copy** (for tests). For **live** edits,
> run the equivalent on the **VM** (`/home/sebastien/task-tracker/task-tracker`, via SSH,
> using `.venv/bin/python` + `Config`), or use the app's in-UI Delete button. Editing the
> laptop's local `dev.db` no longer changes the live site.

Use the venv python with the app's Config so paths resolve to that instance's data:

```bash
cd "/c/Users/sebastien.poncelet/TaskTracker" && ./.venv/Scripts/python.exe -c "
import sqlite3, os
from config import Config
root=Config.UPLOAD_ROOT
con=sqlite3.connect(Config.SQLITE_PATH); con.row_factory=sqlite3.Row
con.execute('PRAGMA busy_timeout=8000')
# ... work ...
"
```

- `Config.SQLITE_PATH` = the live `dev.db`; `Config.UPLOAD_ROOT` = the live `uploads`.
- **Deleting a task** = delete photo FILES on disk + `task_photos` rows + cached thumbnails
  under `uploads/.thumbs/w*/…/<id>_*` + the `tasks` row. Always show the task first.
- **Removing photos** = delete the files + rows + the matching `.thumbs` cache entries.
- **Never lose field photos.** Back up before overwriting; a past bug (`rm -rf uploads/2026`
  in a test) destroyed real photos — recover via OneDrive Recycle Bin. NEVER run destructive
  filesystem commands against the live `uploads`; test with an **isolated temp UPLOAD_ROOT**.

### Reopening a task (the timestamp rule)
Set `status='open'`. Then: if `started_manual=1`/`completed_manual=1`, **keep** those
timestamps (a manager set them by hand); if `0`, **clear** the corresponding
`started_at`/`completed_at` (+ `started_by`/`completed_by`) since they came from photos.

## Domain rules (don't break these)

- **Manual timestamps win.** Editing a time on `/manage/tasks` sets `started_manual`/
  `completed_manual`; later photo uploads must NOT overwrite a manually-set time.
- **Duration** counts only the **08:00–16:00 shift**. Single-day jobs = actual elapsed,
  capped at 8h. Multi-day jobs = per-day shift window (so nights don't inflate; e.g.
  05/07 14:00 → 07/07 14:00 = 16h). On-hold time is banked shift-bounded and subtracted.
- **Daily tasks**: mark a task DAILY (a `DAILY …` line on publish, or the Make-daily toggle)
  → a hidden `is_daily=1` **template**. Each day `ensure_todays_dailies()` spawns a dated
  instance `DAILY - <date> - …` (`daily_of`=template). Operators + the edit list show only
  today's; masters live under the edit-screen "Daily masters" filter. Green DAILY badge.
- **Extra / Rent**: operator-created jobs. `is_extra` (cyan EXTRA badge) / `is_rent`
  (blue RENT badge). Rent button shows only for on-site users in `RENT_USERNAMES`.
- **On-hold** hides tasks from operators (greyed, non-clickable) and blocks work (409);
  they sort last. **Report** is keyed on completion date, has an in-progress page + photo
  cards, and a status pie (scope = created OR completed that day).
- Roles: `manager` + `developer` are manager-level (`MANAGER_ROLES`); `technician` = operator.

## Verifying changes (always test before committing)

Test against an **isolated temp DB** with Flask's test client — never the live data:

```python
import os, sys, tempfile
sys.path.insert(0, r"C:\Users\sebastien.poncelet\TaskTracker")
tmp=tempfile.mkdtemp()
os.environ.update({"DB_BACKEND":"sqlite","SQLITE_PATH":os.path.join(tmp,"t.db"),
  "UPLOAD_ROOT":os.path.join(tmp,"up"),"SECRET_KEY":"t","SESSION_COOKIE_SECURE":"0","FLASK_DEBUG":"0"})
import app as m; app=m.app; import db as d
c=app.test_client(); c.post("/login",data={"username":"manager","password":"biopower"},follow_redirects=True)
```

`node --check` inline template JS. `sqlite3.connect(...)` a temp DB for schema checks.
Seed manager login: `manager` / `biopower`.

## Commit-to-GitHub flow

Edits are made in the local `TaskTracker` folder; publish them by copying into the repo:

```bash
REPO="/c/Users/sebastien.poncelet/OneDrive - Biopower NV/Bureaublad/Claude Code"
OD="$REPO/task-tracker"; LOCAL="/c/Users/sebastien.poncelet/TaskTracker"
git -C "$REPO" ls-files task-tracker | while read -r f; do rel="${f#task-tracker/}"; if ! cmp -s "$OD/$rel" "$LOCAL/$rel"; then echo "  $rel"; cp "$LOCAL/$rel" "$OD/$rel"; fi; done
git -C "$REPO" add task-tracker
git -C "$REPO" commit -F - <<'EOF'
<subject>

<body>

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
git -C "$REPO" push origin main
```

- Commit author is `sebastien.poncelet@biopower.be` (already configured).
- **Never commit**: `.env`, `dev.db`, `uploads/` photos, `cloudflared.exe`, logs, backups
  (all gitignored — keep it that way).
- **Bump the service-worker cache** `tt-shell-vN` in `static/service-worker.js` whenever a
  **static asset** (css/js/sw) changes, so devices re-cache. Navigations (templates) are
  network-first and don't need a bump.
- **`APP_VERSION`** in `app.py` (footer + `<meta name=version>`); bump on a release.
- Data edits (deletes, reopens) are NOT committed — they're live-DB changes only.
- **Restart the app** to load code/template changes (the user runs it themselves).

## Cloudflare tunnel / hosting (on the VM)

- Tunnel `tasktracker` (id `59f22012-df09-427d-9598-10c75ac735e5`) → `127.0.0.1:5000`.
  DNS `biopower-tasks.org` points to it.
- **Symptoms**: `1033`/`530` = tunnel (cloudflared) down; **`502`/bad gateway** = tunnel up
  but the app on 5000 is down. Health: `https://biopower-tasks.org/healthz` = `{"ok":true}`.
- Both app + tunnel are **systemd services** on the VM (`tasktracker`, `cloudflared`) —
  auto-restart on crash and start on boot, no watchdog hacks. Check with
  `systemctl status tasktracker cloudflared` and `journalctl -u tasktracker -n 50`.

## Deployment (done — running on the VM)

Hosting moved to a dedicated Ubuntu VM (systemd services for app + tunnel; no memory
contention). Runbook: **`task-tracker/deploy/MIGRATION.md`**. Deploy code updates with
`cd ~/task-tracker && git pull && sudo systemctl restart tasktracker`. On Linux the app
runs under **gunicorn** (`app:app`, port 5000), not waitress.

## Reference

Local memory notes: `[[work-from-local-tasktracker]]`, `[[tunnel-autostart-tasktracker]]`.
