# Task Tracker — Migration to a Proxmox Ubuntu VM

A complete, copy-paste runbook for moving Task Tracker off the Windows laptop onto
a dedicated Ubuntu VM on your Proxmox host. When you're done the app runs 24/7 as a
systemd service, the Cloudflare tunnel runs as its own service, and both auto-restart
on crash and on reboot — no laptop, no watchdogs, no memory starvation.

**The public URL (`https://biopower-tasks.org`) does not change.**

---

## 0. What you'll end up with

| Piece            | Where                                   |
|------------------|-----------------------------------------|
| App code         | `/opt/task-tracker`                     |
| Database         | `/opt/task-tracker/dev.db` (SQLite)     |
| Photos           | `/opt/task-tracker/uploads`             |
| App service      | `systemd: tasktracker` (gunicorn :5000) |
| Tunnel service   | `systemd: cloudflared`                  |

Two phases:
- **Phase A — Build the VM** (steps 1–7): set everything up and test it locally. The
  laptop keeps running the live site the whole time, so there's zero downtime yet.
- **Phase B — Cut over** (steps 8–10): copy the latest data, move the tunnel, verify,
  then retire the laptop.

Total hands-on time: ~45–60 min. You can do Phase A whenever; do Phase B when you're
ready to flip the switch (a quiet moment is nice, cutover downtime is ~2–5 min).

> Throughout, lines starting with `#laptop` run in **Windows PowerShell on the laptop**,
> and `$` lines run in the **VM's Ubuntu terminal**.

---

## 1. Create the VM in Proxmox

Create a new VM:

- **OS**: Ubuntu Server **24.04 LTS** (download the ISO, upload to Proxmox storage).
- **RAM**: **2048 MB** (2 GB). 1 GB works but 2 GB gives comfortable headroom for photo
  processing. It'll idle around 300–500 MB.
- **CPU**: **2** cores (vCPU).
- **Disk**: **40 GB** (OS + years of photos; you can grow it later in Proxmox if needed).
- **Network**: bridged (`vmbr0`) so it gets a LAN IP.

Install Ubuntu Server (minimal), create your admin user during install, and **enable
"Install OpenSSH server"** when the installer offers it (you need SSH to copy files in).

After first boot, log in and note the VM's IP address:

```bash
ip -4 addr show | grep inet
```

Write that IP down — call it `VM_IP` below. (Optional but recommended: give the VM a
**static IP / DHCP reservation** so it never changes.)

---

## 2. Base system setup

SSH into the VM from the laptop (PowerShell has `ssh` built in):

```powershell
#laptop
ssh youruser@VM_IP
```

Then on the VM, update and install what we need:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip git sqlite3 curl rsync
```

Create a dedicated service user and the app directory:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin tasktracker
sudo mkdir -p /opt/task-tracker
sudo chown tasktracker:tasktracker /opt/task-tracker
```

---

## 3. Get the app onto the VM

Clone the repo and copy the app into `/opt/task-tracker`. **Note:** the app lives in a
`task-tracker/` subfolder inside the repo, so we copy that subfolder's contents.

```bash
cd /tmp
git clone https://github.com/BioICTghb/task-tracker.git
sudo cp -r /tmp/task-tracker/task-tracker/. /opt/task-tracker/
sudo chown -R tasktracker:tasktracker /opt/task-tracker
```

Create the Python virtualenv and install dependencies:

```bash
cd /opt/task-tracker
sudo -u tasktracker python3 -m venv .venv
sudo -u tasktracker .venv/bin/pip install --upgrade pip
sudo -u tasktracker .venv/bin/pip install -r requirements.txt
```

> If `pillow-heif` fails to build, install the system lib and retry:
> `sudo apt install -y libheif1` then re-run the pip install line.

---

## 4. Configure the environment (`.env`)

Create `/opt/task-tracker/.env`:

```bash
sudo -u tasktracker tee /opt/task-tracker/.env >/dev/null <<'EOF'
DB_BACKEND=sqlite
SQLITE_PATH=/opt/task-tracker/dev.db
UPLOAD_ROOT=/opt/task-tracker/uploads
SESSION_COOKIE_SECURE=1
FLASK_DEBUG=0
SECRET_KEY=PASTE_THE_SAME_KEY_FROM_THE_LAPTOP
EOF
```

**Critical — the SECRET_KEY:** open the laptop's existing env file and copy the
`SECRET_KEY=...` value **verbatim** into the file above (replace `PASTE_THE_SAME_KEY_...`).
Reusing the same key means everyone stays logged in after the move; a new key logs
everyone out.

On the laptop, print it to copy:

```powershell
#laptop
Get-Content "C:\Users\sebastien.poncelet\TaskTracker\.env" | Select-String SECRET_KEY
```

Then fix permissions (the file holds a secret):

```bash
sudo chown tasktracker:tasktracker /opt/task-tracker/.env
sudo chmod 600 /opt/task-tracker/.env
```

---

## 5. Copy your data (database + photos) — first pass

This copies a **snapshot** so you can test the VM. You'll copy the *final* data again at
cutover (step 8). The two things to move live in `C:\Users\sebastien.poncelet\TaskTracker\`:

- `dev.db` — the database (all tasks, users, timestamps)
- `uploads\` — all the photos

**Easiest method — WinSCP (GUI):**
1. Download WinSCP (free), connect with **SFTP** to `VM_IP`, your user + password.
2. Navigate to `/tmp` on the right (VM) side.
3. Drag `dev.db` and the whole `uploads` folder from
   `C:\Users\sebastien.poncelet\TaskTracker\` into `/tmp`.

**Or command line — `scp` (built into Windows PowerShell):**

```powershell
#laptop
scp "C:\Users\sebastien.poncelet\TaskTracker\dev.db" youruser@VM_IP:/tmp/
scp -r "C:\Users\sebastien.poncelet\TaskTracker\uploads" youruser@VM_IP:/tmp/
```

> The `uploads` folder has a `.thumbs` cache and `.origbak`/`.heic` backup files you
> don't need — but copying everything is harmless; the cache just regenerates.

Now move the data into place on the VM and fix ownership:

```bash
sudo cp /tmp/dev.db /opt/task-tracker/dev.db
sudo cp -r /tmp/uploads/. /opt/task-tracker/uploads/
sudo chown -R tasktracker:tasktracker /opt/task-tracker/dev.db /opt/task-tracker/uploads
```

Sanity-check the DB copied (should print a task count):

```bash
sudo -u tasktracker sqlite3 /opt/task-tracker/dev.db "SELECT COUNT(*) AS tasks FROM tasks;"
```

---

## 6. Run the app as a systemd service

Install the service unit that ships in the repo and start it:

```bash
sudo cp /opt/task-tracker/deploy/tasktracker.service /etc/systemd/system/tasktracker.service
sudo systemctl daemon-reload
sudo systemctl enable --now tasktracker
sudo systemctl status tasktracker      # should say: active (running)
```

Test it locally (no tunnel yet):

```bash
curl -s http://127.0.0.1:5000/healthz    # expect: {"ok":true}
```

If that returns `{"ok":true}`, the app is running on the VM. 🎉
(If not: `journalctl -u tasktracker -n 50 --no-pager` shows why.)

---

## 7. Move the Cloudflare tunnel to the VM

Your existing tunnel is named **`tasktracker`** and DNS for `biopower-tasks.org` already
points to it. The cleanest path is to **run that same tunnel from the VM** — no DNS
change, no re-login. It just needs its two credential files copied over.

### 7a. Install cloudflared on the VM

```bash
sudo curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
cloudflared --version
sudo mkdir -p /etc/cloudflared
```

### 7b. Copy the tunnel credentials from the laptop

The credentials live in `C:\Users\sebastien.poncelet\.cloudflared\` on the laptop:
- `59f22012-df09-427d-9598-10c75ac735e5.json`  ← the tunnel credentials
- `cert.pem`  ← your Cloudflare login cert

Copy both to the VM (WinSCP into `/tmp`, or scp):

```powershell
#laptop
scp "C:\Users\sebastien.poncelet\.cloudflared\59f22012-df09-427d-9598-10c75ac735e5.json" youruser@VM_IP:/tmp/
scp "C:\Users\sebastien.poncelet\.cloudflared\cert.pem" youruser@VM_IP:/tmp/
```

Put them where cloudflared expects them:

```bash
sudo cp /tmp/59f22012-df09-427d-9598-10c75ac735e5.json /etc/cloudflared/
mkdir -p ~/.cloudflared && cp /tmp/cert.pem ~/.cloudflared/cert.pem
```

### 7c. Write the tunnel config

```bash
sudo tee /etc/cloudflared/config.yml >/dev/null <<'EOF'
tunnel: 59f22012-df09-427d-9598-10c75ac735e5
credentials-file: /etc/cloudflared/59f22012-df09-427d-9598-10c75ac735e5.json

ingress:
  - hostname: biopower-tasks.org
    service: http://127.0.0.1:5000
  - service: http_status:404
EOF
```

**Don't install the tunnel service yet** — that happens at cutover (step 8), so you
don't have two machines serving the same tunnel at once.

---

## 8. CUTOVER — flip the live site to the VM

Do this when you're ready for the ~2–5 min switch. Order matters (stop laptop first so
no data is written during the copy).

**8.1 — Stop the app on the laptop** (so the database stops changing):

```powershell
#laptop
Get-Process python,pythonw | Stop-Process -Force
```

**8.2 — Copy the FINAL data to the VM** (repeat step 5's copy so you capture everything
since the first pass). WinSCP overwrite, or:

```powershell
#laptop
scp "C:\Users\sebastien.poncelet\TaskTracker\dev.db" youruser@VM_IP:/tmp/
scp -r "C:\Users\sebastien.poncelet\TaskTracker\uploads" youruser@VM_IP:/tmp/
```

Then on the VM, put it in place and restart the app:

```bash
sudo systemctl stop tasktracker
sudo cp /tmp/dev.db /opt/task-tracker/dev.db
sudo cp -r /tmp/uploads/. /opt/task-tracker/uploads/
sudo chown -R tasktracker:tasktracker /opt/task-tracker/dev.db /opt/task-tracker/uploads
sudo systemctl start tasktracker
curl -s http://127.0.0.1:5000/healthz    # {"ok":true}
```

**8.3 — Stop the tunnel on the laptop** (only one machine may run this tunnel):

```powershell
#laptop
Get-Process cloudflared | Stop-Process -Force
```

**8.4 — Start the tunnel on the VM as a service:**

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared    # active (running); logs show "Registered tunnel connection"
```

---

## 9. Verify the live site

From anywhere:

```bash
curl -s https://biopower-tasks.org/healthz     # {"ok":true}
```

Then open **https://biopower-tasks.org** in a browser and log in — your accounts,
tasks, and photos should all be there (same data, same URL). Have an operator open it
on their phone too.

If the URL returns 502/530 for a minute, that's just the tunnel reconnecting — wait and
retry. If it persists: `sudo journalctl -u cloudflared -n 50 --no-pager`.

---

## 10. Retire the laptop (stop it fighting the VM)

On the laptop, remove the old auto-start bits so it never tries to run the app or tunnel
again (that would conflict with the VM):

```powershell
#laptop
# stop & delete the tunnel watchdog scheduled task
schtasks /delete /tn "CloudflareTunnelWatchdog" /f
# remove the Startup shortcut that launches the tunnel at logon
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\CloudflareTunnel-TaskTracker.lnk" -ErrorAction SilentlyContinue
# make sure nothing is still running
Get-Process cloudflared,python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
```

You can keep the `C:\Users\sebastien.poncelet\TaskTracker` folder as a backup for a
while, then delete it once you're confident the VM is solid.

---

## 11. Day-to-day operations (on the VM)

**Check status / logs:**
```bash
systemctl status tasktracker cloudflared
journalctl -u tasktracker -n 50 --no-pager     # app logs
journalctl -u cloudflared -n 50 --no-pager     # tunnel logs
```

**Deploy a code update** (when I push changes to GitHub):
```bash
cd /tmp && rm -rf task-tracker && git clone https://github.com/BioICTghb/task-tracker.git
sudo cp -r /tmp/task-tracker/task-tracker/app.py /tmp/task-tracker/task-tracker/db.py \
          /tmp/task-tracker/task-tracker/templates /tmp/task-tracker/task-tracker/static \
          /opt/task-tracker/
sudo chown -R tasktracker:tasktracker /opt/task-tracker
sudo -u tasktracker /opt/task-tracker/.venv/bin/pip install -r /opt/task-tracker/requirements.txt
sudo systemctl restart tasktracker
```
> (Data — `dev.db` and `uploads/` — is never overwritten by updates; only code is copied.)

**Nightly backup** (simple, keeps the DB + a photo archive safe). Create
`/opt/task-tracker/backup.sh`:
```bash
sudo tee /opt/task-tracker/backup.sh >/dev/null <<'EOF'
#!/bin/bash
set -e
DEST=/opt/task-tracker/backups
mkdir -p "$DEST"
STAMP=$(date +%F)
# consistent DB copy even while the app is running
sqlite3 /opt/task-tracker/dev.db ".backup '$DEST/dev-$STAMP.db'"
# keep last 14 days of DB snapshots
find "$DEST" -name 'dev-*.db' -mtime +14 -delete
EOF
sudo chmod +x /opt/task-tracker/backup.sh
sudo chown -R tasktracker:tasktracker /opt/task-tracker/backups 2>/dev/null || true
# run it every night at 01:30
( sudo crontab -l 2>/dev/null; echo "30 1 * * * /opt/task-tracker/backup.sh" ) | sudo crontab -
```
For off-box safety, also snapshot the VM in Proxmox periodically, and/or copy
`/opt/task-tracker/uploads` somewhere else occasionally.

---

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| `curl localhost:5000/healthz` fails | `journalctl -u tasktracker -n 50 --no-pager` — usually a `.env` typo, wrong path, or permissions. |
| Site is 502 / "bad gateway" | App is down: `systemctl status tasktracker`. Tunnel can't reach it. |
| Site is 1033 / 530 | Tunnel is down: `systemctl status cloudflared`, `journalctl -u cloudflared`. |
| Photos don't display (HEIC) | Confirm `pillow-heif` installed: `/opt/task-tracker/.venv/bin/python -c "import pillow_heif"`. |
| Everyone got logged out | The `SECRET_KEY` in `.env` doesn't match the laptop's — put the original back and `systemctl restart tasktracker`. |
| "database is locked" under load | Lower gunicorn workers: edit the service `ExecStart` to `-w 2`, `daemon-reload`, restart. |

---

## Alternative: create a brand-new tunnel instead of moving it

If you'd rather not copy the credential files, you can make a fresh tunnel on the VM
(this **re-points** the DNS record):

```bash
cloudflared tunnel login          # prints a URL — open it in any browser, approve
cloudflared tunnel create tasktracker-vm
# note the new Tunnel ID it prints, then put it in /etc/cloudflared/config.yml
cloudflared tunnel route dns tasktracker-vm biopower-tasks.org
sudo cloudflared service install
```
> If `route dns` says the record already exists, delete the existing `biopower-tasks.org`
> CNAME in the Cloudflare dashboard (DNS tab) first, then re-run it.

The "move the existing tunnel" method in step 7 is simpler and needs no dashboard work,
so prefer that unless you have a reason not to.
