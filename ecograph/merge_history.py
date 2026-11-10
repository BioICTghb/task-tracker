#!/usr/bin/env python3
"""Merge a history backfill CSV into the hourly log (one-off, but safe to re-run).

    python3 merge_history.py history.csv ~/ecograph/data/flow-hourly-2026.csv

The history CSV comes from the recorder's History page (1-minute samples integrated
per UTC hour, source="history"). Rule at the seam: history wins for every hour up to
its last hour; live counter rows are kept only after that. Rows with no volume (the
first-run baseline) are dropped. The target is rewritten atomically with a .bak copy.
"""
import csv
import os
import shutil
import sys

from ecograph_poll import HOURLY_HEADER

N = len(HOURLY_HEADER)


def read(path, default_source):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        r = (r + [""] * N)[:N]
        if not r[6]:
            r[6] = default_source  # rows written before the source column existed
        out.append(r)
    return out


def main(hist_path, target):
    hist = read(hist_path, "history")
    live = read(target, "counter")
    if not hist:
        sys.exit("history file is empty")
    last_hist = max(r[0] for r in hist)
    merged = {r[0]: r for r in hist}
    kept_live = 0
    for r in live:
        if r[6] == "history":
            continue  # re-run: the new history file replaces the old backfill
        if r[0] > last_hist and r[1] != "":
            merged[r[0]] = r
            kept_live += 1
    rows = [merged[k] for k in sorted(merged)]

    if os.path.exists(target):
        shutil.copy2(target, target + ".bak")
    tmp = target + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HOURLY_HEADER)
        w.writerows(rows)
    os.replace(tmp, target)
    print(f"{len(hist)} history hours ({hist[0][0]} .. {last_hist}) + {kept_live} live hours "
          f"-> {len(rows)} rows in {target}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(os.path.expanduser(sys.argv[1]), os.path.expanduser(sys.argv[2]))
