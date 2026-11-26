"""Import the shift roster from the master Excel on the file share.

The spreadsheet is the single source of truth; the app's grid is read-only. This
runs nightly from the operator account's crontab (no root, no permanent mount:
the file is read straight over SMB) and can also be triggered from the Schedule
page.

Design notes, all of them lessons from this roster:

* Columns are matched by **name**, never by position. A person leaving, or a blank
  spacer column appearing, used to shift everybody's roster sideways.
* Nothing is guessed. A name in the sheet with no matching column is reported and
  skipped; a column with no name in the sheet is left untouched.
* A **safety gate** refuses the whole run if too few names matched or too much of
  the window would change, so a restructured template (the 2027 revision is
  already planned) cannot silently wipe the rosters overnight.
* The year *inside* the file is ignored - the sheet still carries 2022 dates. Only
  month and day are read, and the year comes from the file being opened.

Usage:
    python roster_import.py [--dry-run] [--weeks N] [--year YYYY] [--force]
"""
import argparse
import datetime
import io
import os
import re
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

CONFIG_PATH = os.environ.get("ROSTER_ENV", os.path.expanduser("~/.roster.env"))

# Refuse the run rather than write rubbish.
MIN_NAMES_MATCHED = 5            # fewer than this => the template probably changed
# Share of *already-filled* cells that may be overwritten. Filling empty future
# weeks is routine and must not trip the gate (the very first import fills
# everything); rewriting a roster that is already there is the dangerous case.
MAX_OVERWRITE_FRACTION = 0.40
# Weeks of the recent past to re-read each run. Rosters here get adjusted well after the
# fact, so roughly two months of history is re-read every night rather than trusting that
# a past week is settled. Past dates never raise the operators' "your shift changed"
# banner - apply_week only notifies from today onward.
WEEKS_BACK = int(os.environ.get("ROSTER_WEEKS_BACK", "9"))
MIN_EXISTING_TO_JUDGE = 20       # too little history to draw any conclusion from

# Headings in the sheet that label a legend or a section rather than a person. Kept
# as an explicit list because "Diensten" and "Piotr" are both single words - any
# heuristic would either hide a real operator or keep crying wolf about a legend.
# Extend it without a deploy: ROSTER_IGNORE_LABELS=a,b in ~/.roster.env.
LEGEND_LABELS = {
    "feestdag / schoolvakantie", "feestdag", "schoolvakantie", "diensten", "verlof",
    "overige", "halve dagen", "legenda", "legend", "totaal", "total",
}


def is_legend(name, extra=()):
    """A heading that is not somebody's name. '/' catches combined labels like
    'Feestdag / schoolvakantie' without listing every variant."""
    return name in LEGEND_LABELS or name in extra or "/" in name


def load_config(path=CONFIG_PATH):
    cfg = {}
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()
    missing = [k for k in ("ROSTER_SMB_HOST", "ROSTER_SMB_SHARE", "ROSTER_SMB_PATH_TEMPLATE",
                           "ROSTER_SMB_USER", "ROSTER_SMB_PASSWORD") if not cfg.get(k)]
    if missing:
        raise RuntimeError("%s is missing: %s" % (path, ", ".join(missing)))
    return cfg


def norm(name):
    """Compare names forgivingly: the sheet has 'Damian Garczynski ' with a
    trailing space and 'Savrii\\nAnatolii' with a line break inside it."""
    return re.sub(r"\s+", " ", (name or "").replace("\n", " ")).strip().lower()


def fetch_workbook(cfg, year):
    """The workbook's bytes for a given year, read straight off the share."""
    import smbclient
    user, domain = cfg["ROSTER_SMB_USER"], cfg.get("ROSTER_SMB_DOMAIN", "")
    smbclient.register_session(cfg["ROSTER_SMB_HOST"],
                               username=(domain + "\\" + user) if domain else user,
                               password=cfg["ROSTER_SMB_PASSWORD"])
    rel = cfg["ROSTER_SMB_PATH_TEMPLATE"].format(year=year)
    unc = "\\\\%s\\%s\\%s" % (cfg["ROSTER_SMB_HOST"], cfg["ROSTER_SMB_SHARE"],
                              rel.replace("/", "\\"))
    with smbclient.open_file(unc, mode="rb") as f:
        return f.read(), unc


def cell_text(value):
    """'11,6' from 11.6, 'ADV' from text. Excel stores the hours as numbers and
    shows them with a comma; roster_hours() accepts either separator."""
    if value is None:
        return ""
    if isinstance(value, float):
        s = ("%.2f" % value).rstrip("0").rstrip(".")
        return s.replace(".", ",")
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def cell_color(cell):
    """'#rrggbb' only for a real solid fill. An unfilled cell reports its colour as
    theme 0 or 00000000 - reading those literally would make every blank day black."""
    fill = getattr(cell, "fill", None)
    if fill is None or (fill.patternType or "") != "solid":
        return ""
    rgb = getattr(getattr(fill, "fgColor", None), "rgb", None)
    if not isinstance(rgb, str) or len(rgb) < 6:
        return ""
    hexv = "#" + rgb[-6:].lower()
    if hexv == "#000000":          # Excel's "no fill" sentinel, not a shift colour
        return ""
    return hexv


def find_layout(ws, labels):
    """Locate the header row and the date column instead of hard-coding them, so a
    reshuffled template still imports. Returns (header_row, date_col, {col: label})."""
    wanted = {norm(l) for l in labels}
    best = (0, None, {})
    for r in range(1, min(ws.max_row, 40) + 1):
        found = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and norm(v) in wanted:
                found[c] = norm(v)
        if len(found) > best[0]:
            best = (len(found), r, found)
    header_row = best[1]
    if not header_row:
        return None, None, {}
    # The date column is whichever holds the most date cells.
    counts = {}
    for c in range(1, min(ws.max_column, 12) + 1):
        n = 0
        for r in range(header_row, ws.max_row + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, (datetime.date, datetime.datetime)):
                n += 1
        counts[c] = n
    date_col = max(counts, key=counts.get) if counts else None
    if not counts.get(date_col):
        date_col = None
    return header_row, date_col, best[2]


#: Marks a run that did not see the whole roster, so an hourly retry has something to do.
INCOMPLETE_MARK = "could not be read"


def last_run_incomplete():
    """True if the most recent run was refused, or could not read some dates.

    Lets an hourly retry cost nothing when there is nothing to retry: the master is
    usually only locked for a few minutes while somebody edits it, and hammering the
    share every hour regardless would be rude and would hide the real state.
    """
    import app as m
    import db
    with m.app.app_context():
        row = db.query("SELECT ok, warnings FROM import_runs ORDER BY id DESC LIMIT 1",
                       one=True)
    if not row:
        return True                      # never run: there is work to do
    if not row["ok"]:
        return True
    return INCOMPLETE_MARK in (row["warnings"] or "")


def _already_exported(year, week):
    """True if the hours export has produced a file for that week already."""
    try:
        import hr_export
        return os.path.exists(os.path.join(
            hr_export.OUT_DIR, "Weekly Hours Tracker W%d %d.xlsx" % (week, year)))
    except Exception:
        return False


def run_import(weeks=8, dry_run=False, force=False, year=None, today=None,
               weeks_back=WEEKS_BACK):
    """Import the roster. Returns (ok, summary, warnings, detail)."""
    import openpyxl
    import app as m
    import db
    import schedule_io

    warnings, detail = [], []
    cfg = load_config()
    ignore_extra = {norm(x) for x in (cfg.get("ROSTER_IGNORE_LABELS") or "").split(",") if x.strip()}
    today = today or datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    # Start WEEKS_BACK weeks before this Monday: corrections to the week just gone are
    # normal (an absence entered after the fact), and a forward-only window never saw
    # them - Taras's ADV of 16 Nov was still showing his old shift ten days later, and
    # the HR export for that week was built from the stale figure.
    start = monday - datetime.timedelta(days=weeks_back * 7)
    window = [start + datetime.timedelta(days=i) for i in range((weeks + weeks_back) * 7)]

    with m.app.app_context():
        columns = db.query("SELECT id, position, label, user_id, retired "
                           "FROM schedule_columns ORDER BY position")
        labels = [c["label"] for c in columns]
        by_label = {norm(c["label"]): i for i, c in enumerate(columns)}

        # One workbook per calendar year the window touches, so a week spanning
        # New Year still gets both halves.
        years = sorted({(year or d.year) for d in window})
        sheets = {}
        for y in years:
            try:
                data, unc = fetch_workbook(cfg, y)
            except Exception as e:
                text = str(e)
                if "another process" in text or "0xc0000043" in text:
                    # Much the commonest failure: somebody has the master open in
                    # Excel, which takes a deny-read lock over SMB.
                    warnings.append("%s: somebody has the master workbook open in Excel, so it "
                                    "could not be read - those dates were left untouched" % y)
                else:
                    warnings.append("the %s workbook could not be read (%s: %s)"
                                    % (y, type(e).__name__, text))
                continue
            if data[:4] != b"PK\x03\x04":
                warnings.append("the %s workbook is still protected/encrypted, so it "
                                "could not be read" % y)
                continue
            vals = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
            sty = openpyxl.load_workbook(io.BytesIO(data))
            ws_v = vals[vals.sheetnames[0]]
            ws_s = sty[sty.sheetnames[0]]
            header_row, date_col, found = find_layout(ws_v, labels)
            if not header_row or not date_col:
                warnings.append("%s: could not find the names row or the date column" % y)
                continue
            unknown, ignored = [], []
            for c in range(1, ws_v.max_column + 1):
                v = ws_v.cell(row=header_row, column=c).value
                if not isinstance(v, str) or not norm(v) or c in found or len(norm(v)) <= 3:
                    continue
                if norm(v) in by_label or norm(v).isdigit():
                    continue
                (ignored if is_legend(norm(v), ignore_extra) else unknown).append(str(v).strip()[:30])
            if ignored:
                # One quiet line: these are expected, and burying a real problem in
                # them is how Piotr went a month without a roster.
                warnings.append("%s: ignored legend headings (%s)"
                                % (y, ", ".join(sorted(set(ignored))[:8])))
            for u in unknown[:10]:
                warnings.append("%s: sheet column '%s' matches no column in the app - "
                                "NOBODY RECEIVES THOSE SHIFTS" % (y, u))
            for lbl in labels:
                if norm(lbl) not in set(found.values()):
                    warnings.append("%s: column '%s' is not in the sheet - left untouched" % (y, lbl))
            # (month, day) -> row
            rowof = {}
            for r in range(header_row, ws_v.max_row + 1):
                v = ws_v.cell(row=r, column=date_col).value
                if isinstance(v, (datetime.date, datetime.datetime)):
                    d = v.date() if isinstance(v, datetime.datetime) else v
                    rowof.setdefault((d.month, d.day), r)
            sheets[y] = (ws_v, ws_s, found, rowof)
            detail.append("%s: names row %d, date column %d, %d names matched, %d dated rows"
                          % (y, header_row, date_col, len(found), len(rowof)))

        if not sheets:
            return False, "No workbook could be read - nothing was changed.", warnings, detail

        matched = max(len(s[2]) for s in sheets.values())
        if matched < MIN_NAMES_MATCHED and not force:
            return (False, "Refused: only %d names in the sheet matched a column (expected at "
                           "least %d). The template has probably changed - nothing was written."
                    % (matched, MIN_NAMES_MATCHED), warnings, detail)

        # Build the weeks, then decide whether to write them.
        planned, missing_days, unreadable_days = [], 0, 0
        for wstart in range(0, len(window), 7):
            days = window[wstart:wstart + 7]
            iso, rows = [], []
            for d in days:
                entry = sheets.get(year or d.year)
                if entry is None:
                    # That year's workbook could not be read at all. A day with no
                    # source is NOT an empty day: writing blanks here wiped a whole
                    # year's roster the moment somebody had the file open in Excel.
                    unreadable_days += 1
                    continue
                ws_v, ws_s, found, rowof = entry
                r = rowof.get((d.month, d.day))
                if r is None:
                    missing_days += 1
                    continue            # likewise: no row means no information
                row = [{"text": "", "color": ""} for _ in columns]
                for c, lbl in found.items():
                    idx = by_label.get(lbl)
                    if idx is None:
                        continue
                    row[idx] = {"text": cell_text(ws_v.cell(row=r, column=c).value),
                                "color": cell_color(ws_s.cell(row=r, column=c))}
                iso.append(d.isoformat())
                rows.append(row)
            if rows:
                planned.append((iso, rows))
        if missing_days:
            warnings.append("%d day(s) in the window have no row in the sheet - left untouched"
                            % missing_days)
        if unreadable_days:
            warnings.append("%d day(s) fall in a year whose workbook could not be read - "
                            "left untouched" % unreadable_days)
        if not planned:
            return (False, "Nothing could be read for any day in the window - nothing was "
                           "changed.", warnings, detail)

        now_iso = m.now_local().isoformat(timespec="microseconds")
        today_iso = today.isoformat()
        total_cells = sum(len(r) * len(rws) for r, rws in planned) or 1

        # Dry run first, so the gate can judge the size of the change.
        changed = overwritten = existing = 0
        for iso, rows in planned:
            c, _, o, e = schedule_io.apply_week(iso, rows, columns, now_iso, today_iso,
                                                dry_run=True)
            changed += c
            overwritten += o
            existing += e
        if existing >= MIN_EXISTING_TO_JUDGE and not force:
            frac = overwritten / float(existing)
            if frac > MAX_OVERWRITE_FRACTION:
                return (False, "Refused: the import would overwrite %d of the %d shifts already "
                               "in this window (%.0f%%), far more than expected. Check the "
                               "spreadsheet - nothing was written.%s"
                        % (overwritten, existing, frac * 100,
                           # Name the likeliest cause in the summary rather than leaving
                           # it buried in the warnings, where it went unread once already.
                           " Note: %d day(s) could not be read at all - see the warnings."
                           % unreadable_days if unreadable_days else ""),
                        warnings, detail)

        if dry_run:
            return (True, "Dry run: would change %d of %d cells over %d weeks "
                          "(%d of %d existing shifts overwritten)."
                    % (changed, total_cells, len(planned), overwritten, existing),
                    warnings, detail)

        notified, restated = [], []
        for iso, rows in planned:
            c, n, _, _ = schedule_io.apply_week(iso, rows, columns, now_iso, today_iso)
            for x in n:
                if x not in notified:
                    notified.append(x)
            # A change to a week HR has already been given means their workbook is now
            # out of date. Nobody would notice otherwise: the export never overwrites a
            # delivered file, so the stale one just sits there.
            if c and iso and iso[-1] < today_iso:
                wk = datetime.date.fromisoformat(iso[0]).isocalendar()
                if _already_exported(wk[0], wk[1]):
                    restated.append("W%d %d" % (wk[1], wk[0]))
        if restated:
            warnings.append("the roster changed for %s, which HR has already been sent - "
                            "re-run the hours export for those week(s)" % ", ".join(restated))
        summary = ("Imported %d weeks from the master file: %d cells changed%s."
                   % (len(planned), changed,
                      ("; shift change flagged to " + ", ".join(notified)) if notified else ""))
        return True, summary, warnings, detail


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    ap.add_argument("--weeks", type=int, default=8, help="weeks to import from this Monday (default 8)")
    ap.add_argument("--weeks-back", type=int, default=WEEKS_BACK,
                    help="weeks of the recent past to re-read, for late corrections "
                         "(default %d)" % WEEKS_BACK)
    ap.add_argument("--year", type=int, help="force a single workbook year")
    ap.add_argument("--start", help="YYYY-MM-DD: import from the Monday of this week "
                                    "instead of the current one (for backfilling)")
    ap.add_argument("--force", action="store_true", help="bypass the safety gate")
    ap.add_argument("--only-if-needed", action="store_true",
                    help="do nothing unless the last run was refused or could not read "
                         "some dates (for an hourly retry)")
    a = ap.parse_args()
    if a.only_if_needed and not last_run_incomplete():
        print("Nothing to retry: the last import read the whole roster.")
        return 0
    start = datetime.datetime.strptime(a.start, "%Y-%m-%d").date() if a.start else None
    ok, summary, warnings, detail = run_import(weeks=a.weeks, dry_run=a.dry_run,
                                               force=a.force, year=a.year, today=start,
                                               weeks_back=a.weeks_back)
    if not a.dry_run:
        # Record refusals and failures too - a blocked import must be visible on
        # the Schedule page, not just in cron's mail.
        import app as m
        with m.app.app_context():
            m.record_import_run_standalone(ok, summary, warnings)
    for d in detail:
        print("   ", d)
    for w in warnings:
        print("  warning:", w)
    print(("OK: " if ok else "FAILED: ") + summary)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
