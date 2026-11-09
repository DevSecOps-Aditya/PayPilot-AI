/* PayPilot AI front-end. Vanilla JS, one module per page, keyed by <body data-page>. */
(() => {
  "use strict";
  const PP = window.PP || {};

  /* ---------- helpers ---------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

  const nf = new Intl.NumberFormat(PP.locale || "en-IN", {
    style: "currency", currency: PP.currency || "INR", minimumFractionDigits: 0, maximumFractionDigits: 2,
  });
  const money = (n) => nf.format(Number(n) || 0);
  const pct0 = (n) => `${Math.round(Number(n) || 0)}%`;
  const fmtDate = (s) => new Date(`${s}T00:00:00`).toLocaleDateString(PP.locale, { day: "numeric", month: "short", year: "numeric" });
  const todayMonth = (PP.today || new Date().toISOString().slice(0, 10)).slice(0, 7);

  async function api(path, { method = "GET", body } = {}) {
    const res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      location.href = "/login";
      throw new Error("Please sign in again");
    }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }

  function toast(message, kind = "info") {
    let box = $("#toasts");
    if (!box) { box = document.createElement("div"); box.id = "toasts"; box.setAttribute("role", "status"); document.body.appendChild(box); }
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.classList.add("out"), 3200);
    setTimeout(() => el.remove(), 3600);
  }

  const fail = (err) => toast(err.message || "Something went wrong", "error");

  /** Minimal, XSS-safe markdown: input is escaped first, then headings/lists/bold/italic/code are added. */
  function md(src, offset = 2) {
    const inline = (t) => t
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*(?!\s)(.+?)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
    let html = "", list = null;
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of esc(src).split(/\r?\n/)) {
      const line = raw.trimEnd();
      let m;
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { close(); const lvl = Math.min(m[1].length + offset - 1, 5); html += `<h${lvl}>${inline(m[2])}</h${lvl}>`; }
      else if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
      else if (!line.trim()) close();
      else { close(); html += `<p>${inline(line)}</p>`; }
    }
    close();
    return html;
  }

  /* ---------- dialogs & forms ---------- */
  function wireDialog(dlg) {
    if (!dlg) return;
    dlg.addEventListener("click", (e) => {
      const r = dlg.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close();
    });
    $$("[data-close]", dlg).forEach((b) => b.addEventListener("click", () => dlg.close()));
  }
  const formObj = (form) => Object.fromEntries(new FormData(form).entries());
  async function busy(btn, fn) {
    if (btn) btn.disabled = true;
    try { return await fn(); } finally { if (btn) btn.disabled = false; }
  }
  const setOptions = (sel, values, { blank } = {}) => {
    sel.innerHTML = (blank ? `<option value="">${esc(blank)}</option>` : "") + values.map((v) => `<option>${esc(v)}</option>`).join("");
  };

  /* ---------- charts (dependency-free SVG) ---------- */
  const PALETTE = ["#5b8cff", "#2dd4bf", "#fbbf24", "#fb7185", "#a78bfa", "#34d399", "#f472b6", "#60a5fa", "#94a3b8"];
  /** Round the axis maximum up so that each of the 4 gridline steps is a round number (1, 2, 2.5, 5 or 10 × 10ⁿ). */
  const niceCeil = (n) => {
    const raw = n / 4, p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p * 4;
  };
  const short = (n) => (n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${+(n / 1e3).toFixed(1)}k` : `${Math.round(n)}`);

  function topN(items, n) {
    if (items.length <= n) return items;
    const head = items.slice(0, n - 1);
    return [...head, { label: "Other", value: items.slice(n - 1).reduce((a, i) => a + i.value, 0) }];
  }

  function donut(el, items, centerLabel = "Total") {
    const total = items.reduce((a, i) => a + i.value, 0);
    if (!total) { el.innerHTML = `<div class="empty">No expenses to chart yet.</div>`; return; }
    const R = 70, C = 2 * Math.PI * R;
    let offset = 0;
    const arcs = items.map((it, i) => {
      const len = (it.value / total) * C;
      const s = `<circle r="${R}" cx="90" cy="90" fill="none" stroke-width="26" style="stroke:${PALETTE[i % PALETTE.length]}" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-offset}" transform="rotate(-90 90 90)"><title>${esc(it.label)}: ${esc(money(it.value))}</title></circle>`;
      offset += len;
      return s;
    }).join("");
    const legend = items.map((it, i) => `<li><span class="dot" style="background:${PALETTE[i % PALETTE.length]}"></span><span class="lg-name">${esc(it.label)}</span><span class="lg-val">${esc(money(it.value))}</span></li>`).join("");
    el.innerHTML = `<div class="donut"><svg viewBox="0 0 180 180" role="img" aria-label="${esc(centerLabel)} by category">${arcs}<text x="90" y="86" text-anchor="middle" class="donut-label">${esc(centerLabel)}</text><text x="90" y="106" text-anchor="middle" class="donut-value">${esc(short(total))}</text></svg><ul class="legend">${legend}</ul></div>`;
  }

  function bars(el, labels, series, { every = 1, label = "Bar chart" } = {}) {
    const W = 640, H = 240, pl = 44, pr = 8, pt = 10, pb = 28;
    const max = niceCeil(Math.max(1, ...series.flatMap((s) => s.values)));
    const iw = W - pl - pr, ih = H - pt - pb, gw = iw / labels.length;
    const bw = Math.min(30, (gw * 0.72) / series.length);
    let g = "";
    for (let i = 0; i <= 4; i++) {
      const y = pt + ih - (ih * i) / 4;
      g += `<line class="grid" x1="${pl}" x2="${W - pr}" y1="${y}" y2="${y}"/><text class="axis" x="${pl - 6}" y="${y + 4}" text-anchor="end">${esc(short((max * i) / 4))}</text>`;
    }
    labels.forEach((lb, i) => {
      const cx = pl + gw * i + gw / 2;
      series.forEach((s, j) => {
        const v = s.values[i] || 0;
        const h = (ih * v) / max;
        const x = cx - (bw * series.length) / 2 + bw * j;
        g += `<rect x="${x.toFixed(1)}" y="${(pt + ih - h).toFixed(1)}" width="${Math.max(bw - 2, 1).toFixed(1)}" height="${h.toFixed(1)}" rx="3" style="fill:${s.color}"><title>${esc(s.name)} · ${esc(lb)}: ${esc(money(v))}</title></rect>`;
      });
      if (i % every === 0) g += `<text class="axis" x="${cx}" y="${H - 8}" text-anchor="middle">${esc(lb)}</text>`;
    });
    const legend = series.length > 1 ? `<div class="chart-legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
    el.innerHTML = `${legend}<svg viewBox="0 0 ${W} ${H}" class="bars" role="img" aria-label="${esc(label)}">${g}</svg>`;
  }

  const progress = (pct, status = "") => `<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(Math.round(pct), 100)}"><span class="${status}" style="width:${Math.min(Math.max(pct, 0), 100)}%"></span></div>`;
  const statusClass = (s) => (s === "over" ? "danger" : s === "warning" ? "warning" : "");
  const emptyHtml = (title, text) => `<div class="empty"><strong>${esc(title)}</strong>${esc(text || "")}</div>`;
  const txAmount = (t) => `<span class="num ${t.type === "income" ? "pos" : ""}">${t.type === "income" ? "+" : "−"}${esc(money(t.amount))}</span>`;

  /* ---------- shell ---------- */
  function initShell() {
    const sidebar = $("#sidebar");
    $("#menu-btn")?.addEventListener("click", () => sidebar.classList.toggle("open"));
    document.addEventListener("click", (e) => {
      if (sidebar?.classList.contains("open") && !sidebar.contains(e.target) && !e.target.closest("#menu-btn")) sidebar.classList.remove("open");
    });
    $("#theme-toggle")?.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("pp-theme", next); } catch (_) { /* private mode */ }
    });
    $("#logout-btn")?.addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } finally { location.href = "/"; }
    });
  }

  /* ---------- pages ---------- */
  const pages = {};

  function authForm(formId, endpoint) {
    const form = $(formId);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#form-error");
      err.textContent = "";
      const btn = $("button[type=submit]", form);
      try {
        await busy(btn, () => api(endpoint, { method: "POST", body: formObj(form) }));
        location.href = "/dashboard";
      } catch (ex) { err.textContent = ex.message; }
    });
  }
  pages.login = () => authForm("#login-form", "/api/auth/login");
  pages.register = () => authForm("#register-form", "/api/auth/register");

  /* Dashboard */
  pages.dashboard = async () => {
    $$("[data-ask]").forEach((b) => b.addEventListener("click", () => { location.href = `/assistant?q=${encodeURIComponent(b.dataset.ask)}`; }));
    const payoutDlg = $("#payout-dialog");
    const payoutStatus = $("#payout-status");
    const credentialsForm = $("#payout-credentials-form");
    const credentialsError = $("#payout-credentials-error");
    const payoutForm = $("#payout-form");
    const payoutError = $("#payout-error");
    const payoutElements = [
      payoutDlg, payoutStatus, credentialsForm, credentialsError, payoutForm, payoutError,
      $("#payout-credentials-step"), $("#payout-change-credentials"), $("#payout-result"),
      $("#payout-approval-link"), $("#payout-copy-link"), $("#set-payout-btn"),
    ];
    const payoutMarkupReady = payoutElements.every(Boolean);

    function showCredentials() {
      $("#payout-credentials-step").classList.remove("hidden");
      payoutForm.classList.add("hidden");
      $("#payout-change-credentials").classList.add("hidden");
      credentialsForm.reset();
      credentialsError.textContent = "";
      $("#payout-client-id").focus();
    }

    async function refreshPayout() {
      const state = await api("/api/payments");
      if (!state.configured) {
        payoutStatus.textContent = "The app owner must enable secure credential storage and configure the public callback URL.";
        credentialsForm.classList.add("hidden");
        payoutForm.classList.add("hidden");
        return;
      }
      credentialsForm.classList.remove("hidden");
      if (state.connection?.has_credentials) {
        payoutStatus.textContent = "PayPal Sandbox credentials validated and stored encrypted. Enter the merchant ID and order details.";
        $("#payout-merchant-id").value = state.connection.merchant_id || "";
        $("#payout-credentials-step").classList.add("hidden");
        payoutForm.classList.remove("hidden");
        $("#payout-change-credentials").classList.remove("hidden");
        return;
      }
      payoutStatus.textContent = "Enter your PayPal Sandbox app credentials to continue.";
      $("#payout-credentials-step").classList.remove("hidden");
      payoutForm.classList.add("hidden");
      $("#payout-change-credentials").classList.add("hidden");
    }

    if (payoutMarkupReady) {
    wireDialog(payoutDlg);
    $("#set-payout-btn").addEventListener("click", async () => {
      payoutError.textContent = "";
      $("#payout-result").classList.add("hidden");
      payoutDlg.showModal();
      try { await refreshPayout(); } catch (ex) { payoutStatus.textContent = ex.message; }
    });

    credentialsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      credentialsError.textContent = "";
      try {
        await busy($("#payout-save-credentials"), async () => {
          await api("/api/paypal/connect", {
            method: "POST",
            body: formObj(credentialsForm),
          });
          credentialsForm.reset();
          toast("PayPal credentials validated and stored securely.", "success");
          await refreshPayout();
        });
      } catch (ex) { credentialsError.textContent = ex.message; }
    });

    $("#payout-change-credentials").addEventListener("click", showCredentials);

    payoutForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      payoutError.textContent = "";
      try {
        const result = await busy($("#payout-create-btn"), () => api("/api/payments/orders", {
          method: "POST",
          body: formObj(payoutForm),
        }));
        const approvalLink = $("#payout-approval-link");
        approvalLink.href = result.approval_url;
        $("#payout-result").classList.remove("hidden");
        toast("Order created. Share its PayPal approval link with your customer.", "success");
      } catch (ex) { payoutError.textContent = ex.message; }
    });

    $("#payout-copy-link").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("#payout-approval-link").href);
        toast("Approval link copied", "success");
      } catch (ex) {
        toast("Could not copy the link. Copy it from your browser instead.", "error");
      }
    });
    } else {
      console.error("Payout dialog markup is outdated. Reload the Dashboard to update it.");
      const payoutButton = $("#set-payout-btn");
      if (payoutDlg) wireDialog(payoutDlg);
      if (payoutButton) {
        payoutButton.addEventListener("click", () => {
          if (payoutStatus) {
            payoutStatus.textContent = "This Dashboard is out of date. Reload the page to use the updated payout form.";
          }
          const oldConnectButton = $("#payout-connect-btn");
          if (oldConnectButton) oldConnectButton.disabled = true;
          if (payoutDlg?.showModal) payoutDlg.showModal();
        });
      }
    }

    $("#seed-btn")?.addEventListener("click", (e) => busy(e.currentTarget, async () => {
      try { await api("/api/demo/seed", { method: "POST" }); location.reload(); } catch (ex) { fail(ex); }
    }));
    const d = await api("/api/dashboard");
    const s = d.summary;
    $("#month-label").textContent = s.month_label;
    $("#onboarding").classList.toggle("hidden", d.has_data);

    $("#st-income").textContent = money(s.income);
    $("#st-income-sub").textContent = s.prev_income ? `${money(s.prev_income)} last month` : "";
    $("#st-expense").textContent = money(s.expenses);
    const chg = s.expense_change_pct;
    $("#st-expense-sub").textContent = chg == null ? "" : `${chg > 0 ? "↑" : "↓"} ${Math.abs(chg).toFixed(0)}% vs last month`;
    const net = $("#st-net");
    net.textContent = money(s.net);
    net.className = `value ${s.net >= 0 ? "pos" : "neg"}`;
    $("#st-net-sub").textContent = s.projected_expenses ? `Projected month-end spend: ${money(Math.round(s.projected_expenses))}` : "";
    $("#st-rate").textContent = s.savings_rate == null ? "–" : pct0(s.savings_rate);
    $("#st-rate-sub").textContent = s.savings_rate == null ? "Add income to calculate" : s.savings_rate >= 20 ? "Above the 20% target" : "Target: 20%";

    donut($("#cat-chart"), topN(s.categories.map((c) => ({ label: c.category, value: c.total })), 6), "Spent");
    bars($("#daily-chart"), d.daily.map((x) => String(x.day)), [{ name: "Spent", values: d.daily.map((x) => x.total), color: "var(--primary)" }],
      { every: Math.ceil(d.daily.length / 10), label: "Daily spending this month" });

    $("#budget-list").innerHTML = d.budgets.length
      ? `<div class="stack">${[...d.budgets].sort((a, b) => b.pct - a.pct).slice(0, 4).map((b) => `<div><div class="row between small"><span>${esc(b.category)}</span><span class="num muted">${esc(money(b.spent))} / ${esc(money(b.limit))}</span></div>${progress(b.pct, statusClass(b.status))}</div>`).join("")}</div>`
      : emptyHtml("No budgets yet", "Set limits in the Budget planner.");
    $("#goal-list").innerHTML = d.goals.length
      ? `<div class="stack">${d.goals.map((g) => `<div><div class="row between small"><span>${esc(g.title)}</span><span class="num muted">${pct0(g.pct)}</span></div>${progress(g.pct, "primary")}</div>`).join("")}</div>`
      : emptyHtml("No goals yet", "Create one to track progress.");
    const up = d.subscriptions.upcoming;
    $("#sub-list").innerHTML = up.length
      ? `<ul class="list">${up.slice(0, 5).map((x) => `<li><div><div class="title">${esc(x.name)}</div><div class="xs muted">${x.days_until === 0 ? "Today" : `In ${x.days_until} day${x.days_until === 1 ? "" : "s"}`}</div></div><span class="num">${esc(money(x.amount))}</span></li>`).join("")}</ul>`
      : emptyHtml("Nothing due soon", `${d.subscriptions.count} active · ${money(d.subscriptions.monthly_total)}/month`);
    $("#recent").innerHTML = d.recent.length
      ? `<ul class="list">${d.recent.map((t) => `<li><div><div class="title">${esc(t.description || t.category)}</div><div class="xs muted">${esc(t.category)} · ${esc(fmtDate(t.date))}</div></div>${txAmount(t)}</li>`).join("")}</ul>`
      : emptyHtml("No transactions yet");
    $("#alerts").innerHTML = d.alerts.length
      ? d.alerts.map((a) => `<div class="alert ${esc(a.level)}"><div><strong>${esc(a.title)}</strong>${esc(a.text)}</div></div>`).join("")
      : `<div class="alert success"><div><strong>All clear</strong>No alerts right now. You're on track.</div></div>`;
  };

  /* Expenses */
  pages.expenses = () => {
    const dlg = $("#tx-dialog"), form = $("#tx-form"), err = $("#tx-error");
    wireDialog(dlg);
    setOptions($("#f-cat"), [...PP.expenseCategories, ...PP.incomeCategories], { blank: "All categories" });
    $("#f-month").value = todayMonth;
    let items = [], editing = null, timer;

    const fillCategories = (selected) => {
      const type = form.elements.type.value;
      setOptions($("#tx-category"), type === "income" ? PP.incomeCategories : PP.expenseCategories);
      if (selected) $("#tx-category").value = selected;
    };
    $$("input[name=type]", form).forEach((r) => r.addEventListener("change", () => fillCategories()));

    function openForm(tx) {
      editing = tx || null;
      err.textContent = "";
      $("#tx-title").textContent = tx ? "Edit transaction" : "Add transaction";
      form.elements.type.value = tx ? tx.type : "expense";
      fillCategories(tx?.category);
      $("#tx-amount").value = tx ? tx.amount : "";
      $("#tx-date").value = tx ? tx.date : PP.today;
      $("#tx-desc").value = tx ? tx.description : "";
      dlg.showModal();
      $("#tx-amount").focus();
    }

    async function load() {
      const p = new URLSearchParams();
      if ($("#f-month").value) p.set("month", $("#f-month").value);
      if ($("#f-type").value) p.set("type", $("#f-type").value);
      if ($("#f-cat").value) p.set("category", $("#f-cat").value);
      if ($("#f-q").value.trim()) p.set("q", $("#f-q").value.trim());
      try {
        const d = await api(`/api/transactions?${p}`);
        items = d.items;
        $("#t-income").textContent = money(d.totals.income);
        $("#t-expense").textContent = money(d.totals.expense);
        const net = d.totals.income - d.totals.expense;
        $("#t-net").textContent = money(net);
        $("#t-net").className = `value ${net >= 0 ? "pos" : "neg"}`;
        $("#tx-empty").classList.toggle("hidden", items.length > 0);
        $("#tx-body").innerHTML = items.map((t) => `<tr>
          <td class="muted num">${esc(fmtDate(t.date))}</td>
          <td>${esc(t.description || "—")}</td>
          <td><span class="badge ${t.type === "income" ? "success" : ""}">${esc(t.category)}</span></td>
          <td class="right">${txAmount(t)}</td>
          <td class="actions"><button class="btn btn-ghost btn-sm" data-act="edit" data-id="${t.id}">Edit</button><button class="btn btn-ghost btn-sm btn-danger" data-act="del" data-id="${t.id}">Delete</button></td></tr>`).join("");
      } catch (ex) { fail(ex); }
    }

    $("#add-btn").addEventListener("click", () => openForm());
    ["f-month", "f-type", "f-cat"].forEach((id) => $(`#${id}`).addEventListener("change", load));
    $("#f-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
    $("#f-all").addEventListener("click", () => { $("#f-month").value = ""; load(); });
    $("#tx-body").addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const tx = items.find((t) => t.id === Number(b.dataset.id));
      if (b.dataset.act === "edit") return openForm(tx);
      if (!confirm("Delete this transaction?")) return;
      try { await api(`/api/transactions/${tx.id}`, { method: "DELETE" }); toast("Deleted", "success"); load(); } catch (ex) { fail(ex); }
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      try {
        await busy($("button[type=submit]", form), () => api(editing ? `/api/transactions/${editing.id}` : "/api/transactions",
          { method: editing ? "PUT" : "POST", body: formObj(form) }));
        dlg.close();
        toast(editing ? "Transaction updated" : "Transaction added", "success");
        load();
      } catch (ex) { err.textContent = ex.message; }
    });
    load();
    if (new URLSearchParams(location.search).get("add")) openForm();
  };

  /* Budgets */
  pages.budgets = () => {
    const dlg = $("#budget-dialog"), form = $("#budget-form"), err = $("#budget-error");
    wireDialog(dlg);
    setOptions($("#b-category"), PP.expenseCategories);
    const statusText = { ok: "On track", warning: "Near limit", over: "Over budget" };
    const statusBadge = { ok: "success", warning: "warning", over: "danger" };
    let items = [];

    function openForm(b) {
      err.textContent = "";
      $("#budget-title").textContent = b ? `Edit ${b.category} budget` : "Set budget";
      if (b) $("#b-category").value = b.category;
      $("#b-limit-input").value = b ? b.limit : "";
      dlg.showModal();
      $("#b-limit-input").focus();
    }
    async function save(category, monthly_limit) {
      await api("/api/budgets", { method: "POST", body: { category, monthly_limit } });
      toast("Budget saved", "success");
      load();
    }

    async function load() {
      try {
        const [d, sg] = await Promise.all([api("/api/budgets"), api("/api/budgets/suggestions")]);
        items = d.items;
        $("#month-label").textContent = d.month_label;
        $("#b-limit").textContent = money(d.total_limit);
        $("#b-spent").textContent = money(d.total_spent);
        const left = d.total_limit - d.total_spent;
        $("#b-left").textContent = money(left);
        $("#b-left").className = `value ${left >= 0 ? "pos" : "neg"}`;
        $("#budget-empty").classList.toggle("hidden", items.length > 0);
        $("#budget-cards").innerHTML = items.map((b) => `<article class="card budget-card">
          <div class="head"><div><h3>${esc(b.category)}</h3><div class="muted small num">${esc(money(b.spent))} of ${esc(money(b.limit))}</div></div><span class="badge ${statusBadge[b.status]}">${statusText[b.status]}</span></div>
          ${progress(b.pct, statusClass(b.status))}
          <div class="row between" style="margin-top:var(--sp-3)"><span class="small ${b.remaining < 0 ? "neg" : "muted"}">${b.remaining >= 0 ? `${esc(money(b.remaining))} left` : `${esc(money(-b.remaining))} over`}</span>
          <span><button class="btn btn-ghost btn-sm" data-act="edit" data-id="${b.id}">Edit</button><button class="btn btn-ghost btn-sm btn-danger" data-act="del" data-id="${b.id}">Delete</button></span></div></article>`).join("");

        $("#suggestions").innerHTML = sg.items.length
          ? `<ul class="list">${sg.items.map((i) => `<li><div><div class="title">${esc(i.category)}</div><div class="xs muted">Avg ${esc(money(i.avg_spend))}/month → suggest ${esc(money(i.suggested_limit))}</div></div><button class="btn btn-sm" data-apply="${esc(i.category)}" data-limit="${i.suggested_limit}">Apply</button></li>`).join("")}</ul>`
          : emptyHtml("No suggestions", "Suggestions appear once you have a few months of spending, for categories without a budget.");
        const p = sg.plan;
        $("#plan503020").innerHTML = sg.income > 0
          ? `<p class="small muted">Based on your average monthly income of ${esc(money(sg.income))}.</p><div class="stack">${[["Needs (rent, bills, groceries)", p.needs, 50], ["Wants (dining, fun, shopping)", p.wants, 30], ["Savings and debt payoff", p.savings, 20]].map(([n, v, w]) => `<div><div class="row between small"><span>${n}</span><span class="num">${esc(money(v))} · ${w}%</span></div>${progress(w, "primary")}</div>`).join("")}</div>`
          : emptyHtml("Not enough income data", "Add income transactions to see a split.");
      } catch (ex) { fail(ex); }
    }

    $("#add-btn").addEventListener("click", () => openForm());
    $("#budget-cards").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const b = items.find((x) => x.id === Number(btn.dataset.id));
      if (btn.dataset.act === "edit") return openForm(b);
      if (!confirm(`Delete the ${b.category} budget?`)) return;
      try { await api(`/api/budgets/${b.id}`, { method: "DELETE" }); load(); } catch (ex) { fail(ex); }
    });
    $("#suggestions").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-apply]");
      if (btn) busy(btn, () => save(btn.dataset.apply, Number(btn.dataset.limit)).catch(fail));
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      try { await busy($("button[type=submit]", form), () => save($("#b-category").value, $("#b-limit-input").value)); dlg.close(); } catch (ex) { err.textContent = ex.message; }
    });
    load();
  };

  /* Goals */
  pages.goals = () => {
    const gDlg = $("#goal-dialog"), gForm = $("#goal-form"), gErr = $("#goal-error");
    const cDlg = $("#contrib-dialog"), cForm = $("#contrib-form"), cErr = $("#contrib-error");
    wireDialog(gDlg); wireDialog(cDlg);
    let items = [], editing = null, contributing = null;

    function openGoal(g) {
      editing = g || null;
      gErr.textContent = "";
      $("#goal-title").textContent = g ? "Edit goal" : "New goal";
      $("#g-title").value = g ? g.title : "";
      $("#g-target").value = g ? g.target_amount : "";
      $("#g-saved").value = 0;
      $("#g-saved-wrap").classList.toggle("hidden", !!g);
      $("#g-deadline").value = g?.deadline || "";
      gDlg.showModal();
      $("#g-title").focus();
    }

    async function load() {
      try {
        items = (await api("/api/goals")).items;
        $("#goal-empty").classList.toggle("hidden", items.length > 0);
        $("#goal-cards").innerHTML = items.map((g) => {
          const badge = g.status === "done" ? `<span class="badge success">Achieved</span>` : g.status === "overdue" ? `<span class="badge danger">Past deadline</span>` : g.deadline ? `<span class="badge info">${g.days_left} days left</span>` : "";
          const need = g.monthly_needed ? `<div class="small" style="margin-top:var(--sp-2)">Save <strong class="num">${esc(money(g.monthly_needed))}</strong> / month to finish on time</div>` : "";
          return `<article class="card goal-card">
            <div class="head"><h3>${esc(g.title)}</h3>${badge}</div>
            <div class="amount">${esc(money(g.saved_amount))} <span class="muted small">of ${esc(money(g.target_amount))}</span></div>
            <div style="margin:var(--sp-3) 0">${progress(g.pct, g.status === "done" ? "" : "primary")}</div>
            <div class="small muted">${pct0(g.pct)} complete · ${esc(money(g.remaining))} to go${g.deadline ? ` · by ${esc(fmtDate(g.deadline))}` : ""}</div>${need}
            <div class="row" style="margin-top:var(--sp-4)"><button class="btn btn-sm btn-primary" data-act="add" data-id="${g.id}">Add money</button><button class="btn btn-sm btn-ghost" data-act="edit" data-id="${g.id}">Edit</button><button class="btn btn-sm btn-ghost btn-danger" data-act="del" data-id="${g.id}">Delete</button></div></article>`;
        }).join("");
      } catch (ex) { fail(ex); }
    }

    $("#add-btn").addEventListener("click", () => openGoal());
    $("#goal-cards").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const g = items.find((x) => x.id === Number(btn.dataset.id));
      if (btn.dataset.act === "edit") return openGoal(g);
      if (btn.dataset.act === "add") {
        contributing = g; cErr.textContent = ""; $("#contrib-title").textContent = `Add to ${g.title}`; $("#c-amount").value = ""; cDlg.showModal(); $("#c-amount").focus(); return;
      }
      if (!confirm(`Delete the goal "${g.title}"?`)) return;
      try { await api(`/api/goals/${g.id}`, { method: "DELETE" }); load(); } catch (ex) { fail(ex); }
    });
    gForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      gErr.textContent = "";
      const data = formObj(gForm);
      if (!data.deadline) data.deadline = null;
      try {
        await busy($("button[type=submit]", gForm), () => api(editing ? `/api/goals/${editing.id}` : "/api/goals", { method: editing ? "PUT" : "POST", body: data }));
        gDlg.close(); toast("Goal saved", "success"); load();
      } catch (ex) { gErr.textContent = ex.message; }
    });
    cForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      cErr.textContent = "";
      try {
        await busy($("button[type=submit]", cForm), () => api(`/api/goals/${contributing.id}/contribute`, { method: "POST", body: { amount: $("#c-amount").value } }));
        cDlg.close(); toast("Nice, progress added", "success"); load();
      } catch (ex) { cErr.textContent = ex.message; }
    });
    load();
  };

  /* Subscriptions */
  pages.subscriptions = () => {
    const dlg = $("#sub-dialog"), form = $("#sub-form"), err = $("#sub-error");
    wireDialog(dlg);
    let items = [], editing = null;

    function openForm(s) {
      editing = s || null;
      err.textContent = "";
      $("#sub-title").textContent = s ? "Edit subscription" : "Add subscription";
      $("#s-name").value = s ? s.name : "";
      $("#s-amount").value = s ? s.amount : "";
      $("#s-cycle").value = s ? s.billing_cycle : "monthly";
      $("#s-next").value = s ? s.next_date : PP.today;
      $("#s-active").checked = s ? !!s.is_active : true;
      dlg.showModal();
      $("#s-name").focus();
    }

    async function load() {
      try {
        const d = await api("/api/subscriptions");
        items = d.items;
        $("#s-count").textContent = d.summary.count;
        $("#s-monthly").textContent = money(d.summary.monthly_total);
        $("#s-yearly").textContent = money(d.summary.yearly_total);
        $("#sub-empty").classList.toggle("hidden", items.length > 0);
        $("#sub-body").innerHTML = items.map((s) => {
          const when = s.days_until === 0 ? "Today" : `In ${s.days_until} day${s.days_until === 1 ? "" : "s"}`;
          return `<tr style="${s.is_active ? "" : "opacity:.55"}">
            <td><strong>${esc(s.name)}</strong> ${s.is_active ? "" : `<span class="badge">Paused</span>`}</td>
            <td><span class="badge">${esc(s.billing_cycle)}</span></td>
            <td>${esc(fmtDate(s.next_date))} <span class="muted xs">${s.is_active ? `· ${when}` : ""}</span></td>
            <td class="right num">${esc(money(s.amount))}</td><td class="right num">${esc(money(s.monthly_cost))}</td>
            <td class="actions"><button class="btn btn-ghost btn-sm" data-act="edit" data-id="${s.id}">Edit</button><button class="btn btn-ghost btn-sm btn-danger" data-act="del" data-id="${s.id}">Delete</button></td></tr>`;
        }).join("");
      } catch (ex) { fail(ex); }
    }

    $("#add-btn").addEventListener("click", () => openForm());
    $("#sub-body").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const s = items.find((x) => x.id === Number(btn.dataset.id));
      if (btn.dataset.act === "edit") return openForm(s);
      if (!confirm(`Delete ${s.name}?`)) return;
      try { await api(`/api/subscriptions/${s.id}`, { method: "DELETE" }); load(); } catch (ex) { fail(ex); }
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      const data = formObj(form);
      data.is_active = $("#s-active").checked;
      try {
        await busy($("button[type=submit]", form), () => api(editing ? `/api/subscriptions/${editing.id}` : "/api/subscriptions", { method: editing ? "PUT" : "POST", body: data }));
        dlg.close(); toast("Subscription saved", "success"); load();
      } catch (ex) { err.textContent = ex.message; }
    });
    load();
  };

  /* AI assistant */
  pages.assistant = async () => {
    const log = $("#chat-log"), input = $("#chat-input"), send = $("#chat-send");
    const bubble = (role, html, extra = "") => {
      const el = document.createElement("div");
      el.className = `msg ${role === "user" ? "user" : "bot"}`;
      el.innerHTML = role === "user" ? esc(html) : `<span class="who">PayPilot AI</span>${html}${extra}`;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
      return el;
    };
    const offlineNote = `<div class="xs muted" style="margin-top:6px">Offline mode: answered from your data without the AI model.</div>`;

    async function ask(text) {
      if (!text.trim()) return;
      bubble("user", text);
      input.value = "";
      send.disabled = true;
      const typing = bubble("bot", `<span class="typing"><span></span><span></span><span></span></span>`);
      try {
        const r = await api("/api/assistant/chat", { method: "POST", body: { message: text } });
        typing.remove();
        bubble("bot", md(r.reply), r.mode === "offline" ? offlineNote : "");
      } catch (ex) {
        typing.remove();
        bubble("bot", `<p>Sorry, I couldn't answer that: ${esc(ex.message)}</p>`);
      } finally { send.disabled = false; input.focus(); }
    }

    $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
    $$("#chips .chip").forEach((c) => c.addEventListener("click", () => ask(c.textContent)));
    $("#clear-chat").addEventListener("click", async () => {
      if (!confirm("Clear this conversation?")) return;
      try { await api("/api/assistant/history", { method: "DELETE" }); log.innerHTML = ""; greet(); } catch (ex) { fail(ex); }
    });

    const greet = () => bubble("bot", `<p>Hi, I'm PayPilot AI, your financial copilot. Ask me about your spending, budgets, subscriptions or goals.</p>`);
    try {
      const [st, hist] = await Promise.all([api("/api/assistant/status"), api("/api/assistant/history")]);
      const badge = $("#ai-status");
      badge.textContent = st.gemini ? "Gemini connected" : "Offline mode";
      badge.className = `badge ${st.gemini ? "success" : "warning"}`;
      badge.title = st.gemini ? `Model: ${st.model}` : "Set GEMINI_API_KEY in .env for full AI answers";
      if (!hist.items.length) greet();
      hist.items.forEach((m) => bubble(m.role === "user" ? "user" : "bot", m.role === "user" ? m.content : md(m.content), m.mode === "offline" ? offlineNote : ""));
    } catch (ex) { fail(ex); }
    const q = new URLSearchParams(location.search).get("q");
    if (q) { history.replaceState(null, "", "/assistant"); ask(q); }
  };

  /* Reports & monthly summary */
  pages.reports = () => {
    const monthEl = $("#r-month");
    monthEl.value = todayMonth;

    function showReport(r) {
      $("#rv-title").textContent = r.title;
      const badge = $("#rv-mode");
      badge.classList.remove("hidden");
      badge.textContent = r.mode === "gemini" ? "AI-written" : "Offline summary";
      badge.className = `badge ${r.mode === "gemini" ? "success" : "warning"}`;
      $("#rv-body").innerHTML = md(r.content, 1); // "##" renders as <h2> in reports
    }

    async function loadSummary() {
      try {
        const d = await api(`/api/summary?month=${monthEl.value || todayMonth}`);
        const s = d.summary;
        $("#sum-title").textContent = `${s.month_label} summary`;
        $("#sum-badge").textContent = `${s.txn_count} transaction${s.txn_count === 1 ? "" : "s"}`;
        $("#m-income").textContent = money(s.income);
        $("#m-expense").textContent = money(s.expenses);
        $("#m-change").textContent = s.expense_change_pct == null ? "" : `${s.expense_change_pct > 0 ? "↑" : "↓"} ${Math.abs(s.expense_change_pct).toFixed(0)}% vs previous month`;
        const net = $("#m-net");
        net.textContent = money(s.net);
        net.className = `value ${s.net >= 0 ? "pos" : "neg"}`;
        $("#m-rate").textContent = s.savings_rate == null ? "–" : pct0(s.savings_rate);
        $("#m-cats").innerHTML = s.categories.length
          ? `<div class="stack">${s.categories.slice(0, 5).map((c) => `<div><div class="row between small"><span>${esc(c.category)}</span><span class="num muted">${esc(money(c.total))} · ${pct0(c.pct)}</span></div>${progress(c.pct, "primary")}</div>`).join("")}</div>`
          : emptyHtml("No expenses this month");
        $("#m-top").innerHTML = s.top_expenses.length
          ? `<ul class="list">${s.top_expenses.map((t) => `<li><div><div class="title">${esc(t.description || t.category)}</div><div class="xs muted">${esc(t.category)} · ${esc(fmtDate(t.tx_date))}</div></div><span class="num">${esc(money(t.amount))}</span></li>`).join("")}</ul>`
          : emptyHtml("Nothing to show");
      } catch (ex) { fail(ex); }
    }

    async function loadList() {
      try {
        const d = await api("/api/reports");
        $("#report-list").innerHTML = d.items.length
          ? `<ul class="list">${d.items.map((r) => `<li><button class="btn btn-ghost btn-sm" data-open="${r.id}" style="text-align:left;flex:1;justify-content:flex-start">${esc(r.title)}<span class="muted xs">&nbsp;· ${esc(r.created_at.slice(0, 10))}</span></button><button class="btn btn-ghost btn-sm btn-danger" data-del="${r.id}" aria-label="Delete report">Delete</button></li>`).join("")}</ul>`
          : emptyHtml("No saved reports", "Generated reports are saved here.");
      } catch (ex) { fail(ex); }
    }

    monthEl.addEventListener("change", loadSummary);
    $("#gen-btn").addEventListener("click", (e) => {
      const btn = e.currentTarget;
      const label = btn.innerHTML;
      busy(btn, async () => {
        btn.textContent = "Generating…";
        try {
          const r = await api("/api/reports/generate", { method: "POST", body: { month: monthEl.value || todayMonth } });
          showReport(r);
          if (r.mode === "offline") toast("Gemini unavailable, so this report was built from your data directly.", "info");
          loadList();
        } catch (ex) { fail(ex); } finally { btn.innerHTML = label; }
      });
    });
    $("#report-list").addEventListener("click", async (e) => {
      const open = e.target.closest("[data-open]"), del = e.target.closest("[data-del]");
      try {
        if (open) showReport(await api(`/api/reports/${open.dataset.open}`));
        if (del && confirm("Delete this report?")) { await api(`/api/reports/${del.dataset.del}`, { method: "DELETE" }); loadList(); }
      } catch (ex) { fail(ex); }
    });
    loadSummary();
    loadList();
  };

  /* Analytics */
  pages.analytics = () => {
    const DAYS = { Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday" };
    async function load() {
      try {
        const d = await api(`/api/analytics?months=${$("#a-period").value}`);
        const st = d.stats;
        $("#k-avg-month").textContent = money(Math.round(st.avg_monthly_expense));
        $("#k-avg-day").textContent = money(Math.round(st.avg_daily_spend));
        const saved = $("#k-saved");
        saved.textContent = money(st.total_saved);
        saved.className = `value ${st.total_saved >= 0 ? "pos" : "neg"}`;
        $("#k-rate").textContent = st.savings_rate == null ? "" : `${pct0(st.savings_rate)} of income`;
        $("#k-largest").textContent = st.largest_expense ? money(st.largest_expense.amount) : "–";
        $("#k-largest-sub").textContent = st.largest_expense ? `${st.largest_expense.description || st.largest_expense.category} · ${fmtDate(st.largest_expense.date)}` : "";

        bars($("#trend-chart"), d.monthly.map((m) => m.label), [
          { name: "Income", values: d.monthly.map((m) => m.income), color: "var(--success)" },
          { name: "Expenses", values: d.monthly.map((m) => m.expense), color: "var(--danger)" },
        ], { label: "Income versus expenses by month" });
        donut($("#cat-chart"), topN(d.categories.map((c) => ({ label: c.category, value: c.total })), 7), "Spent");
        bars($("#weekday-chart"), d.weekday.map((w) => w.day), [{ name: "Spent", values: d.weekday.map((w) => w.total), color: "var(--primary)" }], { label: "Spending by weekday" });
        const peak = d.weekday.reduce((a, b) => (b.total > a.total ? b : a), d.weekday[0]);
        if (peak.total > 0) $("#weekday-chart").insertAdjacentHTML("beforeend", `<p class="small muted" style="margin:var(--sp-2) 0 0">You spend the most on ${DAYS[peak.day]}s.</p>`);
        $("#merchants").innerHTML = d.top_merchants.length
          ? `<div class="table-wrap"><table><thead><tr><th>Description</th><th class="right">Times</th><th class="right">Total</th></tr></thead><tbody>${d.top_merchants.map((m) => `<tr><td>${esc(m.name)}</td><td class="right num">${m.n}</td><td class="right num">${esc(money(m.total))}</td></tr>`).join("")}</tbody></table></div>`
          : emptyHtml("No data yet", "Add expenses with descriptions to see where most of your money goes.");
      } catch (ex) { fail(ex); }
    }
    $("#a-period").addEventListener("change", load);
    load();
  };

  /* ---------- boot ---------- */
  document.addEventListener("DOMContentLoaded", () => {
    initShell();
    const run = pages[document.body.dataset.page];
    if (run) Promise.resolve(run()).catch(fail);
  });
})();
