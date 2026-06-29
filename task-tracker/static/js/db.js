/*
 * IndexedDB wrapper. Two stores:
 *   tasks   — cached task list (keyPath: id)
 *   outbox  — photos waiting to upload (auto key), each {task_id, photo_type, timestamp, blob}
 *
 * Exposed as window.TT.db
 */
(function () {
  const DB_NAME = "task-tracker";
  const DB_VERSION = 1;

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
    async outboxCount() {
      const all = await api.getOutbox();
      return all.length;
    },
  };

  window.TT = window.TT || {};
  window.TT.db = api;
})();
