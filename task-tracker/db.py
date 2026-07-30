"""
Database helper. Two backends:
  - mysql  (production, default) via PyMySQL
  - sqlite (zero-setup local dev/testing) via stdlib sqlite3

Backend is chosen by Config.DB_BACKEND. The app uses %s placeholders and dict
rows for both; for sqlite we translate placeholders and use a Row factory.
"""
from flask import g
from config import Config

IS_SQLITE = Config.DB_BACKEND == "sqlite"


# --------------------------------------------------------------------- sqlite
def _sqlite_connect():
    import sqlite3

    conn = sqlite3.connect(Config.SQLITE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout waits briefly instead of erroring if another connection is writing.
    # (WAL is intentionally NOT enabled here: it misbehaves on OneDrive-synced folders.
    #  On a dedicated Linux server you can re-enable it — see deploy notes.)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _sqlite_init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          started_at TIMESTAMP,
          completed_at TIMESTAMP,
          is_extra INTEGER NOT NULL DEFAULT 0,
          client_key TEXT
        );
        CREATE TABLE IF NOT EXISTS task_photos (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL,
          photo_type TEXT NOT NULL,
          file_path TEXT NOT NULL,
          timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (task_id, photo_type),
          FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          display_name TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'technician',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS schedule_cells (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          work_date TEXT NOT NULL,
          col_index INTEGER NOT NULL,
          shift TEXT NOT NULL DEFAULT '',
          color TEXT NOT NULL DEFAULT '',
          UNIQUE (work_date, col_index)
        );
        """
    )
    # Idempotent migrations for databases created before these columns existed.
    for ddl in (
        "ALTER TABLE tasks ADD COLUMN is_extra INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN client_key TEXT",
        "ALTER TABLE tasks ADD COLUMN started_by TEXT",
        "ALTER TABLE tasks ADD COLUMN completed_by TEXT",
        "ALTER TABLE tasks ADD COLUMN on_hold INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN hold_started_at TIMESTAMP",
        "ALTER TABLE tasks ADD COLUMN held_seconds INTEGER NOT NULL DEFAULT 0",
        # Set when a manager edits the time by hand, so later photo uploads
        # don't overwrite it ("manual time wins").
        "ALTER TABLE tasks ADD COLUMN started_manual INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN completed_manual INTEGER NOT NULL DEFAULT 0",
        # Operator-created 'rent' jobs (hours worked to pay on-site rent),
        # tracked separately from unplanned 'extra' jobs.
        "ALTER TABLE tasks ADD COLUMN is_rent INTEGER NOT NULL DEFAULT 0",
        # Daily recurring tasks: is_daily=1 marks a (hidden) template; each day an
        # instance is generated with daily_of set to that template's id.
        "ALTER TABLE tasks ADD COLUMN is_daily INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN daily_of INTEGER",
        "ALTER TABLE schedule_cells ADD COLUMN color TEXT NOT NULL DEFAULT ''",
        # Operator a job was tagged for with "@Name" in the published line, so
        # their own jobs sort first and stand out in their list.
        "ALTER TABLE tasks ADD COLUMN assigned_to TEXT",
        # When a cell's shift/color last actually changed, so operators can be
        # warned about swaps to their own upcoming shifts (see /api/schedule-changes).
        "ALTER TABLE schedule_cells ADD COLUMN updated_at TEXT",
        # Last time an operator acknowledged seeing their schedule. Changes with
        # updated_at newer than this raise the "your schedule changed" banner.
        "ALTER TABLE users ADD COLUMN schedule_seen_at TEXT",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass  # column already present
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_client_key ON tasks(client_key)")
    conn.commit()


# ---------------------------------------------------------------------- mysql
def _mysql_connect():
    import pymysql

    return pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# --------------------------------------------------------------------- shared
def get_db():
    if "db" not in g:
        g.db = _sqlite_connect() if IS_SQLITE else _mysql_connect()
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _prep(sql):
    # sqlite uses ? placeholders; the app writes %s everywhere
    return sql.replace("%s", "?") if IS_SQLITE else sql


def query(sql, args=None, one=False):
    db = get_db()
    cur = db.cursor()
    cur.execute(_prep(sql), args or ())
    rows = cur.fetchall()
    if IS_SQLITE:
        rows = [dict(r) for r in rows]
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=None):
    """Run a write statement, commit, return lastrowid."""
    db = get_db()
    cur = db.cursor()
    cur.execute(_prep(sql), args or ())
    last_id = cur.lastrowid
    db.commit()
    cur.close()
    return last_id


def init_app(app):
    app.teardown_appcontext(close_db)
    if IS_SQLITE:
        conn = _sqlite_connect()
        _sqlite_init_schema(conn)
        conn.close()
