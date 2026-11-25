"""Weekly hours export for HR.

Rebuilds the "Weekly Hours Tracker" workbook from the rostered hours: one sheet per
weekday plus a Total, in the same layout and with the same formulas HR already uses,
so the file can still be edited by hand when a week is unusual.

Hours come from the **roster**, not from the shift clock. The clock is an
operational tool and is not used consistently enough to pay from - three of ten
operators clocked nothing in a recent four-week stretch - so paying from it would
underpay people for work they did.

Category per operator per day (columns B..G):

    weekday  night shift            -> C  (x1.3)
    weekday  any other shift        -> B  (x1.1)
    Sat/Sun  night shift            -> G  (x2)    weekend overrides night
    Sat/Sun  any other shift        -> F  (x2)
    paid absence (ADV/FD/Sick/...)  -> E  (x1)    "holiday pay or bank holiday"
    column D (x1.5) is overtime     -> left empty on purpose, HR fills it in

The finished workbook is **placed in a folder on the share** (the same server the
roster is read from, credentials in ~/.roster.env), which is how HR picks it up. It is
never silently overwritten: HR types overtime into column D by hand, so if the file for
that week is already there, a re-run writes a dated copy beside it instead of destroying
their work. Mailing it is still possible (--mail) but off by default.

Usage:
    python hr_export.py                 # the week that just ended (run on a Tuesday)
    python hr_export.py --week 46       # a specific ISO week of this year
    python hr_export.py --start 2026-11-16
    python hr_export.py --no-publish    # write it locally only, touch the share not at all
    python hr_export.py --mail --to me@example.com    # also email it
"""
import argparse
import datetime
import io
import os
import smtplib
import sys
from email.message import EmailMessage

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

MAIL_CONFIG = os.environ.get("HR_MAIL_ENV", os.path.expanduser("~/.hrmail.env"))
OUT_DIR = os.environ.get("HR_EXPORT_DIR", os.path.expanduser("~/hr-exports"))

# The share credentials already live in ~/.roster.env (the roster is read from the same
# server), so publishing reuses them rather than asking for a second copy of a password.
SHARE_CONFIG = os.environ.get("HR_SHARE_ENV", os.path.expanduser("~/.roster.env"))
# Folder inside the share, i.e. L:\3_ebo_production\5_hr\Weekly hour exports.
# Override with HR_SHARE_DIR in ~/.roster.env when HR move it.
DEFAULT_SHARE_DIR = r"3_ebo_production\5_hr\Weekly hour exports"

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Columns B..G: (letter, group label, sub label, multiplier). Row 3 carries the
# multipliers, and the Cost formula multiplies by whatever is in row 3 - so these
# can be changed in the sheet without touching this file.
CATEGORIES = [
    ("B", "Day", 1.1),
    ("C", "Night", 1.3),
    ("D", "Day", 1.5),      # overtime - never filled in by this script
    ("E", "", 1),           # holiday pay / bank holiday
    ("F", "Day", 2),        # weekend
    ("G", "Night", 2),      # weekend
]
FIRST_ROW = 4               # operators start here, identical on every sheet
LAST_ROW = 24               # Total row sums FIRST_ROW..LAST_ROW, as in HR's file
TOTAL_ROW = 25


def load_env_file(path):
    """Read a KEY=value file. Missing file is not an error - the caller decides."""
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with io.open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def load_mail_config(path=MAIL_CONFIG):
    return load_env_file(path)


def publish_to_share(path, cfg=None):
    """Copy the finished workbook into HR's folder on the share.

    Never overwrites: HR fill in the overtime column by hand, so an existing file for
    that week is treated as theirs. A re-run lands beside it with a timestamp, and the
    caller reports that so nobody assumes the first file was refreshed.
    """
    cfg = cfg if cfg is not None else load_env_file(SHARE_CONFIG)
    host = cfg.get("HR_SHARE_HOST") or cfg.get("ROSTER_SMB_HOST")
    share = cfg.get("HR_SHARE_SHARE") or cfg.get("ROSTER_SMB_SHARE")
    user = cfg.get("HR_SHARE_USER") or cfg.get("ROSTER_SMB_USER")
    password = cfg.get("HR_SHARE_PASSWORD") or cfg.get("ROSTER_SMB_PASSWORD")
    folder = cfg.get("HR_SHARE_DIR") or DEFAULT_SHARE_DIR
    if not (host and share and user and password):
        return False, "share not configured (need ROSTER_SMB_* in %s) - local file only" % SHARE_CONFIG

    import smbclient
    smbclient.register_session(host, username=user, password=password)
    base = "\\\\%s\\%s\\%s" % (host, share, folder.strip("\\/").replace("/", "\\"))
    try:
        smbclient.makedirs(base, exist_ok=True)
    except Exception:
        pass            # already there, or no permission to create - the write will say
    name = os.path.basename(path)
    dest = base + "\\" + name
    try:
        exists = smbclient.path.exists(dest)
    except Exception:
        exists = False
    note = "placed in %s" % folder
    if exists:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M")
        name = "%s (rebuilt %s)%s" % (os.path.splitext(name)[0], stamp,
                                      os.path.splitext(name)[1])
        dest = base + "\\" + name
        note = "%s already there and left untouched - wrote %s beside it" % (
            os.path.basename(path), name)
    with open(path, "rb") as src, smbclient.open_file(dest, mode="wb") as out:
        out.write(src.read())
    return True, note


def monday_of_last_week(today=None):
    """The Monday of the week that just ended - what a Tuesday run should report."""
    today = today or datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())
    return this_monday - datetime.timedelta(days=7)


def monday_of_iso_week(year, week):
    return datetime.date.fromisocalendar(year, week, 1)


def build_rows(monday):
    """[{name, rate, days: {0..6: (column_letter, hours, note)}}] in roster order."""
    import app as m
    import db

    warnings = []
    days = [monday + datetime.timedelta(days=i) for i in range(7)]
    iso = [d.isoformat() for d in days]

    with m.app.app_context():
        colors = {r["hex"]: r["label"]
                  for r in db.query("SELECT hex, label FROM shift_colors")}
        cols = db.query(
            "SELECT c.position, c.label, c.user_id, c.retired, u.display_name, p.rate "
            "FROM schedule_columns c "
            "LEFT JOIN users u ON u.id = c.user_id "
            "LEFT JOIN pay_rates p ON p.user_id = c.user_id "
            "ORDER BY c.position")
        marks = ",".join(["%s"] * len(iso))
        cells = {}
        for r in db.query(
            "SELECT work_date, col_index, shift, color FROM schedule_cells "
            "WHERE work_date IN (%s)" % marks, tuple(iso)
        ):
            wd = r["work_date"] if isinstance(r["work_date"], str) else r["work_date"].isoformat()
            cells[(wd, r["col_index"])] = r

        rows = []
        for c in cols:
            # A retired column, or one with nobody linked, is not somebody to pay.
            if c["retired"] or not c["user_id"]:
                continue
            if c["rate"] is None:
                warnings.append("%s has no hourly rate set - hours exported, Cost will be 0"
                                % c["display_name"])
            entry = {"name": c["display_name"], "rate": c["rate"], "days": {}}
            for i, d in enumerate(days):
                cell = cells.get((d.isoformat(), c["position"]))
                if not cell:
                    continue
                text = (cell["shift"] or "").strip()
                if not text:
                    continue
                hours = m.roster_hours(text)
                label = m.schedule_shift_label(cell["color"], colors)
                weekend = d.weekday() >= 5
                if hours is None:
                    # A code with no number: a paid absence. Its length is not in the
                    # cell, so a standard day is credited (ABSENCE_DAY_HOURS).
                    entry["days"][i] = ("E", m.ABSENCE_DAY_HOURS, text)
                elif weekend:
                    entry["days"][i] = ("G" if label == "Night" else "F", hours, text)
                else:
                    entry["days"][i] = ("C" if label == "Night" else "B", hours, text)
            rows.append(entry)
    return rows, warnings


def _header(ws, week_no):
    """The header HR's own workbook uses, so the file stays familiar and editable."""
    from openpyxl.styles import Alignment, Font

    # Values and styles first: once a range is merged, every cell but the top-left
    # one is read-only, so merging early makes writing E2 impossible.
    ws["A1"] = "Employee"
    ws["B1"] = "WEEK %d" % week_no
    ws["F1"] = "WEEKEND"
    ws["H1"] = "Total Hours"
    ws["I1"] = "Rate"
    ws["J1"] = "Cost"
    for letter, sub, mult in CATEGORIES:
        ws["%s2" % letter] = sub
        ws["%s3" % letter] = mult
    for row in ("1", "2", "3"):
        for letter in "ABCDEFGHIJ":
            cell = ws["%s%s" % (letter, row)]
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
    for rng in ("A1:A3", "B1:E1", "F1:G1", "H1:H3", "I1:I3", "J1:J3", "D2:E2"):
        ws.merge_cells(rng)
    ws.column_dimensions["A"].width = 18
    for letter in "BCDEFGHIJ":
        ws.column_dimensions[letter].width = 12


def build_workbook(monday, rows):
    import openpyxl

    week_no = monday.isocalendar()[1]
    wb = openpyxl.Workbook()
    total = wb.active
    total.title = "Total"
    sheets = {}
    for d in DAYS:
        sheets[d] = wb.create_sheet(d)

    for ws in [total] + [sheets[d] for d in DAYS]:
        _header(ws, week_no)

    for i, entry in enumerate(rows):
        r = FIRST_ROW + i
        if r > LAST_ROW:
            break
        for ws in [total] + [sheets[d] for d in DAYS]:
            ws["A%d" % r] = entry["name"]
            ws["I%d" % r] = entry["rate"] if entry["rate"] is not None else None
        for day_i, day_name in enumerate(DAYS):
            ws = sheets[day_name]
            placed = entry["days"].get(day_i)
            if placed:
                letter, hours, note = placed
                ws["%s%d" % (letter, r)] = hours
                # The roster code itself, so HR can see why a day landed where it did.
                ws["%s%d" % (letter, r)].comment = None
                if letter == "E":
                    ws["K%d" % r] = note
        # Same formulas as HR's file, so edited cells still recalculate.
        for ws in [sheets[d] for d in DAYS]:
            ws["H%d" % r] = "=SUM(B%d:G%d)" % (r, r)
            ws["J%d" % r] = "=" + "+".join(
                "(%s%d*%s$3*$I%d)" % (L, r, L, r) for L, _s, _m in CATEGORIES)
        for letter in "BCDEFGHJ":
            total["%s%d" % (letter, r)] = "=" + "+".join(
                "%s!%s%d" % (d, letter, r) for d in DAYS)
        # Guarded, unlike the original: an operator with no hours showed #DIV/0!
        total["I%d" % r] = '=IFERROR(J%d/H%d,"")' % (r, r)

    for ws in [total] + [sheets[d] for d in DAYS]:
        ws["A%d" % TOTAL_ROW] = "Total"
        for letter in "BCDEFGHIJ":
            ws["%s%d" % (letter, TOTAL_ROW)] = "=SUM(%s%d:%s%d)" % (
                letter, FIRST_ROW, letter, LAST_ROW)
    total["E28"] = "Dayshift 100% is holiday pay OR Bank Holiday"
    total["E29"] = ("Day 1.5 is overtime and is left empty by the export - fill it in by hand.")
    total["E30"] = ("Hours come from the roster. A code such as ADV/FD/Sick/SOL counts as "
                    "%.1f h of holiday pay; the code is shown in column K." % _absence_hours())
    return wb


def _absence_hours():
    import app as m
    return m.ABSENCE_DAY_HOURS


def send_mail(cfg, path, monday, rows, warnings):
    """Mail the workbook. Refuses unless HR_MAIL_ENABLED is yes, so a half-finished
    setup cannot reach HR by accident."""
    if (cfg.get("HR_MAIL_ENABLED") or "no").lower() not in ("yes", "true", "1"):
        return False, "mail disabled (HR_MAIL_ENABLED is not yes) - file written only"
    to = [x.strip() for x in (cfg.get("HR_MAIL_TO") or "").split(",") if x.strip()]
    if not to:
        return False, "no HR_MAIL_TO set - file written only"
    sunday = monday + datetime.timedelta(days=6)
    week_no = monday.isocalendar()[1]

    msg = EmailMessage()
    msg["Subject"] = "Hours week %d (%s - %s)" % (
        week_no, monday.strftime("%d %b"), sunday.strftime("%d %b %Y"))
    msg["From"] = cfg.get("HR_MAIL_FROM") or (cfg.get("HR_SMTP_USER") or "noreply@biopower.be")
    msg["To"] = ", ".join(to)
    cc = [x.strip() for x in (cfg.get("HR_MAIL_CC") or "").split(",") if x.strip()]
    if cc:
        msg["Cc"] = ", ".join(cc)
    body = ["Rostered hours for week %d (%s to %s), %d operators." %
            (week_no, monday.isoformat(), sunday.isoformat(), len(rows)), ""]
    body.append("Hours come from the roster. Overtime (Day 1.5) is left empty to fill in.")
    if warnings:
        body += ["", "Worth checking:"] + ["  - " + w for w in warnings]
    msg.set_content("\n".join(body))
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", filename=os.path.basename(path),
                           subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    host = cfg.get("HR_SMTP_HOST")
    if not host:
        return False, "no HR_SMTP_HOST set - file written only"
    port = int(cfg.get("HR_SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=30) as s:
        if (cfg.get("HR_SMTP_STARTTLS") or "yes").lower() in ("yes", "true", "1"):
            s.starttls()
        if cfg.get("HR_SMTP_USER"):
            s.login(cfg["HR_SMTP_USER"], cfg.get("HR_SMTP_PASSWORD") or "")
        s.send_message(msg)
    return True, "emailed to %s" % ", ".join(to)


def run_export(monday=None, mail=False, out_dir=OUT_DIR, to=None, publish=True):
    monday = monday or monday_of_last_week()
    rows, warnings = build_rows(monday)
    if not rows:
        return False, "No operators with a linked schedule column - nothing to export.", None, warnings
    wb = build_workbook(monday, rows)
    os.makedirs(out_dir, exist_ok=True)
    name = "Weekly Hours Tracker W%d %d.xlsx" % (monday.isocalendar()[1], monday.isocalendar()[0])
    path = os.path.join(out_dir, name)
    wb.save(path)

    # The local copy under ~/hr-exports is written first and always kept, so a share
    # that is down or full never costs you the week's figures.
    notes = []
    if publish:
        try:
            _, pnote = publish_to_share(path)
        except Exception as e:
            pnote = "share write FAILED (%s: %s) - the file is still at %s" % (
                type(e).__name__, e, path)
            warnings.append(pnote)
        notes.append(pnote)

    note = "not mailed"
    if mail:
        cfg = load_mail_config()
        if to:
            cfg["HR_MAIL_TO"] = to
            cfg["HR_MAIL_ENABLED"] = "yes"
        try:
            _, note = send_mail(cfg, path, monday, rows, warnings)
        except Exception as e:
            note = "mail FAILED (%s: %s) - the file is still at %s" % (type(e).__name__, e, path)
            warnings.append(note)
        notes.append(note)
    summary = "Week %d: %d operators, %s; %s" % (
        monday.isocalendar()[1], len(rows), os.path.basename(path),
        "; ".join(notes) or "local file only")
    return True, summary, path, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--week", type=int, help="ISO week number (default: the week that just ended)")
    ap.add_argument("--year", type=int, default=datetime.date.today().year)
    ap.add_argument("--start", help="YYYY-MM-DD inside the week to export")
    ap.add_argument("--no-publish", action="store_true",
                    help="write the file locally only, do not touch the share")
    ap.add_argument("--mail", action="store_true", help="also email it (off by default)")
    ap.add_argument("--to", help="email it to this address instead of HR (for testing)")
    a = ap.parse_args()
    if a.week:
        monday = monday_of_iso_week(a.year, a.week)
    elif a.start:
        d = datetime.datetime.strptime(a.start, "%Y-%m-%d").date()
        monday = d - datetime.timedelta(days=d.weekday())
    else:
        monday = monday_of_last_week()
    ok, summary, path, warnings = run_export(monday=monday, mail=a.mail or bool(a.to),
                                             to=a.to, publish=not a.no_publish)
    for w in warnings:
        print("  warning:", w)
    print(("OK: " if ok else "FAILED: ") + summary)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
