/*
 * Service worker — app-shell caching + Background Sync.
 *
 * Caching strategy:
 *   - App shell (HTML pages, CSS, JS, manifest, icons): cache-first, so the
 *     UI opens instantly and works fully offline.
 *   - GET /api/tasks: network-first with cache fallback (fresh when online,
 *     last-known list when offline). The page also keeps its own IndexedDB copy.
 *   - Everything else: network, falling back to cache.
 *
 * Background Sync ('sync-photos'): drains the IndexedDB outbox even if the
 * page has been closed, so queued photos upload as soon as the OS grants
 * connectivity.
 */
const CACHE = "tt-shell-v14";
const SHELL = [
  "/",
  "/tech",
  "/tech/extra",
  "/manifest.json",
  "/static/css/style.css",
  "/static/js/db.js",
  "/static/js/photo.js",
  "/static/js/sync.js",
  "/static/js/app.js",
  "/static/icons/icon-192.svg",
  "/static/icons/icon-512.svg",
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never cache POSTs (photo uploads / publish)

  const url = new URL(req.url);

  // Network-first for the task list
  if (url.pathname === "/api/tasks") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Don't cache uploaded photos or per-task API calls — just pass through.
  if (url.pathname.startsWith("/uploads/") || url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }

  // HTML pages are server-rendered (the report carries live data), so use
  // network-first: fresh when online, cached copy as an offline fallback.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((c) => c || caches.match("/tech")))
    );
    return;
  }

  // Cache-first for static shell assets (CSS / JS / icons / fonts)
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      });
    })
  );
});

/* ---------------- Background Sync: drain the outbox from the SW ------------- */
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-photos") {
    event.waitUntil(drainOutbox());
  }
});

function idbOpen() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("task-tracker", 1);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

function idbGetAll(db, store) {
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, "readonly").objectStore(store).getAll();
    t.onsuccess = () => resolve(t.result);
    t.onerror = () => reject(t.error);
  });
}

function idbDelete(db, store, key) {
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, "readwrite");
    t.objectStore(store).delete(key);
    t.oncomplete = () => resolve();
    t.onerror = () => reject(t.error);
  });
}

function idbPut(db, store, value) {
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, "readwrite");
    t.objectStore(store).put(value);
    t.oncomplete = () => resolve();
    t.onerror = () => reject(t.error);
  });
}

// Create offline 'extra' jobs and re-point their queued photos to the new id.
async function createLocalJobs(db) {
  const jobs = (await idbGetAll(db, "localjobs")).filter((j) => !j.synced);
  for (const j of jobs) {
    const res = await fetch("/api/tasks/extra", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: j.title, timestamp: j.created_at, client_key: j.localId }),
    });
    if (!res.ok) continue;
    const data = await res.json();
    const items = await idbGetAll(db, "outbox");
    for (const item of items) {
      if (item.task_id === j.localId) { item.task_id = data.id; await idbPut(db, "outbox", item); }
    }
    j.synced = true;
    j.serverId = data.id;
    await idbPut(db, "localjobs", j);
  }
}

async function drainOutbox() {
  const db = await idbOpen();
  await createLocalJobs(db);
  const items = await idbGetAll(db, "outbox");
  for (const item of items) {
    if (typeof item.task_id !== "number") continue; // job not created yet
    try {
      const photo = item.buffer
        ? new Blob([item.buffer], { type: "image/jpeg" })
        : item.blob;

      const fd = new FormData();
      fd.append("photo_type", item.photo_type);
      fd.append("timestamp", item.timestamp);
      fd.append("photo", photo, `${item.task_id}_${item.photo_type}.jpg`);
      const res = await fetch(`/api/tasks/${item.task_id}/photo`, { method: "POST", body: fd });
      if (res.ok) await idbDelete(db, "outbox", item.key);
    } catch (e) {
      // Still offline — throw so the platform retries the sync later.
      throw e;
    }
  }
}
