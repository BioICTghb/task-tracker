# Task Tracker — Migration to a Proxmox Ubuntu VM

A complete, copy-paste runbook for moving Task Tracker off the Windows laptop onto a
dedicated Ubuntu VM on Proxmox. When done, the app runs 24/7 as a systemd service, the
Cloudflare tunnel runs as its own service, and both auto-restart on crash and on reboot.

**The public URL (`https://biopower-tasks.org`) does not change.**

> This guide matches the actual setup: the app lives in your home folder
> **`/home/sebastien/task-tracker/task-tracker`** and runs as user **`sebastien`**.
> Lines marked `#laptop` run in **Windows PowerShell**; `$` lines run on the **VM**.

---

## ▶ WHERE YOU ARE

- [x] **1. VM created** (Ubuntu 24.04, 2 GB RAM, 2 vCPU, disk to grow to 40 GB)
- [x] **2. Base packages installed** (`apt`, git, etc.) — networking on DHCP, working
- [x] **3. App cloned + venv built + `pip install` done** in `~/task-tracker/task-tracker`
- [ ] **4. Configure `.env`  ← RESUME HERE**
- [ ] 5. Copy data (dev.db + uploads)
- [ ] 6. App as a systemd service
- [ ] 7. Move the Cloudflare tunnel
- [ ] 8. Cutover (final data + flip tunnel)
- [ ] 9. Verify the live site
- [ ] 10. Retire the laptop

If you ever need to redo step 3 from scratch, this is the clean block:
```bash
cd ~ && rm -rf task-tracker
git clone https://github.com/BioICTghb/task-tracker.git
cd task-tracker/task-tracker
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 4. Configure the environment (`.env`)   ← RESUME HERE

Create the `.env` in the app folder:

```bash
tee ~/task-tracker/task-tracker/.env >/dev/null <<'EOF'
DB_BACKEND=sqlite
SQLITE_PATH=/home/sebastien/task-tracker/task-tracker/dev.db
UPLOAD_ROOT=/home/sebastien/task-tracker/task-tracker/uploads
SESSION_COOKIE_SECURE=1
FLASK_DEBUG=0
SECRET_KEY=PASTE_THE_SAME_KEY_FROM_THE_LAPTOP
EOF
chmod 600 ~/task-tracker/task-tracker/.env
```

**Critical — the SECRET_KEY:** replace `PASTE_THE_SAME_KEY_FROM_THE_LAPTOP` with the
**exact** key from the laptop. Reusing it keeps everyone logged in; a new key logs
everyone out. Print it on the laptop to copy:

```powershell
#laptop
Get-Content "C:\Users\sebastien.poncelet\TaskTracker\.env" | Select-String SECRET_KEY
```

Edit the file to paste it in:
```bash
nano ~/task-tracker/task-tracker/.env
```

---

## 5. Copy your data (database + photos) — first pass

A **snapshot** so you can test. You'll copy the *final* data again at cutover (step 8).
The two things to move live in `C:\Users\sebastien.poncelet\TaskTracker\`:

- `dev.db` — the database (all tasks, users, timestamps)
- `uploads\` — all the photos

**Easiest — WinSCP (GUI):** connect via **SFTP** to the VM's IP with your user/password,
and drag `dev.db` + the whole `uploads` folder into `/tmp`.

**Or `scp` from PowerShell:**
```powershell
#laptop
scp "C:\Users\sebastien.poncelet\TaskTracker\dev.db" sebastien@VM_IP:/tmp/
scp -r "C:\Users\sebastien.poncelet\TaskTracker\uploads" sebastien@VM_IP:/tmp/
```

Then on the VM, move it into the app folder:
```bash
cp /tmp/dev.db ~/task-tracker/task-tracker/dev.db
mkdir -p ~/task-tracker/task-tracker/uploads
cp -r /tmp/uploads/. ~/task-tracker/task-tracker/uploads/
```

Sanity-check the DB (should print a task count):
```bash
sqlite3 ~/task-tracker/task-tracker/dev.db "SELECT COUNT(*) AS tasks FROM tasks;"
```

---

## 6. Run the app as a systemd service

Create the service unit (note: it runs as **you**, from your home folder):

```bash
sudo tee /etc/systemd/system/tasktracker.service >/dev/null <<'EOF'
[Unit]
Description=Task Tracker (Flask/gunicorn)
After=network.target

[Service]
User=sebastien
WorkingDirectory=/home/sebastien/task-tracker/task-tracker
ExecStart=/home/sebastien/task-tracker/task-tracker/.venv/bin/gunicorn -w 3 -b 127.0.0.1:5000 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tasktracker
sudo systemctl status tasktracker      # should say: active (running)
```

Test locally (no tunnel yet):
```bash
curl -s http://127.0.0.1:5000/healthz   # expect: {"ok":true}
```
If that returns `{"ok":true}`, the app runs on the VM. 🎉
(If not: `journalctl -u tasktracker -n 50 --no-pager` shows why — usually a `.env` path/typo.)

---

## 7. Move the Cloudflare tunnel to the VM

Your tunnel is named **`tasktracker`** and DNS for `biopower-tasks.org` already points to
it. Easiest: **run that same tunnel from the VM** — no DNS change, no re-login; just copy
its two credential files.

### 7a. Install cloudflared on the VM
```bash
sudo curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
cloudflared --version
sudo mkdir -p /etc/cloudflared
```

### 7b. Copy the tunnel credentials from the laptop
They live in `C:\Users\sebastien.poncelet\.cloudflared\`:
- `59f22012-df09-427d-9598-10c75ac735e5.json`  ← tunnel credentials
- `cert.pem`  ← Cloudflare login cert

```powershell
#laptop
scp "C:\Users\sebastien.poncelet\.cloudflared\59f22012-df09-427d-9598-10c75ac735e5.json" sebastien@VM_IP:/tmp/
scp "C:\Users\sebastien.poncelet\.cloudflared\cert.pem" sebastien@VM_IP:/tmp/
```

Put them in place on the VM:
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

**Don't start the tunnel yet** — that's step 8, so two machines don't serve it at once.

---

## 8. CUTOVER — flip the live site to the VM

Do this when you're ready for the ~2–5 min switch. Order matters (stop the laptop first).

**8.1 — Stop the app on the laptop** (database stops changing):
```powershell
#laptop
Get-Process python,pythonw | Stop-Process -Force
```

**8.2 — Copy the FINAL data to the VM** (WinSCP overwrite, or scp again):
```powershell
#laptop
scp "C:\Users\sebastien.poncelet\TaskTracker\dev.db" sebastien@VM_IP:/tmp/
scp -r "C:\Users\sebastien.poncelet\TaskTracker\uploads" sebastien@VM_IP:/tmp/
```
Then on the VM, put it in place and restart the app:
```bash
sudo systemctl stop tasktracker
cp /tmp/dev.db ~/task-tracker/task-tracker/dev.db
cp -r /tmp/uploads/. ~/task-tracker/task-tracker/uploads/
sudo systemctl start tasktracker
curl -s http://127.0.0.1:5000/healthz    # {"ok":true}
```

**8.3 — Stop the tunnel on the laptop** (only one machine may run it):
```powershell
#laptop
Get-Process cloudflared | Stop-Process -Force
```

**8.4 — Start the tunnel on the VM as a service:**
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared    # active (running); log shows "Registered tunnel connection"
```

---

## 9. Verify the live site

```bash
curl -s https://biopower-tasks.org/healthz     # {"ok":true}
```
Then open **https://biopower-tasks.org** and log in — accounts, tasks, and photos should
all be there (same data, same URL). Have an operator check on their phone.

502/530 for a minute = tunnel reconnecting; wait and retry. If it persists:
`sudo journalctl -u cloudflared -n 50 --no-pager`.

---

## 10. Retire the laptop

Stop the laptop from fighting the VM over the same tunnel:
```powershell
#laptop
schtasks /delete /tn "CloudflareTunnelWatchdog" /f
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\CloudflareTunnel-TaskTracker.lnk" -ErrorAction SilentlyContinue
Get-Process cloudflared,python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
```
Keep the `C:\Users\sebastien.poncelet\TaskTracker` folder as a backup for a while, then
delete it once the VM is proven solid.

---

## 11. Day-to-day operations (on the VM)

**Status / logs:**
```bash
systemctl status tasktracker cloudflared
journalctl -u tasktracker -n 50 --no-pager     # app logs
journalctl -u cloudflared -n 50 --no-pager     # tunnel logs
```

**Deploy a code update** (your app folder is a git checkout, so it's just a pull):
```bash
cd ~/task-tracker && git pull
cd ~/task-tracker/task-tracker
.venv/bin/pip install -r requirements.txt      # in case deps changed
sudo systemctl restart tasktracker
```
> `dev.db` and `uploads/` are gitignored, so `git pull` never touches your data.

**Nightly DB backup** (consistent copy even while running):
```bash
tee ~/task-tracker/task-tracker/backup.sh >/dev/null <<'EOF'
#!/bin/bash
set -e
DEST=~/task-tracker/task-tracker/backups
mkdir -p "$DEST"
sqlite3 ~/task-tracker/task-tracker/dev.db ".backup '$DEST/dev-$(date +%F).db'"
find "$DEST" -name 'dev-*.db' -mtime +14 -delete
EOF
chmod +x ~/task-tracker/task-tracker/backup.sh
( crontab -l 2>/dev/null; echo "30 1 * * * $HOME/task-tracker/task-tracker/backup.sh" ) | crontab -
```
Also snapshot the VM in Proxmox now and then, and/or copy `uploads/` off-box occasionally.

---

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| `curl localhost:5000/healthz` fails | `journalctl -u tasktracker -n 50 --no-pager` — usually a `.env` path/typo or permissions. |
| Site is 502 / "bad gateway" | App down: `systemctl status tasktracker`. |
| Site is 1033 / 530 | Tunnel down: `systemctl status cloudflared`, `journalctl -u cloudflared`. |
| Photos don't display (HEIC) | `~/task-tracker/task-tracker/.venv/bin/python -c "import pillow_heif"` should not error. |
| Everyone got logged out | `SECRET_KEY` in `.env` doesn't match the laptop's — put the original back, `systemctl restart tasktracker`. |
| "database is locked" under load | Edit the service `ExecStart` to `-w 2`, `sudo systemctl daemon-reload && sudo systemctl restart tasktracker`. |
| `pip install` Pillow conflict | Make sure `requirements.txt` says `Pillow==12.2.0` (fixed in the repo); `git pull` if not. |

---

## Alternative: brand-new tunnel instead of moving it

If you'd rather not copy the credential files, make a fresh tunnel on the VM (this
**re-points** the DNS record):
```bash
cloudflared tunnel login          # prints a URL — open it in any browser, approve
cloudflared tunnel create tasktracker-vm
# put the new Tunnel ID it prints into /etc/cloudflared/config.yml
cloudflared tunnel route dns tasktracker-vm biopower-tasks.org
sudo cloudflared service install
```
> If `route dns` says the record exists, delete the `biopower-tasks.org` CNAME in the
> Cloudflare dashboard (DNS tab) first, then re-run it. The "move the existing tunnel"
> method in step 7 is simpler and needs no dashboard work — prefer that.
