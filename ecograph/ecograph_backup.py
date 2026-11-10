#!/usr/bin/env python3
"""Copy the Ecograph hourly logs to the Company file share (daily, from cron).

    ~/task-tracker/task-tracker/.venv/bin/python ~/ecograph/ecograph_backup.py

Destination: \\\\<ROSTER_SMB_HOST>\\<ROSTER_SMB_SHARE>\\3_ebo_production\\Ecograph\\
Every *.csv in ~/ecograph/data is mirrored there (the yearly logs and the history
backfill). Each file is written under a temporary name, size-checked, then renamed
over the old copy, so an interrupted run never leaves a truncated backup.

Credentials come from ~/.roster.env (the same account the roster import reads the
share with); nothing is stored here. Needs smbprotocol, which lives in the task-tracker
venv.
"""
import glob
import os
import sys
from datetime import datetime

import smbclient

ENV = os.path.expanduser("~/.roster.env")
DATA = os.path.expanduser("~/ecograph/data")
DEST_DIR = r"3_ebo_production\Ecograph"


def load_env(path):
    cfg = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def main():
    cfg = load_env(ENV)
    user = cfg["ROSTER_SMB_USER"]
    if cfg.get("ROSTER_SMB_DOMAIN"):
        user = cfg["ROSTER_SMB_DOMAIN"] + "\\" + user
    smbclient.register_session(cfg["ROSTER_SMB_HOST"], username=user,
                               password=cfg["ROSTER_SMB_PASSWORD"])
    dest = "\\\\%s\\%s\\%s" % (cfg["ROSTER_SMB_HOST"], cfg["ROSTER_SMB_SHARE"], DEST_DIR)
    smbclient.makedirs(dest, exist_ok=True)

    files = sorted(glob.glob(os.path.join(DATA, "*.csv")))
    if not files:
        sys.exit("no CSV files to back up in " + DATA)
    for src in files:
        name = os.path.basename(src)
        with open(src, "rb") as f:
            data = f.read()
        final = dest + "\\" + name
        tmp = final + ".partial"
        with smbclient.open_file(tmp, mode="wb") as out:
            out.write(data)
        size = smbclient.stat(tmp).st_size
        if size != len(data):
            smbclient.remove(tmp)
            sys.exit(f"{name}: wrote {size} of {len(data)} bytes, old backup kept")
        smbclient.replace(tmp, final)
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} backed up {name} ({len(data)} bytes) -> {final}")


if __name__ == "__main__":
    main()
