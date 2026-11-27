/*
 * DFX Work: shared script parts. Load before the page script:
 *   <script src="../work.js"></script>
 * Top-level const and function here are globals for the page script.
 *
 *   esc(s)            escape text for HTML
 *   now()             "HH:MM"
 *   BOT               agent avatar icon (SVG string)
 *   reduce            true when the system asks for reduced motion
 *   RING, RCOL, RC    status word -> ring kind and arc share; colours; ring circumference
 *   chip(word)        status ring with label, e.g. chip("Review 2/5"); one marker for every status
 *   fillRings(root)   value role: arcs fill from zero when a list or view appears
 *   id8(uuid)         short id, full id in the title
 *   table(el, spec)   table from {cols, rows, click}
 *   enter(nodes)      staggered entry (jk-enter)
 *   WAIT_RING         the wait ring as SVG (used in the open-question footer)
 *   foot(ask)         footer of an open question from {check, answers, since}
 *   countUp(el, to, suffix)  value role for numbers
 *   fold(), watchFold(cb)    foldables: html[data-fold] book|laptop, --fold-first on .fold-grid
 *
 * RING must match status.json. checks/run.sh compares them.
 */

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
const now = () => new Date().toTimeString().slice(0, 5);
const BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M9 12h.01M15 12h.01"/></svg>';
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Status word -> ring (design A). Arc share = phase: implement 1/4, check 1/2, review 3/4. */
const RING = {
  queued: ["idle", 0], pending: ["idle", 0],
  /* "waiting" is the agent's own order state for an open question or blocker (order_state, coordinator_orders.py). */
  waiting: ["wait", .5], question: ["wait", .5],
  implementing: ["work", .25], working: ["work", .25], "local check": ["work", .5], running: ["work", .5], active: ["work", .5], review: ["work", .75], reviewing: ["work", .75],
  ready: ["ok", 1], passed: ["ok", 1], approved: ["ok", 1], acked: ["ok", 1],
  done: ["done", 1], merged: ["done", 1],
  failed: ["bad", 1], "check failed": ["bad", 1], blocked: ["bad", 1],
  cancelled: ["cancel", 0],
};
const RCOL = { idle: "var(--neutral-500)", cancel: "var(--neutral-500)", wait: "var(--warn)", work: "var(--blue-500)", ok: "var(--status-success)", done: "var(--navy-800)", bad: "var(--red-500)" };
const RC = 2 * Math.PI * 6;
function chip(word) {
  const m = String(word).match(/^(.*?)\s+(\d+\/\d+)$/);
  const label = m ? m[1] : String(word), meta = m ? m[2] : "";
  const [kind, frac] = RING[label.toLowerCase()] || ["idle", 0];
  const col = RCOL[kind];
  let g;
  if (kind === "done") g = `<circle cx="8" cy="8" r="7" fill="${col}"/><path d="M5 8.2l2 2 4-4.2" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`;
  else if (kind === "bad") g = `<circle cx="8" cy="8" r="7" fill="${col}"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`;
  // Ready: thinner ring (1.5) and a smaller check with at least 1.6 px air to the ring.
  else if (kind === "ok") g = `<circle cx="8" cy="8" r="6" fill="none" stroke="${col}" stroke-width="1.5"/><path d="M5.6 8.1l1.7 1.7 3.1-3.3" fill="none" stroke="${col}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
  else if (kind === "cancel") g = `<circle cx="8" cy="8" r="6" fill="none" stroke="${col}" stroke-width="2"/><path d="M4 12L12 4" stroke="${col}" stroke-width="2" stroke-linecap="round"/>`;
  else if (kind === "idle") g = `<g class="dash-spin"><circle cx="8" cy="8" r="6" fill="none" stroke="${col}" stroke-width="2" stroke-dasharray="2.4 2.3"/></g>`;
  else {
    const arc = `<circle class="arc jk-value-arc-c" cx="8" cy="8" r="6" stroke="${col}" data-to="${(frac * RC).toFixed(2)}" style="stroke-dasharray:${(frac * RC).toFixed(2)} ${RC.toFixed(2)}"/>`;
    g = `<circle class="track" cx="8" cy="8" r="6"/>` + (kind === "work" ? `<g class="spin">${arc}</g>` : arc);
    if (kind === "wait") g += `<circle cx="8" cy="8" r="2.5" fill="${col}"/>`;
  }
  return `<span class="ring" role="img" aria-label="${esc(word)}"><svg viewBox="0 0 16 16" aria-hidden="true">${g}</svg><span class="label">${esc(label)}</span>${meta ? `<span class="meta">${esc(meta)}</span>` : ""}</span>`;
}
// Value role from motion.css: arcs fill from zero when a view or list appears.
function fillRings(root) {
  if (reduce) return;
  const arcs = [...root.querySelectorAll(".ring .arc")];
  arcs.forEach(a => { a.style.transition = "none"; a.style.strokeDasharray = `0 ${RC.toFixed(2)}`; });
  requestAnimationFrame(() => requestAnimationFrame(() => arcs.forEach(a => { a.style.transition = ""; a.style.strokeDasharray = `${a.dataset.to} ${RC.toFixed(2)}`; })));
}
const id8 = v => `<code class="id" title="${esc(v)}">${esc(v.slice(0, 8))}</code>`;

function table(el, spec) {
  el.innerHTML = `<table><thead><tr>${spec.cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    spec.rows.map((r, i) => `<tr class="jk-enter${spec.click ? " click" : ""}${spec.click && i === 0 ? " sel" : ""}" style="--jk-i:${i}" data-i="${i}">${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")
  }</tbody></table>`;
}

function enter(nodes) {
  if (reduce) return;
  nodes.forEach((n, i) => { n.classList.remove("jk-enter"); void n.offsetWidth; n.style.setProperty("--jk-i", i); n.classList.add("jk-enter"); });
}

const WAIT_RING = `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--neutral-200)" stroke-width="2"/><circle cx="8" cy="8" r="6" fill="none" stroke="var(--warn)" stroke-width="2" stroke-linecap="round" stroke-dasharray="18.85 37.7" transform="rotate(-90 8 8)"/><circle cx="8" cy="8" r="2.5" fill="var(--warn)"/></svg>`;

function foot(a) {
  const acts = (a.answers.includes("override") ? `<button type="button" class="act" data-act="override">Override</button>` : "")
    + (a.answers.includes("cancel") ? `<button type="button" class="act danger" data-act="cancel">Cancel order</button>` : "");
  return `<div class="foot"><span class="state">${WAIT_RING}Waiting for you</span><span class="since">${esc(a.since)}</span>${a.check ? `<span class="check">${esc(a.check)}</span>` : ""}<span class="spacer"></span><span class="acts">${acts}</span>
    <div class="reason"><label></label><input type="text" maxlength="500" placeholder="Reason, one line" aria-label="Reason"><button type="button" class="act" data-act="back">Back</button><button type="button" class="btn btn-primary btn-sm jk-press" data-act="confirm" disabled></button></div></div>`;
}

function countUp(el, to, suffix) {
  if (reduce) { el.textContent = to + suffix; return; }
  const t0 = performance.now(), d = 700, ease = x => 1 - Math.pow(1 - x, 3);
  // A frame timestamp can lie before t0; clamp so the number never goes below zero.
  const step = t => { const p = Math.min(1, Math.max(0, (t - t0) / d)); el.textContent = Math.round(to * ease(p)) + suffix; if (p < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

/* Foldables. Reads the screen segments (Viewport Segments API; on iPhone Duo behind a Safari
 * feature flag) and sets html[data-fold] to "book" (two segments side by side) or "laptop"
 * (stacked), otherwise removes it. Every element with class fold-grid gets --fold-first: the
 * size of its first column or row so that it ends 16 px before the fold. --fold-gap is the
 * width of the fold itself. Without the API nothing changes.
 * ?fold=book|laptop[&hinge=px] simulates the two halves for previews and checks. */
function foldSegments() {
  const q = new URLSearchParams(location.search), sim = q.get("fold");
  if (sim === "book" || sim === "laptop") {
    const g = +(q.get("hinge") || 0), W = innerWidth, H = innerHeight;
    return sim === "book"
      ? [new DOMRect(0, 0, (W - g) / 2, H), new DOMRect((W + g) / 2, 0, (W - g) / 2, H)]
      : [new DOMRect(0, 0, W, (H - g) / 2), new DOMRect(0, (H + g) / 2, W, (H - g) / 2)];
  }
  const s = window.viewport && window.viewport.segments;
  return s && s.length === 2 ? [...s] : null;
}
function fold() {
  const root = document.documentElement, s = foldSegments();
  if (!s) { delete root.dataset.fold; return null; }
  const book = s[1].left >= s[0].right;
  root.dataset.fold = book ? "book" : "laptop";
  const at = book ? s[0].right : s[0].bottom;
  root.style.setProperty("--fold-gap", (book ? s[1].left - s[0].right : s[1].top - s[0].bottom) + "px");
  document.querySelectorAll(".fold-grid").forEach(el => {
    el.style.removeProperty("--fold-first");
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    const first = book ? at - r.left - parseFloat(cs.paddingLeft) - 16 : at - r.top - parseFloat(cs.paddingTop) - 16;
    el.style.setProperty("--fold-first", Math.max(120, Math.round(first)) + "px");
  });
  return root.dataset.fold;
}
function watchFold(onChange) {
  let last = fold();
  const again = () => { const now = fold(); if (now !== last) { last = now; if (onChange) onChange(now); } };
  addEventListener("resize", again);
  if (navigator.devicePosture) navigator.devicePosture.addEventListener("change", again);
}
