"""
Task Tracker — minimal Flask backend.

Managers paste a list -> tasks created.
Technicians take before/after photos -> status auto-advances.
Photos are stored on disk under uploads/YYYY/MM/DD/.

Designed and developed by Sébastien Poncelet.
Copyright (c) Sébastien Poncelet. All rights reserved.
"""
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, send_from_directory, abort,
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
APP_VERSION = "1.1.0"


def now_local():
    return datetime.now(BRUSSELS)


def today_local():
    return now_local().strftime("%Y-%m-%d")


# Normal day shift. Reported time only counts hours inside this window, so a job
# spanning multiple days is capped at 8 h/day and idle nights don't inflate it.
SHIFT_START_HOUR = 8   # 08:00
SHIFT_END_HOUR = 16    # 16:00


def worked_seconds(start, end):
    """Seconds worked between start and end, counting only each day's shift
    window (SHIFT_START_HOUR..SHIFT_END_HOUR). E.g. 05/07 14:00 -> 07/07 14:00
    = 2h (05th) + 8h (06th) + 6h (07th) = 16h, not 48h."""
    if not start or not end or end <= start:
        return 0
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

    # Expose the version (and author) to every template.
    @app.context_processor
    def inject_app_meta():
        return {"app_version": APP_VERSION, "app_author": "Sébastien Poncelet"}

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

    ensure_seed_manager()

    PUBLIC_ENDPOINTS = {"login", "static", "service_worker", "manifest", "healthz"}

    def current_user():
        return session.get("user")

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
            if not u or u.get("role") != "manager":
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
        return render_template("manager.html")

    # ----------------------------------------------------------- operators (admin)
    @app.route("/admin")
    @manager_required
    def admin():
        users = db.query(
            "SELECT id, username, display_name, role, active, created_at "
            "FROM users ORDER BY role, username"
        )
        return render_template("admin.html", users=users, msg=request.args.get("msg"))

    @app.route("/admin/users", methods=["POST"])
    @manager_required
    def admin_create_user():
        username = (request.form.get("username") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") if request.form.get("role") in ("manager", "technician") else "technician"
        if not username or not display_name or len(password) < 4:
            return redirect(url_for("admin", msg="Name, login and a 4+ char password are required."))
        if db.query("SELECT id FROM users WHERE username=%s", (username,), one=True):
            return redirect(url_for("admin", msg="That login already exists."))
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, role) VALUES (%s, %s, %s, %s)",
            (username, generate_password_hash(password), display_name, role),
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

    @app.route("/admin/users/<int:uid>/delete", methods=["POST"])
    @manager_required
    def admin_delete_user(uid):
        if uid == current_user()["id"]:
            return redirect(url_for("admin", msg="You can't delete your own account."))
        managers = db.query("SELECT COUNT(*) AS n FROM users WHERE role='manager' AND active=1", one=True)["n"]
        target = db.query("SELECT role FROM users WHERE id=%s", (uid,), one=True)
        if target and target["role"] == "manager" and managers <= 1:
            return redirect(url_for("admin", msg="Keep at least one manager."))
        db.execute("DELETE FROM users WHERE id=%s", (uid,))
        return redirect(url_for("admin", msg="Operator removed."))

    # ------------------------------------------------ edit task descriptions (manager)
    @app.route("/manage/tasks")
    @manager_required
    def manage_tasks():
        only_hold = request.args.get("filter") == "hold"
        rows = db.query(
            "SELECT id, title, status, started_at, completed_at, on_hold, held_seconds "
            "FROM tasks " + ("WHERE on_hold=1 " if only_hold else "") + "ORDER BY id DESC"
        )
        # Format timestamps for <input type=datetime-local> (YYYY-MM-DDThh:mm:ss)
        for r in rows:
            for k in ("started_at", "completed_at"):
                v = r[k]
                r[k + "_local"] = (v.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(v, "strftime")
                                   else str(v)[:19].replace(" ", "T")) if v else ""
        return render_template("edit_tasks.html", tasks=rows,
                               saved=request.args.get("saved"), only_hold=only_hold)

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
            db.execute("UPDATE tasks SET title=%s WHERE id=%s", (title, task_id))
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
                                filter=request.form.get("filter") or None))

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
                add = max(int((now_local().replace(tzinfo=None) - started).total_seconds()), 0)
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
                                filter=request.form.get("filter") or None))

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
        return redirect(url_for("manage_tasks", filter=request.form.get("filter") or None))

    @app.route("/tech")
    def tech_list():
        return render_template("technician_list.html")

    @app.route("/tech/task/<int:task_id>")
    def tech_task(task_id):
        return render_template("technician_task.html", task_id=task_id)

    @app.route("/tech/extra")
    def tech_extra():
        # Client page; the local job id is read from the URL fragment so the page
        # is fully cacheable and works offline.
        return render_template("tech_extra.html")

    def fetch_range(d_from, d_to):
        """Jobs finished (completed) between d_from and d_to (inclusive), with photos + duration.
        Keyed on completion date so the report shows what was actually finished that day,
        regardless of when the job was created; unfinished jobs are excluded."""
        rows = db.query(
            """
            SELECT t.id, t.title, t.status, t.created_at, t.started_at, t.completed_at,
                   t.is_extra, t.started_by, t.completed_by, t.held_seconds,
                   bp.file_path AS before_path, bp.timestamp AS before_ts,
                   ap.file_path AS after_path,  ap.timestamp AS after_ts
            FROM tasks t
            LEFT JOIN task_photos bp ON bp.task_id = t.id AND bp.photo_type = 'before'
            LEFT JOIN task_photos ap ON ap.task_id = t.id AND ap.photo_type = 'after'
            WHERE DATE(t.completed_at) BETWEEN %s AND %s
            ORDER BY t.completed_at, t.id
            """,
            (d_from, d_to),
        )
        # Duration computed in Python so the SQL stays portable across backends.
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

    def parse_range():
        """Read from/to (or legacy single `date`) and return a sorted (from, to)."""
        today = today_local()
        d_from = request.args.get("from") or request.args.get("date") or today
        d_to = request.args.get("to") or request.args.get("date") or d_from
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        return d_from, d_to

    @app.route("/report")
    @manager_required
    def report():
        d_from, d_to = parse_range()
        return render_template("report.html", tasks=fetch_range(d_from, d_to),
                               d_from=d_from, d_to=d_to)

    @app.route("/photos")
    @manager_required
    def photos():
        d_from, d_to = parse_range()
        # Only jobs that actually have a photo to show
        rows = [r for r in fetch_range(d_from, d_to)
                if r["before_path"] or r["after_path"] or r["progress"]]
        return render_template("photos.html", tasks=rows, d_from=d_from, d_to=d_to)

    # --------------------------------------------------------------------- API
    @app.route("/api/tasks", methods=["GET"])
    def api_list_tasks():
        """All non-completed tasks for technicians (plus today's completed)."""
        # On-hold jobs sort to the very bottom (below completed); otherwise
        # started first, then open, then completed. Works on sqlite and mysql.
        order_sql = ("CASE WHEN on_hold=1 THEN 3 "
                     "WHEN status='started' THEN 0 "
                     "WHEN status='open' THEN 1 ELSE 2 END")
        rows = db.query(
            f"""
            SELECT id, title, status, is_extra, client_key, on_hold,
                   created_at, started_at, completed_at
            FROM tasks
            WHERE status <> 'completed'
               OR DATE(completed_at) = %s
            ORDER BY {order_sql}, id DESC
            """,
            (today_local(),),
        )
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
        created = []
        for title in titles:
            new_id = db.execute(
                "INSERT INTO tasks (title, created_at) VALUES (%s, %s)", (title, created_at)
            )
            created.append({"id": new_id, "title": title, "status": "open"})
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
        new_id = db.execute(
            "INSERT INTO tasks (title, created_at, is_extra, client_key) VALUES (%s, %s, 1, %s)",
            (title, created_at, client_key),
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
        return send_from_directory(root, filename)

    # PWA files must be served from the site root scope
    static_dir = os.path.join(app.root_path, "static")

    @app.route("/service-worker.js")
    def service_worker():
        resp = send_from_directory(static_dir, "service-worker.js")
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
