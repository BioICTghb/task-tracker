/*
 * IndexedDB wrapper. Stores:
 *   tasks     — cached task list (keyPath: id)
 *   outbox    — photos waiting to upload (auto key) {task_id, photo_type, timestamp, buffer}
 *   localjobs — extra jobs created offline before they have a server id
 *               (keyPath: localId) {localId, title, status, created_at, synced}
 *
 * Exposed as window.TT.db
 */
(function () {
  const DB_NAME = "task-tracker";
  const DB_VERSION = 2;

  function open() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains("tasks")) {
          db.createObjectStore("tasks", { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains("outbox")) {
          db.createObjectStore("outbox", { keyPath: "key", autoIncrement: true });
        }
        if (!db.objectStoreNames.contains("localjobs")) {
          db.createObjectStore("localjobs", { keyPath: "localId" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  function tx(store, mode, fn) {
    return open().then(
      (db) =>
        new Promise((resolve, reject) => {
          const t = db.transaction(store, mode);
          const s = t.objectStore(store);
          let result;
          const req = fn(s);
          // If fn returned an IDBRequest, capture its result (which is
          // `undefined` for a missing key — important so callers can tell
          // "not found" from a real record).
          if (req && typeof req === "object" && "onsuccess" in req) {
            req.onsuccess = () => { result = req.result; };
          }
          t.oncomplete = () => resolve(result);
          t.onerror = () => reject(t.error);
          t.onabort = () => reject(t.error);
        })
    );
  }

  const api = {
    // ---- tasks cache ----
    async saveTasks(tasks) {
      return tx("tasks", "readwrite", (s) => {
        s.clear();
        tasks.forEach((t) => s.put(t));
      });
    },
    async getTasks() {
      return tx("tasks", "readonly", (s) => s.getAll());
    },
    async getTask(id) {
      return tx("tasks", "readonly", (s) => s.get(Number(id)));
    },
    async setTaskStatus(id, status) {
      const t = await api.getTask(id);
      if (!t) return;
      t.status = status;
      return tx("tasks", "readwrite", (s) => s.put(t));
    },

    // ---- outbox ----
    async queuePhoto(item) {
      return tx("outbox", "readwrite", (s) => s.add(item));
    },
    async getOutbox() {
      return tx("outbox", "readonly", (s) => s.getAll());
    },
    async removeOutbox(key) {
      return tx("outbox", "readwrite", (s) => s.delete(key));
    },
    // Is this specific photo still waiting to upload? Lets a page report the fate
    // of the photo just taken instead of the state of the whole outbox.
    async hasOutbox(key) {
      return (await tx("outbox", "readonly", (s) => s.get(key))) !== undefined;
    },
    async outboxCount() {
      const all = await api.getOutbox();
      return all.length;
    },
    // Re-point queued photos from a local job id to its real server id (after sync)
    async remapOutboxTask(oldId, newId) {
      const all = await api.getOutbox();
      return tx("outbox", "readwrite", (s) => {
        all.forEach((item) => {
          if (item.task_id === oldId) { item.task_id = newId; s.put(item); }
        });
      });
    },

    // ---- localjobs (extra jobs created offline) ----
    async addLocalJob(job) {
      return tx("localjobs", "readwrite", (s) => s.put(job));
    },
    async getLocalJobs() {
      return tx("localjobs", "readonly", (s) => s.getAll());
    },
    async getLocalJob(localId) {
      return tx("localjobs", "readonly", (s) => s.get(localId));
    },
    async setLocalJobStatus(localId, status) {
      const j = await api.getLocalJob(localId);
      if (!j) return;
      j.status = status;
      return tx("localjobs", "readwrite", (s) => s.put(j));
    },
    async removeLocalJob(localId) {
      return tx("localjobs", "readwrite", (s) => s.delete(localId));
    },
  };

  window.TT = window.TT || {};
  window.TT.db = api;
})();
