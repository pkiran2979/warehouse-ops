(function () {
  const $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => [...(r || document).querySelectorAll(s)];

  // Table quick filter: <input data-filter="#tableId">
  $$("[data-filter]").forEach(inp => inp.addEventListener("input", () => {
    const q = inp.value.toLowerCase();
    $$("tbody tr", $(inp.dataset.filter)).forEach(tr => { tr.hidden = !tr.textContent.toLowerCase().includes(q); });
  }));

  // Searchable dropdown: <input data-select-filter="#selectId">
  $$("[data-select-filter]").forEach(inp => {
    const sel = $(inp.dataset.selectFilter), all = [...sel.options];
    inp.addEventListener("input", () => {
      const q = inp.value.toLowerCase(), keep = sel.value;
      sel.replaceChildren(...all.filter(o => o.text.toLowerCase().includes(q) || o.value === keep));
      if (sel.options.length) sel.dispatchEvent(new Event("change"));
    });
  });

  // Confirm before destructive actions: <form data-confirm="message">
  $$("form[data-confirm]").forEach(f => f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

  // Prevent double posting (important for a stock ledger)
  $$("form").forEach(f => f.addEventListener("submit", e => {
    if (e.defaultPrevented) return;
    setTimeout(() => $$("button:not([type=button])", f).forEach(b => { b.disabled = true; b.dataset.t = b.textContent; b.textContent = "Saving..."; }), 0);
  }));
  window.addEventListener("pageshow", () => $$("button[data-t]").forEach(b => { b.disabled = false; b.textContent = b.dataset.t; }));

  // Show/hide password
  $$("[data-toggle-pw]").forEach(b => b.addEventListener("click", () => {
    const i = $(b.dataset.togglePw); i.type = i.type === "password" ? "text" : "password"; b.textContent = i.type === "password" ? "Show" : "Hide";
  }));

  // Password strength hint
  $$("[data-strength]").forEach(i => i.addEventListener("input", () => {
    const v = i.value, ok = v.length >= 8 && /\d/.test(v) && /[A-Za-z]/.test(v), out = $(i.dataset.strength);
    out.textContent = !v ? "" : ok ? "Looks good" : "Needs 8+ characters with letters and numbers";
    out.className = "hint " + (ok ? "text-success" : "text-danger");
  }));

  // Receive: putaway hint
  const mat = $("#mat"), hint = $("#hint");
  if (mat && hint) { const upd = () => { const o = mat.selectedOptions[0]; if (o) hint.textContent = "Suggested bin: " + o.dataset.bin + "  |  " + o.dataset.note + (o.dataset.shelf ? "  |  Shelf life: " + o.dataset.shelf + " days (expiry auto-set)" : ""); }; mat.addEventListener("change", upd); upd(); }

  // Issue: choose material@bin, set max qty
  const bs = $("#bs");
  if (bs) { const upd = () => { const o = bs.selectedOptions[0]; if (!o) return; $("#mid").value = o.dataset.m; const q = $("#qty"); q.max = o.dataset.q; $("#avail").textContent = "Available: " + o.dataset.q + " " + o.dataset.u; }; bs.addEventListener("change", upd); upd(); }

  // Count: show system quantity
  const cm = $("#cm"), cb = $("#cb");
  if (cm && cb) { const upd = () => { if (!cm.value || !cb.value) return; fetch("/api/stock?material_id=" + cm.value + "&bin_id=" + cb.value, { credentials: "same-origin" }).then(r => r.json()).then(d => { $("#sys").textContent = "System quantity in this bin: " + d.qty + " " + d.unit; }).catch(() => {}); }; cm.addEventListener("change", upd); cb.addEventListener("change", upd); upd(); }

  // Auto-dismiss success messages
  $$(".alert-success").forEach(a => setTimeout(() => a.remove(), 6000));
})();
