/* VibeTrader 95 frontend. Vibe coded. No framework. No regrets. (Some regrets.) */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const usd = (v, sign = false) => {
  const n = Number(v || 0);
  const s = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (n < 0 ? "-" : sign ? "+" : "") + "$" + s;
};
const pct = (v) => (Number(v || 0) >= 0 ? "+" : "") + Number(v || 0).toFixed(2) + "%";
const cls = (v) => (Number(v || 0) >= 0 ? "win" : "loss");
const px = (v) => {
  const n = Number(v || 0);
  if (!n) return "0";
  if (n >= 1) return n.toFixed(4);
  const zeros = Math.max(0, -Math.floor(Math.log10(n)) - 1);
  return n.toFixed(Math.min(zeros + 4, 14));
};
const ago = (iso) => {
  if (!iso) return "-";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  return m < 60 ? `${m.toFixed(0)}m` : `${(m / 60).toFixed(1)}h`;
};
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

let STATUS = null;
let SETTINGS = null;
let zTop = 100;

/* ------------------------------------------------------------------ api */
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
    const msg = Array.isArray(d) ? d.map((x) => `${(x.loc || []).join(".")}: ${x.msg}`).join("\n") : d || res.statusText;
    throw new Error(msg);
  }
  return data;
}
async function safe(fn) {
  try { return await fn(); } catch (e) { errorBox(e.message); }
}

/* ------------------------------------------------------------------ windows */
function focusWin(w) {
  $$(".window").forEach((x) => x.classList.add("inactive"));
  w.classList.remove("inactive");
  w.style.zIndex = ++zTop;
  renderTasks();
}
function openWin(id) {
  const w = document.getElementById(id);
  if (!w) return;
  w.classList.remove("hidden");
  focusWin(w);
  if (id === "w-brain") safe(loadBrain);
  if (id === "w-trades") safe(loadTrades);
  if (id === "w-settings") safe(loadSettings);
  if (id === "w-live") renderLive();
}
function closeWin(w) { w.classList.add("hidden"); renderTasks(); }

function makeDraggable(w) {
  const bar = $(".titlebar", w);
  bar.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".tb")) return;
    focusWin(w);
    const sx = e.clientX, sy = e.clientY, ox = w.offsetLeft, oy = w.offsetTop;
    bar.setPointerCapture(e.pointerId);
    const move = (ev) => {
      w.style.left = Math.max(-w.offsetWidth + 80, ox + ev.clientX - sx) + "px";
      w.style.top = Math.max(0, oy + ev.clientY - sy) + "px";
    };
    const up = () => { bar.removeEventListener("pointermove", move); bar.removeEventListener("pointerup", up); };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
  });
  w.addEventListener("pointerdown", () => focusWin(w));
}
function decorate(w) {
  const bar = $(".titlebar", w);
  const mk = (txt, fn, title) => {
    const b = document.createElement("button");
    b.className = "btn tb"; b.textContent = txt; b.title = title; b.onclick = fn; bar.appendChild(b);
  };
  mk("_", () => closeWin(w), "Minimize");
  mk("□", () => {
    if (w.dataset.max) {
      Object.assign(w.style, JSON.parse(w.dataset.max)); delete w.dataset.max;
    } else {
      w.dataset.max = JSON.stringify({ left: w.style.left, top: w.style.top, width: w.style.width, height: w.style.height });
      Object.assign(w.style, { left: "0px", top: "0px", width: "100%", height: "100%" });
    }
  }, "Maximize");
  mk("×", () => closeWin(w), "Close");
  makeDraggable(w);
}
function renderTasks() {
  const t = $("#tasks");
  t.innerHTML = "";
  $$(".window").filter((w) => !w.classList.contains("hidden")).forEach((w) => {
    const b = document.createElement("button");
    b.className = "btn" + (w.classList.contains("inactive") ? "" : " pressed");
    b.textContent = $(".titlebar .t", w).textContent;
    b.onclick = () => focusWin(w);
    t.appendChild(b);
  });
}

/* ------------------------------------------------------------------ dialogs */
let dialogCount = 0;
function dialog({ title = "VibeTrader", icon = "ℹ️", html = "", buttons = [{ label: "OK", value: true }], modal = false,
  input = null, autoClose = 0, cursed = false }) {
  return new Promise((resolve) => {
    const shade = modal ? document.createElement("div") : null;
    if (shade) { shade.className = "modal-shade"; document.body.appendChild(shade); }
    const d = document.createElement("div");
    d.className = "dialog raised" + (cursed ? " cursed" : "") + (modal ? " keep" : "");
    const n = dialogCount++ % 6;
    d.style.left = Math.max(8, (innerWidth - 380) / 2 + (modal ? 0 : n * 26 - 60)) + "px";
    d.style.top = Math.max(30, innerHeight / 2 - 140 + (modal ? 0 : n * 24 - 60)) + "px";
    d.innerHTML = `<div class="titlebar"><span class="t">${esc(title)}</span><button class="btn tb" data-x>×</button></div>
      <div class="dbody"><div class="dicon">${icon}</div><div style="flex:1">${html}
      ${input ? `<div style="margin-top:8px"><input type="${input.type || "text"}" style="width:100%" value="${esc(input.value || "")}" placeholder="${esc(input.placeholder || "")}"></div>` : ""}
      </div></div><div class="dactions"></div>`;
    const inp = $("input", d);
    const done = (v) => { d.remove(); shade && shade.remove(); resolve(v); };
    buttons.forEach((b) => {
      const btn = document.createElement("button");
      btn.className = "btn " + (b.cls || "");
      btn.textContent = b.label;
      btn.onclick = () => done(inp && b.value === true ? inp.value : b.value);
      $(".dactions", d).appendChild(btn);
    });
    $("[data-x]", d).onclick = () => done(null);
    if (inp) inp.addEventListener("keydown", (e) => { if (e.key === "Enter") done(inp.value); });
    document.body.appendChild(d);
    d.style.zIndex = 9700 + dialogCount;
    if (inp) inp.focus();
    if (autoClose) setTimeout(() => d.isConnected && done(null), autoClose);
    // cap the popup storm
    const popups = $$(".dialog:not(.keep)");
    if (popups.length > 4) popups[0].remove();
  });
}
const errorBox = (msg) => dialog({ title: "Error", icon: "❌", html: `<b>This program has performed an illegal operation.</b><br><br>${esc(msg).replace(/\n/g, "<br>")}` });

/* ------------------------------------------------------------------ clippy + bsod */
let clippyTimer = null;
function clippy(text, ms = 9000) {
  const b = $("#clippyBubble");
  b.textContent = text;
  b.classList.remove("hidden");
  clearTimeout(clippyTimer);
  clippyTimer = setTimeout(() => b.classList.add("hidden"), ms);
}
document.addEventListener("pointermove", (e) => {
  const c = $("#clippy svg").getBoundingClientRect();
  const dx = Math.max(-2, Math.min(2, (e.clientX - c.left - 27) / 80));
  const dy = Math.max(-2, Math.min(2, (e.clientY - c.top - 30) / 80));
  $("#eyeL").setAttribute("cx", 20 + dx); $("#eyeL").setAttribute("cy", 30 + dy);
  $("#eyeR").setAttribute("cx", 38 + dx); $("#eyeR").setAttribute("cy", 30 + dy);
});
function bsod(msg) {
  $("#bsodMsg").textContent = msg;
  $("#bsod").classList.remove("hidden");
  const off = () => { $("#bsod").classList.add("hidden"); removeEventListener("keydown", off); };
  addEventListener("keydown", off);
  $("#bsod").onclick = off;
}

/* ------------------------------------------------------------------ status / portfolio */
async function refreshStatus() {
  const s = await api("/api/status");
  STATUS = s;
  const live = s.mode === "live";
  const eq = live ? s.live.equity : s.paper.equity;
  const cash = live ? s.live.cash : s.paper.cash;
  $("#modeBadge").textContent = live ? (s.dry_run ? "LIVE MODE (DRY RUN)" : "☢ LIVE MODE - REAL MONEY ☢") : "PAPER MODE (fake money, real feelings)";
  $("#modeBadge").style.color = live ? "#ff3030" : "#00ff00";
  $("#eqVal").textContent = usd(eq);
  $("#cashVal").textContent = usd(cash);
  const pnl = live ? (s.live.summary || {}).total_pnl_usd || 0 : s.paper.pnl_usd;
  $("#pnlBig").textContent = usd(pnl, true);
  $("#pnlBig").className = "bigmoney " + cls(pnl);
  $("#pnlPct").textContent = live ? "realized" : pct(s.paper.pnl_pct) + " since start";
  $("#engineState").innerHTML = s.running ? '<b class="win blink">● RUNNING</b>' : '<b class="loss">● STOPPED</b>';
  $("#btnStart").disabled = s.running;
  $("#btnStop").disabled = !s.running;
  $("#stBrain").textContent = "brain: " + s.brain + (s.groq_enabled ? "" : " (no GROQ key!)");
  $("#stTick").textContent = `tick #${s.tick_count} ${s.last_tick ? ago(s.last_tick) + " ago" : ""}`;
  $("#stErr").textContent = s.last_error ? "⚠ " + s.last_error : "errors: none";
  $("#trayMode").textContent = live ? "☢️" : "📄";
  $("#trayMode").title = s.mode;
  if (!$("#w-live").classList.contains("hidden")) renderLive();
  return s;
}

async function refreshPositions() {
  const rows = await api("/api/positions");
  const tb = $("#posBody");
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="8">No bags held. Yet.</td></tr>'; return rows; }
  tb.innerHTML = rows.map((t) => `<tr>
      <td title="${esc(t.mint)}"><b>${esc(t.symbol)}</b></td>
      <td>${t.mode === "live" ? "☢️" : "📄"}</td>
      <td>${usd(t.cost_usd)}</td><td>${usd(t.value_usd)}</td>
      <td class="${cls(t.unrealized_pct)}">${pct(t.unrealized_pct)}</td>
      <td>-${esc(t.stop_loss_pct)}% / +${esc(t.take_profit_pct)}%</td>
      <td>${ago(t.opened_at)}</td>
      <td><button class="btn" style="min-width:0;padding:1px 6px" data-close="${Number(t.id)}" title="Sell now">💸</button></td></tr>`).join("");
  $$("[data-close]", tb).forEach((b) => b.onclick = async () => {
    const ok = await dialog({ title: "Paper hands?", icon: "❓", html: "Sell this bag right now?", buttons: [{ label: "Yes, dump it", value: true }, { label: "No", value: false }] });
    if (ok) safe(async () => { await api(`/api/positions/${b.dataset.close}/close`, { method: "POST" }); refreshAll(); });
  });
  $("#tickerText").textContent = rows.map((t) => `$${t.symbol} ${pct(t.unrealized_pct)}`).join("  ***  ") +
    "  ***  " + pick(["WAGMI", "NGMI", "HODL.EXE IS NOT RESPONDING", "SER THIS IS A CASINO", "BUY HIGH SELL LOW", "GROQ SAYS HI"]) + "  ***  ";
  return rows;
}

async function loadTrades() {
  const rows = await api("/api/trades?limit=200");
  $("#tradesBody").innerHTML = rows.map((t) => `<tr>
    <td>${Number(t.id)}</td><td>${esc((t.closed_at || "").replace("T", " ").slice(5, 16))}</td><td>${t.mode === "live" ? "☢️" : "📄"}</td>
    <td><b>${esc(t.symbol)}</b></td><td>${usd(t.cost_usd)}</td><td class="${cls(t.pnl_usd)}">${usd(t.pnl_usd, true)}</td>
    <td class="${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td><td title="${esc(t.exit_reason)}">${esc((t.exit_reason || "").slice(0, 40))}</td>
    <td>${t.exit_tx && !["PAPER", "DRYRUN"].includes(t.exit_tx) ? `<a href="https://solscan.io/tx/${encodeURIComponent(t.exit_tx)}" target="_blank" rel="noopener">🔗</a>` : esc(t.exit_tx || "")}</td>
  </tr>`).join("") || '<tr><td colspan="9">Nothing closed yet. Diamond hands or no hands?</td></tr>';
}

/* ------------------------------------------------------------------ thoughts feed */
function addThought(evt) {
  const box = $("#thoughts");
  const div = document.createElement("div");
  const t = new Date(evt.ts).toLocaleTimeString();
  let k = "thought", body = "";
  switch (evt.type) {
    case "thought":
      if (evt.skip_reason && evt.brain === "filter") { k += " filtered"; body = `🚫 <b>${esc(evt.symbol)}</b> filtered: ${esc(evt.skip_reason)}`; break; }
      if (evt.kind === "bail") { body = `🤔 <b>${esc(evt.symbol)}</b> bail check → <b>${esc(evt.action)}</b>: ${esc(evt.reasoning)}`; break; }
      if (evt.action === "buy") k += " buy";
      if (evt.skip_reason) k += " err";
      body = `<span class="who">${evt.action === "buy" ? "🟢 APE" : "⚪ SKIP"} ${esc(evt.symbol)}</span>
        ${evt.confidence != null ? `conf ${(evt.confidence * 100).toFixed(0)}%` : ""} ${evt.vibe != null ? `vibe ${"🔥".repeat(Math.round(evt.vibe / 2))}` : ""}
        <br>${esc(evt.reasoning || evt.skip_reason || "")}`;
      break;
    case "decision_blocked": k += " warn"; body = `🛑 risk engine blocked <b>${esc(evt.symbol)}</b>: ${esc(evt.reason)}`; break;
    case "trade_opened": k += " buy"; body = `💸 BOUGHT <b>${esc(evt.symbol)}</b> for ${usd(evt.usd)} @ ${px(evt.price)} ${evt.tx === "DRYRUN" ? "(dry run)" : ""}`; break;
    case "trade_closed": k += evt.pnl_usd >= 0 ? " close-win" : " close-loss";
      body = `${evt.pnl_usd >= 0 ? "🤑" : "💀"} SOLD <b>${esc(evt.symbol)}</b> ${usd(evt.pnl_usd, true)} (${pct(evt.pnl_pct)}) - ${esc(evt.reason)}`; break;
    case "lesson": k += " lesson"; body = `🎓 LESSON from ${esc(evt.symbol)} (${pct(evt.pnl_pct)}): <i>${esc(evt.lesson)}</i>`; break;
    case "error": k += " err"; body = `❌ ${esc(evt.msg)}`; break;
    case "log": k += evt.level === "warn" ? " warn" : ""; body = `💬 ${esc(evt.msg)}`; break;
    case "mode": k += " warn"; body = `☢️ mode is now <b>${esc(String(evt.mode).toUpperCase())}</b>`; break;
    case "killed": k += " err"; body = `⏻ KILL SWITCH. closed: ${esc(evt.closed.join(", ") || "none")} ${evt.failed.length ? "FAILED: " + esc(evt.failed.join("; ")) : ""}`; break;
    default: return;
  }
  div.className = k;
  div.innerHTML = `<span class="muted">[${esc(t)}]</span> ${body}`;
  box.prepend(div);
  while (box.children.length > 300) box.lastChild.remove();
}

function handleEvent(evt) {
  addThought(evt);
  if (evt.replay) return;
  switch (evt.type) {
    case "trade_opened":
      dialog({ title: `${evt.symbol}.EXE`, icon: "🚀", autoClose: 9000,
        html: `<b>${esc(evt.symbol)}.EXE has performed a legal operation</b> and bought ${usd(evt.usd)} of bags.<br><br>
          Vibe score: ${"🔥".repeat(Math.max(1, Math.round((evt.vibe || 5) / 2)))}<br><i>${esc((evt.reasoning || "").slice(0, 220))}</i>` });
      clippy(`It looks like you're buying ${evt.symbol}! Would you like help losing money faster?`);
      refreshAll();
      break;
    case "trade_closed":
      if (evt.pnl_usd >= 0) {
        dialog({ title: "CONGRATULATIONS!!!", icon: "🏆", cursed: true, autoClose: 9000,
          html: `<b class="blink">YOU ARE A WINNER!</b><br>${esc(evt.symbol)} closed ${usd(evt.pnl_usd, true)} (${pct(evt.pnl_pct)})<br><span class="muted">${esc(evt.reason)}</span>` });
        clippy(pick(["Number go up! 📈", "I'd like to thank my GPU.", "Buy the dip, sell the rip, repeat until broke."]));
      } else {
        dialog({ title: "General Protection Fault", icon: "💀", autoClose: 9000,
          html: `${esc(evt.symbol)} caused a general protection fault in module WALLET.DLL.<br><b class="loss">${usd(evt.pnl_usd, true)} (${pct(evt.pnl_pct)})</b><br><span class="muted">${esc(evt.reason)}</span>` });
        if (evt.pnl_pct <= -25) bsod(`${evt.symbol} lost ${pct(evt.pnl_pct)} (${usd(evt.pnl_usd, true)}). Reason: ${evt.reason}`);
        else clippy(pick(["It looks like you lost money. Would you like to write a sad tweet?", "That's not a loss, that's tuition.", "Have you tried turning your portfolio off and on again?"]));
      }
      refreshAll();
      break;
    case "lesson":
      clippy(`I learned something! "${evt.lesson}"`, 14000);
      if (!$("#w-brain").classList.contains("hidden")) safe(loadBrain);
      break;
    case "error": $("#stErr").textContent = "⚠ " + evt.msg; break;
    case "equity": case "mode": case "reset": case "killed": refreshAll(); break;
  }
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (m) => { try { handleEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  es.onerror = () => { $("#stErr").textContent = "⚠ lost connection to the mothership, retrying..."; };
}

$("#hideSkips").onchange = (e) => $("#thoughts").classList.toggle("hide-filtered", e.target.checked);
const hideStyle = document.createElement("style");
hideStyle.textContent = ".hide-filtered .filtered{display:none}";
document.head.appendChild(hideStyle);

/* ------------------------------------------------------------------ brain */
let eqChart = null, pnlChart = null;
async function loadBrain() {
  const q = encodeURIComponent($("#lessonQ").value || "");
  const sort = $("#lessonSort").value;
  const mode = STATUS && STATUS.mode === "live" ? "live" : "paper";
  const b = await api(`/api/brain?q=${q}&sort=${sort}&mode=${mode}`);
  const o = b.overall.all;
  $("#overallBox").innerHTML = o.trades
    ? `TRADES ${o.trades} | W ${o.wins} / L ${o.losses} | WIN RATE ${(o.win_rate * 100).toFixed(1)}% | AVG ${pct(o.avg_pnl_pct)} | EXPECTANCY ${pct(o.expectancy_pct)} | TOTAL ${usd(o.total_pnl_usd, true)}`
    : "NO CLOSED TRADES YET. THE BRAIN IS A SMOOTH, UNWRINKLED BRAIN.";
  $("#lessonList").innerHTML = b.lessons.map((l, i) => `
    <div class="lessoncard" style="--r:${((i * 37) % 5) - 2}deg">
      <div><b>#${Number(l.id)}</b> from <b>${esc(l.symbol)}</b> <span class="${cls(l.pnl_pct)}">${pct(l.pnl_pct)}</span>
        <span class="muted">· ${esc((l.ts || "").slice(0, 16).replace("T", " "))} · cited ${Number(l.cited)}x · ${esc(l.brain || "")} · trade #${Number(l.trade_id)}</span></div>
      <div style="font-size:14px; margin:4px 0">📌 ${esc(l.lesson)}</div>
      ${l.what_worked ? `<div class="win">✔ ${esc(l.what_worked)}</div>` : ""}
      ${l.what_failed ? `<div class="loss">✘ ${esc(l.what_failed)}</div>` : ""}
      <div>${(l.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
        ${(l.buckets || []).map((t) => `<span class="tag" style="background:#0ff">${esc(t)}</span>`).join("")}</div>
    </div>`).join("") || "<p>No lessons yet. Let the bot lose some fake money first.</p>";

  $("#statsBody").innerHTML = b.stats.map((s) => {
    const bar = Math.round(s.win_rate * 20);
    return `<tr><td>${esc(s.bucket)}</td><td>${s.n}</td><td>${s.wins}</td><td>${s.losses}</td>
      <td>${(s.win_rate * 100).toFixed(0)}%</td><td class="${cls(s.avg_pnl_pct)}">${pct(s.avg_pnl_pct)}</td>
      <td style="font-family:monospace"><span class="win">${"█".repeat(bar)}</span><span class="loss">${"░".repeat(20 - bar)}</span></td></tr>`;
  }).join("") || '<tr><td colspan="7">No stats yet.</td></tr>';

  if (window.Chart) {
    const eqData = b.equity.map((e) => ({ x: e.ts.slice(5, 16).replace("T", " "), y: e.equity_usd }));
    eqChart && eqChart.destroy();
    eqChart = new Chart($("#eqChart"), {
      type: "line",
      data: { labels: eqData.map((d) => d.x), datasets: [{ label: `${mode.toUpperCase()} EQUITY (USD)`, data: eqData.map((d) => d.y),
        borderColor: "#ff00ff", backgroundColor: "rgba(0,255,0,.25)", fill: true, pointRadius: 0, tension: 0.2, borderWidth: 3 }] },
      options: { maintainAspectRatio: false, animation: false, plugins: { legend: { labels: { font: { family: "Comic Sans MS" } } } },
        scales: { x: { ticks: { maxTicksLimit: 8 } } } },
    });
    const trades = (await api("/api/trades?limit=100")).reverse();
    pnlChart && pnlChart.destroy();
    pnlChart = new Chart($("#pnlChart"), {
      type: "bar",
      data: { labels: trades.map((t) => t.symbol), datasets: [{ label: "P&L PER TRADE (%)", data: trades.map((t) => t.pnl_pct),
        backgroundColor: trades.map((t) => (t.pnl_pct >= 0 ? "#00d000" : "#ff0033")), borderColor: "#000", borderWidth: 1 }] },
      options: { maintainAspectRatio: false, animation: false, plugins: { legend: { labels: { font: { family: "Comic Sans MS" } } } } },
    });
  }
}
$$(".tab").forEach((t) => t.onclick = () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".tabpanel").forEach((p) => p.classList.toggle("hidden", p.id !== t.dataset.tab));
});
$("#lessonGo").onclick = () => safe(loadBrain);
$("#lessonQ").addEventListener("keydown", (e) => e.key === "Enter" && safe(loadBrain));
$("#lessonSort").onchange = () => safe(loadBrain);
$("#askGo").onclick = () => safe(async () => {
  const q = $("#askQ").value.trim();
  if (!q) return;
  $("#askA").textContent = "📎 thinking really hard...";
  const r = await api("/api/brain/ask", { method: "POST", body: { question: q } });
  $("#askA").textContent = r.answer;
});
$("#clippy").onclick = () => { openWin("w-brain"); $('[data-tab="t-ask"]').click(); };

/* ------------------------------------------------------------------ settings */
const SECTIONS = {
  engine: ["scan_interval_sec", "max_candidates_per_tick", "decision_cooldown_min", "include_watchlist", "watchlist"],
  filters: ["min_liquidity_usd", "min_volume_h24_usd", "min_age_min", "max_rug_score"],
  risk: ["min_confidence", "max_position_pct", "max_open_positions", "max_liquidity_share_pct", "default_stop_loss_pct",
    "default_take_profit_pct", "trailing_stop_pct", "trailing_activation_pct", "max_hold_min", "daily_loss_cap_usd", "groq_bail_check"],
  paper: ["paper_base_slippage_pct", "paper_fee_pct", "paper_network_fee_usd"],
  live: ["live_max_sol_per_trade", "live_max_exposure_usd", "live_slippage_bps"],
};
async function loadSettings() {
  SETTINGS = await api("/api/settings");
  const f = $("#iniForm");
  f.innerHTML = "";
  for (const [sect, keys] of Object.entries(SECTIONS)) {
    f.insertAdjacentHTML("beforeend", `<div class="sect">[${sect}]</div>`);
    for (const k of keys) {
      const v = SETTINGS[k];
      let input;
      if (typeof v === "boolean") input = `<input type="checkbox" id="set-${k}" data-k="${k}" ${v ? "checked" : ""}>`;
      else if (Array.isArray(v)) input = `<input type="text" id="set-${k}" data-k="${k}" data-list="1" value="${esc(v.join(","))}">`;
      else input = `<input type="number" id="set-${k}" step="any" data-k="${k}" value="${esc(v)}">`;
      f.insertAdjacentHTML("beforeend", `<label for="set-${k}">${k}=</label>${input}`);
    }
  }
  if (STATUS && STATUS.paper.starting_credit) $("#paperCredit").value = STATUS.paper.starting_credit;
}
$("#saveSettings").onclick = () => safe(async () => {
  const patch = {};
  $$("#iniForm [data-k]").forEach((i) => {
    const k = i.dataset.k;
    patch[k] = i.type === "checkbox" ? i.checked : i.dataset.list ? i.value.split(",").map((x) => x.trim()).filter(Boolean) : Number(i.value);
  });
  SETTINGS = await api("/api/settings", { method: "POST", body: patch });
  dialog({ title: "SETTINGS.INI", icon: "💾", html: "Settings saved. Please restart your computer for changes to take effect.<br><span class='muted'>(just kidding, they apply next tick)</span>", autoClose: 5000 });
  refreshStatus();
});
$("#paperReset").onclick = () => safe(async () => {
  const credit = Number($("#paperCredit").value);
  const wipe = $("#wipeBrain").checked;
  const ok = await dialog({ title: "Confirm", icon: "⚠️", modal: true,
    html: `Reset paper account to <b>${usd(credit)}</b>?${wipe ? "<br><b class='loss'>The brain will be wiped too. All lessons lost.</b>" : ""}`,
    buttons: [{ label: "Do it", value: true }, { label: "Cancel", value: false }] });
  if (!ok) return;
  await api("/api/paper/reset", { method: "POST", body: { starting_credit: credit, wipe_brain: wipe } });
  refreshAll();
});

/* ------------------------------------------------------------------ live mode */
function renderLive() {
  if (!STATUS) return;
  const r = STATUS.live.readiness;
  const live = STATUS.mode === "live";
  const sw = $("#liveSwitch");
  sw.classList.toggle("on", live);
  sw.classList.toggle("locked", !r.ready && !live);
  $(".knob", sw).textContent = live ? "LIVE" : "PAPER";
  $("#liveReasons").innerHTML = r.ready ? '<li class="win">All systems go. God help us.</li>' : r.reasons.map((x) => `<li class="loss">✘ ${esc(x)}</li>`).join("");
  $("#dryRunNote").innerHTML = r.dry_run ? '<b style="color:#b000b0">LIVE_DRY_RUN=true: swaps are simulated, never sent.</b>' : '<b class="loss blink">LIVE_DRY_RUN=false: REAL TRANSACTIONS.</b>';
  const p = STATUS.paper.summary || {};
  let grade = "N/A", warn = "";
  if (p.trades) {
    grade = p.expectancy_pct > 3 ? "A+" : p.expectancy_pct > 1 ? "B" : p.expectancy_pct > 0 ? "C" : p.expectancy_pct > -3 ? "D" : "F";
    if (p.trades < 20) warn = "⚠ fewer than 20 paper trades - sample size is a meme";
    else if (p.expectancy_pct <= 0) warn = "⚠ negative expectancy on paper. going live = paying tuition in real money";
  }
  $("#paperCard").innerHTML = p.trades
    ? `GRADE: ${grade}<br>TRADES ${p.trades} | WIN RATE ${(p.win_rate * 100).toFixed(1)}% | EXPECTANCY ${pct(p.expectancy_pct)}<br>AVG WIN ${pct(p.avg_win_pct)} | AVG LOSS ${pct(p.avg_loss_pct)} | TOTAL ${usd(p.total_pnl_usd, true)}${warn ? `<br><span style="color:#ff0">${esc(warn)}</span>` : ""}`
    : "NO PAPER TRADES YET. GO PRACTICE.";
  $("#liveWallet").textContent = STATUS.live.wallet ? `wallet: ${STATUS.live.wallet} · equity ${usd(STATUS.live.equity)}` : STATUS.live.error || "";
}
$("#liveSwitch").onclick = () => safe(async () => {
  if (!STATUS) return;
  if (STATUS.mode === "live") {
    const ok = await dialog({ title: "Chicken out?", icon: "🐔", modal: true, html: "Switch back to PAPER mode? Open live bags stay open and keep being managed.",
      buttons: [{ label: "Back to paper", value: true }, { label: "Stay degen", value: false }] });
    if (ok) { await api("/api/mode", { method: "POST", body: { mode: "paper" } }); refreshAll(); }
    return;
  }
  const r = STATUS.live.readiness;
  if (!r.ready) {
    return dialog({ title: "Access Denied", icon: "🔒", html: `LIVE_MODE.BAT cannot run:<ul>${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` });
  }
  const step1 = await dialog({ title: "WARNING", icon: "☢️", modal: true, cursed: true,
    html: `<b>You are about to trade REAL MONEY.</b><br><br>Memecoins + an LLM = you can lose everything in this wallet.
      Per-trade cap: ${SETTINGS ? esc(SETTINGS.live_max_sol_per_trade) : "?"} SOL · exposure cap: ${SETTINGS ? usd(SETTINGS.live_max_exposure_usd) : "?"} · daily loss cap: ${SETTINGS ? usd(SETTINGS.daily_loss_cap_usd) : "?"}
      ${r.dry_run ? "<br><br>(Dry run is ON - swaps will be simulated only.)" : ""}`,
    buttons: [{ label: "I understand", value: true, cls: "danger" }, { label: "Take me back", value: false }] });
  if (!step1) return;
  const phrase = await dialog({ title: "Final answer?", icon: "⌨️", modal: true, html: 'Type <b>SEND IT</b> to go live.',
    input: { placeholder: "SEND IT" }, buttons: [{ label: "Go live", value: true, cls: "danger" }, { label: "Cancel", value: null }] });
  if (!phrase) return;
  await api("/api/mode", { method: "POST", body: { mode: "live", confirm: phrase } });
  bsod("Just kidding. You're live now. Good luck.");
  refreshAll();
});
async function killSwitch() {
  openWin("w-live");
  const liquidate = $("#killLiquidate").checked;
  const ok = await dialog({ title: "Shut Down Windows", icon: "⏻", modal: true,
    html: `Stop the engine${liquidate ? " <b>and market-sell every open position</b>" : ""}?`,
    buttons: [{ label: "Yes", value: true, cls: "danger" }, { label: "No", value: false }] });
  if (!ok) return;
  const r = await api("/api/kill", { method: "POST", body: { liquidate } });
  dialog({ title: "Shut down", icon: "🌙", html: `It is now safe to turn off your computer.<br>Closed: ${esc(r.closed.join(", ") || "nothing")}${r.failed.length ? `<br><b class="loss">FAILED: ${esc(r.failed.join("; "))}</b>` : ""}` });
  refreshAll();
}
$("#killBtn").onclick = () => safe(killSwitch);

/* ------------------------------------------------------------------ engine control + start menu */
async function startEngine() {
  await api("/api/engine/start", { method: "POST" });
  clippy(pick(["It looks like you're trying to get rich! Starting engine...", "Engaging hopium thrusters 🚀", "Scanning for rugs to not get pulled by..."]));
  refreshStatus();
}
async function stopEngine() { await api("/api/engine/stop", { method: "POST" }); refreshStatus(); }
$("#btnStart").onclick = () => safe(startEngine);
$("#btnStop").onclick = () => safe(stopEngine);

$("#startbtn").onclick = (e) => { e.stopPropagation(); $("#startmenu").classList.toggle("hidden"); $("#startbtn").classList.toggle("pressed"); };
document.addEventListener("click", (e) => {
  if (!e.target.closest("#startmenu")) { $("#startmenu").classList.add("hidden"); $("#startbtn").classList.remove("pressed"); }
});
$$(".icon[data-open]").forEach((el) => {
  el.addEventListener("dblclick", () => openWin(el.dataset.open));
  el.addEventListener("click", () => { if (matchMedia("(max-width: 760px)").matches) openWin(el.dataset.open); });
});
$$("#startmenu [data-open]").forEach((el) => el.addEventListener("click", () => {
  openWin(el.dataset.open); $("#startmenu").classList.add("hidden");
}));
$$("[data-act]").forEach((el) => el.onclick = () => {
  $("#startmenu").classList.add("hidden");
  const a = el.dataset.act;
  if (a === "start") safe(startEngine);
  if (a === "stop") safe(stopEngine);
  if (a === "kill") safe(killSwitch);
  if (a === "bsod") bsod("This is only a test. Your bags are fine. Probably.");
});

/* ------------------------------------------------------------------ first run wizard */
async function firstRun() {
  const credit = await dialog({ title: "VibeTrader 95 Setup", icon: "💿", modal: true, cursed: true,
    html: `<b>Welcome to VibeTrader 95 Setup!</b><br><br>Before the bot can gamble with real money, it has to prove itself with fake money.
      How much <b>paper money</b> should it start with?`,
    input: { type: "number", value: "1000", placeholder: "1000" },
    buttons: [{ label: "Next >", value: true }, { label: "Cancel", value: null }] });
  if (!credit || Number(credit) <= 0) return;
  await safe(() => api("/api/paper/reset", { method: "POST", body: { starting_credit: Number(credit), wipe_brain: false } }));
  clippy(`It looks like you just gave me ${usd(credit)} of fake money! Press START and watch me learn.`, 12000);
  refreshAll();
}

/* ------------------------------------------------------------------ boot */
async function refreshAll() {
  await safe(async () => {
    await refreshStatus();
    await refreshPositions();
    if (!$("#w-trades").classList.contains("hidden")) await loadTrades();
  });
}
function tickClock() { $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }

(async function boot() {
  $$(".window").forEach(decorate);
  focusWin($("#w-portfolio"));
  tickClock(); setInterval(tickClock, 15000);
  try {
    const n = Number(localStorage.getItem("vt95_hits") || 1336) + 1;
    localStorage.setItem("vt95_hits", String(n));
    $("#hits").textContent = String(n).padStart(6, "0");
  } catch { $("#hits").textContent = "001337"; }
  connectEvents();
  const s = await safe(refreshStatus);
  await safe(refreshPositions);
  await safe(loadSettings);
  if (s && !s.paper.initialized) firstRun();
  else clippy(s && !s.groq_enabled
    ? "It looks like GROQ_API_KEY is missing! I'll use MAGIC8BALL.EXE instead. Add a key to .env for real brains."
    : "Hi! I'm Clippy-Groq. Double-click an icon, or click me to ask what the bot learned.");
  setInterval(() => refreshAll(), 8000);
})();
