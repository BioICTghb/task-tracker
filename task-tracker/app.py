"""
Task Tracker — minimal Flask backend.

Managers paste a list -> tasks created.
Technicians take before/after photos -> status auto-advances.
Photos are stored on disk under uploads/YYYY/MM/DD/.

Designed and developed by Sébastien Poncelet.
Copyright (c) Sébastien Poncelet. All rights reserved.
"""
import os
import re
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, send_from_directory, send_file, abort,
    session, redirect, url_for, g,
)
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config
import db

# Pillow is used server-side as a safety net: normalise uploads that aren't already
# JPEG (notably iPhone HEIC saved with a .jpg name) into browser-displayable JPEG.
from PIL import Image, ImageOps
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # HEIC support optional; JPEG/PNG still handled
    pass

# All timestamps are stored and shown in Belgian time (auto summer/winter DST),
# regardless of the timezone the server itself runs in.
BRUSSELS = ZoneInfo("Europe/Brussels")

# App version — bump this on each release. Shown in the footer of every page.
APP_VERSION = "1.5.0"

# Operators who live on site and log rent hours — only they see the Rent button.
RENT_USERNAMES = {"taras_d", "ivan_d", "sebastian_r", "tomasz_m", "sebastien_p"}

# Roles with manager-level access (publish, report, edit, admin). 'developer' is
# a manager with a different label.
MANAGER_ROLES = {"manager", "developer"}

# Assignable roles and how they're labelled. 'developer' is manager-level with a
# different title, so it must never be presented as an operator.
ROLE_LABELS = {"manager": "Manager", "developer": "Developer", "technician": "Operator"}

# Work-schedule grid columns (C1-C12), fixed order matching the Excel sheet
# managers currently keep shifts in.
SCHEDULE_OPERATORS = [
    "Savrii Anatolii", "Maksym Hnyp", "Marcin Piatkowski", "Tomasz Madry",
    "Taras Dymkar", "Sebastian Rek", "Daniel Gorzynski", "Damian Meczykowski",
    "Damian Garczynski", "Ivan Dymkar", "Savrii Volodymyr", "Bartek Berendt",
]
SCHEDULE_ROWS = 7  # Mon-Sun (weekend shifts are worked too)

# Schedule cell colors that map to an actual shift (matches the swatches offered
# on the Schedule page). Any other color (ADV/holiday/JV/other) or no color is
# left out of the "who's on shift" report summary. Matching is nearest-color
# (see schedule_shift_label below), not exact-hex — pasted colors from Excel
# vary slightly by file/theme even for "the same" color.
SCHEDULE_SHIFT_COLORS = {
    "#92d050": "Early",
    "#ffc000": "Day",
    "#00b0f0": "Late",     # blue: was labelled "Night", but it is the late shift
    "#ff0000": "Night",    # red: the actual night shift
    # Excel's other standard red ("Dark Red") is far enough from #ff0000 to fall
    # outside the match tolerance, so anchor it explicitly rather than lose it.
    "#c00000": "Night",
    "#ffffcc": "Drive",
}

# Cell text that reads better to everyone else than the word pasted from Excel.
# Matched case-insensitively; anything not listed is shown exactly as typed.
SCHEDULE_TEXT_LABELS = {
    "drijfveer": "pro-rate",
}
SCHEDULE_SHIFT_MATCH_TOLERANCE = 60  # max RGB Euclidean distance to still count as a match


def schedule_shift_label(hex_color):
    """Nearest-color match against SCHEDULE_SHIFT_COLORS, within tolerance.
    Returns None if the color is missing/unrecognized (blank, ADV, holiday, JV, etc.)."""
    hex_color = (hex_color or "").strip().lstrip("#")
    if len(hex_color) != 6:
        return None
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None
    best_label, best_dist = None, SCHEDULE_SHIFT_MATCH_TOLERANCE
    for ref_hex, label in SCHEDULE_SHIFT_COLORS.items():
        rr, rg, rb = (int(ref_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        dist = ((r - rr) ** 2 + (g - rg) ** 2 + (b - rb) ** 2) ** 0.5
        if dist < best_dist:
            best_label, best_dist = label, dist
    return best_label


# Clock-in/out. A shift never runs longer than this, whatever the roster says —
# it is the backstop for an operator who forgets to clock out.
SHIFT_MAX_HOURS = 12.0

# How long after clock-in a clock-out is still accepted before the shift is treated
# as forgotten. Longer than SHIFT_MAX_HOURS on purpose: a night shift plus handover
# runs past 12 h door to door, and those operators must still be able to clock out
# (the recorded length is then capped at SHIFT_MAX_HOURS).
SHIFT_CLOCKOUT_WINDOW_HOURS = 16.0

# A daily belongs to its *working* day, and night shifts run past midnight. Until
# this hour the next morning, yesterday's started dailies stay live - still in the
# operator's list and untouched by the sweep - so a night worker can finish them.
DAILY_GRACE_HOUR = 12

# Roster cell texts that mean a shift without stating its length.
SHIFT_NAMED_HOURS = {"DRIVE": 7.6, "DRIJFVEER": 7.6}


def roster_hours(text):
    """Hours a roster cell is worth: '7,6' -> 7.6, 'DRIVE' -> 7.6. None for
    holiday/ADV/JV or anything unrecognised — those are not shifts, so a clock-in
    against one has no rostered length to expire against."""
    t = (text or "").strip().upper()
    if not t:
        return None
    if t in SHIFT_NAMED_HOURS:
        return SHIFT_NAMED_HOURS[t]
    m = re.fullmatch(r"(\d{1,2})(?:[.,](\d{1,2}))?", t)
    if not m:
        return None
    hours = float(m.group(1) + "." + (m.group(2) or "0"))
    return hours if 0 < hours <= 24 else None


def shift_text_label(text):
    """How a schedule cell's text is shown to people (report badges, the operator's
    own week). The grid itself keeps whatever the manager pasted from Excel."""
    t = (text or "").strip()
    return SCHEDULE_TEXT_LABELS.get(t.lower(), t)


def asset_version(static_dir):
    """Short hash of the static css/js (name + size + mtime), used to stamp the
    service-worker cache name and to cache-bust <link>/<script> URLs. Computed
    once per process, so a deploy (which restarts the service) picks up changes
    automatically instead of relying on a hand-edited version number."""
    import hashlib
    h = hashlib.sha1()
    for sub in ("css", "js"):
        d = os.path.join(static_dir, sub)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(("%s:%d:%d;" % (name, st.st_size, int(st.st_mtime))).encode())
    sw = os.path.join(static_dir, "service-worker.js")
    if os.path.exists(sw):
        st = os.stat(sw)
        h.update(("sw:%d:%d;" % (st.st_size, int(st.st_mtime))).encode())
    return h.hexdigest()[:10]


# Operators on one job are stored in tasks.assigned_to as a "A, B" list — jobs
# are regularly shared ("Ivan Dymkar & Taras Dymkar"), and both need to see it.
ASSIGN_SEP = ", "


def parse_assignees(title, operator_names):
    """Every operator named in a published line, in the order they appear.

    The '@' picker in the publish box inserts just the operator's name (the '@' is
    only the trigger — the job reads cleaner without it), so names are matched
    bare here; a leading '@' is still accepted for lines typed by hand.

    Matched whole-word, longest name first, so 'Damian M' is not claimed from
    inside 'Damian Meczykowski'. Because a match needs the operator's full display
    name, an incidental mention is unlikely -- and a line that does name an
    operator in full is fair to treat as theirs."""
    text = title or ""
    found = []                      # (position in text, name)
    claimed = []                    # spans already taken by a longer name
    for name in sorted(operator_names or [], key=len, reverse=True):
        pattern = r"(?<!\w)@?" + re.escape(name) + r"(?!\w)"
        for mt in re.finditer(pattern, text, re.IGNORECASE):
            if any(mt.start() < end and start < mt.end() for start, end in claimed):
                continue            # inside a longer name already matched
            claimed.append((mt.start(), mt.end()))
            found.append((mt.start(), name))
            break                   # one mention per operator is enough
    return [name for _, name in sorted(found)]


def assignees_of(value):
    """tasks.assigned_to -> list of operator names."""
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def now_local():
    return datetime.now(BRUSSELS)


def today_local():
    return now_local().strftime("%Y-%m-%d")


def daily_grace_day():
    """The day whose *started* daily instances are still live alongside today's:
    yesterday until DAILY_GRACE_HOUR, then today (so nothing extra)."""
    now = now_local()
    if now.hour < DAILY_GRACE_HOUR:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def split_daily_prefix(line):
    """If a line starts with 'DAILY' (any case, followed by a space/separator),
    return (True, rest-without-prefix); otherwise (False, line)."""
    s = line.lstrip()
    if s[:5].lower() == "daily" and (len(s) == 5 or not s[5].isalpha()):
        rest = s[5:].lstrip(" \t-:–—")
        if rest:
            return True, rest
    return False, line


# Normal day shift. Reported time only counts hours inside this window, so a job
# spanning multiple days is capped at 8 h/day and idle nights don't inflate it.
SHIFT_START_HOUR = 8   # 08:00
SHIFT_END_HOUR = 16    # 16:00


def worked_seconds(start, end):
    """Seconds worked between start and end.

    Single-day jobs count actual elapsed time (capped at one 8h shift), so
    early-morning (pre-08:00) or evening work still counts. Multi-day jobs count
    only each day's shift window (08:00-16:00) so idle nights don't inflate them,
    e.g. 05/07 14:00 -> 07/07 14:00 = 2h + 8h + 6h = 16h."""
    if not start or not end or end <= start:
        return 0
    if start.date() == end.date():
        secs = int((end - start).total_seconds())
        return min(secs, (SHIFT_END_HOUR - SHIFT_START_HOUR) * 3600)
    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        ws = datetime(day.year, day.month, day.day, SHIFT_START_HOUR)
        we = datetime(day.year, day.month, day.day, SHIFT_END_HOUR)
        lo = start if start > ws else ws       # later of (start, shift start)
        hi = end if end < we else we           # earlier of (end, shift end)
        if hi > lo:
            total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return int(total)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    # Make sure the upload root exists
    os.makedirs(app.config["UPLOAD_ROOT"], exist_ok=True)

    # Hash of the static assets, used for cache-busting and the SW cache name.
    ASSET_VERSION = asset_version(os.path.join(app.root_path, "static"))

    # Expose the version (and author) to every template.
    @app.context_processor
    def inject_app_meta():
        return {"app_version": APP_VERSION, "app_author": "Sébastien Poncelet",
                "asset_v": ASSET_VERSION}

    # --------------------------------------------------------------- auth setup
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Secure cookie when served over HTTPS (the production tunnel). Off for local http.
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

    def ensure_seed_manager():
        """Create a starter manager account on a fresh install so someone can log in."""
        with app.app_context():
            count = db.query("SELECT COUNT(*) AS n FROM users", one=True)["n"]
            if count == 0:
                db.execute(
                    "INSERT INTO users (username, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, 'manager')",
                    ("manager", generate_password_hash("biopower"), "Manager"),
                )
                app.logger.warning(
                    "Seeded default manager account: username 'manager' / password 'biopower' "
                    "- change this in the Operators page after first login."
                )

    def baseline_schedule_seen():
        """Give every account a schedule_seen_at, so a shift change is never
        swallowed. Without this an operator who hasn't opened the app yet has a
        NULL marker, and their first view would baseline *after* the change --
        silently losing the very notice the manager was told had been sent. Runs
        once (only NULL rows), so on deploy everyone counts as up to date now and
        only later changes raise the banner."""
        with app.app_context():
            db.execute("UPDATE users SET schedule_seen_at=%s WHERE schedule_seen_at IS NULL",
                       (now_local().isoformat(timespec="microseconds"),))

    ensure_seed_manager()
    baseline_schedule_seen()

    PUBLIC_ENDPOINTS = {"login", "static", "service_worker", "manifest", "healthz"}

    def current_user():
        return session.get("user")

    def active_operators():
        """Display names of active operators — the '@' autocomplete list and the
        vocabulary parse_assignees() matches published lines against."""
        return [u["display_name"] for u in db.query(
            "SELECT display_name FROM users WHERE role='technician' AND active=1 "
            "ORDER BY display_name")]

    @app.context_processor
    def inject_user():
        return {"current_user": current_user()}

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if current_user():
            return None
        # Not logged in: APIs get a clean 401, pages get redirected to login.
        if request.path.startswith("/api/"):
            return jsonify({"error": "login required"}), 401
        return redirect(url_for("login", next=request.full_path))

    def manager_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            u = current_user()
            if not u or u.get("role") not in MANAGER_ROLES:
                abort(403)
            return view(*a, **kw)
        return wrapped

    # ----------------------------------------------------------------- helpers
    def parse_ts(value):
        """Client ISO timestamp -> Belgian-local datetime (fallback: now)."""
        if value:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:           # assume UTC if no offset given
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(BRUSSELS)
            except ValueError:
                pass
        return now_local()

    def photo_dir_for(now):
        rel = os.path.join(now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
        absdir = os.path.join(app.config["UPLOAD_ROOT"], rel)
        os.makedirs(absdir, exist_ok=True)
        return rel, absdir

    def normalize_to_jpeg(path):
        """If a just-saved upload isn't already JPEG (iPhone HEIC, PNG, ...), re-encode
        it to JPEG in place so every browser can display it. EXIF orientation is applied
        and the image is capped at 1600px (matching the client). Best-effort: on any
        failure the original file is left untouched."""
        try:
            with open(path, "rb") as f:
                head = f.read(3)
        except OSError:
            return
        if head == b"\xff\xd8\xff":
            return  # already JPEG — leave as-is (no re-encode)
        try:
            img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            img.thumbnail((1600, 1600))
            img.save(path, "JPEG", quality=85)
        except Exception:
            app.logger.warning("Could not normalise upload to JPEG: %s", path)

    # ------------------------------------------------------------------- pages
    def is_mobile():
        """Rough phone/tablet detection from the User-Agent."""
        ua = request.user_agent.string.lower()
        return any(k in ua for k in (
            "android", "iphone", "ipad", "ipod", "mobile", "windows phone",
        ))

    # -------------------------------------------------------------- auth routes
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            row = db.query(
                "SELECT id, username, password_hash, display_name, role, active "
                "FROM users WHERE username=%s",
                (username,), one=True,
            )
            if row and row["active"] and check_password_hash(row["password_hash"], password):
                session.permanent = True
                session["user"] = {
                    "id": row["id"], "username": row["username"],
                    "name": row["display_name"], "role": row["role"],
                }
                nxt = request.form.get("next") or request.args.get("next") or ""
                if nxt.startswith("/") and not nxt.startswith("//"):
                    return redirect(nxt)
                return redirect(url_for("index"))
            return render_template("login.html", error="Wrong username or password.")
        if current_user():
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def index():
        # Role drives the landing page; technicians never see the publish/report side.
        if current_user().get("role") == "technician":
            return redirect(url_for("tech_list"))
        ensure_todays_dailies()
        # Operators for the "@" mention autocomplete in the publish box.
        return render_template("manager.html", operators=active_operators())

    # ----------------------------------------------------------- operators (admin)
    @app.route("/admin")
    @manager_required
    def admin():
        users = db.query(
            "SELECT id, username, display_name, role, active, created_at "
            "FROM users ORDER BY role, username"
        )
        return render_template("admin.html", users=users, msg=request.args.get("msg"),
                               role_labels=ROLE_LABELS)

    @app.route("/admin/users", methods=["POST"])
    @manager_required
    def admin_create_user():
        username = (request.form.get("username") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") if request.form.get("role") in ROLE_LABELS else "technician"
        if not username or not display_name or len(password) < 4:
            return redirect(url_for("admin", msg="Name, login and a 4+ char password are required."))
        if db.query("SELECT id FROM users WHERE username=%s", (username,), one=True):
            return redirect(url_for("admin", msg="That login already exists."))
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, role, schedule_seen_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (username, generate_password_hash(password), display_name, role,
             # Baselined at creation, so a shift change made before this operator
             # first opens the app still raises their banner.
             now_local().isoformat(timespec="microseconds")),
        )
        return redirect(url_for("admin", msg=f"Added {display_name}."))

    @app.route("/admin/users/<int:uid>/password", methods=["POST"])
    @manager_required
    def admin_reset_password(uid):
        password = request.form.get("password") or ""
        if len(password) < 4:
            return redirect(url_for("admin", msg="Password must be at least 4 characters."))
        db.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                   (generate_password_hash(password), uid))
        return redirect(url_for("admin", msg="Password updated."))

    def _manager_level_count():
        """Accounts that can still administer the app. Counts every manager-level
        role, not just 'manager' — a developer keeps the lights on too."""
        marks = ",".join(["%s"] * len(MANAGER_ROLES))
        return db.query(
            f"SELECT COUNT(*) AS n FROM users WHERE active=1 AND role IN ({marks})",
            tuple(sorted(MANAGER_ROLES)), one=True)["n"]

    @app.route("/admin/users/<int:uid>/delete", methods=["POST"])
    @manager_required
    def admin_delete_user(uid):
        if uid == current_user()["id"]:
            return redirect(url_for("admin", msg="You can't delete your own account."))
        target = db.query("SELECT role FROM users WHERE id=%s", (uid,), one=True)
        if target and target["role"] in MANAGER_ROLES and _manager_level_count() <= 1:
            return redirect(url_for("admin", msg="Keep at least one manager-level account."))
        db.execute("DELETE FROM users WHERE id=%s", (uid,))
        return redirect(url_for("admin", msg="Operator removed."))

    @app.route("/admin/users/<int:uid>/role", methods=["POST"])
    @manager_required
    def admin_set_role(uid):
        """Change someone's role — the only way to hand out 'developer', which is
        manager-level with its own title."""
        role = request.form.get("role")
        if role not in ROLE_LABELS:
            return redirect(url_for("admin", msg="Unknown role."))
        target = db.query("SELECT role, display_name FROM users WHERE id=%s", (uid,), one=True)
        if not target:
            abort(404)
        losing_access = target["role"] in MANAGER_ROLES and role not in MANAGER_ROLES
        if losing_access:
            if uid == current_user()["id"]:
                return redirect(url_for("admin", msg="You can't remove your own manager access."))
            if _manager_level_count() <= 1:
                return redirect(url_for("admin", msg="Keep at least one manager-level account."))
        db.execute("UPDATE users SET role=%s WHERE id=%s", (role, uid))
        return redirect(url_for("admin",
                                msg=f"{target['display_name']} is now {ROLE_LABELS[role]}."))

    # ----------------------------------------------------------- work schedule
    def _monday_of(d):
        return d - timedelta(days=d.weekday())

    def _month_bounds(year, month):
        first = datetime(year, month, 1).date()
        if month == 12:
            nxt = datetime(year + 1, 1, 1).date()
        else:
            nxt = datetime(year, month + 1, 1).date()
        return first, nxt - timedelta(days=1)

    def _month_week_starts(first, last):
        """Monday of each week (Mon-Sun block) that overlaps the given month."""
        monday = _monday_of(first)
        starts = []
        while monday <= last:
            starts.append(monday)
            monday += timedelta(days=7)
        return starts

    @app.route("/schedule")
    @manager_required
    def schedule():
        mo = request.args.get("month")
        try:
            anchor = datetime.strptime(mo, "%Y-%m").date() if mo else now_local().date()
        except ValueError:
            anchor = now_local().date()
        year, month = anchor.year, anchor.month
        first, last = _month_bounds(year, month)
        week_starts = _month_week_starts(first, last)
        range_from, range_to = week_starts[0], week_starts[-1] + timedelta(days=SCHEDULE_ROWS - 1)

        rows = db.query(
            "SELECT work_date, col_index, shift, color FROM schedule_cells "
            "WHERE work_date BETWEEN %s AND %s",
            (range_from.isoformat(), range_to.isoformat()),
        )
        grid = {}
        for r in rows:
            wd = r["work_date"] if isinstance(r["work_date"], str) else r["work_date"].isoformat()
            grid[(wd, r["col_index"])] = {"text": r["shift"], "color": r["color"] or ""}
        empty = {"text": "", "color": ""}

        weeks = []
        for wstart in week_starts:
            dates = [wstart + timedelta(days=i) for i in range(SCHEDULE_ROWS)]
            table = [
                {
                    "date": d.isoformat(),
                    "label": d.strftime("%a %d %b"),
                    "cells": [grid.get((d.isoformat(), c + 1), empty) for c in range(len(SCHEDULE_OPERATORS))],
                }
                for d in dates
            ]
            weeks.append({"week_start": wstart.isoformat(), "table": table})

        prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return render_template(
            "schedule.html", operators=SCHEDULE_OPERATORS, weeks=weeks,
            month_label=first.strftime("%B %Y"),
            prev_month="%04d-%02d" % prev_month,
            next_month="%04d-%02d" % next_month,
        )

    def _operator_col_for(display_name):
        """Match a logged-in user's display name to a Schedule column, so an
        operator can see their own shifts. Tolerant of abbreviated last names
        (e.g. account "Sebastian R" vs roster "Sebastian Rek", matching the
        first_l initial-only convention already used for usernames like
        taras_d/ivan_d). None if they're not on the roster."""
        dn_tokens = (display_name or "").strip().lower().split()
        if not dn_tokens:
            return None
        matches = []
        for i, name in enumerate(SCHEDULE_OPERATORS, start=1):
            name_tokens = name.strip().lower().split()
            if not name_tokens or dn_tokens[0] != name_tokens[0]:
                continue
            if len(dn_tokens) == 1:
                matches.append(i)  # first-name-only; resolved below only if unique
                continue
            dn_last, name_last = dn_tokens[-1], name_tokens[-1]
            if dn_last == name_last or dn_last.startswith(name_last) or name_last.startswith(dn_last):
                matches.append(i)
        # Two roster entries can share a first name (e.g. two "Savrii"s) — only
        # trust the match if it's unambiguous, otherwise don't guess.
        return matches[0] if len(matches) == 1 else None

    @app.route("/api/my-schedule")
    def api_my_schedule():
        """One week's shifts for the logged-in operator (matched by display name
        against the Schedule roster). ?offset=N picks a week N weeks from the
        current one (0 = this week, -1 = last week, 1 = next week, ...). Used by
        the technician's task list to show a small 'my schedule' widget with a
        prev/today/next week scroller."""
        user = current_user() or {}
        col = _operator_col_for(user.get("name"))
        if not col:
            return jsonify({"days": [], "on_roster": False})
        try:
            offset = int(request.args.get("offset", 0))
        except ValueError:
            offset = 0
        offset = max(-52, min(52, offset))  # sane bounds against abuse
        monday = _monday_of(now_local().date()) + timedelta(days=7 * offset)
        days = [monday + timedelta(days=i) for i in range(SCHEDULE_ROWS)]
        iso_days = [d.isoformat() for d in days]
        placeholders = ",".join(["%s"] * len(iso_days))
        rows = db.query(
            f"SELECT work_date, shift, color FROM schedule_cells "
            f"WHERE col_index=%s AND work_date IN ({placeholders})",
            (col, *iso_days),
        )
        by_date = {}
        for r in rows:
            wd = r["work_date"] if isinstance(r["work_date"], str) else r["work_date"].isoformat()
            by_date[wd] = r
        out = []
        for d in days:
            iso = d.isoformat()
            r = by_date.get(iso)
            out.append({
                "date": iso, "label": d.strftime("%a %d %b"),
                "text": shift_text_label(r["shift"] if r else ""),
                "color": (r["color"] if r else "") or "",
            })
        return jsonify({
            "days": out, "on_roster": True, "offset": offset,
            "week_label": f"{days[0].strftime('%d %b')} – {days[-1].strftime('%d %b')}",
        })

    _HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

    @app.route("/api/schedule", methods=["POST"])
    @manager_required
    def api_save_schedule():
        """Bulk-save the grid for one week. Body:
        {"week_start": "YYYY-MM-DD",
         "cells": [[{"text": "...", "color": "#rrggbb"}, ...12], ...5 rows]}."""
        data = request.get_json(silent=True) or {}
        try:
            monday = datetime.strptime(data.get("week_start") or "", "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "invalid week_start"}), 400
        cells = data.get("cells")
        if not isinstance(cells, list) or len(cells) != SCHEDULE_ROWS or \
           any(not isinstance(row, list) or len(row) != len(SCHEDULE_OPERATORS) for row in cells):
            return jsonify({"error": "invalid cells"}), 400

        upsert_sql = (
            """INSERT INTO schedule_cells (work_date, col_index, shift, color, updated_at) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(work_date, col_index) DO UPDATE SET shift=excluded.shift, color=excluded.color, updated_at=excluded.updated_at"""
            if db.IS_SQLITE else
            """INSERT INTO schedule_cells (work_date, col_index, shift, color, updated_at) VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE shift=VALUES(shift), color=VALUES(color), updated_at=VALUES(updated_at)"""
        )
        # Existing values for this week, so updated_at only advances on a *real*
        # change (the client re-saves the whole grid; we must not flag no-ops).
        week_isos = [(monday + timedelta(days=i)).isoformat() for i in range(SCHEDULE_ROWS)]
        ph = ",".join(["%s"] * len(week_isos))
        prev = {}
        for r in db.query(
            f"SELECT work_date, col_index, shift, color, updated_at FROM schedule_cells "
            f"WHERE work_date IN ({ph})", tuple(week_isos)):
            wd = r["work_date"] if isinstance(r["work_date"], str) else r["work_date"].isoformat()
            prev[(wd, r["col_index"])] = r
        now_iso = now_local().isoformat(timespec="microseconds")
        today_iso = now_local().date().isoformat()
        notified = []          # operators whose *upcoming* shifts changed

        for i, row in enumerate(cells):
            d = week_isos[i]
            for c, cell in enumerate(row):
                cell = cell if isinstance(cell, dict) else {"text": cell, "color": ""}
                text = (cell.get("text") or "").strip()[:64]
                color = (cell.get("color") or "").strip()
                color = color if _HEX_COLOR_RE.match(color) else ""
                old = prev.get((d, c + 1))
                if old is None:
                    # New cell only counts as a change if it actually has a shift.
                    changed = text != ""
                    keep = None
                else:
                    changed = (old["shift"] or "") != text or (old["color"] or "") != color
                    keep = old["updated_at"]
                db.execute(upsert_sql, (d, c + 1, text, color, now_iso if changed else keep))
                if changed and d >= today_iso and c < len(SCHEDULE_OPERATORS):
                    name = SCHEDULE_OPERATORS[c]
                    if name not in notified:
                        notified.append(name)
        # Tell the manager who will see a "your shift changed" notice, so saving
        # isn't a silent act with invisible consequences.
        return jsonify({"ok": True, "notified": notified})

    @app.route("/api/schedule-changes")
    def api_schedule_changes():
        """Upcoming shifts for the logged-in operator that changed since they last
        acknowledged their schedule. Drives the "your schedule changed" banner on
        the task list. First-ever view silently baselines (no alarm about the
        schedule that already existed)."""
        user = current_user() or {}
        uid = user.get("id")
        col = _operator_col_for(user.get("name"))
        if not uid or not col:
            return jsonify({"changes": [], "count": 0})
        row = db.query("SELECT schedule_seen_at FROM users WHERE id=%s", (uid,), one=True)
        seen_at = row["schedule_seen_at"] if row else None
        if not seen_at:
            db.execute("UPDATE users SET schedule_seen_at=%s WHERE id=%s",
                       (now_local().isoformat(timespec="microseconds"), uid))
            return jsonify({"changes": [], "count": 0})
        # Only shifts from today forward matter (a past swap can't be worked).
        today = now_local().date()
        horizon = today + timedelta(days=14)
        rows = db.query(
            "SELECT work_date, shift FROM schedule_cells "
            "WHERE col_index=%s AND updated_at IS NOT NULL AND updated_at > %s "
            "AND work_date >= %s AND work_date < %s ORDER BY work_date",
            (col, seen_at, today.isoformat(), horizon.isoformat()),
        )
        changes = []
        for r in rows:
            wd = r["work_date"] if isinstance(r["work_date"], str) else r["work_date"].isoformat()
            label = datetime.strptime(wd, "%Y-%m-%d").date().strftime("%a %d %b")
            shift = (r["shift"] or "").strip()
            changes.append({"date": wd, "label": label, "shift": shift or "— (no longer on shift)"})
        return jsonify({"changes": changes, "count": len(changes)})

    # ------------------------------------------------------------ shift clock
    def _fmt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _as_dt(v):
        """A stored timestamp (str on sqlite, datetime on mysql) -> aware datetime."""
        if hasattr(v, "strftime"):
            return v if v.tzinfo else v.replace(tzinfo=BRUSSELS)
        try:
            return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRUSSELS)
        except ValueError:
            return None

    def shift_allowance(user_name, started):
        """How long this shift may run: the hours on that day's roster cell, capped
        by SHIFT_MAX_HOURS. Falls back to the cap when the day isn't a rostered
        shift (holiday, or the operator isn't on the roster at all)."""
        hours = None
        col = _operator_col_for(user_name)
        if col:
            row = db.query(
                "SELECT shift FROM schedule_cells WHERE col_index=%s AND work_date=%s",
                (col, started.strftime("%Y-%m-%d")), one=True)
            if row:
                hours = roster_hours(row["shift"])
        return timedelta(hours=min(hours or SHIFT_MAX_HOURS, SHIFT_MAX_HOURS))

    def auto_close_stale_shifts():
        """Close shifts that were plainly forgotten.

        A shift only counts as abandoned once SHIFT_CLOCKOUT_WINDOW_HOURS have
        passed (not SHIFT_MAX_HOURS: a night plus handover runs past 12 h). Rostered
        hours are *paid* hours and people work past them - a break, an early start,
        overtime - so closing at the rostered length cut operators off while they
        were still on the floor. When a forgotten shift is closed, its end is
        recorded at the rostered length: the best guess of when they stopped, never
        'now', which would invent hours. auto_closed=1 marks it as a guess, and a
        real clock-out that turns up later still corrects it (api_shift_punch)."""
        now = now_local()
        window = timedelta(hours=SHIFT_CLOCKOUT_WINDOW_HOURS)
        for s in db.query(
            "SELECT s.id, s.started_at, u.display_name FROM shifts s "
            "JOIN users u ON u.id = s.user_id WHERE s.ended_at IS NULL"
        ):
            started = _as_dt(s["started_at"])
            if not started or now < started + window:
                continue
            end = started + shift_allowance(s["display_name"], started)
            db.execute("UPDATE shifts SET ended_at=%s, auto_closed=1 WHERE id=%s",
                       (_fmt(end), s["id"]))

    def reopen_shifts_closed_early():
        """Repair shifts the old rule closed at the rostered length while the
        operator was still working. Only shifts still inside their 12 h window, with
        no later shift for that person, are reopened; the corrected sweep closes
        them properly if they are then forgotten. Nothing the current rule closes
        can match - it only closes once the 12 h window has passed - so this is
        safe on every start and does nothing once old records age out."""
        now = now_local()
        window = timedelta(hours=SHIFT_CLOCKOUT_WINDOW_HOURS)
        with app.app_context():
            for s in db.query(
                "SELECT id, user_id, started_at FROM shifts "
                "WHERE auto_closed=1 AND ended_at IS NOT NULL AND started_at >= %s",
                (_fmt(now - window),),
            ):
                started = _as_dt(s["started_at"])
                if not started or now >= started + window:
                    continue
                if db.query("SELECT 1 FROM shifts WHERE user_id=%s AND id>%s",
                            (s["user_id"], s["id"]), one=True):
                    continue            # they already clocked in again - leave it
                db.execute("UPDATE shifts SET ended_at=NULL, auto_closed=0 WHERE id=%s",
                           (s["id"],))

    reopen_shifts_closed_early()

    def current_shift(uid):
        return db.query(
            "SELECT id, started_at FROM shifts WHERE user_id=%s AND ended_at IS NULL "
            "ORDER BY id DESC", (uid,), one=True)

    def shift_state(uid):
        row = current_shift(uid)
        if not row:
            return {"on_shift": False}
        started = _as_dt(row["started_at"])
        return {
            "on_shift": True,
            "started_at": _fmt(started) if started else None,
            "since": started.strftime("%H:%M") if started else "",
            "seconds": int((now_local() - started).total_seconds()) if started else 0,
        }

    @app.route("/api/shift")
    def api_shift_get():
        user = current_user() or {}
        if not user.get("id"):
            return jsonify({"error": "not logged in"}), 403
        auto_close_stale_shifts()
        return jsonify(shift_state(user["id"]))

    @app.route("/api/shift", methods=["POST"])
    def api_shift_punch():
        """Clock in or out. Body: {action: 'start'|'end', timestamp, client_key}.

        The action is explicit rather than a toggle: a double tap, or a phone
        showing a stale screen, must not flip the clock the wrong way. The
        timestamp is when the operator tapped, so a punch queued offline records
        the real moment rather than when the phone got signal. client_key makes a
        replayed punch a no-op instead of a second shift."""
        user = current_user() or {}
        uid = user.get("id")
        if not uid:
            return jsonify({"error": "not logged in"}), 403
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        if action not in ("start", "end"):
            return jsonify({"error": "invalid action"}), 400
        key = (data.get("client_key") or "").strip() or None
        when = parse_ts(data.get("timestamp"))

        if key and db.query("SELECT id FROM shifts WHERE client_key=%s", (key,), one=True):
            return jsonify(dict(shift_state(uid), duplicate=True))   # already applied

        auto_close_stale_shifts()
        open_shift = current_shift(uid)

        if action == "start":
            if open_shift:
                # Already clocked in (another device, or the phone missed the reply).
                return jsonify(dict(shift_state(uid), already=True))
            db.execute(
                "INSERT INTO shifts (user_id, started_at, client_key) VALUES (%s, %s, %s)",
                (uid, _fmt(when), key),
            )
        else:
            cap = timedelta(hours=SHIFT_MAX_HOURS)
            window = timedelta(hours=SHIFT_CLOCKOUT_WINDOW_HOURS)
            target = open_shift
            if not target:
                # The sweep may already have closed this shift with a guessed end -
                # a forgotten clock-out, or a clock-out queued offline that synced
                # late. A real clock-out inside that shift's 12 h window is better
                # information than the guess, so it corrects the record.
                last = db.query(
                    "SELECT id, started_at FROM shifts WHERE user_id=%s AND auto_closed=1 "
                    "ORDER BY id DESC", (uid,), one=True)
                s0 = _as_dt(last["started_at"]) if last else None
                newer = last and db.query(
                    "SELECT 1 FROM shifts WHERE user_id=%s AND id>%s",
                    (uid, last["id"]), one=True)
                if not (s0 and not newer and s0 <= when <= s0 + window):
                    return jsonify(dict(shift_state(uid), already=True))
                target = last
            started = _as_dt(target["started_at"])
            # Never end before the start, and never past SHIFT_MAX_HOURS - the ceiling
            # on any shift. Rostered hours are NOT a cap on the time actually worked.
            end = when
            if started:
                end = max(end, started)
                end = min(end, started + cap)
            db.execute("UPDATE shifts SET ended_at=%s, auto_closed=0, "
                       "client_key=COALESCE(client_key,%s) WHERE id=%s",
                       (_fmt(end), key, target["id"]))
        return jsonify(shift_state(uid))

    def shift_rows(d_from, d_to):
        """Shifts started within the range - hours, rostered hours, flags - plus
        hours per operator and who is on shift right now. Shared by the Shifts page
        and the report. Rows are keyed on the day a shift *started*, so a night
        shift stays with the day it belongs to rather than splitting at midnight."""
        auto_close_stale_shifts()
        now = now_local()

        def item(r):
            started = _as_dt(r["started_at"])
            ended = _as_dt(r["ended_at"]) if r["ended_at"] else None
            secs = int(((ended or now) - started).total_seconds()) if started else 0
            day = started.strftime("%Y-%m-%d") if started else ""
            rostered = None
            col = _operator_col_for(r["display_name"]) if started else None
            if col:
                cell = db.query(
                    "SELECT shift FROM schedule_cells WHERE col_index=%s AND work_date=%s",
                    (col, day), one=True)
                if cell:
                    rostered = roster_hours(cell["shift"])
            end = None
            if ended:
                # A night shift ends the next day; say so rather than show an end
                # time that looks earlier than its start.
                end = ended.strftime("%H:%M") + (" +1" if started and ended.date() > started.date() else "")
            return {
                "id": r["id"], "name": r["display_name"], "day": day,
                "label": started.strftime("%a %d %b") if started else "",
                "start": started.strftime("%H:%M") if started else "—",
                "end": end,
                "hours": secs / 3600.0,
                "rostered": rostered,
                "auto_closed": bool(r["auto_closed"]),
                "open": ended is None,
            }

        select = ("SELECT s.id, s.started_at, s.ended_at, s.auto_closed, u.display_name "
                  "FROM shifts s JOIN users u ON u.id = s.user_id ")
        shifts = [item(r) for r in db.query(
            select + "WHERE DATE(s.started_at) BETWEEN %s AND %s "
            "ORDER BY s.started_at DESC, u.display_name", (d_from, d_to))]
        totals = {}
        for s in shifts:
            totals[s["name"]] = totals.get(s["name"], 0.0) + s["hours"]
        # Who is on shift *now* is live state, independent of the dates being
        # viewed - a night shift that began yesterday is still on the floor today.
        open_now = [item(r) for r in db.query(
            select + "WHERE s.ended_at IS NULL ORDER BY s.started_at")]
        return {
            "shifts": shifts,
            "totals": sorted(({"name": k, "hours": v} for k, v in totals.items()),
                             key=lambda x: -x["hours"]),
            "open_now": open_now,
        }

    @app.route("/shifts")
    @manager_required
    def shifts_page():
        """Who is clocked in now, and the shifts worked over a date range."""
        d_from, d_to = parse_range()
        return render_template("shifts.html", d_from=d_from, d_to=d_to,
                               **shift_rows(d_from, d_to))

    @app.route("/api/schedule-ack", methods=["POST"])
    def api_schedule_ack():
        """Operator confirms they've seen their (changed) schedule — clears the banner."""
        user = current_user() or {}
        uid = user.get("id")
        if not uid:
            return jsonify({"ok": False}), 403
        db.execute("UPDATE users SET schedule_seen_at=%s WHERE id=%s",
                   (now_local().isoformat(timespec="microseconds"), uid))
        return jsonify({"ok": True})

    # ------------------------------------------------ edit task descriptions (manager)
    @app.route("/manage/tasks")
    @manager_required
    def manage_tasks():
        ensure_todays_dailies()
        flt = request.args.get("filter")
        params = ()
        if flt == "hold":
            where = "WHERE on_hold=1 "
        elif flt == "daily":
            where = "WHERE is_daily=1 "            # the recurring 'master' templates
        else:
            # Normal view: hide daily masters, and hide daily instances once their
            # working day has passed so they don't accumulate. Yesterday's *started*
            # ones stay until DAILY_GRACE_HOUR - a night shift is still on them.
            where = ("WHERE is_daily=0 AND (daily_of IS NULL OR DATE(created_at)=%s "
                     "OR (status='started' AND DATE(created_at)=%s)) ")
            params = (today_local(), daily_grace_day())
        # Search box: match the description, or an exact task id when digits are
        # typed (so "421" finds #421). Ignores the daily-instance date prefix.
        q = (request.args.get("q") or "").strip()
        if q:
            where += "AND (title LIKE %s"
            params = params + ("%" + q + "%",)
            if q.isdigit():
                where += " OR id=%s"
                params = params + (int(q),)
            where += ") "
        rows = db.query(
            "SELECT id, title, status, started_at, completed_at, on_hold, held_seconds, "
            "is_daily, daily_of, assigned_to "
            "FROM tasks " + where + "ORDER BY id DESC",
            params,
        )
        # Format timestamps for <input type=datetime-local> (YYYY-MM-DDThh:mm:ss)
        for r in rows:
            for k in ("started_at", "completed_at"):
                v = r[k]
                r[k + "_local"] = (v.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(v, "strftime")
                                   else str(v)[:19].replace(" ", "T")) if v else ""
        return render_template("edit_tasks.html", tasks=rows,
                               saved=request.args.get("saved"), flt=flt, q=q)

    def _parse_dtlocal(v):
        """'2026-06-30T08:00' or '...:00' from a datetime-local field -> DB string / None."""
        v = (v or "").strip().replace("T", " ")
        if not v:
            return None
        return v if len(v) > 16 else v + ":00"   # add seconds if the field omitted them

    @app.route("/manage/tasks/<int:task_id>", methods=["POST"])
    @manager_required
    def manage_task_update(task_id):
        title = (request.form.get("title") or "").strip()
        started = _parse_dtlocal(request.form.get("started_at"))
        finished = _parse_dtlocal(request.form.get("completed_at"))
        if title:
            # Re-read the @-tag from the edited text so assignment tracks the title.
            db.execute("UPDATE tasks SET title=%s, assigned_to=%s WHERE id=%s",
                       (title, ASSIGN_SEP.join(parse_assignees(title, active_operators())) or None,
                        task_id))
        # A hand-entered time "wins": flag it so later photo uploads won't overwrite it.
        # Clearing a field (None) drops the flag so the photo flow can set it again.
        db.execute(
            "UPDATE tasks SET started_at=%s, started_manual=%s, "
            "completed_at=%s, completed_manual=%s WHERE id=%s",
            (started, 1 if started else 0, finished, 1 if finished else 0, task_id),
        )
        # Keep the before/after photo times in sync with the task times
        if started:
            db.execute("UPDATE task_photos SET timestamp=%s WHERE task_id=%s AND photo_type='before'",
                       (started, task_id))
        if finished:
            db.execute("UPDATE task_photos SET timestamp=%s WHERE task_id=%s AND photo_type='after'",
                       (finished, task_id))
        return redirect(url_for("manage_tasks", saved=task_id,
                                filter=request.form.get("filter") or None,
                                q=request.form.get("q") or None,
                                _anchor="task-%d" % task_id))

    @app.route("/manage/tasks/<int:task_id>/hold", methods=["POST"])
    @manager_required
    def manage_task_hold(task_id):
        """Toggle a task on/off hold. While on hold, elapsed time is not counted;
        when taken off hold, the parked interval is banked into held_seconds so the
        report duration excludes it."""
        t = db.query("SELECT on_hold, hold_started_at FROM tasks WHERE id=%s", (task_id,), one=True)
        if not t:
            abort(404)
        if t["on_hold"]:
            add = 0
            hs = t["hold_started_at"]
            if hs:
                started = hs if isinstance(hs, datetime) else datetime.fromisoformat(str(hs))
                # Count the parked interval on the SAME basis as duration (shift hours
                # only) so a hold spanning nights can't exceed the worked time.
                add = worked_seconds(started, now_local().replace(tzinfo=None))
            db.execute(
                "UPDATE tasks SET on_hold=0, hold_started_at=NULL, held_seconds=held_seconds+%s "
                "WHERE id=%s",
                (add, task_id),
            )
        else:
            db.execute(
                "UPDATE tasks SET on_hold=1, hold_started_at=%s WHERE id=%s",
                (now_local().strftime("%Y-%m-%d %H:%M:%S"), task_id),
            )
        return redirect(url_for("manage_tasks", saved=task_id,
                                filter=request.form.get("filter") or None,
                                q=request.form.get("q") or None,
                                _anchor="task-%d" % task_id))

    @app.route("/manage/tasks/<int:task_id>/delete", methods=["POST"])
    @manager_required
    def manage_task_delete(task_id):
        """Permanently delete a task: its photo files on disk, photo rows, then the task."""
        for p in db.query("SELECT file_path FROM task_photos WHERE task_id=%s", (task_id,)):
            abs_path = os.path.join(app.config["UPLOAD_ROOT"], p["file_path"].replace("/", os.sep))
            try:
                if os.path.exists(abs_path):
                    os.remove(abs_path)
            except OSError:
                pass  # leave orphan file rather than fail the delete
        db.execute("DELETE FROM task_photos WHERE task_id=%s", (task_id,))
        db.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        return redirect(url_for("manage_tasks", filter=request.form.get("filter") or None,
                                q=request.form.get("q") or None))

    @app.route("/manage/tasks/<int:task_id>/daily", methods=["POST"])
    @manager_required
    def manage_task_daily(task_id):
        """Toggle a task as a daily recurring template. On => hidden from operators
        and a dated 'DAILY - <date> - ...' instance is spawned each day."""
        t = db.query("SELECT is_daily FROM tasks WHERE id=%s", (task_id,), one=True)
        if not t:
            abort(404)
        new = 0 if t["is_daily"] else 1
        db.execute("UPDATE tasks SET is_daily=%s WHERE id=%s", (new, task_id))
        if new:
            ensure_todays_dailies()
        # A task made 'daily' moves to the masters list; un-made returns to normal.
        return redirect(url_for("manage_tasks", saved=task_id,
                                filter="daily" if new else None))

    @app.route("/tech")
    def tech_list():
        show_rent = (current_user() or {}).get("username") in RENT_USERNAMES
        return render_template("technician_list.html", show_rent=show_rent)

    @app.route("/tech/task/<int:task_id>")
    def tech_task(task_id):
        return render_template("technician_task.html", task_id=task_id)

    @app.route("/tech/extra")
    def tech_extra():
        # Client page; the local job id is read from the URL fragment so the page
        # is fully cacheable and works offline.
        return render_template("tech_extra.html")

    _JOB_SELECT = """
            SELECT t.id, t.title, t.status, t.created_at, t.started_at, t.completed_at,
                   t.is_extra, t.is_rent, t.daily_of, t.started_by, t.completed_by, t.held_seconds,
                   t.on_hold,
                   bp.file_path AS before_path, bp.timestamp AS before_ts,
                   ap.file_path AS after_path,  ap.timestamp AS after_ts
            FROM tasks t
            LEFT JOIN task_photos bp ON bp.task_id = t.id AND bp.photo_type = 'before'
            LEFT JOIN task_photos ap ON ap.task_id = t.id AND ap.photo_type = 'after'
    """

    def _enrich_jobs(rows):
        """Add duration, date labels, and progress photos so rows render in the
        report table and photo cards (shared by completed and in-progress lists)."""
        for r in rows:
            r["duration_s"] = None
            r["progress"] = []
            ca = r["created_at"]
            r["created_date"] = ca.strftime("%Y-%m-%d") if hasattr(ca, "strftime") else str(ca)[:10]
            fa = r["completed_at"]
            r["finished_date"] = fa.strftime("%Y-%m-%d") if hasattr(fa, "strftime") else (str(fa)[:10] if fa else "")
            if r["started_at"] and r["completed_at"]:
                start = r["started_at"] if isinstance(r["started_at"], datetime) else \
                    datetime.fromisoformat(str(r["started_at"]))
                end = r["completed_at"] if isinstance(r["completed_at"], datetime) else \
                    datetime.fromisoformat(str(r["completed_at"]))
                # Count only shift hours (max 8 h/day) so multi-day jobs aren't
                # inflated by nights, then exclude any time parked on hold.
                dur = worked_seconds(start, end) - int(r["held_seconds"] or 0)
                r["duration_s"] = max(dur, 0)

        # Attach progress photos (taken between before and after), ordered by slot.
        if rows:
            by_id = {r["id"]: r for r in rows}
            placeholders = ",".join(["%s"] * len(by_id))
            progress = db.query(
                f"""SELECT task_id, photo_type, file_path, timestamp
                    FROM task_photos
                    WHERE photo_type LIKE %s AND task_id IN ({placeholders})
                    ORDER BY task_id, photo_type""",
                ("progress%", *by_id.keys()),
            )
            for p in progress:
                by_id[p["task_id"]]["progress"].append(p)
        return rows

    def fetch_range(d_from, d_to):
        """Jobs finished (completed) between d_from and d_to (inclusive), with photos +
        duration. Keyed on completion date; unfinished jobs are excluded."""
        rows = db.query(
            _JOB_SELECT + " WHERE DATE(t.completed_at) BETWEEN %s AND %s ORDER BY t.completed_at, t.id",
            (d_from, d_to),
        )
        return _enrich_jobs(rows)

    def parse_range():
        """Read from/to (or legacy single `date`) and return a sorted (from, to)."""
        today = today_local()
        d_from = request.args.get("from") or request.args.get("date") or today
        d_to = request.args.get("to") or request.args.get("date") or d_from
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        return d_from, d_to

    def _days_since(v):
        """Whole days between a stored timestamp and today, or None if unparseable."""
        s = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v or "")[:10]
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None
        return (now_local().date() - d).days

    def fetch_started(d_from, d_to):
        """Every job still in progress, not just those started inside the report's
        dates: 'started' is the job's current state, so a job opened last week and
        still unfinished belongs in this list — that is the point of it. Oldest
        first, so jobs that have been hanging around surface at the top.

        Same rich photo data as completed jobs, so they render as photo cards on
        their own report page. The date range is deliberately unused."""
        rows = db.query(
            _JOB_SELECT + " WHERE t.status='started' AND t.is_daily=0"
            " ORDER BY t.started_at, t.id"
        )
        rows = _enrich_jobs(rows)
        for r in rows:
            r["days_open"] = _days_since(r["started_at"])
        return rows

    def report_summary(d_from, d_to):
        """Status breakdown of the day's tasks, with SVG pie slices, so the manager
        sees done/started/open/extra at a glance. Scope = tasks created that day OR
        completed that day, so completions of carried-over jobs are counted."""
        rows = db.query(
            "SELECT is_extra, is_rent, status, COUNT(*) AS n FROM tasks "
            "WHERE is_daily=0 AND (DATE(created_at) BETWEEN %s AND %s "
            "                      OR DATE(completed_at) BETWEEN %s AND %s) "
            "GROUP BY is_extra, is_rent, status",
            (d_from, d_to, d_from, d_to),
        )
        counts = {"Completed": 0, "Started": 0, "Open": 0, "Extra": 0}
        for r in rows:
            if r["is_extra"] or r["is_rent"]:
                counts["Extra"] += r["n"]
            elif r["status"] == "completed":
                counts["Completed"] += r["n"]
            elif r["status"] == "started":
                counts["Started"] += r["n"]
            else:
                counts["Open"] += r["n"]
        colors = {"Completed": "#198754", "Started": "#ffc107", "Open": "#6c757d", "Extra": "#0dcaf0"}
        order = ["Completed", "Started", "Open", "Extra"]
        total = sum(counts.values())
        segments = [{"label": k, "count": counts[k], "color": colors[k],
                     "pct": (counts[k] / total * 100 if total else 0)} for k in order]

        # Build SVG pie slice paths (radius 88, centred at 90,90; start at top).
        paths = []
        cx = cy = 90.0
        rad = 88.0
        ang = -math.pi / 2
        for s in segments:
            if not s["count"]:
                continue
            frac = s["count"] / total
            if frac >= 0.99995:   # a single full-circle slice
                paths.append({"color": s["color"],
                              "d": "M %g %g a %g %g 0 1 0 %g 0 a %g %g 0 1 0 %g 0"
                              % (cx - rad, cy, rad, rad, 2 * rad, rad, rad, -2 * rad)})
                break
            a2 = ang + frac * 2 * math.pi
            x1, y1 = cx + rad * math.cos(ang), cy + rad * math.sin(ang)
            x2, y2 = cx + rad * math.cos(a2), cy + rad * math.sin(a2)
            large = 1 if frac > 0.5 else 0
            paths.append({"color": s["color"],
                          "d": "M %g %g L %.2f %.2f A %g %g 0 %d 1 %.2f %.2f Z"
                          % (cx, cy, x1, y1, rad, rad, large, x2, y2)})
            ang = a2
        return {"segments": segments, "total": total, "paths": paths}

    def todays_shift_summary():
        """Who's on which shift today (the day the report is viewed), from the
        schedule's color-coded cells. Only Early/Day/Late/Night/Drive are shown —
        holidays, JV, and blank cells are left out."""
        today = today_local()
        rows = db.query(
            "SELECT col_index, shift, color FROM schedule_cells WHERE work_date=%s", (today,)
        )
        by_col = {r["col_index"]: r for r in rows}
        out = []
        for i, name in enumerate(SCHEDULE_OPERATORS, start=1):
            r = by_col.get(i)  # col_index is 1-based
            if not r:
                continue
            label = schedule_shift_label(r["color"])
            if not label:
                continue
            out.append({"name": name, "shift": label,
                        "text": shift_text_label(r["shift"])})
        return {"date": today, "entries": out}

    @app.route("/report")
    @manager_required
    def report():
        d_from, d_to = parse_range()
        return render_template("report.html", tasks=fetch_range(d_from, d_to),
                               started=fetch_started(d_from, d_to),
                               summary=report_summary(d_from, d_to),
                               shift_summary=todays_shift_summary(),
                               shift_data=shift_rows(d_from, d_to),
                               d_from=d_from, d_to=d_to)

    @app.route("/photos")
    @manager_required
    def photos():
        d_from, d_to = parse_range()
        # Only jobs that actually have a photo to show
        rows = [r for r in fetch_range(d_from, d_to)
                if r["before_path"] or r["after_path"] or r["progress"]]
        return render_template("photos.html", tasks=rows, d_from=d_from, d_to=d_to)

    @app.route("/photos.zip")
    @manager_required
    def photos_zip():
        """Download the photos for a date range (or one job with ?task=<id>) as a
        zip, for sending on without saving each image by hand. Files are named
        job<id>-<type>.jpg so they stay identifiable outside the app."""
        import io
        import zipfile

        task_id = request.args.get("task")
        if task_id and task_id.isdigit():
            rows = [r for r in fetch_range("1970-01-01", "2999-12-31")
                    if r["id"] == int(task_id)]
            label = "job%s" % task_id
        else:
            d_from, d_to = parse_range()
            rows = fetch_range(d_from, d_to)
            label = d_from if d_from == d_to else "%s_to_%s" % (d_from, d_to)

        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for t in rows:
                shots = [("before", t["before_path"]), ("after", t["after_path"])]
                shots += [("progress%d" % (i + 1), p["file_path"])
                          for i, p in enumerate(t["progress"] or [])]
                for kind, rel in shots:
                    if not rel:
                        continue
                    abs_path = os.path.join(app.config["UPLOAD_ROOT"],
                                            rel.replace("/", os.sep))
                    if not os.path.exists(abs_path):
                        continue
                    ext = os.path.splitext(rel)[1] or ".jpg"
                    z.write(abs_path, "job%d-%s%s" % (t["id"], kind, ext))
                    count += 1
        if not count:
            abort(404)
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name="photos-%s.zip" % label)

    def sweep_stale_dailies():
        """Close out daily instances whose day has passed, so they stop collecting.

        A daily belongs to its day: once that day is over it can no longer be
        worked (operators only ever see today's), so leaving it 'started' forever
        just grew a list of stale jobs nobody could reach from the UI.

          - started but unfinished -> completed at the end of its own day, flagged
            auto_closed. Closing at 'now' would date the work to whenever someone
            happened to load a page, sometimes weeks later.
          - never started -> deleted: nothing was done, so there is nothing worth
            keeping. Guarded on having no photos, so a job with any field
            documentation is never removed by an automatic sweep.

        On-hold tasks are skipped either way — a hold is a deliberate decision by
        a manager and the sweep has no business overriding it.

        "Its day has passed" means the *working* day: yesterday's instances are left
        alone until DAILY_GRACE_HOUR, because a night shift started yesterday is
        still working them after midnight."""
        grace = daily_grace_day()
        for r in db.query(
            "SELECT id, created_at, started_at, started_by FROM tasks "
            "WHERE daily_of IS NOT NULL AND status='started' AND on_hold=0 "
            "AND DATE(created_at) < %s", (grace,)
        ):
            day = str(r["created_at"])[:10] if not hasattr(r["created_at"], "strftime") \
                else r["created_at"].strftime("%Y-%m-%d")
            end = "%s 23:59:59" % day
            started = str(r["started_at"] or "")[:19] if not hasattr(r["started_at"], "strftime") \
                else r["started_at"].strftime("%Y-%m-%d %H:%M:%S")
            if started and started > end:      # started late in the evening
                end = started
            db.execute(
                "UPDATE tasks SET status='completed', completed_at=%s, auto_closed=1, "
                "completed_by=COALESCE(completed_by, started_by) WHERE id=%s",
                (end, r["id"]),
            )
        for r in db.query(
            "SELECT id FROM tasks WHERE daily_of IS NOT NULL AND status='open' "
            "AND on_hold=0 AND DATE(created_at) < %s", (grace,)
        ):
            if db.query("SELECT 1 FROM task_photos WHERE task_id=%s", (r["id"],), one=True):
                continue                        # has photos - never auto-delete those
            db.execute("DELETE FROM tasks WHERE id=%s", (r["id"],))

    def ensure_todays_dailies():
        """Roll the dailies over: close out any whose day has passed, then create
        today's instance for each daily template that lacks one. Instances are
        normal tasks titled 'DAILY - <date> - <template>' with daily_of set;
        templates (is_daily=1) are never shown to operators themselves."""
        sweep_stale_dailies()
        today = today_local()
        now = now_local().strftime("%Y-%m-%d %H:%M:%S")
        for tpl in db.query("SELECT id, title, assigned_to FROM tasks WHERE is_daily=1"):
            has = db.query("SELECT 1 FROM tasks WHERE daily_of=%s AND DATE(created_at)=%s",
                           (tpl["id"], today), one=True)
            if not has:
                # Instances inherit the template's @-tagged operator.
                db.execute(
                    "INSERT INTO tasks (title, created_at, daily_of, assigned_to) "
                    "VALUES (%s, %s, %s, %s)",
                    ("DAILY - %s - %s" % (today, tpl["title"]), now, tpl["id"],
                     tpl["assigned_to"]),
                )

    # --------------------------------------------------------------------- API
    @app.route("/api/tasks", methods=["GET"])
    def api_list_tasks():
        """All non-completed tasks for technicians (plus today's completed).
        Daily templates are hidden; only today's daily instances are shown."""
        ensure_todays_dailies()
        today = today_local()
        # Order: on-hold always at the very bottom; then today's (unfinished)
        # dailies, started, open, completed. Works on sqlite and mysql.
        hold_sql = "CASE WHEN on_hold=1 THEN 1 ELSE 0 END"
        status_sql = ("CASE WHEN daily_of IS NOT NULL AND status <> 'completed' THEN 0 "
                      "WHEN status='started' THEN 1 "
                      "WHEN status='open' THEN 2 ELSE 3 END")
        rows = db.query(
            f"""
            SELECT id, title, status, is_extra, is_rent, daily_of, client_key, on_hold,
                   assigned_to, created_at, started_at, completed_at
            FROM tasks
            WHERE (status <> 'completed' OR DATE(completed_at) = %s)
              AND is_daily = 0
              AND (daily_of IS NULL OR DATE(created_at) = %s
                   OR (status = 'started' AND DATE(created_at) = %s))
            ORDER BY {hold_sql}, {status_sql}, id DESC
            """,
            (today, today, daily_grace_day()),
        )
        # Float the viewer's own jobs to the top, on-hold still last. Done here and
        # not in SQL because a job can be shared by several operators ("A, B"),
        # which no portable ORDER BY expresses cleanly. The sort is stable, so the
        # ordering above survives within each group.
        me = (current_user() or {}).get("name") or ""
        rows.sort(key=lambda r: (1 if r["on_hold"] else 0,
                                 0 if me and me in assignees_of(r["assigned_to"]) else 1))
        for r in rows:
            for k in ("created_at", "started_at", "completed_at"):
                if isinstance(r[k], datetime):
                    r[k] = r[k].isoformat()
        return jsonify(rows)

    @app.route("/api/tasks/<int:task_id>", methods=["GET"])
    def api_get_task(task_id):
        row = db.query("SELECT id, title, status FROM tasks WHERE id=%s", (task_id,), one=True)
        if not row:
            abort(404)
        # Which progress slots are already used (so the phone can offer the next)
        prog = db.query(
            "SELECT photo_type FROM task_photos WHERE task_id=%s AND photo_type LIKE %s "
            "ORDER BY photo_type",
            (task_id, "progress%"),
        )
        row["progress"] = [p["photo_type"] for p in prog]
        return jsonify(row)

    @app.route("/api/tasks", methods=["POST"])
    @manager_required
    def api_create_tasks():
        """Bulk create — body: {"text": "line1\\nline2\\n..."}. One line = one task."""
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        titles = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not titles:
            return jsonify({"error": "no tasks found"}), 400

        created_at = now_local().strftime("%Y-%m-%d %H:%M:%S")
        operators = active_operators()
        created = []
        for title in titles:
            daily, clean = split_daily_prefix(title)
            assignee = ASSIGN_SEP.join(parse_assignees(title, operators)) or None
            if daily:
                # A 'DAILY ...' line becomes a hidden recurring template.
                new_id = db.execute(
                    "INSERT INTO tasks (title, created_at, is_daily, assigned_to) VALUES (%s, %s, 1, %s)",
                    (clean, created_at, assignee),
                )
            else:
                new_id = db.execute(
                    "INSERT INTO tasks (title, created_at, assigned_to) VALUES (%s, %s, %s)",
                    (title, created_at, assignee),
                )
            created.append({"id": new_id, "title": clean if daily else title, "status": "open"})
        ensure_todays_dailies()   # spawn today's instances for any new daily templates
        return jsonify({"created": created, "count": len(created)}), 201

    @app.route("/api/tasks/extra", methods=["POST"])
    def api_create_extra():
        """Create a single unplanned (technician-initiated) job.
        Body: {"title": "...", "timestamp": optional ISO of when it was created}.
        Used by the offline 'Extra job' flow — the phone creates one locally and
        calls this when back online, then uploads its photos against the new id.
        """
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title required"}), 400
        client_key = (data.get("client_key") or "").strip() or None

        # Idempotent: if this phone already created the job (same client_key),
        # return the existing one instead of making a duplicate.
        if client_key:
            existing = db.query(
                "SELECT id, title, status FROM tasks WHERE client_key=%s", (client_key,), one=True
            )
            if existing:
                return jsonify(existing), 200

        created_at = parse_ts(data.get("timestamp")).strftime("%Y-%m-%d %H:%M:%S")
        # kind = 'rent' marks a rent-paying job; anything else is a normal extra job.
        is_rent = 1 if data.get("kind") == "rent" else 0
        is_extra = 0 if is_rent else 1
        new_id = db.execute(
            "INSERT INTO tasks (title, created_at, is_extra, is_rent, client_key) "
            "VALUES (%s, %s, %s, %s, %s)",
            (title, created_at, is_extra, is_rent, client_key),
        )
        return jsonify({"id": new_id, "title": title, "status": "open"}), 201

    @app.route("/api/tasks/<int:task_id>/photo", methods=["POST"])
    def api_upload_photo(task_id):
        """
        multipart/form-data:
          photo      = image file
          photo_type = 'before' | 'after'
          timestamp  = optional client ISO time (offline capture time)

        photo_type = 'before' | 'after' | 'progress1'..'progress4'
        Side effect: 'before' -> status 'started', 'after' -> status 'completed'.
        Progress photos (taken between start and end) do not change the status.
        Idempotent per (task, type): re-upload overwrites the same file/row.
        """
        task = db.query("SELECT id, status, on_hold FROM tasks WHERE id=%s", (task_id,), one=True)
        if not task:
            abort(404)
        if task["on_hold"]:
            return jsonify({"error": "This task is on hold and can't be worked right now."}), 409

        allowed = ("before", "after", "progress1", "progress2", "progress3", "progress4")
        photo_type = request.form.get("photo_type", "")
        if photo_type not in allowed:
            return jsonify({"error": "invalid photo_type"}), 400

        file = request.files.get("photo")
        if file is None or file.filename == "":
            return jsonify({"error": "missing photo"}), 400

        ts = parse_ts(request.form.get("timestamp"))
        rel_dir, abs_dir = photo_dir_for(ts)
        filename = f"{task_id}_{photo_type}.jpg"
        rel_path = os.path.join(rel_dir, filename).replace("\\", "/")
        abs_path = os.path.join(abs_dir, filename)
        file.save(abs_path)
        normalize_to_jpeg(abs_path)   # HEIC/other -> JPEG so it always displays

        # Upsert photo row (re-upload of same task+type overwrites)
        if db.IS_SQLITE:
            upsert_sql = """
                INSERT INTO task_photos (task_id, photo_type, file_path, timestamp)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(task_id, photo_type)
                DO UPDATE SET file_path=excluded.file_path, timestamp=excluded.timestamp
            """
        else:
            upsert_sql = """
                INSERT INTO task_photos (task_id, photo_type, file_path, timestamp)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE file_path=VALUES(file_path), timestamp=VALUES(timestamp)
            """
        db.execute(
            upsert_sql,
            (task_id, photo_type, rel_path, ts.strftime("%Y-%m-%d %H:%M:%S")),
        )

        # Who is doing this (the logged-in operator)
        who = (current_user() or {}).get("name")

        # Advance status (progress photos leave the status unchanged) + record who.
        if photo_type == "before":
            db.execute(
                "UPDATE tasks SET status='started', "
                "started_at = CASE WHEN started_manual=1 THEN started_at "
                "                   ELSE COALESCE(started_at,%s) END, "
                "started_by=COALESCE(started_by,%s) WHERE id=%s",
                (ts.strftime("%Y-%m-%d %H:%M:%S"), who, task_id),
            )
            new_status = "started"
        elif photo_type == "after":
            db.execute(
                "UPDATE tasks SET status='completed', "
                "completed_at = CASE WHEN completed_manual=1 THEN completed_at "
                "                    ELSE %s END, "
                "completed_by=%s WHERE id=%s",
                (ts.strftime("%Y-%m-%d %H:%M:%S"), who, task_id),
            )
            new_status = "completed"
        else:
            new_status = task["status"]

        return jsonify({"ok": True, "task_id": task_id, "status": new_status, "file_path": rel_path})

    # Serve stored photos
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        # Tolerate either extension: if the exact file is missing, try the
        # .jpg <-> .jpeg counterpart so both formats display interchangeably.
        root = app.config["UPLOAD_ROOT"]
        if not os.path.exists(os.path.join(root, filename)):
            base, ext = os.path.splitext(filename)
            alt = {".jpg": ".jpeg", ".jpeg": ".jpg"}.get(ext.lower())
            if alt and os.path.exists(os.path.join(root, base + alt)):
                filename = base + alt

        # Optional ?w=<px>: serve a downscaled, cached JPEG. Used by the report /
        # photo pages so printed PDFs stay small; full-size is served otherwise
        # (tap-to-open still shows the original).
        w = request.args.get("w", type=int)
        if w and 0 < w <= 2000 and filename.lower().endswith((".jpg", ".jpeg")):
            src = os.path.abspath(os.path.join(root, filename))
            if src.startswith(os.path.abspath(root)) and os.path.exists(src):
                cache = os.path.join(root, ".thumbs", "w%d" % w, filename)
                try:
                    if not os.path.exists(cache) or os.path.getmtime(cache) < os.path.getmtime(src):
                        os.makedirs(os.path.dirname(cache), exist_ok=True)
                        img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
                        if img.width > w:
                            img = img.resize((w, round(img.height * w / img.width)))
                        img.save(cache, "JPEG", quality=72)
                    return send_file(cache, mimetype="image/jpeg")
                except Exception:
                    pass  # fall through to the original on any failure
        return send_from_directory(root, filename)

    # PWA files must be served from the site root scope
    static_dir = os.path.join(app.root_path, "static")

    @app.route("/service-worker.js")
    def service_worker():
        # Served rendered (not as a static file) so the cache name carries the
        # asset hash: any css/js edit retires the old shell cache by itself.
        with open(os.path.join(static_dir, "service-worker.js"), encoding="utf-8") as f:
            js = f.read().replace("__ASSET_VERSION__", ASSET_VERSION)
        resp = app.response_class(js, mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(static_dir, "manifest.json")

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=app.config["DEBUG"])
