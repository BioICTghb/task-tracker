"""
Task Tracker — minimal Flask backend.

Managers paste a list -> tasks created.
Technicians take before/after photos -> status auto-advances.
Photos are stored on disk under uploads/YYYY/MM/DD/.
"""
import os
from datetime import datetime, timezone

from flask import (
    Flask, render_template, request, jsonify, send_from_directory, abort,
)

from config import Config
import db


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    # Make sure the upload root exists
    os.makedirs(app.config["UPLOAD_ROOT"], exist_ok=True)

    # ----------------------------------------------------------------- helpers
    def parse_ts(value):
        """Accept a client ISO timestamp, fall back to server time."""
        if value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def photo_dir_for(now):
        rel = os.path.join(now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
        absdir = os.path.join(app.config["UPLOAD_ROOT"], rel)
        os.makedirs(absdir, exist_ok=True)
        return rel, absdir

    # ------------------------------------------------------------------- pages
    def is_mobile():
        """Rough phone/tablet detection from the User-Agent."""
        ua = request.user_agent.string.lower()
        return any(k in ua for k in (
            "android", "iphone", "ipad", "ipod", "mobile", "windows phone",
        ))

    @app.route("/")
    def index():
        # Phones go straight to the technician (complete tasks) view;
        # PCs get the manager (add tasks) view.
        if is_mobile():
            return render_template("technician_list.html")
        return render_template("manager.html")

    @app.route("/tech")
    def tech_list():
        return render_template("technician_list.html")

    @app.route("/tech/task/<int:task_id>")
    def tech_task(task_id):
        return render_template("technician_task.html", task_id=task_id)

    def fetch_day(date):
        """Tasks created on `date`, with their before/after photo paths + duration."""
        rows = db.query(
            """
            SELECT t.id, t.title, t.status, t.created_at, t.started_at, t.completed_at,
                   bp.file_path AS before_path, bp.timestamp AS before_ts,
                   ap.file_path AS after_path,  ap.timestamp AS after_ts
            FROM tasks t
            LEFT JOIN task_photos bp ON bp.task_id = t.id AND bp.photo_type = 'before'
            LEFT JOIN task_photos ap ON ap.task_id = t.id AND ap.photo_type = 'after'
            WHERE DATE(t.created_at) = %s
            ORDER BY t.id
            """,
            (date,),
        )
        # Duration computed in Python so the SQL stays portable across backends.
        for r in rows:
            r["duration_s"] = None
            if r["started_at"] and r["completed_at"]:
                start = r["started_at"] if isinstance(r["started_at"], datetime) else \
                    datetime.fromisoformat(str(r["started_at"]))
                end = r["completed_at"] if isinstance(r["completed_at"], datetime) else \
                    datetime.fromisoformat(str(r["completed_at"]))
                r["duration_s"] = int((end - start).total_seconds())
        return rows

    @app.route("/report")
    def report():
        date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
        return render_template("report.html", tasks=fetch_day(date), date=date)

    @app.route("/photos")
    def photos():
        date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
        # Only jobs that actually have a photo to show
        rows = [r for r in fetch_day(date) if r["before_path"] or r["after_path"]]
        return render_template("photos.html", tasks=rows, date=date)

    # --------------------------------------------------------------------- API
    @app.route("/api/tasks", methods=["GET"])
    def api_list_tasks():
        """All non-completed tasks for technicians (plus today's completed)."""
        if db.IS_SQLITE:
            order_sql = "CASE status WHEN 'started' THEN 0 WHEN 'open' THEN 1 ELSE 2 END"
            today_sql = "DATE(completed_at) = DATE('now')"
        else:
            order_sql = "FIELD(status,'started','open','completed')"
            today_sql = "DATE(completed_at) = CURDATE()"
        rows = db.query(
            f"""
            SELECT id, title, status,
                   created_at, started_at, completed_at
            FROM tasks
            WHERE status <> 'completed'
               OR {today_sql}
            ORDER BY {order_sql}, id DESC
            """
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
        return jsonify(row)

    @app.route("/api/tasks", methods=["POST"])
    def api_create_tasks():
        """Bulk create — body: {"text": "line1\\nline2\\n..."}. One line = one task."""
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        titles = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not titles:
            return jsonify({"error": "no tasks found"}), 400

        created = []
        for title in titles:
            new_id = db.execute("INSERT INTO tasks (title) VALUES (%s)", (title,))
            created.append({"id": new_id, "title": title, "status": "open"})
        return jsonify({"created": created, "count": len(created)}), 201

    @app.route("/api/tasks/<int:task_id>/photo", methods=["POST"])
    def api_upload_photo(task_id):
        """
        multipart/form-data:
          photo      = image file
          photo_type = 'before' | 'after'
          timestamp  = optional client ISO time (offline capture time)

        Side effect: 'before' -> status 'started', 'after' -> status 'completed'.
        Idempotent per (task, type): re-upload overwrites the same file/row.
        """
        task = db.query("SELECT id, status FROM tasks WHERE id=%s", (task_id,), one=True)
        if not task:
            abort(404)

        photo_type = request.form.get("photo_type", "")
        if photo_type not in ("before", "after"):
            return jsonify({"error": "photo_type must be before|after"}), 400

        file = request.files.get("photo")
        if file is None or file.filename == "":
            return jsonify({"error": "missing photo"}), 400

        ts = parse_ts(request.form.get("timestamp"))
        rel_dir, abs_dir = photo_dir_for(ts)
        filename = f"{task_id}_{photo_type}.jpg"
        rel_path = os.path.join(rel_dir, filename).replace("\\", "/")
        abs_path = os.path.join(abs_dir, filename)
        file.save(abs_path)

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

        # Advance status
        if photo_type == "before":
            db.execute(
                "UPDATE tasks SET status='started', started_at=COALESCE(started_at,%s) WHERE id=%s",
                (ts.strftime("%Y-%m-%d %H:%M:%S"), task_id),
            )
            new_status = "started"
        else:
            db.execute(
                "UPDATE tasks SET status='completed', completed_at=%s WHERE id=%s",
                (ts.strftime("%Y-%m-%d %H:%M:%S"), task_id),
            )
            new_status = "completed"

        return jsonify({"ok": True, "task_id": task_id, "status": new_status, "file_path": rel_path})

    # Serve stored photos
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_ROOT"], filename)

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
