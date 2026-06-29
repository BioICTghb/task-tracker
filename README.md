# Task Tracker

A minimal, **offline-first** task tracking web app (PWA) for an industrial environment — it replaces a WhatsApp task list. Managers paste a list of jobs; technicians document each job with **before / after photos** (plus optional progress shots). Everything else is automatic.

> The application lives in [`task-tracker/`](task-tracker/). Full documentation is in
> [`task-tracker/README.md`](task-tracker/README.md), and a plain-language setup
> guide for non-technical users is in [`task-tracker/SETUP_GUIDE.txt`](task-tracker/SETUP_GUIDE.txt).

## What it does

- **Managers** paste a task list (one line = one task) and publish it.
- **Technicians** open a job and take a **before** photo, optional **progress** photos (up to 4), and an **after** photo. No typing required.
- **Unplanned "extra" jobs** can be created on the spot (works fully offline).
- **Logins & roles** — managers publish/report and manage operators; technicians complete their task list. Every job records who started and finished it.
- **Reports** — by single day or date range, printable/PDF, with one page of photos per job.

## Offline first

- Service worker caches the app shell so it opens with no connection.
- Photos and timestamps are stored on the phone (IndexedDB) and **sync automatically** when the network returns — no user action.
- A banner always shows **ONLINE / OFFLINE / SYNCING**.

## Tech

- Backend: Python + Flask. SQLite for simple/single-server use; MySQL supported for production (see [`schema.sql`](task-tracker/schema.sql)).
- Frontend: Bootstrap + vanilla JS PWA (service worker, IndexedDB, background sync).
- Photos stored on the filesystem under `uploads/YYYY/MM/DD/`; timestamps in Europe/Brussels.

## Run it locally

See [`task-tracker/README.md`](task-tracker/README.md) §3. The quickest path uses the
zero-setup SQLite backend; on first start a default manager account is created
(`manager` / `biopower`) — **change it immediately** from the Operators page.

## Status

Built and refined during live field testing. Deployment to an always-on Linux
server (with a fixed link via Cloudflare Tunnel) is planned — see the deployment
section of [`task-tracker/README.md`](task-tracker/README.md).
