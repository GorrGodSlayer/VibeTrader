// Concept brain.
// Similar lessons bunch into concepts (glowing nodes); every lesson is a dot in its concept's cluster, and every synapse
// a lesson fired is its own strand from that dot to the related concept. A force layout pulls strongly connected
// concepts together. New lessons arrive live over SSE: the dot flies into its concept (or founds a new one) and its
// synapses shoot out to the concepts it relates to.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const $ = (id) => document.getElementById(id);
const container = $("brain3d");
const tip = $("brainTip");
const hud = $("brainHud");
const logEl = $("connLog");
const logCount = $("connLogCount");
const callout = $("brainCallout");
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v || 0).toFixed(2)}%`;
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const COL = {
  up: new THREE.Color(css("--up")),
  down: new THREE.Color(css("--down")),
  accent: new THREE.Color(css("--accent")),
  strand: new THREE.Color("#7f93b5"),
  idle: new THREE.Color("#6b7580"),
  prior: new THREE.Color("#5b8cff"),
};
const STRAND_SEG = 8;
const LIVE_FLY_MS = 1400;
const LIVE_FIRE_MS = 1200;
const LIVE_HOLD_MS = 9000;
const LOG_MAX = 300;
const LABEL_TOP = 10;

/* ---------------------------------------------------------------- deterministic helpers */
function rng(seed) {
  let s = seed >>> 0 || 1;
  return () => { s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
}
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
function fibPoint(i, n, r) {
  // i-th of n points spread evenly over a sphere of radius r
  const y = 1 - ((i + 0.5) / Math.max(1, n)) * 2;
  const rad = Math.sqrt(1 - y * y);
  const t = GOLDEN * i;
  return new THREE.Vector3(Math.cos(t) * rad * r, y * r, Math.sin(t) * rad * r);
}
const conceptColor = (c) => (c.prior ? COL.prior.clone() : c.n > 1 ? COL.down.clone().lerp(COL.up, c.win_rate) : COL.idle.clone());
const conceptRadius = (n) => 0.07 + 0.045 * Math.sqrt(n);
const conceptSize = (n) => 0.14 + 0.08 * Math.sqrt(n);
// playbook (innate) concepts hold rules, not lessons: size them by rule count
const weight = (c) => (c.prior ? 4 + (c.rules?.length || 0) * 1.5 : c.n);

/* ---------------------------------------------------------------- three.js setup */
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
} catch {
  $("brainFallback").classList.remove("hidden");
  throw new Error("WebGL unavailable");
}
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setClearColor(0x000000);
container.prepend(renderer.domElement);
const labelRenderer = new CSS2DRenderer();
labelRenderer.domElement.className = "b3-labels";
container.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 200);
camera.position.set(0, 0, 9);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.screenSpacePanning = true;
controls.minDistance = 2;
controls.maxDistance = 30;
controls.autoRotate = !reduceMotion;
controls.autoRotateSpeed = 0.35;
renderer.domElement.addEventListener("pointerdown", () => { controls.autoRotate = false; });

const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.55, 0.4, 0.5);
composer.addPass(bloom);
composer.addPass(new OutputPass());

const strandGroup = new THREE.Group();
const dotGroup = new THREE.Group();
const nodeGroup = new THREE.Group();
const liveGroup = new THREE.Group();
scene.add(strandGroup, dotGroup, nodeGroup, liveGroup);

function resize() {
  const w = container.clientWidth, h = container.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  labelRenderer.setSize(w, h);
  composer.setSize(w, h);
  bloom.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(container);

const DOT = (() => {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.35, "rgba(255,255,255,.6)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
})();
const additive = { transparent: true, depthWrite: false, blending: THREE.AdditiveBlending };

/* ---------------------------------------------------------------- model */
let concepts = new Map();      // id -> concept view + { pos, vel, sprite, labelObj, members: [lessonIds] }
let synapses = new Map();      // "a|b" -> { a, b, weight }
let lessons = [];              // chronological
let lessonById = new Map();    // id -> { lesson, concept, links, offset, dotIndex }
let strands = [];              // { lessonId, from concept, to concept, isNew } in chronological order
let dots = null, strandLines = null;
let alpha = 0;                 // layout temperature
let ready = false;
let highlightIds = null;       // lesson ids to emphasize (search / hover)
let focusConcept = null;       // hovered concept id
const bornLive = new Set();    // concepts founded during this session: always labelled

const key = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`);

function placeInitial(c, i, n) {
  // deterministic start on a sunflower disc; the force layout takes it from there
  const r = (1.2 + Math.sqrt(n) * 0.9) * Math.sqrt((i + 0.5) / Math.max(1, n));
  const t = i * GOLDEN;
  return new THREE.Vector3(Math.cos(t) * r, Math.sin(t) * r, (rng(c.id)() - 0.5) * 0.6);
}

function ingest(data) {
  concepts = new Map();
  synapses = new Map();
  lessonById = new Map();
  strands = [];
  lessons = data.lessons || [];
  const list = data.concepts || [];
  list.forEach((c, i) => concepts.set(c.id, { ...c, members: [], pos: placeInitial(c, i, list.length), vel: new THREE.Vector3() }));
  for (const s of data.synapses || []) synapses.set(key(s.a, s.b), { a: s.a, b: s.b, weight: s.weight });
  const seenPairs = new Set();
  for (const l of lessons) {
    const asg = data.assignments?.[l.id] || data.assignments?.[String(l.id)];
    if (!asg) continue;
    const c = concepts.get(asg.concept);
    if (!c) continue;
    c.members.push(l.id);
    lessonById.set(l.id, { lesson: l, concept: asg.concept, links: asg.links || [], newConcept: asg.new_concept });
    for (const lk of asg.links || []) {
      const k = key(asg.concept, lk.concept);
      strands.push({ lessonId: l.id, from: asg.concept, to: lk.concept, isNew: !seenPairs.has(k) });
      seenPairs.add(k);
    }
  }
}

/* ---------------------------------------------------------------- force layout */
function simulate(steps, temp) {
  const list = [...concepts.values()];
  const syn = [...synapses.values()];
  for (let it = 0; it < steps; it++) {
    const t = temp * (1 - it / (steps + 1));
    for (const c of list) c.vel.multiplyScalar(0.6);
    // repulsion between every pair of concepts, scaled by their size
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const a = list[i], b = list[j];
        const d = a.pos.clone().sub(b.pos);
        const dist2 = Math.max(0.05, d.lengthSq());
        // big pairs push hard (room for their clusters and labels); small concepts may sit close to what they relate to
        const f = (0.6 + 0.05 * Math.sqrt(weight(a) * weight(b))) / dist2;
        d.normalize().multiplyScalar(f);
        a.vel.add(d);
        b.vel.sub(d);
      }
    }
    // synapses pull connected concepts together, stronger with more firings
    for (const s of syn) {
      const a = concepts.get(s.a), b = concepts.get(s.b);
      if (!a || !b) continue;
      const d = b.pos.clone().sub(a.pos);
      const len = d.length() || 0.001;
      const rest = 1.8 + conceptRadius(weight(a)) + conceptRadius(weight(b));
      const k = 0.025 * Math.log2(1 + s.weight);
      d.multiplyScalar(((len - rest) * k) / len);
      a.vel.add(d);
      b.vel.sub(d);
    }
    for (const c of list) {
      c.vel.add(c.pos.clone().multiplyScalar(-0.02)); // gravity keeps loners from drifting off
      c.pos.add(c.vel.clone().clampLength(0, 0.5).multiplyScalar(t));
      c.pos.z *= 0.9; // mostly flat: a readable map that still has depth when rotated
    }
  }
}

/* ---------------------------------------------------------------- geometry */
function lessonOffset(entry, idxInConcept, n) {
  return fibPoint(idxInConcept, Math.max(n, 1), conceptRadius(n) * (0.75 + 0.25 * rng(entry.lesson.id)()));
}

function clear(group) {
  for (const o of [...group.children]) {
    o.traverse((x) => { x.geometry?.dispose(); x.material?.dispose(); if (x.isCSS2DObject) x.element.remove(); });
    group.remove(o);
  }
}

function buildNodes() {
  clear(nodeGroup);
  const ranked = [...concepts.values()].sort((a, b) => b.n - a.n);
  const labelled = new Set([...ranked.slice(0, LABEL_TOP).map((c) => c.id), ...bornLive,
    ...[...concepts.values()].filter((c) => c.prior).map((c) => c.id)]);
  for (const c of concepts.values()) {
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: DOT, color: conceptColor(c), ...additive }));
    sprite.scale.setScalar(conceptSize(weight(c)));
    sprite.position.copy(c.pos);
    sprite.userData = { concept: c.id, base: conceptSize(weight(c)), flashAt: -1e9, bornAt: -1e9 };
    if (labelled.has(c.id)) {
      const el = document.createElement("div");
      el.className = "b3-node";
      el.innerHTML = c.prior
        ? `<span class="prior">${esc(c.label)}</span> <span class="dim">· ${c.rules?.length || 0} rules</span>`
        : `${esc(c.label)} <span class="${c.avg_pnl_pct > 0 ? "up" : "down"}">${pct(c.avg_pnl_pct)}</span> <span class="dim">· ${c.n}</span>`;
      const lbl = new CSS2DObject(el);
      lbl.position.set(0, -0.62, 0); // below the glow, in the sprite's local (scaled) space
      sprite.add(lbl);
    }
    c.sprite = sprite;
    nodeGroup.add(sprite);
  }
}

function buildDots() {
  clear(dotGroup);
  const pos = new Float32Array(lessons.length * 3);
  const col = new Float32Array(lessons.length * 3);
  let i = 0;
  const counters = new Map();
  for (const l of lessons) {
    const e = lessonById.get(l.id);
    if (!e) continue;
    const c = concepts.get(e.concept);
    const k = counters.get(e.concept) || 0;
    counters.set(e.concept, k + 1);
    e.offset = lessonOffset(e, k, c.n);
    e.dotIndex = i;
    const cc = ((l.pnl_pct || 0) > 0 ? COL.up : COL.down).clone().multiplyScalar(0.9);
    col.set([cc.r, cc.g, cc.b], i * 3);
    i++;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  g.setDrawRange(0, i);
  dots = new THREE.Points(g, new THREE.PointsMaterial({ size: 0.05, map: DOT, vertexColors: true, ...additive }));
  dotGroup.add(dots);
  updateDotPositions();
  applyHighlight();
}

function buildStrands() {
  clear(strandGroup);
  const n = strands.length;
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(n * STRAND_SEG * 2 * 3), 3));
  g.setAttribute("color", new THREE.BufferAttribute(new Float32Array(n * STRAND_SEG * 2 * 3), 3));
  strandLines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: strandOpacity(), depthWrite: false, blending: THREE.AdditiveBlending }));
  strandGroup.add(strandLines);
  colorStrands();
  updateStrandPositions();
}

function strandOpacity() {
  return Math.min(0.85, Math.max(0.08, 1.5 / Math.sqrt(Math.max(1, strands.length / 10))));
}

function colorStrands() {
  if (!strandLines) return;
  const col = strandLines.geometry.attributes.color.array;
  strands.forEach((s, i) => {
    const on = !highlightIds || highlightIds.has(s.lessonId);
    const focus = focusConcept == null || s.from === focusConcept || s.to === focusConcept;
    const base = s.isNew ? COL.accent : COL.strand;
    const k = on && focus ? 1 : 0.08;
    for (let j = 0; j < STRAND_SEG * 2; j++) {
      // fade from the lesson (bright) to the target concept
      const t = Math.floor(j / 2) / STRAND_SEG;
      const f = k * (1 - 0.6 * t);
      col.set([base.r * f, base.g * f, base.b * f], (i * STRAND_SEG * 2 + j) * 3);
    }
  });
  strandLines.geometry.attributes.color.needsUpdate = true;
}

function dotWorld(e) {
  return concepts.get(e.concept).pos.clone().add(e.offset);
}

function updateDotPositions() {
  if (!dots) return;
  const arr = dots.geometry.attributes.position.array;
  for (const e of lessonById.values()) {
    if (e.dotIndex == null || e.live) continue;
    const p = dotWorld(e);
    arr[e.dotIndex * 3] = p.x; arr[e.dotIndex * 3 + 1] = p.y; arr[e.dotIndex * 3 + 2] = p.z;
  }
  dots.geometry.attributes.position.needsUpdate = true;
  dots.geometry.computeBoundingSphere();
}

function strandCurve(s) {
  const e = lessonById.get(s.lessonId);
  const a = dotWorld(e);
  const target = concepts.get(s.to).pos;
  const mid = a.clone().lerp(target, 0.5);
  const r = rng(s.lessonId * 7 + s.to);
  mid.add(new THREE.Vector3(r() - 0.5, r() - 0.5, r() - 0.5).multiplyScalar(0.35));
  return new THREE.QuadraticBezierCurve3(a, mid, target).getPoints(STRAND_SEG);
}

function updateStrandPositions() {
  if (!strandLines) return;
  const arr = strandLines.geometry.attributes.position.array;
  strands.forEach((s, i) => {
    const pts = strandCurve(s);
    for (let j = 0; j < STRAND_SEG; j++) {
      const o = (i * STRAND_SEG * 2 + j * 2) * 3;
      arr.set([pts[j].x, pts[j].y, pts[j].z, pts[j + 1].x, pts[j + 1].y, pts[j + 1].z], o);
    }
  });
  strandLines.geometry.attributes.position.needsUpdate = true;
  strandLines.geometry.computeBoundingSphere();
}

function applyHighlight() {
  if (dots) {
    const col = dots.geometry.attributes.color.array;
    for (const e of lessonById.values()) {
      if (e.dotIndex == null) continue;
      const on = (!highlightIds || highlightIds.has(e.lesson.id)) && (focusConcept == null || e.concept === focusConcept);
      const c = ((e.lesson.pnl_pct || 0) > 0 ? COL.up : COL.down).clone().multiplyScalar(on ? 0.95 : 0.12);
      col.set([c.r, c.g, c.b], e.dotIndex * 3);
    }
    dots.geometry.attributes.color.needsUpdate = true;
  }
  colorStrands();
  for (const c of concepts.values()) {
    if (!c.sprite) continue;
    const linked = focusConcept == null || c.id === focusConcept || synapses.has(key(c.id, focusConcept));
    c.sprite.material.opacity = linked ? 1 : 0.2;
  }
}

/* ---------------------------------------------------------------- HUD, log, callout */
function updateHud() {
  const fired = [...synapses.values()].reduce((a, s) => a + s.weight, 0);
  hud.textContent = `${concepts.size} concepts · ${synapses.size} synapses (${fired.toLocaleString()} firings) · ${lessons.length.toLocaleString()} lessons`;
}

function conceptLabel(id) {
  return concepts.get(id)?.label || `concept ${id}`;
}

function linkChips(e) {
  const home = `<span class="node home" title="concept">${esc(conceptLabel(e.concept))}</span>`;
  const links = (e.links || []).map((lk) => `<span class="arr${lk.new ? " new" : ""}"${lk.new ? ' title="new synapse"' : ""}>⟶</span><span class="node" title="fired synapse (strength ${lk.weight})">${esc(lk.label || conceptLabel(lk.concept))}</span>`).join("");
  return home + links;
}

function logItem(e, fresh) {
  const l = e.lesson, p = l.pnl_pct || 0;
  const el = document.createElement("div");
  el.className = `cl-item ${p > 0 ? "win" : "loss"}${fresh ? " fresh" : ""}`;
  el.innerHTML = `<div class="cl-top"><span>${esc((l.ts || "").replace("T", " ").slice(5, 16))}</span><span class="sym">${esc(l.symbol)}</span>
      <span class="${p > 0 ? "up" : "down"}">${pct(p)}</span>${e.newConcept ? '<span class="new">new concept</span>' : (e.links || []).some((lk) => lk.new) ? '<span class="new">new synapse</span>' : ""}</div>
    <div class="cl-chain">${linkChips(e)}</div><div class="cl-text">${esc(l.lesson)}</div>`;
  el.onmouseenter = () => { highlightIds = new Set([l.id]); focusConcept = null; applyHighlight(); el.classList.add("active"); };
  el.onmouseleave = () => { highlightIds = searchIds; applyHighlight(); el.classList.remove("active"); };
  el.onclick = () => window.dispatchEvent(new CustomEvent("brain:lesson", { detail: l.id }));
  return el;
}

function renderLog() {
  logEl.innerHTML = "";
  const recent = lessons.slice(-LOG_MAX).reverse();
  if (!recent.length) logEl.innerHTML = '<div class="empty">Concepts and synapses appear as lessons are learned</div>';
  for (const l of recent) {
    const e = lessonById.get(l.id);
    if (e) logEl.appendChild(logItem(e, false));
  }
  logCount.textContent = lessons.length ? `${Math.min(lessons.length, LOG_MAX)} of ${lessons.length}` : "";
}

const calloutQueue = [];
let calloutTimer = null;
function showCallout(e) {
  calloutQueue.push(e);
  if (!calloutTimer) nextCallout();
}
function nextCallout() {
  const e = calloutQueue.shift();
  if (!e) { callout.classList.remove("show"); calloutTimer = null; return; }
  const l = e.lesson, p = l.pnl_pct || 0;
  const newSyn = (e.links || []).filter((lk) => lk.new).length;
  callout.innerHTML = `<div class="kicker">LESSON LEARNED · ${esc(new Date().toLocaleTimeString([], { hour12: false }))}</div>
    <div class="head"><span class="sym">${esc(l.symbol)}</span><span class="mono ${p > 0 ? "up" : "down"}">${pct(p)}</span></div>
    <div class="meta">${e.newConcept ? "Founded a new concept" : "Joined concept"}</div>
    <div class="cl-chain">${linkChips(e)}</div>
    <div class="text">${esc(l.lesson)}</div>
    <div class="meta">${(e.links || []).length} synapse${(e.links || []).length === 1 ? "" : "s"} fired${newSyn ? ` · ${newSyn} new` : ""}</div>`;
  callout.classList.add("show");
  calloutTimer = setTimeout(nextCallout, calloutQueue.length ? 4000 : LIVE_HOLD_MS);
}

/* ---------------------------------------------------------------- live learning */
const live = []; // animations: { e, start, from, strands: [line], pulse }

function learn(evt) {
  if (!ready || !evt || evt.concept == null || lessonById.has(evt.id)) { scheduleRefresh(); return; }
  const l = { id: evt.id, ts: evt.ts, trade_id: evt.trade_id, symbol: evt.symbol, pnl_pct: evt.pnl_pct, lesson: evt.lesson, buckets: evt.buckets, cited: 0 };
  lessons.push(l);
  let c = concepts.get(evt.concept);
  const now = performance.now();
  if (!c) {
    // a brand-new concept is born next to the concept it relates to most, or at the edge of the brain
    const anchor = evt.links?.[0] && concepts.get(evt.links[0].concept);
    const centre = controls.target.clone();
    const out = anchor ? anchor.pos.clone().sub(centre).setZ(0).normalize() : new THREE.Vector3(1, 0.4, 0).normalize();
    const pos = (anchor ? anchor.pos.clone() : centre).add(out.multiplyScalar(1.4));
    bornLive.add(evt.concept);
    c = { id: evt.concept, label: evt.concept_label, n: 0, wins: 0, losses: 0, win_rate: 0, avg_pnl_pct: 0, members: [], pos, vel: new THREE.Vector3() };
    concepts.set(c.id, c);
  }
  const won = (l.pnl_pct || 0) > 0;
  c.n += 1; c.wins += won ? 1 : 0; c.losses = c.n - c.wins; c.win_rate = c.wins / c.n;
  c.avg_pnl_pct = ((c.avg_pnl_pct || 0) * (c.n - 1) + (l.pnl_pct || 0)) / c.n;
  c.members.push(l.id);
  for (const lk of evt.links || []) {
    const k = key(c.id, lk.concept);
    const s = synapses.get(k) || { a: Math.min(c.id, lk.concept), b: Math.max(c.id, lk.concept), weight: 0 };
    s.weight += 1;
    synapses.set(k, s);
  }
  const e = { lesson: l, concept: c.id, links: evt.links || [], newConcept: !!evt.new_concept, live: true };
  lessonById.set(l.id, e);
  e.offset = lessonOffset(e, c.members.length - 1, c.n);

  // rebuild the static layers so the new concept/sprite/size are in place, then animate the new lesson on top
  buildNodes();
  if (evt.new_concept) {
    c.sprite.userData.bornAt = now;
    alpha = 1; // let the layout settle the newcomer in
  }
  c.sprite.userData.flashAt = now + LIVE_FLY_MS;
  for (const lk of evt.links || []) {
    const t = concepts.get(lk.concept);
    if (t?.sprite) t.sprite.userData.flashAt = now + LIVE_FLY_MS + LIVE_FIRE_MS;
  }
  const pulse = new THREE.Sprite(new THREE.SpriteMaterial({ map: DOT, color: won ? COL.up : COL.down, ...additive }));
  pulse.scale.setScalar(0.28);
  const from = camera.position.clone().lerp(dotWorld(e), 0.55).add(new THREE.Vector3(0, 1.2, 0));
  pulse.position.copy(reduceMotion ? dotWorld(e) : from);
  liveGroup.add(pulse);
  live.push({ e, start: now, from, pulse, lines: [] });

  logEl.querySelector(".empty")?.remove();
  logEl.prepend(logItem(e, true));
  while (logEl.children.length > LOG_MAX) logEl.lastChild.remove();
  logCount.textContent = `${Math.min(lessons.length, LOG_MAX)} of ${lessons.length}`;
  showCallout(e);
  updateHud();
  // fold everything back into the static layers once the animation has played out
  scheduleRefresh(LIVE_FLY_MS + LIVE_FIRE_MS + LIVE_HOLD_MS);
}

function strandLine(e, lk) {
  const s = { lessonId: e.lesson.id, from: e.concept, to: lk.concept };
  const pts = strandCurve(s);
  const c = (lk.new ? COL.accent : COL.strand).clone().multiplyScalar(2);
  const g = new THREE.BufferGeometry().setFromPoints(pts);
  g.setAttribute("color", new THREE.Float32BufferAttribute(pts.flatMap(() => [c.r, c.g, c.b]), 3));
  g.setDrawRange(0, 0);
  return new THREE.Line(g, new THREE.LineBasicMaterial({ vertexColors: true, ...additive }));
}

/* ---------------------------------------------------------------- camera framing */
function fitView() {
  const list = [...concepts.values()];
  if (!list.length) return;
  // frame the concepts that hold 90% of the lessons; tiny outlying concepts shouldn't shrink everything else
  const total = list.reduce((a, c) => a + weight(c), 0) || 1;
  const center = list.reduce((a, c) => a.add(c.pos.clone().multiplyScalar(weight(c))), new THREE.Vector3()).divideScalar(total);
  const byDist = list.map((c) => ({ d: c.pos.distanceTo(center) + conceptRadius(weight(c)) + 0.6, n: weight(c) })).sort((x, y) => x.d - y.d);
  let acc = 0, radius = 1.5;
  for (const x of byDist) {
    acc += x.n;
    radius = Math.max(radius, x.d);
    if (acc >= total * 0.9) break;
  }
  const fov = (camera.fov * Math.PI) / 180;
  const fitH = radius / Math.sin(fov / 2);
  const fitW = radius / Math.sin(Math.atan(Math.tan(fov / 2) * camera.aspect));
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(0, 0, Math.max(fitH, fitW) * 0.85));
  controls.update();
}

/* ---------------------------------------------------------------- data loading */
let refreshTimer = null;
function scheduleRefresh(ms = 1500) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refresh().catch(() => {}), ms);
}

async function refresh(replay = false) {
  const res = await fetch("/api/brain/graph");
  if (!res.ok) return;
  const data = await res.json();
  // keep positions of concepts we already know so the layout doesn't jump on every refresh
  const oldPos = new Map([...concepts.values()].map((c) => [c.id, c.pos]));
  ingest(data);
  let fresh = 0;
  for (const c of concepts.values()) {
    if (oldPos.has(c.id)) c.pos = oldPos.get(c.id);
    else fresh++;
  }
  const firstLayout = !ready || fresh === concepts.size;
  if (firstLayout) simulate(320, 1);
  else if (fresh) alpha = 1;
  clear(liveGroup);
  live.length = 0;
  buildNodes();
  buildDots();
  buildStrands();
  renderLog();
  updateHud();
  if (firstLayout) fitView();
  ready = true;
  if (replay) startReplay();
}

/* ---------------------------------------------------------------- replay: re-grow the brain in learning order */
let replayStart = -1, replayMs = 0;
function startReplay() {
  if (!lessons.length) return;
  replayMs = Math.min(9000, Math.max(2500, lessons.length * 6));
  replayStart = performance.now();
}
function applyReplay(now) {
  const t = Math.min(1, (now - replayStart) / replayMs);
  const k = Math.floor(t * lessons.length);
  const cutoff = lessons[Math.max(0, k - 1)]?.id ?? 0;
  dots.geometry.setDrawRange(0, k);
  const strandCount = strands.findIndex((s) => s.lessonId > cutoff);
  strandLines.geometry.setDrawRange(0, (strandCount < 0 ? strands.length : strandCount) * STRAND_SEG * 2);
  for (const c of concepts.values()) {
    const born = lessonById.get(c.members[0])?.lesson.id ?? Infinity;
    if (c.sprite) c.sprite.visible = born <= cutoff;
  }
  if (t >= 1) {
    replayStart = -1;
    dots.geometry.setDrawRange(0, lessons.length);
    strandLines.geometry.setDrawRange(0, Infinity);
    for (const c of concepts.values()) if (c.sprite) c.sprite.visible = true;
  }
}
$("brainReplay").onclick = () => startReplay();

/* ---------------------------------------------------------------- public API (used by app.js) */
let searchIds = null;
function highlight(ids) {
  searchIds = ids ? new Set(ids) : null;
  highlightIds = searchIds;
  applyHighlight();
}
window.Brain3D = { refresh, learn, highlight };

/* ---------------------------------------------------------------- hover + click */
const raycaster = new THREE.Raycaster();
raycaster.params.Points.threshold = 0.04;
const mouse = new THREE.Vector2();
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointermove", (e) => {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(mouse, camera);
  const node = raycaster.intersectObjects(nodeGroup.children, false)[0];
  let html = null, focus = null;
  if (node) {
    const c = concepts.get(node.object.userData.concept);
    focus = c.id;
    const links = [...synapses.values()].filter((s) => s.a === c.id || s.b === c.id).sort((x, y) => y.weight - x.weight);
    const lead = lessonById.get(c.lead)?.lesson.lesson;
    if (c.prior) {
      const more = (c.rules || []).length > 6 ? `<br>… ${c.rules.length - 6} more in the Playbook panel` : "";
      html = `<span class="sym">${esc(c.label)}</span> <span class="muted">· base knowledge</span>
        <div class="body">${(c.rules || []).slice(0, 6).map((r) => `• ${esc(r.rule)}`).join("<br>")}${more}</div>
        <div class="body">${links.length} synapses from learned lessons (${links.reduce((a, s) => a + s.weight, 0)} firings)</div>`;
    } else html = `<span class="sym">${esc(c.label)}</span>
      <div class="grid2"><span>lessons</span><span>${c.n}</span><span>win / loss</span><span>${c.wins} / ${c.losses}</span>
      <span>win rate</span><span>${(c.win_rate * 100).toFixed(1)}%</span>
      <span>avg P&amp;L</span><span class="${c.avg_pnl_pct > 0 ? "up" : "down"}">${pct(c.avg_pnl_pct)}</span>
      <span>synapses</span><span>${links.length} (${links.reduce((a, s) => a + s.weight, 0)} firings)</span></div>
      ${c.terms ? `<div class="body">${esc(c.terms.slice(0, 6).join(" · "))}</div>` : ""}
      ${lead ? `<div class="body">“${esc(lead)}”</div>` : ""}
      <div class="body">Click to show this concept's lessons.</div>`;
  } else if (dots) {
    const hit = raycaster.intersectObject(dots)[0];
    const e = hit && [...lessonById.values()].find((x) => x.dotIndex === hit.index);
    if (e) {
      const l = e.lesson, p = l.pnl_pct || 0;
      html = `<span class="sym">${esc(l.symbol)}</span> <span class="mono ${p > 0 ? "up" : "down"}">${pct(p)}</span>
        <span class="muted"> · ${esc(conceptLabel(e.concept))}</span><div class="body">${esc(l.lesson)}</div>`;
    }
  }
  if (focus !== focusConcept) { focusConcept = focus; applyHighlight(); }
  if (!html) { tip.classList.add("hidden"); renderer.domElement.style.cursor = ""; return; }
  tip.innerHTML = html;
  tip.style.left = Math.max(4, Math.min(e.clientX - rect.left + 14, rect.width - 290)) + "px";
  tip.style.top = Math.max(4, Math.min(e.clientY - rect.top + 14, rect.height - 180)) + "px";
  tip.classList.remove("hidden");
  renderer.domElement.style.cursor = "pointer";
});
renderer.domElement.addEventListener("pointerleave", () => {
  tip.classList.add("hidden");
  if (focusConcept != null) { focusConcept = null; applyHighlight(); }
});
renderer.domElement.addEventListener("click", (e) => {
  const dragged = downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4;
  if (focusConcept != null && !dragged) {
    const c = concepts.get(focusConcept);
    window.dispatchEvent(new CustomEvent("brain:concept", { detail: { id: c.id, label: c.label } }));
  }
});

/* ---------------------------------------------------------------- render loop */
let lowQuality = false, lastFrame = 0, slowFrames = 0;
function adaptQuality(now) {
  if (lowQuality || !lastFrame) return;
  slowFrames = now - lastFrame > 80 ? slowFrames + 1 : Math.max(0, slowFrames - 1);
  if (slowFrames >= 8) { lowQuality = true; renderer.setPixelRatio(1); resize(); }
}

function frame(now) {
  requestAnimationFrame(frame);
  if (document.hidden || container.offsetParent === null || !ready) { lastFrame = 0; return; }
  adaptQuality(now);
  lastFrame = now;

  if (alpha > 0.02) {
    simulate(2, alpha * 0.6);
    alpha *= 0.97;
    for (const c of concepts.values()) c.sprite?.position.copy(c.pos);
    updateDotPositions();
    updateStrandPositions();
  }
  if (replayStart >= 0) applyReplay(now);

  for (const L of live) {
    const age = now - L.start;
    const target = dotWorld(L.e);
    const fly = reduceMotion ? 1 : Math.min(1, age / LIVE_FLY_MS);
    const ease = 1 - Math.pow(1 - fly, 3);
    L.pulse.position.copy(L.from.clone().lerp(target, ease));
    if (fly >= 1 && !L.lines.length && L.e.links.length) {
      for (const lk of L.e.links) {
        const line = strandLine(L.e, lk);
        liveGroup.add(line);
        L.lines.push(line);
      }
    }
    const fire = reduceMotion ? 1 : Math.min(1, Math.max(0, (age - LIVE_FLY_MS) / LIVE_FIRE_MS));
    for (const line of L.lines) line.geometry.setDrawRange(0, Math.ceil(fire * (STRAND_SEG + 1)));
    L.pulse.scale.setScalar(fly < 1 ? 0.28 : 0.28 + 0.2 * Math.sin(Math.min(1, (age - LIVE_FLY_MS) / 600) * Math.PI));
    const fade = Math.max(0, 1 - (age - LIVE_FLY_MS - LIVE_FIRE_MS - LIVE_HOLD_MS) / 1500);
    for (const line of L.lines) line.material.opacity = fade;
  }

  for (const c of concepts.values()) {
    const s = c.sprite;
    if (!s) continue;
    const since = now - s.userData.flashAt;
    const flash = since >= 0 && since < 1200 ? Math.sin((since / 1200) * Math.PI) : 0;
    const born = Math.min(1, Math.max(0, (now - s.userData.bornAt) / 900));
    const grow = s.userData.bornAt > 0 && born < 1 ? born : 1;
    s.scale.setScalar(s.userData.base * grow * (1 + 1.2 * flash));
  }

  controls.update();
  if (lowQuality) renderer.render(scene, camera);
  else composer.render();
  labelRenderer.render(scene, camera);
}

resize();
requestAnimationFrame(frame);
refresh(true).catch((err) => console.error("brain refresh failed", err));
