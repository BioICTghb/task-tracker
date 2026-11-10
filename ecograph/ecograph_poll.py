#!/usr/bin/env python3
r"""Poll an Endress+Hauser Ecograph T (RSG35) and log its channel values to CSV.

The recorder exposes all channels, unauthenticated, as Fieldgate-style XML at
http://<ip>/xml (also /values.xml). Each <device> is one value: the analogue
channel itself (id "AI01IV") and its counters (AI01A0..A3 = interim/daily/
monthly/yearly, AI01TC = total). Standard library only.

Usage:
    python ecograph_poll.py                     # one reading -> stdout
    python ecograph_poll.py --csv flow.csv      # one reading appended to CSV (cron-friendly)
    python ecograph_poll.py --csv flow.csv --interval 60   # loop every 60 s
    python ecograph_poll.py --csv 'data/flow-%Y-%m.csv'    # one file per month

    python ecograph_poll.py --hourly 'data/flow-hourly-%Y.csv'   # hourly volume log

On the task-tracker VM it runs hourly from the user crontab:
    0 * * * * python3 ~/ecograph/ecograph_poll.py --hourly '~/ecograph/data/flow-hourly-\%Y.csv' >> ~/ecograph/poll.log 2>&1
"""
import argparse
import csv
import html
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

DEFAULT_HOST = "10.0.91.73"


def fetch(host, timeout=10):
    """Return (device_time, [ {id, tag, value, unit, status}, ... ])."""
    with urllib.request.urlopen(f"http://{host}/xml", timeout=timeout) as r:
        raw = r.read()
    root = ET.fromstring(raw)  # XML declares ISO-8859-1; ElementTree honours it
    dev_time = datetime.strptime(root.findtext("time"), "%Y%m%d-%H%M%S")
    rows = []
    for d in root.iter("device"):
        v = (d.findtext("v1") or "").strip()
        try:
            value = float(v)
        except ValueError:
            value = None  # channel in error / no value
        rows.append({
            "id": d.get("id"),
            "tag": d.get("tag"),
            "value": value,
            "unit": html.unescape(d.findtext("u1") or ""),
            "status": d.findtext("vstslvl"),
        })
    return dev_time, rows


def append_csv(path, dev_time, rows):
    """Long format: one line per channel per reading - survives channels being added.

    The path may contain strftime codes (e.g. flow-%Y-%m.csv) for one file per month."""
    path = os.path.expanduser(datetime.now().strftime(path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new =not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["polled_at", "device_time", "id", "tag", "value", "unit", "status"])
        now = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            w.writerow([now, dev_time.isoformat(), r["id"], r["tag"],
                        "" if r["value"] is None else r["value"], r["unit"], r["status"]])


# source: "counter" = live run (volume from the total-counter difference, exact);
#         "history" = backfilled from the recorder's 1-minute history (volume integrated from
#         the flow, so it under-reads whenever the signal was clipped at the 30 m3/h range top;
#         clipped_min counts those minutes).
HOURLY_HEADER = ["hour_end", "volume_m3", "span_h", "flow_now_m3h", "total_m3", "status",
                 "source", "clipped_min"]


def append_hourly(path, state_path, dev_time, rows):
    """One row per run: volume that flowed since the previous run, from the total counter.

    Counter differences are exact even if the flow swings between readings, which an
    hourly m3/h snapshot would not be. The previous total is kept in a small state file
    so month/year file boundaries don't break the difference. If a run was missed
    (VM down), span_h shows the row covers more than one hour.
    """
    by_id = {r["id"]: r for r in rows}
    total = by_id.get("AI01TC", {}).get("value")
    flow = by_id.get("AI01IV", {})
    now = datetime.now().replace(second=0, microsecond=0)

    state_path = os.path.expanduser(state_path)
    prev = None
    try:
        with open(state_path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass

    volume = span = ""
    if prev and total is not None and prev.get("total") is not None:
        diff = total - prev["total"]
        if diff >= 0:  # a negative diff means the counter was reset: leave blank
            volume = round(diff, 3)
        span = round((now - datetime.fromisoformat(prev["at"])).total_seconds() / 3600, 2)

    path = os.path.expanduser(now.strftime(path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HOURLY_HEADER)
        w.writerow([now.isoformat(timespec="minutes"), volume, span,
                    "" if flow.get("value") is None else flow["value"],
                    "" if total is None else total, flow.get("status", ""), "counter", ""])

    if total is not None:
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": now.isoformat(), "total": total}, f)
        os.replace(tmp, state_path)


def poll_once(args):
    dev_time, rows = fetch(args.host)
    if args.hourly:
        append_hourly(args.hourly, args.state, dev_time, rows)
    elif args.csv:
        append_csv(args.csv, dev_time, rows)
    else:
        print(f"device time {dev_time}")
        for r in rows:
            print(f"  {r['id']:8} {r['tag']:28} {r['value']!s:>12} {r['unit']}  (status {r['status']})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--csv", help="append readings to this CSV file")
    p.add_argument("--hourly", help="append one volume-per-hour row to this CSV (run from cron at :00)")
    p.add_argument("--state", default="~/.ecograph_last.json", help="where --hourly keeps the previous total")
    p.add_argument("--interval", type=float, help="seconds between polls (omit for a single reading)")
    args = p.parse_args()

    if not args.interval:
        # Single run (cron): ride out a short network/device blip instead of losing the
        # hour's row. A run that still fails is not lost data - the next run's counter
        # difference covers the gap (span_h > 1).
        for attempt in range(1, 6):
            try:
                poll_once(args)
                return
            except Exception as e:
                print(f"{datetime.now():%Y-%m-%d %H:%M:%S} attempt {attempt} failed: {e}", file=sys.stderr)
                if attempt < 5:
                    time.sleep(30)
        sys.exit(1)
    while True:
        started = time.monotonic()
        try:
            poll_once(args)
        except Exception as e:  # keep looping through network blips
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} poll failed: {e}", file=sys.stderr)
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
