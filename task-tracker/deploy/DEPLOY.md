# Deploying Task Tracker on Ubuntu with a Cloudflare Tunnel

Always-on Linux server, SQLite, gunicorn behind a systemd service, and a fixed
HTTPS link via a named Cloudflare Tunnel. No open ports, no manual TLS certs.

---

## 0. What you set up yourself (once)

1. **A free Cloudflare account** — done.
2. **A domain in that account** — done: `biopower-tasks.org` (a dedicated domain,
   so no company-DNS changes needed).
3. Hostname for the app: **`biopower-tasks.org`** (the domain root).

Everything below is on the Ubuntu box.

---

## 1. Server prep

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip sqlite3 git
sudo adduser --system --group --home /opt/task-tracker tasktracker
```

## 2. Get the code + your data

```bash
# Code from GitHub
sudo -u tasktracker git clone https://github.com/BioICTghb/task-tracker.git /tmp/tt
sudo cp -r /tmp/tt/task-tracker/. /opt/task-tracker/      # app lives in the subfolder
```

Copy your **data** from the Windows machine (the DB and photos are NOT in git).
Stop `start.bat` first so `dev.db` isn't mid-write, then transfer:
- `dev.db`  ->  `/opt/task-tracker/dev.db`
- the whole `uploads/` folder  ->  `/opt/task-tracker/uploads/`

```bash
sudo chown -R tasktracker:tasktracker /opt/task-tracker
```

## 3. Python environment + settings

```bash
cd /opt/task-tracker
sudo -u tasktracker python3 -m venv .venv
sudo -u tasktracker .venv/bin/pip install -r requirements.txt gunicorn
sudo -u tasktracker cp deploy/env.production.example .env
sudo -u tasktracker nano .env      # set a real SECRET_KEY (command is in the file)
```

The app auto-migrates the database on first start (adds the newer tables/columns);
your tasks and photos are preserved.

## 4. Run it 24/7 (systemd)

```bash
sudo cp deploy/tasktracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tasktracker
systemctl status tasktracker        # should be "active (running)"
curl -s localhost:5000/healthz      # {"ok": true}
```

`Restart=always` + `enable` = survives crashes and reboots.

## 5. The fixed link (Cloudflare Tunnel)

```bash
# install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared

cloudflared tunnel login                       # opens a link; approve in your Cloudflare account
cloudflared tunnel create tasktracker          # prints a Tunnel ID + writes a credentials .json
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<TUNNEL_ID>.json /etc/cloudflared/
sudo cp deploy/cloudflared-config.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml          # set <TUNNEL_ID> (hostname already = biopower-tasks.org)

cloudflared tunnel route dns tasktracker biopower-tasks.org   # creates the DNS record
sudo cloudflared service install               # runs the tunnel 24/7
```

Your permanent link is now **https://biopower-tasks.org** — valid cert, no open
ports, survives reboots.

## 6. First login + operators

1. Open the link, log in with the seeded account: **`manager` / `biopower`**.
2. Go to **Operators** and:
   - change the manager password (Reset),
   - add your real managers and technicians.
3. Tell technicians the link; they log in once on their phone (remembered ~30 days)
   and "Add to Home Screen".

## 7. Backups (don't skip — photos are the audit record)

```bash
sudo cp deploy/backup.sh /usr/local/bin/tasktracker-backup
sudo chmod +x /usr/local/bin/tasktracker-backup
echo "30 2 * * * root /usr/local/bin/tasktracker-backup" | sudo tee /etc/cron.d/tasktracker-backup
```
Backs up the DB + photos nightly to `/var/backups/tasktracker`, keeping 30 days.

## 8. Updating the app later

```bash
cd /opt/task-tracker
sudo -u tasktracker git -C /tmp/tt pull          # or re-clone
sudo cp -r /tmp/tt/task-tracker/. /opt/task-tracker/
sudo chown -R tasktracker:tasktracker /opt/task-tracker
sudo systemctl restart tasktracker
```
(`.env`, `dev.db`, and `uploads/` are untouched by updates.)

---

### Notes
- Only **outbound** internet is needed — the tunnel dials out to Cloudflare.
- The app's own login protects it, so Cloudflare Access is optional.
- SQLite runs in WAL mode with a busy-timeout, fine for this number of users.
  If you ever outgrow it, switch `DB_BACKEND=mysql` (see `../schema.sql`).
