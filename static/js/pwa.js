// PET PWA shell: service-worker registration, update toast, offline banner, install prompt.
// All user-facing text lives in the template (#pet-pwa); this file contains no strings to translate.
(function () {
  const root = document.getElementById("pet-pwa");
  if (!root) return;
  const html = document.documentElement;
  const offline = document.getElementById("pet-offline");
  const pingUrl = root.getAttribute("data-ping");
  const hasSW = "serviceWorker" in navigator;

  // Retry button on /offline/
  document.querySelectorAll("[data-pwa-reload]").forEach(function (b) {
    b.addEventListener("click", function () { window.location.reload(); });
  });

  // --- offline banner -------------------------------------------------------
  function setOffline(off) {
    if (offline) offline.hidden = !off;
    html.classList.toggle("is-offline", off);
  }
  function probe() {
    if (navigator.onLine === false) { setOffline(true); return; }
    // Only pages that a service worker may have served from cache need a real reachability check.
    if (!pingUrl || !hasSW || !navigator.serviceWorker.controller) { setOffline(false); return; }
    fetch(pingUrl, { method: "HEAD", cache: "no-store", credentials: "same-origin" })
      .then(function () { setOffline(false); })
      .catch(function () { setOffline(true); });
  }
  window.addEventListener("online", probe);
  window.addEventListener("offline", function () { setOffline(true); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) probe(); });
  probe();

  // --- install prompt (Chromium; iOS uses Share > Add to Home Screen) -----------
  const installBtn = document.getElementById("pet-install");
  let installEvent = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    installEvent = e;
    if (installBtn) installBtn.hidden = false;
  });
  if (installBtn) {
    installBtn.addEventListener("click", function () {
      if (!installEvent) return;
      const ev = installEvent;
      installEvent = null;
      installBtn.hidden = true;
      ev.prompt();
    });
  }
  window.addEventListener("appinstalled", function () { if (installBtn) installBtn.hidden = true; });

  // --- service worker ---------------------------------------------------------
  const swUrl = root.getAttribute("data-sw");
  const secure = location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (!swUrl || !hasSW || !secure) return;

  const toast = document.getElementById("pet-update");
  const reloadBtn = document.getElementById("pet-update-reload");
  const dismissBtn = document.getElementById("pet-update-dismiss");
  let reloadRequested = false;

  navigator.serviceWorker.addEventListener("controllerchange", function () {
    if (reloadRequested) window.location.reload();
  });

  function offerUpdate(worker) {
    if (!toast) return;
    toast.hidden = false;
    if (reloadBtn) {
      reloadBtn.onclick = function () {
        reloadRequested = true;
        reloadBtn.disabled = true;
        worker.postMessage({ type: "SKIP_WAITING" });
      };
    }
    if (dismissBtn) dismissBtn.onclick = function () { toast.hidden = true; };
  }

  navigator.serviceWorker.register(swUrl, { scope: "/" }).then(function (reg) {
    if (reg.waiting && navigator.serviceWorker.controller) offerUpdate(reg.waiting);
    reg.addEventListener("updatefound", function () {
      const next = reg.installing;
      if (!next) return;
      next.addEventListener("statechange", function () {
        if (next.state === "installed" && navigator.serviceWorker.controller) offerUpdate(next);
      });
    });
  }).catch(function () { /* registration is best-effort */ });
})();
