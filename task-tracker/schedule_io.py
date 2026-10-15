"""Writing roster cells.

Lives apart from app.py so the nightly importer and the web app share one code
path: the change detection here is what keeps the operators' "your shift changed"
banner honest, so a second implementation would quietly drift from it.
"""
import re

import db

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _upsert_sql():
    if db.IS_SQLITE:
        return (
            "INSERT INTO schedule_cells (work_date, col_index, shift, color, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT(work_date, col_index) DO UPDATE SET shift=excluded.shift, "
            "color=excluded.color, updated_at=excluded.updated_at"
        )
    return (
        "INSERT INTO schedule_cells (work_date, col_index, shift, color, updated_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE shift=VALUES(shift), color=VALUES(color), "
        "updated_at=VALUES(updated_at)"
    )


def apply_week(week_dates, rows, columns, now_iso, today_iso, dry_run=False):
    """Write one week of cells.

    `week_dates` are ISO dates (one per row of `rows`); `rows[r][c]` is a dict with
    "text"/"color" aligned to `columns` (schedule_columns rows in position order).

    `updated_at` only advances when a cell's text or colour genuinely differs, so
    re-importing an unchanged week notifies nobody.

    Returns (changed, notified_labels, overwritten, existing_nonempty).
    `overwritten` counts cells that already held a shift and are being replaced by
    something different - that, not `changed`, is the number worth being nervous
    about: filling empty future weeks is routine, rewriting a roster that is
    already there is what a broken template looks like.
    """
    iso = list(week_dates)
    marks = ",".join(["%s"] * len(iso))
    prev = {}
    for r in db.query(
        "SELECT work_date, col_index, shift, color, updated_at FROM schedule_cells "
        "WHERE work_date IN (%s)" % marks, tuple(iso)
    ):
        wd = r["work_date"] if isinstance(r["work_date"], str) else r["work_date"].isoformat()
        prev[(wd, r["col_index"])] = r

    sql = _upsert_sql()
    changed_count, notified = 0, []
    overwritten = existing_nonempty = 0
    for i, row in enumerate(rows):
        d = iso[i]
        for c, cell in enumerate(row):
            if c >= len(columns):
                break
            pos = columns[c]["position"]
            text = (cell.get("text") or "").strip()[:64]
            color = (cell.get("color") or "").strip().lower()
            color = color if HEX_RE.match(color) else ""
            old = prev.get((d, pos))
            had_shift = old is not None and bool((old["shift"] or "").strip())
            if had_shift:
                existing_nonempty += 1
            if old is None:
                changed = text != ""
                keep = None
            else:
                changed = (old["shift"] or "") != text or (old["color"] or "").lower() != color
                keep = old["updated_at"]
            if changed:
                changed_count += 1
                if had_shift:
                    overwritten += 1
                # Only a column linked to an account can notify anyone, and only a
                # shift still ahead of us is worth telling someone about.
                if d >= today_iso and columns[c]["user_id"]:
                    label = columns[c]["label"]
                    if label not in notified:
                        notified.append(label)
            if not dry_run:
                db.execute(sql, (d, pos, text, color, now_iso if changed else keep))
    return changed_count, notified, overwritten, existing_nonempty
