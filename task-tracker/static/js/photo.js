/*
 * Shared camera-photo handling, used by the normal task page and the
 * offline "extra job" page.  Exposed as window.TT.photo
 *
 * Downscales + recompresses the camera image to a small JPEG and returns an
 * ArrayBuffer. iOS cameras produce huge (sometimes HEIC) images; storing those
 * raw in IndexedDB freezes Safari, so we always shrink first.
 */
(function () {
  function toJpegBuffer(file, maxDim, quality) {
    maxDim = maxDim || 1600;
    quality = quality || 0.7;
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        canvas.toBlob((blob) => {
          if (!blob) { reject(new Error("toBlob failed")); return; }
          const fr = new FileReader();   // most widely supported on iOS
          fr.onload = () => resolve(fr.result);
          fr.onerror = () => reject(fr.error);
          fr.readAsArrayBuffer(blob);
        }, "image/jpeg", quality);
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("image decode failed")); };
      img.src = url;
    });
  }

  // Convert a camera file and queue it in the outbox for `taskId` (number = server
  // job, string = offline extra job). Falls back to raw bytes if conversion fails.
  async function queue(taskId, photoType, file) {
    let buffer;
    try {
      buffer = await toJpegBuffer(file);
    } catch (e) {
      buffer = await file.arrayBuffer();
    }
    // Returns the outbox key, so the caller can report what happened to *this*
    // photo rather than guessing from the state of the whole queue.
    return TT.db.queuePhoto({
      task_id: taskId,
      photo_type: photoType,
      timestamp: new Date().toISOString(),
      buffer: buffer,
    });
  }

  // Does this device have a camera worth offering? Phones/tablets do; a desktop
  // or laptop can realistically only upload an existing file, so asking
  // "take photo or from gallery?" there is a dead end. Feature-detected (touch +
  // coarse pointer) rather than sniffing the user agent; a touchscreen laptop
  // driven by a mouse reports a fine pointer and is treated as a desktop.
  function hasCamera() {
    return (navigator.maxTouchPoints || 0) > 0 &&
           window.matchMedia("(pointer: coarse)").matches;
  }

  // Ask the operator: take a new photo (camera) or pick an existing one
  // (gallery). We set/clear `capture` ourselves so both options work reliably
  // on Android and iOS, then trigger the given hidden <input type=file>.
  // On desktop we skip the prompt and open the file picker directly.
  function choose(inputEl) {
    const ov = document.getElementById("photo-source");
    if (!ov || !hasCamera()) {              // desktop (or markup missing)
      inputEl.removeAttribute("capture");
      inputEl.click();
      return;
    }
    const onPick = (e) => {
      const btn = e.target.closest("[data-src]");
      if (!btn) return;
      ov.classList.remove("show");
      ov.removeEventListener("click", onPick);
      const which = btn.dataset.src;
      if (which === "cancel") return;
      if (which === "camera") inputEl.setAttribute("capture", "environment");
      else inputEl.removeAttribute("capture");
      inputEl.click();
    };
    ov.addEventListener("click", onPick);
    ov.classList.add("show");
  }

  window.TT = window.TT || {};
  window.TT.photo = { toJpegBuffer, queue, choose };
})();
