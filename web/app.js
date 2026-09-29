"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const n = (v) => Number(v || 0);
const usd = (v, sign = false) => {
  const x = n(v);
  const s = Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (x < 0 ? "−" : sign && x > 0 ? "+" : "") + "$" + s;
};
const pct = (v, digits = 2) => (n(v) > 0 ? "+" : n(v) < 0 ? "−" : "") + Math.abs(n(v)).toFixed(digits) + "%";
const fmtVal = (v) => (Array.isArray(v) ? `[${v.length}]` : typeof v === "number" ? +v.toFixed(4) : String(v));
const tone = (v) => (n(v) > 0 ? "up" : n(v) < 0 ? "down" : "");
const price = (v) => {
  const x = n(v);
  if (!x) return "0";
  if (x >= 1) return x.toFixed(4);
  const zeros = Math.max(0, -Math.floor(Math.log10(x)) - 1);
  return x.toFixed(Math.min(zeros + 4, 14));
};
const held = (iso) => {
  if (!iso) return "—";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  return m < 60 ? `${Math.max(0, m).toFixed(0)}m` : m < 1440 ? `${(m / 60).toFixed(1)}h` : `${(m / 1440).toFixed(1)}d`;
};
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const shortTs = (iso) => (iso || "").replace("T", " ").slice(5, 16);

let STATUS = null;
let STATUS_AUTONOMY = {};
let activeConcept = null; // { id, label } when the lessons list is filtered by a concept
let SETTINGS = null;
let settingsDirty = false;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${(x.loc || []).join(".")}: ${x.msg}`).join("\n") : d || res.statusText);
  }
  return data;
}
async function attempt(fn) {
  try { return await fn(); } catch (e) { toast("Request failed", e.message, "down"); }
}

/* ------------------------------------------------------------------ toasts & modal */
function toast(title, body = "", kind = "", ms = 6000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<div class="title">${esc(title)}</div>${body ? `<div class="body">${esc(body)}</div>` : ""}`;
  $("#toasts").appendChild(el);
  while ($("#toasts").children.length > 4) $("#toasts").firstChild.remove();
  setTimeout(() => el.remove(), ms);
}

/** Resolves to true / the input value on confirm, null on cancel. */
function modal({ title, body, ok = "Confirm", danger = false, input = null }) {
  const dlg = $("#modal");
  $("#mTitle").textContent = title;
  $("#mBody").innerHTML = body;
  const okBtn = $("#mOk");
  okBtn.textContent = ok;
  okBtn.className = danger ? "danger" : "primary";
  const inp = $("#mInput");
  inp.classList.toggle("hidden", !input);
  inp.value = input?.value ?? "";
  inp.type = input?.type || "text";
  inp.placeholder = input?.placeholder || "";
  const validate = () => { okBtn.disabled = !!input?.match && inp.value.trim().toUpperCase() !== input.match; };
  inp.oninput = validate;
  validate();
  inp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); if (!okBtn.disabled) dlg.close("ok"); } };
  return new Promise((resolve) => {
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? (input ? inp.value.trim() : true) : null);
    dlg.returnValue = "";
    dlg.showModal();
    (input ? inp : okBtn).focus();
  });
}

/* ------------------------------------------------------------------ views */
const VIEWS = ["desk", "brain", "settings", "live"];
function showView() {
  const v = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "desk";
  VIEWS.forEach((x) => $(`#view-${x}`).classList.toggle("hidden", x !== v));
  $$("nav.tabs a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
  if (v === "brain") { window.Brain3D?.refresh(false); attempt(loadBrain); attempt(loadStrategy); attempt(loadPlaybook); }
  if (v === "settings" && !settingsDirty) attempt(loadSettings);
  if (v === "live") renderLive();
  if (v === "desk") attempt(loadTrades);
}
addEventListener("hashchange", showView);

/* ------------------------------------------------------------------ status + KPIs */
async function refreshStatus() {
  const s = await api("/api/status");
  STATUS = s;
  const live = s.mode === "live";
  const acct = live ? s.live : s.paper;
  const summary = acct.summary || {};

  const pill = $("#modePill");
  pill.textContent = live ? (s.dry_run ? "LIVE · DRY RUN" : "LIVE") : "PAPER";
  pill.className = "pill" + (live ? (s.dry_run ? " dry" : " live") : "");

  $("#engineDot").classList.toggle("on", s.running);
  $("#engineText").textContent = s.running ? "Running" : "Stopped";
  const btn = $("#btnEngine");
  btn.textContent = s.running ? "Stop" : "Start";
  btn.className = s.running ? "" : "primary";

  $("#kEquity").textContent = acct.equity != null ? usd(acct.equity) : "—";
  $("#kEquitySub").textContent = live ? "wallet + positions" : s.paper.initialized ? `start ${usd(s.paper.starting_credit)}` : "not funded";
  const pnl = live ? summary.total_pnl_usd : s.paper.pnl_usd;
  $("#kPnl").textContent = usd(pnl, true);
  $("#kPnl").className = "value " + tone(pnl);
  $("#kPnlSub").textContent = live ? "realized" : pct(s.paper.pnl_pct);
  $("#kCash").textContent = acct.cash != null ? usd(acct.cash) : "—";
  $("#kCashSub").textContent = live ? "SOL less fee reserve" : "available";
  $("#kOpen").textContent = String(acct.open ?? 0);
  $("#kWin").textContent = summary.trades ? (summary.win_rate * 100).toFixed(1) + "%" : "—";
  $("#kWinSub").textContent = summary.trades ? `${summary.wins}W / ${summary.losses}L` : "no trades";
  $("#kExp").textContent = summary.trades ? pct(summary.expectancy_pct) : "—";
  $("#kExp").className = "value " + (summary.trades ? tone(summary.expectancy_pct) : "");
  $("#kLessons").textContent = String(s.lessons);
  const models = Object.values(s.groq?.models || {});
  if (!s.groq_enabled || !models.length) {
    $("#kModel").textContent = "—";
    $("#kModelSub").textContent = "heuristic · no API key";
  } else {
    const low = Math.min(...models.map((m) => m.budget_pct));
    const used = models.reduce((a, m) => a + m.tokens_today, 0);
    $("#kModel").textContent = `${low.toFixed(0)}%`;
    $("#kModel").className = "value " + (low < 15 ? "down" : "");
    $("#kModelSub").textContent = `${(used / 1000).toFixed(1)}k tokens today · ${models.length} models`;
    $("#kModel").parentElement.title = Object.entries(s.groq.roles).map(([role, m]) => {
      const st = s.groq.models[m];
      return `${role}: ${m} · ${st.budget_pct}% paced budget · ${st.tokens_today} tokens today${st.cooldown_s ? ` · cooling ${st.cooldown_s}s` : ""}`;
    }).join("\n");
  }
  const auto = s.autonomy || {};
  $("#autoPill").classList.toggle("hidden", !auto.autonomous);
  $("#autoPill").textContent = auto.paused_until ? "AUTONOMOUS · PAUSED" : "AUTONOMOUS";
  STATUS_AUTONOMY = auto;

  $("#sbTick").textContent = s.last_tick ? `tick #${s.tick_count} · ${held(s.last_tick)} ago` : "tick —";
  $("#sbErr").textContent = s.last_error ? `⚠ ${s.last_error}` : "";
  if (!$("#view-live").classList.contains("hidden")) renderLive();
  return s;
}

$("#btnEngine").onclick = () => attempt(async () => {
  if (STATUS?.running) {
    await api("/api/engine/stop", { method: "POST" });
  } else {
    await api("/api/engine/start", { method: "POST" });
    toast("Engine started", `${STATUS?.mode === "live" ? "Live" : "Paper"} mode · first scan running`, "accent", 3500);
  }
  await refreshStatus();
});

/* ------------------------------------------------------------------ positions & trades */
async function refreshPositions() {
  const rows = await api("/api/positions");
  $("#posEmpty").classList.toggle("hidden", rows.length > 0);
  $("#posCount").textContent = rows.length ? `${rows.length} · ${usd(rows.reduce((a, t) => a + t.value_usd, 0))}` : "";
  $("#posBody").innerHTML = rows.map((t) => `<tr>
      <td><span class="sym" title="${esc(t.mint)}">${esc(t.symbol)}</span>${t.orig_qty && t.qty < t.orig_qty * 0.99 ? ` <span class="muted mono" title="already sold">−${Math.round((1 - t.qty / t.orig_qty) * 100)}%</span>` : ""}</td>
      <td class="muted">${t.mode === "live" ? "live" : "paper"}</td>
      <td class="num">${usd(t.cost_usd)}</td>
      <td class="num">${usd(t.value_usd)}</td>
      <td class="num ${tone(t.unrealized_pct)}">${pct(t.unrealized_pct)}</td>
      <td class="num muted">−${n(t.stop_loss_pct)}% / +${n(t.take_profit_pct)}%</td>
      <td class="num muted">${held(t.opened_at)}</td>
      <td class="num"><button class="ghost" data-close="${Number(t.id)}" data-sym="${esc(t.symbol)}">Close</button></td>
    </tr>`).join("");
  $$("[data-close]").forEach((b) => b.onclick = async () => {
    const ok = await modal({ title: `Close ${b.dataset.sym}?`, body: "Sells the full position at the current market price.", ok: "Close position", danger: true });
    if (ok) attempt(async () => { await api(`/api/positions/${b.dataset.close}/close`, { method: "POST" }); await refreshAll(); });
  });
}

async function loadTrades() {
  const rows = await api("/api/trades?limit=200");
  $("#tradesEmpty").classList.toggle("hidden", rows.length > 0);
  $("#tradeCount").textContent = rows.length ? String(rows.length) : "";
  $("#tradesBody").innerHTML = rows.map((t) => {
    const tx = t.exit_tx && !["PAPER", "DRYRUN"].includes(t.exit_tx)
      ? `<a href="https://solscan.io/tx/${encodeURIComponent(t.exit_tx)}" target="_blank" rel="noopener">view</a>`
      : `<span class="faint">${esc((t.exit_tx || "").toLowerCase())}</span>`;
    return `<tr>
      <td class="num muted" style="text-align:left">${esc(shortTs(t.closed_at))}</td>
      <td><span class="sym">${esc(t.symbol)}</span></td>
      <td class="muted">${t.mode}</td>
      <td class="num">${usd(t.cost_usd)}</td>
      <td class="num ${tone(t.pnl_usd)}">${usd(t.pnl_usd, true)}</td>
      <td class="num ${tone(t.pnl_pct)}">${pct(t.pnl_pct)}</td>
      <td class="clip-text" title="${esc(t.exit_reason)}">${esc(t.exit_reason)}</td>
      <td>${tx}</td></tr>`;
  }).join("");
}

/* ------------------------------------------------------------------ activity feed */
function feedEntry(evt) {
  const conf = evt.confidence != null ? `conf ${(evt.confidence * 100).toFixed(0)}%` : "";
  switch (evt.type) {
    case "thought":
      if (evt.brain === "filter") return { tag: "FILTER", cls: "filter", noise: true, sym: evt.symbol, text: evt.skip_reason };
      if (evt.kind === "bail") return { tag: "CHECK", cls: "", sym: evt.symbol, meta: `→ ${evt.action}`, text: evt.reasoning };
      if (evt.kind === "screen") return { tag: "SCREEN", cls: "", sym: evt.symbol, meta: `from ${evt.pool} candidates`, text: evt.reasoning };
      if (evt.kind === "manage") {
        return evt.action === "sell"
          ? { tag: "EXIT", cls: "sell-down", sym: evt.symbol, meta: "AI exit", text: evt.reasoning }
          : evt.action === "trim" ? { tag: "TRIM", cls: "sell-up", sym: evt.symbol, meta: `AI trim ${Math.round((evt.fraction || 0) * 100)}%`, text: evt.reasoning }
          : { tag: "MANAGE", cls: "", noise: !evt.moved, sym: evt.symbol,
              meta: `stop ${evt.stop > 0 ? "−" : "+"}${Math.abs(n(evt.stop))}% · target +${n(evt.target)}%${evt.moved ? " · moved" : ""}`, text: evt.reasoning };
      }
      if (evt.skip_reason) return { tag: "ERROR", cls: "err", sym: evt.symbol, text: evt.skip_reason };
      return {
        tag: evt.action === "buy" ? "BUY" : "SKIP", cls: evt.action === "buy" ? "buy" : "", sym: evt.symbol,
        meta: [conf, evt.hype != null ? `hype ${evt.hype}/10` : ""].filter(Boolean).join(" · "), text: evt.reasoning,
      };
    case "scaled_out": return {
      tag: "TRIM", cls: "sell-up", sym: evt.symbol,
      meta: `sold ${evt.sold_pct}% for ${usd(evt.usd)} at ${pct(evt.gain_pct)} · ${evt.left_pct}% left`, text: evt.reason };
    case "decision_blocked": return { tag: "BLOCK", cls: "block", sym: evt.symbol, text: `Risk engine: ${evt.reason}` };
    case "trade_opened": return { tag: "OPEN", cls: "buy", sym: evt.symbol, meta: `${usd(evt.usd)} @ ${price(evt.price)}${evt.explore ? " · learning trade" : ""}${evt.tx === "DRYRUN" ? " · dry run" : ""}`, text: "" };
    case "trade_closed": return { tag: "CLOSE", cls: evt.pnl_usd >= 0 ? "sell-up" : "sell-down", sym: evt.symbol, meta: `${usd(evt.pnl_usd, true)} · ${pct(evt.pnl_pct)}`, text: evt.reason };
    case "lesson": return {
      tag: "LESSON", cls: "lesson", sym: evt.symbol,
      meta: [pct(evt.pnl_pct), evt.concept_label ? `${evt.new_concept ? "new concept" : "concept"}: ${evt.concept_label}` : "",
             evt.links?.length ? `${evt.links.length} synapse${evt.links.length > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · "),
      text: evt.lesson };
    case "strategy": return {
      tag: "TUNE", cls: "block", sym: "", meta: `${Object.keys(evt.changes || {}).length} change(s)${evt.pause_minutes ? ` · pause ${evt.pause_minutes}m` : ""}`,
      text: evt.reasoning };
    case "error": return { tag: "ERROR", cls: "err", sym: "", text: evt.msg };
    case "log": return { tag: evt.level === "warn" ? "NOTE" : "SYS", cls: evt.level === "warn" ? "block" : "", noise: evt.level !== "warn", sym: "", text: evt.msg };
    case "mode": return { tag: "MODE", cls: "block", sym: "", text: `Trading mode set to ${String(evt.mode).toUpperCase()}` };
    case "killed": return { tag: "STOP", cls: "err", sym: "", text: `Emergency stop. Closed: ${evt.closed.join(", ") || "none"}${evt.failed.length ? ` · failed: ${evt.failed.join("; ")}` : ""}` };
    default: return null;
  }
}
function addFeed(evt) {
  const e = feedEntry(evt);
  if (!e) return;
  $("#feedEmpty")?.remove();
  const el = document.createElement("div");
  el.className = "feed-item" + (e.noise ? " noise" : "");
  el.innerHTML = `<span class="t">${esc(clock(evt.ts))}</span><span class="tag ${e.cls}">${e.tag}</span>
    <div class="msg"><div class="head">${e.sym ? `<span class="sym">${esc(e.sym)}</span>` : ""}${e.meta ? `<span class="meta">${esc(e.meta)}</span>` : ""}</div>
    ${e.text ? `<div class="text">${esc(e.text)}</div>` : ""}</div>`;
  if ((e.text || "").length > 110) {
    el.dataset.long = "1";
    el.onclick = () => el.classList.toggle("expanded");
  }
  const body = $("#feedBody");
  body.prepend(el);
  while (body.children.length > 400) body.lastChild.remove();
}
$("#hideNoise").onchange = (e) => $("#feed").classList.toggle("hide-noise", e.target.checked);
$("#feed").classList.add("hide-noise");

function onEvent(evt) {
  addFeed(evt);
  if (evt.replay) return;
  switch (evt.type) {
    case "trade_opened":
      toast(`Bought ${evt.symbol}`, `${usd(evt.usd)} at ${price(evt.price)}${evt.tx === "DRYRUN" ? " (dry run)" : ""}`, "accent");
      refreshAll();
      break;
    case "scaled_out":
      toast(`Took profit on ${evt.symbol}`, `sold ${evt.sold_pct}% at ${pct(evt.gain_pct)} · ${evt.left_pct}% still running`, "up");
      refreshAll();
      break;
    case "trade_closed":
      toast(`Closed ${evt.symbol} ${usd(evt.pnl_usd, true)}`, `${pct(evt.pnl_pct)} · ${evt.reason}`, evt.pnl_usd >= 0 ? "up" : "down");
      refreshAll();
      attempt(loadTrades);
      break;
    case "strategy":
      toast("AI strategy review", `${Object.entries(evt.changes || {}).map(([k, v]) => `${k}: ${fmtVal(v[0])} → ${fmtVal(v[1])}`).join(", ") || "no changes"}${evt.pause_minutes ? ` · paused ${evt.pause_minutes} min` : ""}`, "accent", 10000);
      if (!$("#view-brain").classList.contains("hidden")) attempt(loadStrategy);
      refreshAll();
      break;
    case "lesson":
      window.Brain3D?.learn(evt);
      if (!$("#view-brain").classList.contains("hidden")) attempt(loadBrain);
      else toast(`Lesson learned · ${evt.symbol} ${pct(evt.pnl_pct)}`, evt.lesson, "accent", 8000);
      break;
    case "error":
      $("#sbErr").textContent = `⚠ ${evt.msg}`;
      if (/^(BUY|KILL|failed to close)/.test(evt.msg)) toast("Execution error", evt.msg, "down", 10000);
      break;
    case "reset":
      window.Brain3D?.refresh(true);
      refreshAll();
      break;
    case "equity": case "mode": case "killed":
      refreshAll();
      break;
  }
}
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("#sbConn").textContent = "connected"; };
  es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (err) { console.error(err); } };
  es.onerror = () => { $("#sbConn").textContent = "reconnecting…"; };
}

/* ------------------------------------------------------------------ brain */
let eqChart = null, pnlChart = null;
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function chartDefaults() {
  if (!window.Chart) return;
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.borderColor = cssVar("--line");
  Chart.defaults.font.family = cssVar("--mono");
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation = false;
}

async function loadBrain() {
  const q = encodeURIComponent($("#lessonQ").value.trim());
  const mode = STATUS?.mode === "live" ? "live" : "paper";
  const cq = activeConcept ? `&concept=${activeConcept.id}` : "";
  const b = await api(`/api/brain?q=${activeConcept ? "" : q}&sort=${$("#lessonSort").value}&mode=${mode}${cq}`);
  $("#lessonFilter").innerHTML = activeConcept ? `<span class="filter-chip" title="Clear">concept: ${esc(activeConcept.label)} ×</span>` : "";
  if (q || activeConcept) {
    window.Brain3D?.highlight(new Set(b.lessons.map((l) => l.id)));
  } else {
    window.Brain3D?.highlight(null);
  }

  const o = b.overall.all;
  const stat = (label, value, cls = "") => `<div><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`;
  $("#brainStats").innerHTML = o.trades
    ? stat("Trades", o.trades) + stat("Win rate", (o.win_rate * 100).toFixed(1) + "%") + stat("Avg win", pct(o.avg_win_pct), "up")
      + stat("Avg loss", pct(o.avg_loss_pct), "down") + stat("Expectancy", pct(o.expectancy_pct), tone(o.expectancy_pct))
      + stat("Total", usd(o.total_pnl_usd, true), tone(o.total_pnl_usd))
    : stat("Trades", 0) + stat("Win rate", "—") + stat("Avg win", "—") + stat("Avg loss", "—") + stat("Expectancy", "—") + stat("Total", "—");

  $("#lessonCount").textContent = b.lessons.length ? String(b.lessons.length) : "";
  $("#lessonList").innerHTML = b.lessons.map((l) => `
    <div class="lesson" data-lesson-id="${Number(l.id)}">
      <div class="top"><span class="sym">${esc(l.symbol)}</span><span class="${tone(l.pnl_pct)}">${pct(l.pnl_pct)}</span>
        <span>${esc(shortTs(l.ts))}</span><span>cited ${Number(l.cited)}×</span><span>trade #${Number(l.trade_id)}</span><span>${esc(l.brain || "")}</span></div>
      <div class="body">${esc(l.lesson)}</div>
      ${l.what_worked ? `<div class="ww"><span class="up">Worked:</span> ${esc(l.what_worked)}</div>` : ""}
      ${l.what_failed ? `<div class="ww"><span class="down">Failed:</span> ${esc(l.what_failed)}</div>` : ""}
      <div class="chips">${(l.buckets || []).map((t) => `<span class="chip bucket">${esc(t)}</span>`).join("")}${(l.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>
    </div>`).join("") || `<div class="empty">${q ? "No lessons match" : "Lessons appear after the first closed trade"}</div>`;

  $("#statsEmpty").classList.toggle("hidden", b.stats.length > 0);
  $("#statsBody").innerHTML = b.stats.map((s) => `<tr>
      <td class="mono">${esc(s.bucket)}</td><td class="num">${s.n}</td><td class="num">${s.wins} / ${s.losses}</td>
      <td><div style="display:flex;align-items:center;gap:8px"><div class="bar"><span style="width:${(s.win_rate * 100).toFixed(0)}%"></span></div><span class="mono muted">${(s.win_rate * 100).toFixed(0)}%</span></div></td>
      <td class="num ${tone(s.avg_pnl_pct)}">${pct(s.avg_pnl_pct)}</td></tr>`).join("");

  if (!window.Chart) return;
  $("#eqMode").textContent = mode;
  const accent = cssVar("--accent");
  const eq = b.equity;
  if (!eqChart) {
    eqChart = new Chart($("#eqChart"), {
      type: "line",
      data: { labels: [], datasets: [{ data: [], borderColor: accent, backgroundColor: accent + "22", fill: true, pointRadius: 0, borderWidth: 1.5, tension: 0.15 }] },
      options: { scales: { x: { ticks: { maxTicksLimit: 6 }, grid: { display: false } }, y: { ticks: { callback: (v) => "$" + v } } } },
    });
  }
  eqChart.data.labels = eq.map((e) => shortTs(e.ts));
  eqChart.data.datasets[0].data = eq.map((e) => e.equity_usd);
  eqChart.update();

  const trades = (await api("/api/trades?limit=100")).reverse();
  const up = cssVar("--up"), down = cssVar("--down");
  if (!pnlChart) {
    pnlChart = new Chart($("#pnlChart"), {
      type: "bar",
      data: { labels: [], datasets: [{ data: [], backgroundColor: [] }] },
      options: { scales: { x: { ticks: { maxTicksLimit: 10 }, grid: { display: false } }, y: { ticks: { callback: (v) => v + "%" } } } },
    });
  }
  pnlChart.data.labels = trades.map((t) => t.symbol);
  pnlChart.data.datasets[0].data = trades.map((t) => t.pnl_pct);
  pnlChart.data.datasets[0].backgroundColor = trades.map((t) => (t.pnl_pct >= 0 ? up : down));
  pnlChart.update();
}
addEventListener("brain:concept", (e) => {
  activeConcept = e.detail;
  $("#lessonQ").value = "";
  attempt(loadBrain);
});
$("#lessonFilter").onclick = () => { activeConcept = null; attempt(loadBrain); };
addEventListener("brain:lesson", (e) => {
  const el = $(`.lesson[data-lesson-id="${Number(e.detail)}"]`);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth; // restart the animation
  el.classList.add("flash");
});
let searchTimer = null;
$("#lessonQ").addEventListener("input", () => {
  activeConcept = null; clearTimeout(searchTimer); searchTimer = setTimeout(() => attempt(loadBrain), 250); });
$("#lessonSort").onchange = () => attempt(loadBrain);
$("#askGo").onclick = () => attempt(async () => {
  const q = $("#askQ").value.trim();
  if (!q) return;
  const a = $("#askA");
  a.classList.remove("hidden");
  a.textContent = "Thinking…";
  $("#askGo").disabled = true;
  try {
    a.textContent = (await api("/api/brain/ask", { method: "POST", body: { question: q } })).answer;
  } catch (e) {
    a.textContent = `Error: ${e.message}`;
  } finally {
    $("#askGo").disabled = false;
  }
});

/* ------------------------------------------------------------------ AI strategist */
function journalItem(j, fresh = false) {
  const changes = Object.entries(j.changes || {});
  return `<div class="journal-item${fresh ? " fresh" : ""}">
    <div class="top"><span>${esc(shortTs(j.ts))}</span><span>${j.trades_reviewed ?? 0} trades reviewed</span><span>${esc(j.model || "")}</span>
      ${j.pause_minutes ? `<span class="pause">paused ${Math.round(j.pause_minutes)} min</span>` : ""}</div>
    <div class="chg">${changes.length ? changes.map(([k, v]) => `<span>${esc(k)} ${esc(fmtVal(v[0]))} → <b>${esc(fmtVal(v[1]))}</b></span>`).join("") : "<span>no changes</span>"}</div>
    <div class="why">${esc(j.reasoning || "")}</div>
    ${(j.rejected || []).length ? `<div class="rej">Not applied: ${esc(j.rejected.join("; "))}</div>` : ""}
  </div>`;
}
let playbookLoaded = false;
function hostOf(u) {
  try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; }
}
async function loadPlaybook() {
  if (playbookLoaded) return;
  const pb = await api("/api/playbook");
  $("#pbCount").textContent = `${pb.count} rules · base knowledge the trader starts with`;
  $("#playbook").innerHTML = pb.categories.map((c) => `
    <details class="pb-cat"><summary>${esc(c.label)} <span class="muted">· ${c.rules.length}</span></summary>
    ${c.rules.map((r) => `<div class="pb-rule"><div>${esc(r.rule)} <span class="pb-conf ${esc(r.confidence)}">${esc(r.confidence || "")}</span></div>
      ${r.rationale ? `<div class="muted">${esc(r.rationale)}</div>` : ""}
      ${(r.sources || []).length ? `<div class="pb-src">${r.sources.slice(0, 3).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(hostOf(u))}</a>`).join(" · ")}</div>` : ""}
    </div>`).join("")}</details>`).join("") || '<div class="empty">No playbook loaded.</div>';
  playbookLoaded = true;
}
async function loadStrategy() {
  const r = await api("/api/strategy?limit=30");
  const a = r.autonomy || {};
  const bits = [a.autonomous ? "autonomous" : "rule-based"];
  if (a.paused_until) bits.push(`entries paused until ${new Date(a.paused_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
  else if (a.autonomous) bits.push(`next review in ${a.trades_until_review} trade${a.trades_until_review === 1 ? "" : "s"}`);
  $("#stratState").textContent = bits.join(" · ");
  $("#stratResume").classList.toggle("hidden", !a.paused_until);
  $("#stratJournal").innerHTML = r.journal.length ? r.journal.map((j) => journalItem(j)).join("")
    : '<div class="empty">No strategy reviews yet. The AI reviews itself every few closed trades.</div>';
}
$("#stratReview").onclick = () => attempt(async () => {
  $("#stratReview").disabled = true;
  $("#stratReview").textContent = "Reviewing…";
  try {
    await api("/api/strategy/review", { method: "POST" });
    await loadStrategy();
  } finally {
    $("#stratReview").disabled = false;
    $("#stratReview").textContent = "Review now";
  }
});
$("#stratResume").onclick = () => attempt(async () => { await api("/api/strategy/resume", { method: "POST" }); await loadStrategy(); await refreshStatus(); });

/* ------------------------------------------------------------------ settings */
const SETTING_GROUPS = [
  ["Autonomy", [
    ["autonomous", "Autonomous mode", "The AI screens tokens, manages open positions and rewrites the settings marked AI below."],
    ["position_review_every_ticks", "Review positions every N ticks", "How often the AI reviews all open positions (one Groq call per review)."],
    ["strategy_review_every_trades", "Strategy review every N trades", "The AI re-tunes its strategy after this many closed trades."],
    ["strategy_review_max_hours", "…or at least every N hours", "If trades are slow, review anyway after this long (needs at least one new trade)."],
  ]],
  ["Hard backstops (the AI cannot change these)", [
    ["hard_stop_loss_pct", "Hard stop loss (%)", "Always sells at this loss, whatever the AI decided."],
    ["hard_max_hold_hours", "Hard max hold (hours)", "Always sells after this long."],
    ["daily_loss_cap_usd", "Daily loss cap (USD)", "No new entries after this realized loss (UTC day). Required for live."],
  ]],
  ["Scanning", [
    ["scan_interval_sec", "Scan interval", "Seconds between engine ticks."],
    ["max_candidates_per_tick", "Candidates per tick", "Tokens sent to the model each tick, ranked by 1h volume."],
    ["decision_cooldown_min", "Re-evaluation cooldown", "Minutes before the same token is considered again."],
    ["include_watchlist", "Include watchlist", "Also consider the majors below, not just trending tokens."],
    ["watchlist", "Watchlist mints", "Comma-separated Solana mint addresses.", "wide"],
  ]],
  ["Entry filters", [
    ["min_liquidity_usd", "Min liquidity (USD)", "Pool liquidity required before the model sees a token."],
    ["min_volume_h24_usd", "Min 24h volume (USD)", ""],
    ["min_age_min", "Min pair age (min)", "Skips brand-new pairs, where most rugs happen."],
    ["max_rug_score", "Max rug score", "RugCheck normalised score, 0 = clean, 100 = worst."],
    ["max_h1_change_pct", "Skip if up more than (% in 1h)", "Don't chase vertical candles."],
    ["min_h1_change_pct", "Skip if down more than (% in 1h)", "Don't catch falling knives."],
    ["max_entries_per_day", "Max new trades per day", "Overtrading bleeds fees."],
    ["loss_streak_pause", "Pause after N losses in a row", "No revenge trading. 0 disables."],
    ["loss_streak_pause_min", "…for this many minutes", ""],
  ]],
  ["Sizing & exits", [
    ["min_confidence", "Min confidence", "0–1. Buys below this are rejected."],
    ["explore_in_paper", "Exploration trades (paper only)", "Let the AI take small learning trades on untested setups below the confidence bar."],
    ["explore_size_pct", "Exploration size (% equity)", "Cap on each learning trade."],
    ["explore_min_confidence", "Exploration min confidence", "Even learning trades need at least this much conviction."],
    ["max_position_pct", "Max position (% equity)", ""],
    ["max_open_positions", "Max open positions", ""],
    ["max_liquidity_share_pct", "Max % of pool liquidity", "Caps size so you are never the exit liquidity."],
    ["default_stop_loss_pct", "Default stop loss (%)", "Used when the model omits one. Clamped to 3–50%."],
    ["default_take_profit_pct", "Default take profit (%)", "Clamped to 5–500%."],
    ["scale_out_1_pct", "Take profit 1 at (+%)", "Sell a first tranche here (research: +40-100%)."],
    ["scale_out_1_frac", "…sell this fraction", "Of the original position (0.4 = 40%). After it, the stop locks in profit."],
    ["scale_out_2_pct", "Take profit 2 at (+%)", ""],
    ["scale_out_2_frac", "…sell this fraction", "Of the original position; the rest is the trailed runner."],
    ["scale_out_stop_lock_pct", "Profit locked after TP1 (%)", "The stop moves to entry plus this, so the trade can't close as a loss."],
    ["breakeven_after_pct", "Round-trip guard after (+%)", "Once a trade has been up this much…"],
    ["breakeven_lock_pct", "…exit if it falls back to (+%)", "…sell before it gives it all back."],
    ["profit_floor_after_pct", "Profit floor after (+%)", "Once the peak gain reaches this…"],
    ["profit_floor_keep", "…keep this share of the peak", "0.5 = never give back more than half the best gain."],
    ["trailing_stop_pct", "Trailing stop (%)", "Distance below the highest price. 0 disables."],
    ["trailing_activation_pct", "Trailing activates at (%)", "Gain from entry before the trail arms."],
    ["time_stop_min", "Time stop, young tokens (min)", "Exit if the trade isn't up enough by then."],
    ["time_stop_mature_hours", "Time stop, tokens > 7 days old (h)", "Established tokens get longer to work."],
    ["time_stop_min_gain_pct", "…required gain (%)", ""],
    ["liquidity_drain_exit_pct", "Liquidity-pull exit (%)", "Sell if the pool loses this much more liquidity than the price move explains."],
    ["exit_check_sec", "Price check interval (s)", "Stops are checked this often between AI reviews (free, no Groq)."],
    ["max_hold_min", "Max hold (min)", ""],
    ["groq_bail_check", "Model exit checks", "Ask the model every third tick whether to exit early."],
  ]],
  ["Paper fill model", [
    ["paper_base_slippage_pct", "Base slippage (%)", "Added to size-based price impact."],
    ["paper_fee_pct", "Swap fee (%)", ""],
    ["paper_network_fee_usd", "Network fee (USD)", "Per swap."],
  ]],
  ["Live limits", [
    ["live_max_sol_per_trade", "Max SOL per trade", "Required for live."],
    ["live_max_exposure_usd", "Max total exposure (USD)", "Required for live."],
    ["live_slippage_bps", "Swap slippage (bps)", "Passed to Jupiter. 100 bps = 1%."],
  ]],
];

async function loadSettings() {
  SETTINGS = await api("/api/settings");
  const tunable = new Set((await api("/api/strategy?limit=1")).tunable);
  $("#settingsForm").innerHTML = SETTING_GROUPS.map(([group, fields]) => `
    <section class="panel"><div class="panel-head"><h2>${group}</h2></div>
    ${fields.map(([k, label, hint, wide]) => {
      const v = SETTINGS[k];
      const id = `set-${k}`;
      let input;
      if (typeof v === "boolean") input = `<input type="checkbox" class="check" id="${id}" data-k="${k}" ${v ? "checked" : ""}>`;
      else if (Array.isArray(v)) input = `<input type="text" id="${id}" data-k="${k}" data-list="1" value="${esc(v.join(", "))}" spellcheck="false">`;
      else input = `<input type="number" step="any" id="${id}" data-k="${k}" value="${esc(v)}">`;
      const badge = SETTINGS.autonomous && tunable.has(k) ? '<span class="ai-badge" title="The AI tunes this itself in autonomous mode">AI</span>' : "";
      return `<div class="field ${wide || ""}"><label for="${id}">${label}${badge}</label>${input}${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
    }).join("")}</section>`).join("");
  $$("#settingsForm [data-k]").forEach((i) => i.addEventListener("input", () => { settingsDirty = true; $("#settingsNote").textContent = "Unsaved changes"; }));
  settingsDirty = false;
  $("#settingsNote").textContent = "";
  if (STATUS?.paper.starting_credit) $("#paperCredit").value = STATUS.paper.starting_credit;
}
$("#settingsRevert").onclick = () => attempt(loadSettings);
$("#settingsSave").onclick = () => attempt(async () => {
  const patch = {};
  $$("#settingsForm [data-k]").forEach((i) => {
    patch[i.dataset.k] = i.type === "checkbox" ? i.checked
      : i.dataset.list ? i.value.split(",").map((x) => x.trim()).filter(Boolean) : Number(i.value);
  });
  SETTINGS = await api("/api/settings", { method: "POST", body: patch });
  settingsDirty = false;
  $("#settingsNote").textContent = "Saved · applies on the next tick";
  await refreshStatus();
});
$("#paperReset").onclick = () => attempt(async () => {
  const credit = Number($("#paperCredit").value);
  if (!(credit > 0)) return toast("Invalid balance", "Enter a starting balance above zero.", "down");
  const wipe = $("#wipeBrain").checked;
  const ok = await modal({
    title: "Reset paper account?",
    body: `Balance resets to <b>${usd(credit)}</b> and open paper positions are discarded.${wipe ? "<br><br><b>All lessons and bucket stats will be deleted.</b>" : " Lessons are kept."}`,
    ok: "Reset", danger: wipe,
  });
  if (!ok) return;
  await api("/api/paper/reset", { method: "POST", body: { starting_credit: credit, wipe_brain: wipe } });
  toast("Paper account reset", usd(credit), "accent");
  await refreshAll();
});

/* ------------------------------------------------------------------ live */
function renderLive() {
  if (!STATUS) return;
  const s = STATUS, r = s.live.readiness, live = s.mode === "live";
  const pill = $("#liveModePill");
  pill.textContent = live ? "LIVE" : "PAPER";
  pill.className = "pill" + (live ? " live" : "");
  $("#liveModeDesc").innerHTML = live
    ? `<b>Trading real funds</b> via Jupiter, within the limits set in Settings.`
    : `<b>Paper trading.</b> Live trading unlocks when every pre-flight check passes.`;
  $("#btnGoLive").classList.toggle("hidden", live);
  $("#btnGoLive").disabled = !r.ready;
  $("#btnGoPaper").classList.toggle("hidden", !live);
  const dry = $("#dryRunNote");
  dry.className = "notice " + (r.dry_run ? "warn" : "bad");
  dry.textContent = r.dry_run
    ? "LIVE_DRY_RUN is on: swaps are built, signed and simulated over RPC, never sent."
    : "LIVE_DRY_RUN is off: live mode sends real transactions.";

  const checks = [
    ["Groq API key", !r.reasons.some((x) => x.startsWith("GROQ"))],
    ["Wallet private key", !r.reasons.some((x) => x.startsWith("WALLET"))],
    ["Solana RPC URL", !r.reasons.some((x) => x.startsWith("SOLANA_RPC"))],
    ["Signing library (solders)", !r.reasons.some((x) => x.startsWith("solders"))],
    ["Max SOL per trade", !r.reasons.some((x) => x.includes("live_max_sol_per_trade"))],
    ["Max total exposure", !r.reasons.some((x) => x.includes("live_max_exposure_usd"))],
    ["Daily loss cap", !r.reasons.some((x) => x.includes("daily_loss_cap_usd"))],
  ];
  $("#liveChecks").innerHTML = checks.map(([label, ok]) =>
    `<li><span class="${ok ? "ok" : "no"}">${ok ? "✓" : "✕"}</span><span>${label}</span></li>`).join("");

  const p = s.paper.summary || {};
  const cell = (label, value, cls = "") => `<div><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`;
  $("#paperRecord").innerHTML = p.trades
    ? cell("Trades", p.trades) + cell("Win rate", (p.win_rate * 100).toFixed(1) + "%")
      + cell("Expectancy", pct(p.expectancy_pct), tone(p.expectancy_pct)) + cell("Total", usd(p.total_pnl_usd, true), tone(p.total_pnl_usd))
    : cell("Trades", 0) + cell("Win rate", "—") + cell("Expectancy", "—") + cell("Total", "—");
  const warn = $("#paperWarn");
  const msg = !p.trades ? "No paper trades yet. Run paper mode first."
    : p.trades < 20 ? `Only ${p.trades} paper trades. That is too few to judge the strategy.`
    : p.expectancy_pct <= 0 ? "Paper expectancy is negative. Live trading would be expected to lose money." : "";
  warn.textContent = msg;
  warn.className = "notice warn" + (msg ? "" : " hidden");

  $("#liveWallet").textContent = s.live.wallet
    ? `${s.live.wallet}  ·  equity ${usd(s.live.equity)}  ·  ${s.live.open} open`
    : s.live.error || "Not connected (live mode off)";
}
$("#btnGoLive").onclick = () => attempt(async () => {
  const s = SETTINGS || (await api("/api/settings"));
  const phrase = await modal({
    title: "Enable live trading",
    body: `The engine will trade <b>real funds</b> from the configured wallet.<br><br>
      Max per trade: <b>${esc(s.live_max_sol_per_trade)} SOL</b><br>
      Max exposure: <b>${usd(s.live_max_exposure_usd)}</b><br>
      Daily loss cap: <b>${usd(s.daily_loss_cap_usd)}</b><br>
      ${STATUS.dry_run ? "<br>Dry run is on, so swaps will only be simulated." : ""}<br>Type <b>GO LIVE</b> to confirm.`,
    ok: "Enable live trading", danger: true, input: { placeholder: "GO LIVE", match: "GO LIVE" },
  });
  if (!phrase) return;
  await api("/api/mode", { method: "POST", body: { mode: "live", confirm: phrase } });
  toast("Live trading enabled", STATUS.dry_run ? "Dry run: transactions are simulated." : "Real transactions.", "down", 8000);
  await refreshAll();
});
$("#btnGoPaper").onclick = () => attempt(async () => {
  const ok = await modal({ title: "Return to paper trading?", body: "New entries will be paper trades. Open live positions stay open and keep being managed while the engine runs.", ok: "Return to paper" });
  if (!ok) return;
  await api("/api/mode", { method: "POST", body: { mode: "paper" } });
  await refreshAll();
});
$("#killBtn").onclick = () => attempt(async () => {
  const liquidate = $("#killLiquidate").checked;
  const ok = await modal({
    title: "Stop everything?",
    body: `The engine stops immediately.${liquidate ? " <b>Every open position (paper and live) is sold at market.</b>" : " Open positions are left as they are."}`,
    ok: liquidate ? "Stop and sell all" : "Stop engine", danger: true,
  });
  if (!ok) return;
  const r = await api("/api/kill", { method: "POST", body: { liquidate } });
  toast("Engine stopped", liquidate ? `Closed: ${r.closed.join(", ") || "none"}${r.failed.length ? ` · failed: ${r.failed.join("; ")}` : ""}` : "", r.failed.length ? "down" : "accent", 10000);
  await refreshAll();
});

/* ------------------------------------------------------------------ first run */
async function firstRun() {
  const credit = await modal({
    title: "Fund the paper account",
    body: "VibeTrader starts in paper mode, trading live market prices with simulated money. Set the starting balance.",
    ok: "Start paper account", input: { type: "number", value: "1000", placeholder: "1000" },
  });
  if (!credit || !(Number(credit) > 0)) return;
  await attempt(() => api("/api/paper/reset", { method: "POST", body: { starting_credit: Number(credit), wipe_brain: false } }));
  toast("Paper account funded", `${usd(credit)} · press Start to begin`, "accent");
  await refreshAll();
}

/* ------------------------------------------------------------------ boot */
async function refreshAll() {
  await attempt(async () => { await refreshStatus(); await refreshPositions(); });
}

(async function boot() {
  chartDefaults();
  connect();
  const s = await attempt(refreshStatus);
  await attempt(refreshPositions);
  showView();
  if (s && !s.paper.initialized) firstRun();
  else if (s && !s.groq_enabled) toast("No Groq API key", "Using the rule-based heuristic. Add GROQ_API_KEY to .env for model decisions.", "", 9000);
  setInterval(refreshAll, 5000);
})();
