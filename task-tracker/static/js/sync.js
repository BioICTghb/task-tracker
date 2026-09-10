/*
 * Sync engine — drains the IndexedDB outbox to the server.
 * Exposed as window.TT.sync. Idempotent: each item is removed only after a 2xx.
 * window.TT.sync.run() returns true if the outbox is empty afterwards.
 */
(function () {
  // The sync in flight, if any. A second caller joins it instead of being told
  // "failed" — a photo taken while the 30s background poll happened to be
  // running used to report "Saved offline" on a perfectly good connection.
  let inflight = null;

  // Why the server refused a queued item, keyed by outbox key, so the page can
  // explain itself instead of blaming the network.
  const lastErrors = new Map();

  function setStatus(state) {
    const el = document.getElementById("net-status");
    if (!el) return;
    el.classList.remove("net-online", "net-offline", "net-syncing");
    if (state === "syncing") { el.classList.add("net-syncing"); el.textContent = "SYNCING"; }
    else if (state === "offline") { el.classList.add("net-offline"); el.textContent = "OFFLINE"; }
    else { el.classList.add("net-online"); el.textContent = "ONLINE"; }
  }

  // Create any offline 'extra' jobs on the server, then re-point their queued
  // photos from the temporary local id to the real server id.
  async function createLocalJobs() {
    const jobs = (await TT.db.getLocalJobs()).filter((j) => !j.synced);
    let ok = true;
    for (const j of jobs) {
      try {
        const res = await fetch("/api/tasks/extra", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: j.title, timestamp: j.created_at, client_key: j.localId, kind: j.kind }),
        });
        if (res.ok) {
          const data = await res.json();
          await TT.db.remapOutboxTask(j.localId, data.id);
          // Keep the local record (marked synced + real id) so any further
          // photos taken on the extra page still route to the right server job.
          j.synced = true;
          j.serverId = data.id;
          await TT.db.addLocalJob(j);
        } else { ok = false; }
      } catch (e) { ok = false; break; }
    }
    return ok;
  }

  async function drain() {
    setStatus("syncing");
    let allOk = await createLocalJobs();

    // Keep draining until nothing new is left: a photo queued *while* this sync
    // was running must not be left sitting until the next 30s poll.
    const attempted = new Set();
    for (let pass = 0; pass < 5; pass++) {
      const items = (await TT.db.getOutbox()).filter((i) => !attempted.has(i.key));
      if (!items.length) break;

      for (const item of items) {
        attempted.add(item.key);
        // Shift clock-in/out. Sent in queue order so a start always lands before
        // the end that followed it, and carrying the tap time + client_key so a
        // replay is a no-op rather than a second shift.
        if (item.kind === "shift") {
          try {
            const res = await fetch("/api/shift", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                action: item.action,
                timestamp: item.timestamp,
                client_key: item.client_key,
              }),
            });
            if (res.ok) {
              lastErrors.delete(item.key);
              await TT.db.removeOutbox(item.key);
            } else if (res.status === 400 || res.status === 403) {
              lastErrors.set(item.key, { status: res.status, permanent: true });
              await TT.db.removeOutbox(item.key);
              allOk = false;
            } else {
              lastErrors.set(item.key, { status: res.status, permanent: false });
              allOk = false;
            }
          } catch (e) {
            lastErrors.set(item.key, { status: 0, permanent: false });
            allOk = false;
            return allOk;
          }
          continue;
        }
        // Skip photos whose job hasn't been created on the server yet (still a
        // local string id). They upload once their extra job syncs.
        if (typeof item.task_id !== "number") { allOk = false; continue; }
        try {
          // Rebuild a Blob from the stored ArrayBuffer (new items) or use the
          // legacy stored blob (older queued items).
          const photo = item.buffer
            ? new Blob([item.buffer], { type: "image/jpeg" })
            : item.blob;

          const fd = new FormData();
          fd.append("photo_type", item.photo_type);
          fd.append("timestamp", item.timestamp);
          fd.append("photo", photo, `${item.task_id}_${item.photo_type}.jpg`);

          const res = await fetch(`/api/tasks/${item.task_id}/photo`, { method: "POST", body: fd });
          if (res.ok) {
            lastErrors.delete(item.key);
            await TT.db.removeOutbox(item.key);
          } else if (res.status === 404 || res.status === 400 || res.status === 413) {
            // Permanently un-syncable (task deleted, malformed, too large).
            // Retrying forever would keep the queue dirty and make every later
            // photo report a failure it had nothing to do with.
            lastErrors.set(item.key, { status: res.status, permanent: true });
            await TT.db.removeOutbox(item.key);
            allOk = false;
          } else {
            // Reachable but refused for now (409 on-hold, 5xx) — keep and retry.
            lastErrors.set(item.key, { status: res.status, permanent: false });
            allOk = false;
          }
        } catch (e) {
          lastErrors.set(item.key, { status: 0, permanent: false });
          allOk = false; // network dropped mid-sync
          return allOk;
        }
      }
    }
    return allOk;
  }

  async function run() {
    if (!navigator.onLine) { setStatus("offline"); return false; }
    if (inflight) return inflight;      // join it; don't report a false failure

    inflight = (async () => {
      try {
        const allOk = await drain();
        return allOk && (await TT.db.outboxCount()) === 0;
      } finally {
        setStatus(navigator.onLine ? "online" : "offline");
        inflight = null;
      }
    })();
    return inflight;
  }

  // How did *this* queued photo fare? -> "synced" | "offline" | "refused" | "dropped"
  async function statusOf(key) {
    if (key === undefined || key === null) return "synced";
    if (!(await TT.db.hasOutbox(key))) {
      const err = lastErrors.get(key);
      return err && err.permanent ? "dropped" : "synced";
    }
    return navigator.onLine ? "refused" : "offline";
  }

  window.TT = window.TT || {};
  window.TT.sync = { run, setStatus, statusOf, lastErrors };
})();
