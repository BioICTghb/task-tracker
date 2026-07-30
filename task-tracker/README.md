# Task Tracker

A minimal, offline-first task tracking PWA. Replaces a WhatsApp task list.

- **Managers** paste a list of tasks and click *Publish*. Type **`@`** in the paste box to assign an operator — an autocomplete list appears at the cursor, filters as you type, and is picked with the arrow keys + Tab/Enter or a click. Assigned jobs sort first for that operator and are flagged **FOR YOU**.
- **Shift swaps are flagged**: a change to an operator's upcoming shift raises a warning on their task list that stays until they acknowledge it.
- **Reports/photos** can be shown inline, printed, or downloaded as a ZIP (whole day or a single job).
- **Technicians** open a task, take a **before** photo and an **after** photo. Nothing else to type.
- Works **fully offline** — photos and timestamps are stored on the phone and sync automatically when the network returns.

---

## 1. Project structure

```
task-tracker/
├── app.py                  # Flask app + all routes / REST API
├── config.py               # Reads .env (DB creds, paths)
├── db.py                   # MySQL connection helper (PyMySQL)
├── schema.sql              # MySQL schema (tasks, task_photos)
├── requirements.txt
├── .env.example            # Copy to .env and edit
├── uploads/                # Photos: uploads/YYYY/MM/DD/TASKID_before.jpg
├── templates/
│   ├── base.html           # Shell + status banner + script includes
│   ├── manager.html        # Paste box + Publish
│   ├── technician_list.html# "My Tasks" list
│   ├── technician_task.html# Single task: BEFORE / AFTER buttons
│   └── report.html         # Printable report by date
└── static/
    ├── manifest.json       # PWA manifest
    ├── service-worker.js   # Offline cache + background sync
    ├── css/style.css       # Big touch-friendly buttons
    ├── icons/icon-192.svg, icon-512.svg
    └── js/
        ├── db.js           # IndexedDB (tasks cache + photo outbox)
        ├── sync.js         # Drains outbox -> server
        └── app.js          # SW registration + auto-sync triggers
```

## 2. REST API

| Method | Path                         | Purpose                                            |
|--------|------------------------------|----------------------------------------------------|
| GET    | `/api/tasks`                 | List active tasks (cached client-side)             |
| GET    | `/api/tasks/<id>`            | Single task                                        |
| POST   | `/api/tasks`                 | Bulk create. Body `{"text":"line1\nline2"}`        |
| POST   | `/api/tasks/<id>/photo`      | multipart: `photo`, `photo_type`, `timestamp`      |
| GET    | `/uploads/<path>`            | Serve a stored photo                               |
| GET    | `/healthz`                   | Health check                                       |

Posting a `before` photo moves the task to **started**; an `after` photo moves it to **completed**. Timestamps come from the client (capture time) so offline captures keep their real time.

## 3. Local development (Windows)

```powershell
cd task-tracker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Option A — zero-setup (SQLite), no MySQL needed.** Good for trying it out:

```powershell
# .env with DB_BACKEND=sqlite (schema is created automatically on first run)
"DB_BACKEND=sqlite`nSECRET_KEY=dev`nFLASK_DEBUG=1" | Out-File -Encoding utf8 .env
python app.py            # http://localhost:5000
```

**Option B — MySQL (matches production):**

```powershell
mysql -u root -p < schema.sql       # MySQL must be installed/running
copy .env.example .env              # edit DB_USER / DB_PASSWORD (leave DB_BACKEND unset/mysql)
python app.py
```

> The SQLite backend exists only for local convenience. Production uses MySQL
> per the schema in `schema.sql`. Both share identical app code; the backend is
> chosen by the `DB_BACKEND` env var.

Open `http://localhost:5000` (manager) and `http://localhost:5000/tech` (technician).

> **Note on offline/PWA:** Service workers require **HTTPS** *or* `localhost`.
> On `localhost` everything works. On the company network you must serve over
> HTTPS (see below) for install + offline to work on phones.

## 4. Deployment on an internal server

Recommended: **Linux box on the company LAN**, gunicorn behind nginx with a
self-signed (or internal-CA) TLS certificate.

### 4a. App service (gunicorn)

```bash
cd /opt/task-tracker
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# create /opt/task-tracker/.env with production DB creds + a real SECRET_KEY
gunicorn -w 4 -b 127.0.0.1:5000 app:app
```

systemd unit `/etc/systemd/system/tasktracker.service`:

```ini
[Unit]
Description=Task Tracker
After=network.target mysql.service

[Service]
WorkingDirectory=/opt/task-tracker
EnvironmentFile=/opt/task-tracker/.env
ExecStart=/opt/task-tracker/.venv/bin/gunicorn -w 4 -b 127.0.0.1:5000 app:app
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tasktracker
```

### 4b. nginx + HTTPS (required for PWA on phones)

```nginx
server {
    listen 443 ssl;
    server_name tasks.local;          # add this name to internal DNS / hosts

    ssl_certificate     /etc/ssl/tasks/fullchain.pem;
    ssl_certificate_key /etc/ssl/tasks/privkey.pem;

    client_max_body_size 20m;          # phone photos

    location /uploads/ {
        alias /opt/task-tracker/uploads/;
    }
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

Generate an internal cert (simplest: self-signed; better: your company CA):

```bash
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout /etc/ssl/tasks/privkey.pem -out /etc/ssl/tasks/fullchain.pem \
  -subj "/CN=tasks.local"
```

Install the CA/cert on the technicians' phones so the PWA installs cleanly.

### 4c. MySQL

```bash
sudo mysql < schema.sql
sudo mysql -e "CREATE USER 'tasktracker'@'localhost' IDENTIFIED BY 'STRONGPASS';
               GRANT ALL ON task_tracker.* TO 'tasktracker'@'localhost'; FLUSH PRIVILEGES;"
```

Back up `uploads/` and the `task_tracker` database regularly — the photos are
the audit record.

### Windows server alternative

Run the same app with [`waitress`](https://github.com/Pylons/waitress)
(`pip install waitress`, `waitress-serve --listen=127.0.0.1:5000 app:app`)
behind IIS or nginx-for-Windows as the HTTPS reverse proxy.

## 5. Architecture (how offline works)

```
 Phone (PWA)                         Server (Flask)
 ┌──────────────────────────┐        ┌─────────────────────┐
 │ Pages (Bootstrap)        │        │ /api/tasks          │
 │   manager / list / task  │ <───── │ /api/tasks/<id>     │
 │                          │ HTTPS  │ /api/.../photo      │
 │ Service Worker           │ ─────> │                     │
 │   • caches app shell     │        │ MySQL (tasks,       │
 │   • network-first /api   │        │        task_photos) │
 │   • background sync      │        │ uploads/YYYY/MM/DD/ │
 │ IndexedDB                │        └─────────────────────┘
 │   • tasks  (cache)       │
 │   • outbox (photos)      │
 └──────────────────────────┘
```

**Capture is never lost.** When a technician takes a photo, the app:

1. Writes the photo blob + timestamp to the **IndexedDB outbox** *first*.
2. Optimistically updates the local task status and UI.
3. Tries to upload immediately.

If there's no connection, the item simply stays in the outbox. It is uploaded
later by any of three independent triggers (belt-and-suspenders for flaky WiFi):

- the browser `online` event,
- a 30-second poll while a page is open,
- the service worker **Background Sync** (`sync-photos`) — fires even if the
  page was closed, as soon as the OS reports connectivity.

Each outbox item is deleted only after the server returns `2xx`, and the photo
endpoint is idempotent per `(task, before|after)`, so retries are safe.

The status banner shows **ONLINE / OFFLINE / SYNCING** at all times.

### Design priorities (as required)

- **Reliability over features** — local-first write, idempotent upload, triple sync trigger.
- **Speed over completeness** — cached task list renders instantly; capture is two taps.
- **Offline over real-time** — no websockets; eventual sync.
- **Simplicity over configurability** — no priorities, comments, assignments, QR, or kanban.
```
