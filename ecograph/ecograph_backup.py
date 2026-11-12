#!/usr/bin/env python3
r"""Publish the Ecograph hourly logs to the Company file share as one Excel file (daily, cron).

    ~/task-tracker/task-tracker/.venv/bin/python ~/ecograph/ecograph_backup.py

Output: \\<ROSTER_SMB_HOST>\<ROSTER_SMB_SHARE>\@_IT\Wastewater readings - automatic\
        Wastewater flow readings.xlsx   (the Company share, mapped as L: on office PCs)

All ~/ecograph/data/flow-hourly-*.csv files (every year, history backfill + live rows)
become one plain table, oldest first, in Belgian local time, plus a short "Notes" sheet.
The CSVs on the VM stay the raw source; this file is rebuilt from them on every run.
The workbook is written under a temporary name, size-checked, then renamed over the old
one, so an interrupted run never leaves a broken file. If someone has the file open
the rename fails, the old file stays, and the next night's run catches up.

Credentials come from ~/.roster.env (the account the roster import reads the share with).
Needs smbprotocol + openpyxl, which live in the task-tracker venv.
"""
import csv
import glob
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import smbclient
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ENV = os.path.expanduser("~/.roster.env")
DATA = os.path.expanduser("~/ecograph/data")
DEST_DIR = r"@_IT\Wastewater readings - automatic"
XLSX_NAME = "Wastewater flow readings.xlsx"
LOCAL = ZoneInfo("Europe/Brussels")

# (header, width, number format)
COLUMNS = [
    ("Date", 12, "dd/mm/yyyy"),
    ("From", 8, "hh:mm"),
    ("To", 8, "hh:mm"),
    ("Volume (m³)", 13, "0.0"),
    ("Hours covered", 14, "0.00"),
    ("Flow at reading (m³/h)", 22, "0.0"),
    ("Meter total (m³)", 17, "#,##0.0"),
    ("Source", 20, "@"),
    ("Minutes over range", 19, "0"),
    ("Hour end (UTC)", 17, "dd/mm/yyyy hh:mm"),
]

SOURCE_LABEL = {"counter": "Meter counter", "history": "Recorder history *"}

NOTES = [
    ("Wastewater flow readings - Ecograph T (RSG35, 10.0.91.73)", True),
    ("", False),
    ("One row per hour. Date / From / To are Belgian local time (summer time included);", False),
    ("'Hour end (UTC)' is the unambiguous timestamp, e.g. for the doubled hour in October.", False),
    ("", False),
    ("Source", True),
    ("Meter counter: volume = difference of the device's total counter over the hour. Exact.", False),
    ("Recorder history *: backfilled from the recorder's 1-minute history (27 Feb - 10 Nov 2026).", False),
    ("  These read about 2.6x higher than the device counters, because the flow signal was often", False),
    ("  over its 30 m³/h range (warning S902). Do not add them to counter rows without saying so.", False),
    ("", False),
    ("Minutes over range: minutes in the hour the signal sat at the 30 m³/h ceiling (history rows).", False),
    ("Hours covered > 1: a reading was missed (e.g. VM down); that row holds the whole gap.", False),
    ("", False),
    ("Rebuilt every night at about 01:15 from the logs on the task-tracker VM (~/ecograph/data).", False),
]


def num(s):
    return float(s) if s not in ("", None) else None


def load_rows():
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "flow-hourly-*.csv"))):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not r.get("hour_end") or r.get("volume_m3", "") == "":
                    continue  # first-run baseline rows carry no volume
                end_utc = datetime.fromisoformat(r["hour_end"]).replace(tzinfo=timezone.utc)
                span = num(r.get("span_h")) or 1.0
                start_utc = end_utc - timedelta(minutes=round(span * 60))
                start = start_utc.astimezone(LOCAL)
                # "To" is wall-clock From + span, so DST nights read 01:00-02:00 then
                # 03:00-04:00 (spring) and 02:00-03:00 twice (autumn), not 01:00-03:00.
                end = start + timedelta(minutes=round(span * 60))
                clipped = r.get("clipped_min", "")
                rows.append([
                    start.date(), start.time().replace(second=0, microsecond=0),
                    end.time().replace(second=0, microsecond=0),
                    num(r["volume_m3"]), span, num(r.get("flow_now_m3h")), num(r.get("total_m3")),
                    SOURCE_LABEL.get(r.get("source") or "counter", r.get("source")),
                    int(clipped) if clipped not in ("", None) else None,
                    end_utc.replace(tzinfo=None),
                ])
    rows.sort(key=lambda x: x[-1])
    return rows


def build_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Hourly flow"
    ws.append([c[0] for c in COLUMNS])
    for r in rows:
        ws.append(r)
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F4E78")
    for i, (_, width, fmt) in enumerate(COLUMNS, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = width
        cell = ws.cell(row=1, column=i)
        cell.font, cell.fill = head, fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for (c,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
            c.number_format = fmt
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    notes = wb.create_sheet("Notes")
    notes.column_dimensions["A"].width = 100
    for text, bold in NOTES:
        notes.append([text])
        if bold:
            notes.cell(row=notes.max_row, column=1).font = Font(bold=True)
    notes.append([f"Last rebuilt: {datetime.now(LOCAL):%d/%m/%Y %H:%M} (Belgian time), {len(rows)} rows."])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def load_env(path):
    cfg = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def upload(data):
    cfg = load_env(ENV)
    user = cfg["ROSTER_SMB_USER"]
    if cfg.get("ROSTER_SMB_DOMAIN"):
        user = cfg["ROSTER_SMB_DOMAIN"] + "\\" + user
    smbclient.register_session(cfg["ROSTER_SMB_HOST"], username=user,
                               password=cfg["ROSTER_SMB_PASSWORD"])
    dest = "\\\\%s\\%s\\%s" % (cfg["ROSTER_SMB_HOST"], cfg["ROSTER_SMB_SHARE"], DEST_DIR)
    smbclient.makedirs(dest, exist_ok=True)
    final = dest + "\\" + XLSX_NAME
    tmp = dest + "\\~" + XLSX_NAME + ".partial"
    with smbclient.open_file(tmp, mode="wb") as out:
        out.write(data)
    size = smbclient.stat(tmp).st_size
    if size != len(data):
        smbclient.remove(tmp)
        sys.exit(f"wrote {size} of {len(data)} bytes, old file kept")
    try:
        smbclient.replace(tmp, final)
    except OSError as e:
        smbclient.remove(tmp)
        sys.exit(f"could not replace {final} (open in Excel?): {e}. Old file kept; retry next run.")
    return final


def main():
    rows = load_rows()
    if not rows:
        sys.exit("no rows to publish in " + DATA)
    data = build_xlsx(rows)
    if "--local" in sys.argv:  # test: write next to the data instead of to the share
        out = os.path.join(DATA, XLSX_NAME)
        with open(out, "wb") as f:
            f.write(data)
    else:
        out = upload(data)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} published {len(rows)} rows ({len(data)} bytes) -> {out}")


if __name__ == "__main__":
    main()
