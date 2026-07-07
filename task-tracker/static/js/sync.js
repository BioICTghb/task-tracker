/*
 * Sync engine — drains the IndexedDB outbox to the server.
 * Exposed as window.TT.sync. Idempotent: each item is removed only after a 2xx.
 * window.TT.sync.run() returns true if the outbox is empty afterwards.
 */
(function () {
  let running = false;

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

  async function run() {
    if (running) return false;
    if (!navigator.onLine) { setStatus("offline"); return false; }

    running = true;
    setStatus("syncing");
    let allOk = await createLocalJobs();

    const items = await TT.db.getOutbox();
    if (!items.length) {
      running = false;
      setStatus(navigator.onLine ? "online" : "offline");
      return allOk;
    }

    for (const item of items) {
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
          await TT.db.removeOutbox(item.key);
        } else {
          allOk = false; // server reachable but rejected — leave queued, try later
        }
      } catch (e) {
        allOk = false; // network dropped mid-sync
        break;
      }
    }

    running = false;
    setStatus(navigator.onLine ? "online" : "offline");
    return allOk && (await TT.db.outboxCount()) === 0;
  }

  window.TT = window.TT || {};
  window.TT.sync = { run, setStatus };
})();
