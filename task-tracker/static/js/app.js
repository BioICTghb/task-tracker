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

  // Turn the fate of one queued photo (TT.sync.statusOf) into an honest message.
  // "Saved offline" used to be shown for any unsuccessful sync, including a
  // perfectly good upload that merely coincided with the 30s background poll.
  window.syncMessage = function (state, key) {
    const err = (TT.sync.lastErrors && TT.sync.lastErrors.get(key)) || {};
    if (state === "synced") {
      return '<div class="alert alert-success py-2">Saved &amp; synced.</div>';
    }
    if (state === "offline") {
      return '<div class="alert alert-warning py-2">Saved on your phone. '
           + 'It uploads automatically when you are back online.</div>';
    }
    if (state === "dropped") {
      return '<div class="alert alert-danger py-2">This photo could not be saved — '
           + 'the job may have been deleted. Tell your manager before retaking it.</div>';
    }
    if (err.status === 409) {
      return '<div class="alert alert-warning py-2">This job is <strong>on hold</strong>, '
           + 'so the photo was not accepted. It is safe on your phone and will upload '
           + 'once the hold is lifted.</div>';
    }
    return '<div class="alert alert-warning py-2">Saved on your phone — the server did not '
         + 'accept it yet. It will retry automatically.</div>';
  };

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
