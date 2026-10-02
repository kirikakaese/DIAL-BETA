// DIAL portal helpers: theme toggle, menus, delegated data-action handlers, live availability checker,
// auto-refresh. No user-facing strings live here - templates pass text via data-* attributes.
(function () {
  const root = document.documentElement;
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      document.cookie = "dial_theme=" + next + ";path=/;max-age=31536000;SameSite=Lax";
    });
  }

  // <details class="menu">: close when clicking outside or pressing Escape, return focus to the summary
  const menus = document.querySelectorAll("details.menu");
  if (menus.length) {
    document.addEventListener("click", function (e) {
      menus.forEach(function (menu) {
        if (menu.open && !menu.contains(e.target)) menu.removeAttribute("open");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      menus.forEach(function (menu) {
        if (menu.open) { menu.removeAttribute("open"); menu.querySelector("summary").focus(); }
      });
    });
    // Tabbing out of an open menu closes it so focus never lands behind the panel
    menus.forEach(function (menu) {
      menu.addEventListener("focusout", function (e) {
        if (menu.open && e.relatedTarget && !menu.contains(e.relatedTarget)) menu.removeAttribute("open");
      });
    });
  }

  // Event sidebar on small screens (off-canvas): focus moves into the menu on open, back to the toggle on close
  const sideToggle = document.getElementById("sidenav-toggle");
  const side = document.getElementById("sidenav");
  if (sideToggle && side) {
    function setSide(open, refocus) {
      document.body.classList.toggle("sidenav-open", open);
      sideToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        const first = side.querySelector("a, button");
        if (first && window.matchMedia("(max-width: 1000px)").matches) first.focus();
      } else if (refocus) {
        sideToggle.focus();
      }
    }
    sideToggle.addEventListener("click", function () {
      setSide(!document.body.classList.contains("sidenav-open"), false);
    });
    document.addEventListener("click", function (e) {
      if (document.body.classList.contains("sidenav-open") && !side.contains(e.target) && !sideToggle.contains(e.target)) setSide(false, false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("sidenav-open")) setSide(false, true);
    });
  }

  // Delegated actions instead of inline handlers:
  //   <button data-action="print">                          window.print()
  //   <button data-action="copy" data-target="#id" data-copied="Copied">  copy the target's text to the clipboard
  //   <select data-action="submit">                         submit the enclosing form on change
  //   <select data-action="navigate" data-href="/x/__slug__/">  go to data-href with __slug__ replaced by the value
  document.addEventListener("click", function (e) {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.getAttribute("data-action");
    if (action === "print") {
      e.preventDefault();
      window.print();
    } else if (action === "copy") {
      e.preventDefault();
      const target = document.querySelector(el.getAttribute("data-target"));
      if (!target || !navigator.clipboard) return;
      const text = "value" in target && target.value ? target.value : target.innerText;
      navigator.clipboard.writeText(text).then(function () {
        const done = el.getAttribute("data-copied");
        if (!done) return;
        const label = el.textContent;
        el.textContent = done;
        el.setAttribute("aria-live", "polite");
        setTimeout(function () { el.textContent = label; }, 1500);
      });
    }
  });
  document.addEventListener("change", function (e) {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.getAttribute("data-action");
    if (action === "submit" && el.form) {
      el.form.submit();
    } else if (action === "navigate" && el.value) {
      window.location = el.getAttribute("data-href").replace("__slug__", encodeURIComponent(el.value));
    }
  });

  // Live availability: <input data-availability-url="/api/v1/availability/?event=slug&number=">
  document.querySelectorAll("[data-availability-url]").forEach(function (input) {
    const out = document.getElementById(input.getAttribute("data-availability-target") || "availability");
    if (!out) return;
    if (!out.hasAttribute("aria-live")) out.setAttribute("aria-live", "polite");
    const typeSel = document.querySelector(input.getAttribute("data-availability-type") || "#id_type");
    const blockSel = input.getAttribute("data-availability-block")
      ? document.querySelector(input.getAttribute("data-availability-block")) : null;
    let timer = null;
    function check() {
      const n = input.value.trim();
      if (!n) { out.textContent = ""; out.className = "avail"; return; }
      let url = input.getAttribute("data-availability-url") + encodeURIComponent(n);
      if (typeSel) url += "&type=" + encodeURIComponent(typeSel.value);
      if (blockSel && typeSel && typeSel.value === (blockSel.getAttribute("data-only-for-type") || "trunk")) {
        url += "&block_digits=" + encodeURIComponent(blockSel.value);
      }
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          out.innerHTML = "";
          if (d.available) {
            out.className = "avail " + (d.requires_approval ? "warn" : "ok");
            out.textContent = d.requires_approval
              ? "✓ " + n + " " + (out.dataset.msgApproval || "is free but needs approval") + (d.range ? " (" + d.range + ")" : "")
              : "✓ " + n + " " + (out.dataset.msgFree || "is available");
          } else {
            out.className = "avail err";
            out.textContent = "✗ " + (d.reason || "not available");
            if (d.suggestions && d.suggestions.length) {
              const s = document.createElement("div");
              s.className = "suggestions small";
              s.textContent = (out.dataset.msgSuggest || "Free nearby: ") + " ";
              d.suggestions.forEach(function (x) {
                // real buttons (not href="#") so they are keyboard-operable and do not scroll the page
                const b = document.createElement("button");
                b.type = "button"; b.textContent = x; b.className = "btn btn-sm number";
                b.addEventListener("click", function () { input.value = x; input.focus(); check(); });
                s.appendChild(b);
              });
              out.appendChild(s);
            }
          }
        })
        .catch(function () { out.className = "avail"; out.textContent = ""; });
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(check, 250); });
    if (typeSel) typeSel.addEventListener("change", check);
    if (blockSel) blockSel.addEventListener("change", check);
    if (input.value) check();
  });

  // Auto-refresh for live dashboards: <body data-refresh="15">
  const refresh = document.querySelector("[data-refresh]");
  if (refresh) {
    const secs = parseInt(refresh.getAttribute("data-refresh"), 10);
    if (secs > 0) setTimeout(function () { window.location.reload(); }, secs * 1000);
  }

  // Confirm on dangerous forms / buttons: <form data-confirm="..."> or <button data-confirm="...">
  document.addEventListener("submit", function (e) {
    const form = e.target;
    const btn = e.submitter && e.submitter.getAttribute("data-confirm");
    const msg = btn || form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();
