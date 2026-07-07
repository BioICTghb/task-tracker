/*
 * App bootstrap: register service worker, wire connection events,
 * and run the sync engine automatically.
 */
(function () {
  // Task Tracker — designed and developed by Sébastien Poncelet.
  // © Sébastien Poncelet. All rights reserved.
  try {
    console.log(
      "%cTask Tracker%c — made by Sébastien Poncelet",
      "font-weight:700", "color:#666"
    );
  } catch (e) { /* no console */ }

  // Register service worker (PWA + offline shell)
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(console.error);
    });
  }

  function refreshStatus() {
    TT.sync.setStatus(navigator.onLine ? "online" : "offline");
  }

  // Sync automatically when connection returns — no user action needed.
  window.addEventListener("online", () => {
    refreshStatus();
    TT.sync.run();
    requestBackgroundSync();
  });
  window.addEventListener("offline", refreshStatus);

  // Try once on load, and poll as a safety net (covers flaky WiFi where
  // 'online' may not fire). Lightweight: only acts if the outbox is non-empty.
  window.addEventListener("load", async () => {
    refreshStatus();
    if ((await TT.db.outboxCount()) > 0) TT.sync.run();
    requestBackgroundSync();
  });

  setInterval(async () => {
    if (navigator.onLine && (await TT.db.outboxCount()) > 0) TT.sync.run();
  }, 30000);

  // Ask the SW to background-sync even if the page later closes.
  async function requestBackgroundSync() {
    try {
      const reg = await navigator.serviceWorker.ready;
      if (reg.sync) await reg.sync.register("sync-photos");
    } catch (e) { /* background sync unsupported — page-level sync still works */ }
  }
})();

/*
 * Periodic auto-refresh so installed PWAs never get stuck on an old version.
 * Reloads every 15 minutes, but only when it won't interrupt the operator:
 * skips while offline, while a photo upload is queued, while the photo chooser
 * is open, or while a text field is focused (e.g. a manager editing times).
 */
(function () {
  const EVERY_MS = 15 * 60 * 1000;

  function unsafeToReload() {
    if (!navigator.onLine) return true;                 // offline: reload is pointless/disruptive
    const a = document.activeElement;
    if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable)) return true;
    const chooser = document.getElementById("photo-source");
    if (chooser && chooser.offsetParent !== null) return true;   // photo chooser is open
    return false;
  }

  async function maybeReload() {
    try {
      if (window.TT && TT.db && (await TT.db.outboxCount()) > 0) return;  // uploads pending
    } catch (e) { /* ignore */ }
    if (!unsafeToReload()) location.reload();
    // If it wasn't safe, we simply try again at the next interval.
  }

  setInterval(maybeReload, EVERY_MS);
})();
