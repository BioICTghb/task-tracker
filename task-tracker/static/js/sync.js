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

  async function run() {
    if (running) return false;
    if (!navigator.onLine) { setStatus("offline"); return false; }

    const items = await TT.db.getOutbox();
    if (!items.length) { setStatus("online"); return true; }

    running = true;
    setStatus("syncing");
    let allOk = true;

    for (const item of items) {
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
