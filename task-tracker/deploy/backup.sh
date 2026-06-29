#!/usr/bin/env bash
# Nightly backup of the database + photos. Install via cron (see DEPLOY.md step 7).
set -euo pipefail

APP=/opt/task-tracker
DEST=/var/backups/tasktracker
STAMP=$(date +%F)

mkdir -p "$DEST"

# Consistent SQLite snapshot (safe while the app is running)
sqlite3 "$APP/dev.db" ".backup '$DEST/dev-$STAMP.db'"

# Photos (the audit record)
tar czf "$DEST/uploads-$STAMP.tar.gz" -C "$APP" uploads

# Keep the last 30 days
find "$DEST" -type f -mtime +30 -delete
