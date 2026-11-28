"""Line and branch coverage for the orders page script in index.html.

Covered functions, the ones whose added or changed lines are in the branch diff
for src/agent_cli/static/index.html, including the shared block:

- stateIcon
- replaceHtml
- writeHtml
- showView
- section-nav click handler
- placeKey
- placeNode
- topMessage
- scroll listener
- layoutKey
- keepPlace
- relayout
- resize listener
- hashchange listener
- ringWord
- setCount
- listItemsHtml
- eventBit
- eventFact
- eventArticle
- askFootHtml
- parkAsk
- renderFeed
- showHead
- ordersTick
- selectOrder
- orders click handler
- seg click handler
- composer submit handler
- reply-body keydown handler
- cancel click handler
- wireHold, including start, stop, its keydown handler and the hold timer

Top-level statements: STATE_RING assignment, let anchor, let resized,
watchFold(relayout), lastLayout, showView(hash), ordersOpenOnly,
cancelHoldTimer, feedHtml, wireHold(), ordersTick() and setInterval(ordersTick).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.no_pg

NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_cli" / "static"
WORK = STATIC / "design" / "work.js"
PAGE = STATIC / "index.html"

_REPORT: dict[str, Any] | None = None

_HARNESS = r"""
const vm = require("vm");
const payload = __PAYLOAD__;
const NativeURLSearchParams = URLSearchParams;
const context = vm.createContext({
  URLSearchParams: function URLSearchParams(init) { return new NativeURLSearchParams(init); },
  setImmediate: setImmediate,
});
process.on("unhandledRejection", (error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
const stub = String.raw`
const NativeDate = Date;
let nowMs = NativeDate.parse("2026-11-14T00:05:00Z");
function ClockDate() {
  const args = Array.prototype.slice.call(arguments);
  return args.length ? new NativeDate(...args) : new NativeDate(nowMs);
}
ClockDate.now = function () { return nowMs; };
ClockDate.parse = function (value) { return NativeDate.parse(value); };
ClockDate.UTC = function () { return NativeDate.UTC.apply(NativeDate, arguments); };
ClockDate.prototype = NativeDate.prototype;
globalThis.Date = ClockDate;

const rafs = [];
const calls = [];
const intervals = [];
const timers = new Map();
const windowListeners = [];
const docListeners = [];
const scrolled = [];
const historyLog = [];
let timerSeq = 0;
let failSpec = null;
let onCall = null;
let schemaBody = { enabled: true };
let showExtra = {};
let openMode = "normal";
const computed = {
  feed: { overflowY: "visible", position: "static", display: "block", paddingLeft: "0px", paddingTop: "0px" },
  composer: { overflowY: "visible", position: "static", display: "block", paddingLeft: "0px", paddingTop: "0px" },
  orders: { overflowY: "visible", position: "static", display: "block", paddingLeft: "0px", paddingTop: "0px" },
};

class Element {}

function tokens(el) {
  return String(el.className || "").split(/\s+/).filter(Boolean);
}
function decode(value) {
  return String(value)
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}
function parseCompound(part) {
  const spec = { tag: "", id: "", classes: [], attrs: [] };
  const plain = part.replace(/\[[^\]]+\]/g, (token) => {
    const body = token.slice(1, -1);
    const eq = body.indexOf("=");
    if (eq < 0) spec.attrs.push({ name: body.trim().toLowerCase(), value: null });
    else {
      let value = body.slice(eq + 1).trim();
      if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value[value.length - 1] === value[0]) {
        value = value.slice(1, -1);
      }
      spec.attrs.push({ name: body.slice(0, eq).trim().toLowerCase(), value: value });
    }
    return "";
  });
  if (plain === "*") { spec.tag = "*"; return spec; }
  const re = /#([A-Za-z0-9_-]+)|\.([A-Za-z0-9_-]+)|([A-Za-z*][A-Za-z0-9-]*)/g;
  let match;
  while ((match = re.exec(plain))) {
    if (match[1]) spec.id = match[1];
    else if (match[2]) spec.classes.push(match[2]);
    else if (match[3] === "*") spec.tag = "*";
    else if (match[3]) spec.tag = match[3].toLowerCase();
  }
  return spec;
}
function matchesCompound(el, spec) {
  if (!el || !el.tag) return false;
  if (spec.tag && spec.tag !== "*" && el.tag !== spec.tag) return false;
  if (spec.id && el.id !== spec.id) return false;
  for (let i = 0; i < spec.classes.length; i += 1) {
    if (tokens(el).indexOf(spec.classes[i]) < 0) return false;
  }
  for (let i = 0; i < spec.attrs.length; i += 1) {
    const attr = spec.attrs[i];
    if (attr.name === "id") {
      if (!el.id) return false;
      if (attr.value !== null && el.id !== attr.value) return false;
      continue;
    }
    if (!Object.prototype.hasOwnProperty.call(el._attr, attr.name)) return false;
    if (attr.value !== null && el._attr[attr.name] !== attr.value) return false;
  }
  return true;
}
function walk(node, visit) {
  (node.children || []).forEach((child) => { visit(child); walk(child, visit); });
}
function descendants(node) {
  const out = [];
  walk(node, (el) => out.push(el));
  return out;
}
function splitSelectors(selector) {
  return selector.split(",").map((part) => part.trim()).filter(Boolean);
}
function selectorSteps(selector) {
  const steps = [];
  let rest = selector.trim();
  let child = false;
  while (rest.length) {
    const match = /^([^\s>]+)/.exec(rest);
    if (!match) break;
    steps.push({ compound: match[1], child: child });
    rest = rest.slice(match[1].length).replace(/^\s+/, "");
    child = false;
    if (rest.charAt(0) === ">") {
      child = true;
      rest = rest.slice(1).replace(/^\s+/, "");
    }
  }
  return steps;
}
function matchSteps(rootNode, steps) {
  let current = [rootNode];
  steps.forEach((step) => {
    const spec = parseCompound(step.compound);
    const next = [];
    current.forEach((node) => {
      const pool = step.child ? (node.children || []) : descendants(node);
      pool.forEach((el) => { if (matchesCompound(el, spec)) next.push(el); });
    });
    current = next;
  });
  return current;
}
function queryAll(rootNode, selector) {
  const matched = new Set();
  splitSelectors(selector).forEach((part) => {
    matchSteps(rootNode, selectorSteps(part)).forEach((el) => matched.add(el));
  });
  return descendants(rootNode).filter((el) => matched.has(el));
}
function connect(el, on) {
  el.isConnected = on;
  (el.children || []).forEach((child) => connect(child, on));
}
function applyAttrs(el, raw) {
  const re = /([:@A-Za-z_][\w:.-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+)))?/g;
  let match;
  while ((match = re.exec(raw))) {
    const name = match[1].toLowerCase();
    const rawValue = match[2] !== undefined ? match[2] : match[3] !== undefined ? match[3] : match[4] !== undefined ? match[4] : "";
    const value = decode(rawValue);
    el._attr[name] = value;
    if (name === "class") el.className = value;
    else if (name === "id") el.id = value;
    else if (name === "hidden") el.hidden = true;
    else if (name.slice(0, 5) === "data-") {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      el.dataset[key] = value;
    } else if (name === "style") {
      value.split(";").forEach((part) => {
        const cut = part.indexOf(":");
        if (cut < 0) return;
        const key = part.slice(0, cut).trim();
        el._style[key] = part.slice(cut + 1).trim();
      });
    }
  }
}
function parseInto(parent, html) {
  const re = /<(\/?)([A-Za-z][A-Za-z0-9]*)([^>]*)>/g;
  const voidTags = { br: 1, img: 1, input: 1, hr: 1, meta: 1, link: 1, circle: 1, path: 1, rect: 1 };
  const stack = [parent];
  let match;
  while ((match = re.exec(html))) {
    const closing = match[1] === "/";
    const tag = match[2].toLowerCase();
    const attrs = match[3];
    if (closing) {
      while (stack.length > 1 && stack[stack.length - 1].tag !== tag) stack.pop();
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = make(tag);
    applyAttrs(el, attrs);
    stack[stack.length - 1].appendChild(el);
    const selfClose = /\/\s*$/.test(attrs) || voidTags[tag] === 1;
    if (!selfClose) stack.push(el);
  }
}
function make(tag) {
  const el = new Element();
  el.tag = tag;
  el.children = [];
  el.parent = null;
  el.parentElement = null;
  el.id = "";
  el.className = "";
  el.hidden = false;
  el.textContent = "";
  el.value = "";
  el.checked = false;
  el.disabled = false;
  el.dataset = {};
  el._attr = {};
  el._style = {};
  el._html = "";
  el._writes = 0;
  el._listeners = {};
  el.isConnected = false;
  el.offsetTop = 0;
  el.offsetHeight = 0;
  el.offsetWidth = 8;
  el.scrollLeft = 0;
  el.scrollTop = 0;
  el._rect = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  el.classList = {
    add() {
      const names = Array.prototype.slice.call(arguments);
      const set = tokens(el);
      names.forEach((name) => { if (set.indexOf(name) < 0) set.push(name); });
      el.className = set.join(" ");
    },
    remove() {
      const drop = Array.prototype.slice.call(arguments);
      el.className = tokens(el).filter((name) => drop.indexOf(name) < 0).join(" ");
    },
    toggle(name, force) {
      const has = tokens(el).indexOf(name) >= 0;
      const on = force === undefined ? !has : !!force;
      if (on) this.add(name); else this.remove(name);
      return on;
    },
    contains(name) { return tokens(el).indexOf(name) >= 0; },
  };
  el.style = new Proxy(el._style, {
    get(target, prop) {
      if (prop === "setProperty") return (name, value) => { target[name] = String(value); };
      if (prop === "removeProperty") return (name) => { delete target[name]; };
      if (prop === "strokeDasharray") return target["stroke-dasharray"] == null ? "" : target["stroke-dasharray"];
      if (prop === "transition") return target.transition == null ? "" : target.transition;
      const value = target[prop];
      return value == null ? "" : value;
    },
    set(target, prop, value) {
      if (prop === "strokeDasharray") target["stroke-dasharray"] = String(value);
      else if (prop === "transition") target.transition = String(value);
      else target[prop] = String(value);
      return true;
    },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    set(html) {
      el._html = String(html);
      el._writes += 1;
      el.children.slice().forEach((child) => child.remove());
      parseInto(el, el._html);
      if (el.tag === "select") el.value = "";
    },
  });
  el.getAttribute = (name) => (Object.prototype.hasOwnProperty.call(el._attr, name) ? el._attr[name] : null);
  el.setAttribute = (name, value) => {
    el._attr[String(name).toLowerCase()] = String(value);
  };
  el.addEventListener = (type, handler) => {
    if (!el._listeners[type]) el._listeners[type] = [];
    el._listeners[type].push(handler);
  };
  el.appendChild = (child) => {
    if (child.parentElement && child.parentElement !== el) child.remove();
    child.parentElement = el;
    child.parent = el;
    if (el.children.indexOf(child) < 0) el.children.push(child);
    connect(child, el.isConnected);
    return child;
  };
  el.remove = () => {
    if (el.parentElement) {
      el.parentElement.children = el.parentElement.children.filter((item) => item !== el);
    }
    el.parentElement = null;
    el.parent = null;
    connect(el, false);
  };
  el.replaceWith = (node) => {
    const parent = el.parentElement;
    if (!parent) return;
    if (node.parentElement) node.remove();
    const index = parent.children.indexOf(el);
    parent.children.splice(index, 1, node);
    el.parentElement = null;
    el.parent = null;
    connect(el, false);
    node.parentElement = parent;
    node.parent = parent;
    connect(node, parent.isConnected);
  };
  el.focus = () => {};
  el.click = () => { fire(el, "click"); };
  el.matches = (selector) => splitSelectors(selector).some((part) => matchesCompound(el, parseCompound(part)));
  el.closest = (selector) => {
    const groups = splitSelectors(selector).map(parseCompound);
    let node = el;
    while (node) {
      if (node.tag && groups.some((spec) => matchesCompound(node, spec))) return node;
      node = node.parentElement;
    }
    return null;
  };
  el.querySelectorAll = (selector) => queryAll(el, selector);
  el.querySelector = (selector) => {
    const found = queryAll(el, selector);
    return found.length ? found[0] : null;
  };
  el.getBoundingClientRect = () => el._rect;
  el.scrollIntoView = (opts) => {
    scrolled.push({
      id: el.id || "",
      eventId: el.dataset && el.dataset.eventId ? el.dataset.eventId : "",
      block: opts && opts.block ? opts.block : "",
      margin: el.style.scrollMarginTop || "",
      connected: el.isConnected === true,
    });
  };
  return el;
}
function fire(el, type, fields) {
  const event = {
    target: el,
    currentTarget: el,
    key: "",
    repeat: false,
    metaKey: false,
    ctrlKey: false,
    prevented: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() {},
  };
  if (fields) Object.keys(fields).forEach((key) => { event[key] = fields[key]; });
  (el._listeners[type] || []).slice().forEach((fn) => fn(event));
  return event;
}
function fireDoc(type) {
  docListeners.filter((item) => item.type === type).forEach((item) => item.handler({}));
}
function fireWindow(type) {
  windowListeners.filter((item) => item.type === type).forEach((item) => item.fn());
}
function byId(id) {
  let found = null;
  walk(root, (el) => { if (el.id === id) found = el; });
  return found;
}
function mount(parent, el) { parent.appendChild(el); return el; }
function leaf(parent, tag, id, attrs) {
  const el = make(tag);
  if (id) { el.id = id; el._attr.id = id; }
  if (attrs) {
    Object.keys(attrs).forEach((key) => {
      if (key === "class") el.className = attrs[key];
      else if (key.slice(0, 5) === "data-") {
        el._attr[key] = attrs[key];
        const dataKey = key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        el.dataset[dataKey] = attrs[key];
      } else el._attr[key] = attrs[key];
    });
  }
  return mount(parent, el);
}
function box(el, top, right, bottom, left) {
  el._rect = { top: top, right: right, bottom: bottom, left: left, width: right - left, height: bottom - top };
}
function badge(view) {
  const link = document.querySelector('#section-nav a[data-v="' + view + '"]');
  const node = link ? link.querySelector(".count") : null;
  if (!node) return null;
  return { text: node.textContent, className: node.className, label: node.getAttribute("aria-label") };
}
function syncLayout() {
  computed.feed.overflowY = "visible";
  computed.composer.position = "static";
  computed.orders.display = "block";
  delete document.documentElement.dataset.fold;
  relayout();
  flushRaf();
}
function splitUrl(url) {
  const text = String(url);
  const cut = text.indexOf("?");
  const path = cut < 0 ? text : text.slice(0, cut);
  const params = new URLSearchParams(cut < 0 ? "" : text.slice(cut + 1));
  const query = {};
  params.forEach((value, key) => { query[key] = value; });
  return { path: path, query: query };
}
function okResult(result) {
  return {
    ok: true,
    status: 200,
    json() { return Promise.resolve({ ok: true, result: result }); },
  };
}
const OPEN = [
  { order_id: "o-open", state: "waiting", phase: "ask", round: 2, repo: "OWNER/NAME", title: "Open <one>", pr_url: "", head: "abc", updated_at: "2026-11-14T00:00:00Z" },
  { order_id: "o-run", state: "running", phase: "work", round: 1, repo: "OWNER/NAME", title: "Running", pr_url: "", head: "def", updated_at: "not-a-time" },
];
const CLOSED = [
  { order_id: "o-done", state: "done", phase: "", round: 3, repo: "OWNER/NAME", title: "Done", pr_url: "", head: "ghi", updated_at: "2026-11-14T00:00:00Z" },
];
const EVENTS = [
  { id: "e1", seq: 2, order_id: "o-open", kind: "question", wake: true, digest: "What now?", phase: "ask", round: 2, check: "lint", acked: false, at: "2026-11-14T00:00:00Z", answers: ["reply"], opinion: { lane: "ok" } },
  { id: "e2", seq: 1, order_id: "o-open", kind: "note", wake: true, digest: "earlier", phase: "", round: "", check: "", acked: true, at: "2026-11-14T00:00:00Z", opinion: null },
  { id: "e3", seq: 3, order_id: "o-run", kind: "note", wake: false, digest: "", phase: "work", round: 1, check: "", acked: false, at: "", opinion: null },
];
function openOrders() {
  if (openMode === "none") return [];
  if (openMode === "running") return OPEN.map((order) => Object.assign({}, order, { state: "running" }));
  return OPEN;
}
function showBody(id) {
  return Object.assign({
    order_id: id,
    state: "waiting",
    phase: "ask",
    holding_check: null,
    round: 2,
    repo: "OWNER/NAME",
    base: "develop",
    title: "Order " + id,
    pr_url: "https://github.com/OWNER/NAME/pull/3",
    head: "abc",
    last_event: { id: "e1", seq: 2, kind: "question", at: "2026-11-14T00:00:00Z", acked: false },
    checkpoint: { event_id: "e1", seq: 2, kind: "question", check: "lint", acked: false, answers: ["reply", "cancel"] },
    last_intent: null,
    checks: [{ key: "lint", status: "failed", source: "local", evidence: "log" }],
    overridable_checks: [{ key: "lint", status: "failed" }, { key: "other", status: "open" }],
    round_limit: { max_rounds: 3, origin: "default", model: "m", limit: 3 },
    skipped_checks: [],
    assessment: null,
  }, showExtra);
}
function responseFor(record) {
  if (failSpec && failSpec.path === record.path) {
    const spec = failSpec;
    if (spec.once) failSpec = null;
    if (spec.kind === "throw") return Promise.reject(new Error(spec.error || "down"));
    if (spec.kind === "http") {
      return Promise.resolve({
        ok: false,
        status: spec.status || 503,
        json() { return Promise.resolve({ ok: false, error: spec.error || "HTTP fail" }); },
      });
    }
    if (spec.kind === "badjson") {
      return Promise.resolve({ ok: true, status: 200, json() { return Promise.reject(new Error("bad json")); } });
    }
    if (spec.kind === "notok") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json() { return Promise.resolve({ ok: false, error: spec.error || "" }); },
      });
    }
    if (spec.kind === "nulljson") {
      return Promise.resolve({ ok: true, status: 200, json() { return Promise.resolve(null); } });
    }
  }
  const path = record.path;
  const query = record.query;
  if (path === "/api/state") {
    return Promise.resolve({
      ok: true,
      status: 200,
      json() {
        return Promise.resolve({
          generated_at: "2026-11-14T00:00:00Z",
          device_id: "device-1",
          login: "octocat",
          usage_overview: [],
        });
      },
    });
  }
  if (path === "/api/orders/schema") return okResult(schemaBody);
  if (path === "/api/orders/list") {
    const listed = query.open_only === "1" ? openOrders() : openOrders().concat(CLOSED);
    return okResult({ orders: listed });
  }
  if (path === "/api/orders/events") {
    let list = EVENTS.slice();
    if (query.wake_only === "1") list = list.filter((ev) => ev.wake === true);
    if (query.unacked === "1") list = list.filter((ev) => ev.acked === false);
    if (query.after != null) list = list.filter((ev) => Number(ev.seq) > Number(query.after));
    return okResult({ cursor: 9, events: list });
  }
  if (path === "/api/orders/show") return okResult(showBody(query.order_id));
  if (path === "/api/orders/cancel" || path === "/api/orders/reply" || path === "/api/orders/done" || path === "/api/orders/override" || path === "/api/orders/ack") {
    return okResult({ intent: path.split("/").pop(), order_id: record.body && record.body.order_id });
  }
  if (path === "/api/orders/report") return okResult(showBody(query.order_id));
  return Promise.resolve({
    ok: false,
    status: 404,
    json() { return Promise.resolve({ ok: false, error: "no route " + path }); },
  });
}
function matchMedia() { return { matches: false }; }
function requestAnimationFrame(fn) { rafs.push(fn); return rafs.length; }
function flushRaf() {
  const batch = rafs.splice(0, rafs.length);
  batch.forEach((fn) => fn());
  return batch.length;
}
function setTimeout(fn, delay) {
  timerSeq += 1;
  timers.set(timerSeq, { fn: fn, delay: delay, cleared: false });
  return timerSeq;
}
function clearTimeout(id) {
  const item = timers.get(id);
  if (item) item.cleared = true;
}
function fireTimer(id) {
  const item = timers.get(id);
  if (!item || item.cleared) return false;
  item.cleared = true;
  item.fn();
  return true;
}
function setInterval(fn, delay) { intervals.push(delay); return intervals.length; }
function addEventListener(type, fn, options) {
  windowListeners.push({ type: type, fn: fn, options: options || null });
}
function DOMRect(x, y, width, height) {
  this.x = x; this.y = y; this.width = width; this.height = height;
  this.left = x; this.top = y; this.right = x + width; this.bottom = y + height;
}
function getComputedStyle(el) {
  const base = computed[el && el.id] || {};
  return {
    position: base.position || "static",
    overflowY: base.overflowY || "visible",
    display: base.display || "block",
    paddingLeft: base.paddingLeft || "0px",
    paddingTop: base.paddingTop || "0px",
  };
}
function fetch(url, init) {
  const parts = splitUrl(url);
  const record = {
    path: parts.path,
    query: parts.query,
    method: init && init.method ? init.method : "GET",
    body: init && init.body ? JSON.parse(init.body) : null,
  };
  calls.push(record);
  if (onCall) onCall(record);
  return responseFor(record);
}
async function drain() {
  for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve));
}
function since(path) {
  const start = calls.length;
  return () => calls.slice(start).filter((item) => item.path === path);
}
const innerWidth = 1000;
const innerHeight = 800;
const location = { hash: "", search: "", pathname: "/" };
const history = {
  replaceState(state, title, url) {
    historyLog.push(String(url));
    if (String(url).charAt(0) === "#") location.hash = String(url);
  },
};
const navigator = {
  clipboard: { writeText() { return Promise.resolve(); } },
};
const window = { viewport: undefined };
const root = make("html");
root.isConnected = true;
const document = {
  activeElement: null,
  documentElement: root,
  getElementById(id) { return byId(id); },
  querySelector(selector) {
    const found = queryAll(root, selector);
    return found.length ? found[0] : null;
  },
  querySelectorAll(selector) { return queryAll(root, selector); },
  createElement(tag) { return make(tag); },
  addEventListener(type, handler, capture) {
    docListeners.push({ type: type, handler: handler, capture: capture === true });
  },
};
const nav = leaf(root, "nav", "section-nav");
const marker = leaf(nav, "div", "marker");
leaf(nav, "div", "", { class: "group" });
const ordersLink = leaf(nav, "a", "", { "data-v": "orders", class: "active" });
ordersLink.offsetTop = 12;
const eventsLink = leaf(nav, "a", "", { "data-v": "events" });
eventsLink.offsetTop = 48;
const child = leaf(eventsLink, "span", "");
leaf(nav, "a", "", { href: "/tasks" });
leaf(root, "b", "login");
leaf(root, "code", "dev");
leaf(root, "span", "stamp");
leaf(root, "span", "usage-summary");
leaf(root, "p", "err").hidden = true;
const ordersView = leaf(root, "section", "v-orders", { class: "view on" });
leaf(ordersView, "div", "", { class: "fold-grid" });
const seg = leaf(ordersView, "div", "seg");
leaf(seg, "span", "", { class: "knob" });
const openButton = leaf(seg, "button", "", { "data-f": "open", class: "on" });
const allButton = leaf(seg, "button", "", { "data-f": "all" });
leaf(ordersView, "ul", "orders");
leaf(ordersView, "p", "orders-note").hidden = true;
const eventsView = leaf(root, "section", "v-events", { class: "view" });
leaf(eventsView, "input", "ev-wake");
leaf(eventsView, "input", "ev-unacked");
leaf(eventsView, "table", "order-events");
leaf(root, "div", "conv-head");
leaf(root, "h2", "conv-title");
leaf(root, "span", "conv-repo");
leaf(root, "span", "conv-pr");
leaf(root, "span", "conv-round");
leaf(root, "span", "conv-phase");
leaf(root, "div", "conv-state");
const convHead = document.getElementById("conv-head");
["conv-title", "conv-repo", "conv-pr", "conv-round", "conv-phase", "conv-state"].forEach((id) => {
  convHead.appendChild(document.getElementById(id));
});
const composer = leaf(root, "form", "composer");
composer.hidden = true;
composer.offsetHeight = 40;
leaf(composer, "textarea", "od-reply-body");
leaf(composer, "select", "od-override-check");
leaf(composer, "input", "od-override-reason");
leaf(composer, "button", "od-override");
leaf(composer, "input", "od-done-reason");
leaf(composer, "button", "od-done");
leaf(composer, "button", "od-report");
leaf(composer, "input", "od-cancel-reason");
const cancelButton = leaf(composer, "button", "od-cancel");
leaf(composer, "button", "od-reply");
const feed = leaf(ordersView, "div", "feed", { class: "feed" });
leaf(feed, "p", "conv-empty");
const detail = leaf(feed, "div", "order-detail");
detail.hidden = true;
const feedEvents = leaf(detail, "div", "feed-events");
const odBody = leaf(detail, "div", "od-body");
const orderReport = leaf(detail, "div", "order-report");
leaf(orderReport, "div", "od-report-body");
leaf(detail, "p", "od-err").hidden = true;
const park = leaf(detail, "div", "feed-park");
park.hidden = true;
const ask = leaf(park, "div", "feed-ask");
leaf(ask, "pre", "od-question").hidden = true;
leaf(ask, "div", "od-opinion");
leaf(ask, "div", "ask-foot");
leaf(detail, "code", "od-id");
`;
const scenario = String.raw`
async function scenario() {
  const bootLayoutKey = layoutKey();
  const bootLastLayout = lastLayout;
  await drain();
  flushRaf();
  const ordersLink = document.querySelector('#section-nav a[data-v="orders"]');
  const eventsLink = document.querySelector('#section-nav a[data-v="events"]');
  const boot = {
    calls: calls.map((item) => ({ path: item.path, query: item.query, method: item.method })),
    ordersHtml: document.getElementById("orders").innerHTML,
    ordersElapsed: [...document.getElementById("orders").querySelectorAll("span.since")].map((span) => span.textContent),
    eventsHtml: document.getElementById("order-events").innerHTML,
    ordersBadge: badge("orders"),
    eventsBadge: badge("events"),
    note: { hidden: document.getElementById("orders-note").hidden, text: document.getElementById("orders-note").textContent },
    login: document.getElementById("login").textContent,
    usage: document.getElementById("usage-summary").innerHTML,
    dev: document.getElementById("dev").innerHTML,
    stamp: document.getElementById("stamp").textContent,
    errHidden: document.getElementById("err").hidden,
    intervals: intervals.slice(),
    ordersOn: document.getElementById("v-orders").classList.contains("on"),
    eventsOn: document.getElementById("v-events").classList.contains("on"),
    ordersActive: ordersLink.classList.contains("active"),
    marker: document.getElementById("marker")._style["--y"] || "",
    layout: layoutKey(),
    bootLayoutKey: bootLayoutKey,
    bootLastLayout: bootLastLayout,
    openOnly: ordersOpenOnly,
  };

  const savedFields = ORDER_VIEW.list.nested.orders.slice();
  const sample = { order_id: "x", title: "A<B", repo: "OWNER/NAME", state: "waiting", updated_at: "2026-11-14T00:00:00Z", head: "abc" };
  const listFull = listItemsHtml([sample, { order_id: "y", title: "T", repo: "OWNER/NAME", state: "", updated_at: "nope" }]);
  ORDER_VIEW.list.nested.orders = ["order_id"];
  const listBare = listItemsHtml([sample]);
  const listEmpty = listItemsHtml([]);
  ORDER_VIEW.list.nested.orders = savedFields;

  const icons = {
    none: stateIcon(null),
    undef: stateIcon(undefined),
    empty: stateIcon(""),
    merged: stateIcon("merged"),
    mergedCase: stateIcon("Merged"),
    mystery: stateIcon("mystery"),
    ja: stateIcon("ja"),
    localCheck: stateIcon("local-check"),
  };
  const ring = {
    waiting: ringWord("waiting"),
    none: ringWord(null),
    undef: ringWord(undefined),
    empty: ringWord(""),
    other: ringWord("running"),
    number: ringWord(2),
  };
  const bits = {
    none: eventBit(null),
    undef: eventBit(undefined),
    empty: eventBit(""),
    object: eventBit({ a: 1 }),
    text: eventBit("a&b"),
    number: eventBit(2),
  };
  const fact = eventFact({ kind: "question", phase: "", round: 2, check: null });
  const factEmpty = eventFact({ kind: "", phase: null, round: undefined, check: "" });
  const articles = {
    ask: eventArticle({ id: 'a"b<c', kind: "question", phase: "ask", round: 1, check: "lint" }, true),
    digest: eventArticle({ id: "e1", kind: "note", digest: "x<y" }, false),
    emptyDigest: eventArticle({ id: "e2", digest: "" }, false),
    objectDigest: eventArticle({ id: "e3", digest: { a: 1 } }, false),
    noFact: eventArticle({ id: "e4" }, false),
  };
  const feet = {
    full: askFootHtml({ check: "a<b", answers: ["y<z", "ok"] }, { at: "2026-11-14T00:00:00Z" }),
    noEv: askFootHtml({ check: "lint", answers: ["reply"] }, null),
    badTime: askFootHtml({ check: "lint", answers: [] }, { at: "nope" }),
    noCheck: askFootHtml({ check: "", answers: "nope" }, { at: "2026-11-14T00:00:00Z" }),
    numberCheck: askFootHtml({ check: 4, answers: null }, null),
  };

  setCount("missing", 4, true);
  const missingCount = badge("missing");
  setCount("orders", 2, true);
  const attnCount = badge("orders");
  setCount("orders", 4, false);
  const plainCount = badge("orders");
  setCount("orders", 0, false);
  const zeroCount = badge("orders");
  setCount("events", 1, false);

  flushRaf();
  const hiddenHeadRafs = rafs.length;
  showHead(null);
  const hiddenHead = {
    rafs: rafs.length - hiddenHeadRafs,
    title: document.getElementById("conv-title").textContent,
    repo: document.getElementById("conv-repo").textContent,
    round: document.getElementById("conv-round").textContent,
    phase: document.getElementById("conv-phase").textContent,
    state: document.getElementById("conv-state").innerHTML,
    pr: document.getElementById("conv-pr").innerHTML,
  };
  function headOf(shown) {
    flushRaf();
    const before = rafs.length;
    showHead(shown);
    return {
      rafs: rafs.length - before,
      title: document.getElementById("conv-title").textContent,
      repo: document.getElementById("conv-repo").textContent,
      round: document.getElementById("conv-round").textContent,
      phase: document.getElementById("conv-phase").textContent,
      state: document.getElementById("conv-state").innerHTML,
    };
  }
  const heads = {
    hidden: hiddenHead,
    roundNull: headOf({ title: "T", repo: "OWNER/NAME", round: null, phase: "", state: "" }),
    roundUndef: headOf({ title: "", repo: "", round: undefined, phase: null, state: null }),
    roundEmpty: headOf({ round: "", phase: "", state: "" }),
    roundZero: headOf({ title: "T", round: 0, phase: "ask", state: "running" }),
    waiting: headOf({ title: "Order o-open", repo: "OWNER/NAME", round: 2, phase: "ask", state: "waiting" }),
  };
  setHtml(document.getElementById("conv-pr"), "#3");
  showHead(null);
  const clearedHead = {
    title: document.getElementById("conv-title").textContent,
    repo: document.getElementById("conv-repo").textContent,
    round: document.getElementById("conv-round").textContent,
    phase: document.getElementById("conv-phase").textContent,
    state: document.getElementById("conv-state").innerHTML,
    pr: document.getElementById("conv-pr").innerHTML,
  };

  const askNode = document.getElementById("feed-ask");
  const parkNode = document.getElementById("feed-park");
  const host = document.getElementById("feed-events");
  let parkAppends = 0;
  const parkAppend = parkNode.appendChild;
  parkNode.appendChild = function (child) {
    parkAppends += 1;
    return parkAppend.call(parkNode, child);
  };
  host.appendChild(askNode);
  const moved = askNode.parentElement === host;
  const appendsBefore = parkAppends;
  parkAsk();
  const parked = askNode.parentElement === parkNode;
  const appendsAfterMove = parkAppends - appendsBefore;
  const parkKids = parkNode.children.length;
  parkAsk();
  const parkedAgain = askNode.parentElement === parkNode && parkNode.children.length === parkKids;
  const appendsAgain = parkAppends - appendsBefore - appendsAfterMove;

  const scratch = document.getElementById("od-report-body");
  setHtml(scratch, "A");
  const once = scratch._writes;
  setHtml(scratch, "A");
  const skippedWrite = scratch._writes === once;
  replaceHtml(scratch, "A");
  const forcedWrite = scratch._writes === once + 1;

  syncLayout();
  const link = ordersLink;
  const eventsA = eventsLink;
  const markerEl = document.getElementById("marker");
  link.offsetTop = 12;
  eventsA.offsetTop = 48;
  showView("events");
  const eventsView = {
    ordersOn: document.getElementById("v-orders").classList.contains("on"),
    eventsOn: document.getElementById("v-events").classList.contains("on"),
    ordersActive: link.classList.contains("active"),
    eventsActive: eventsA.classList.contains("active"),
    marker: markerEl._style["--y"] || "",
  };
  const items = [];
  for (let i = 0; i < 15; i += 1) items.push('<li class="order"></li>');
  document.getElementById("orders").innerHTML = items.join("");
  shownHtml.delete(document.getElementById("orders"));
  [...document.getElementById("feed").children].forEach((el) => el.classList.remove("jk-enter"));
  flushRaf();
  const rafBefore = rafs.length;
  showView("orders");
  const entered = [...document.getElementById("orders").children].map((el) => el.classList.contains("jk-enter"));
  const feedEntered = [...document.getElementById("feed").children].map((el) => el.classList.contains("jk-enter"));
  const ordersViewOn = {
    marker: markerEl._style["--y"] || "",
    entered: entered,
    feedEntered: feedEntered,
    rafs: rafs.length - rafBefore,
    ordersOn: document.getElementById("v-orders").classList.contains("on"),
    eventsOn: document.getElementById("v-events").classList.contains("on"),
  };
  const yBefore = markerEl._style["--y"] || "";
  markerEl.remove();
  showView("orders");
  const markerMissing = { thrown: false, y: markerEl._style["--y"] || "", found: document.getElementById("marker") };
  document.getElementById("section-nav").appendChild(markerEl);
  link.remove();
  markerEl._style["--y"] = "stay";
  showView("orders");
  const activeMissing = markerEl._style["--y"] || "";
  document.getElementById("section-nav").appendChild(link);
  showView("orders");

  const navEl = document.getElementById("section-nav");
  const group = navEl.querySelector(".group");
  const tasks = navEl.querySelector('a[href="/tasks"]');
  const hist = historyLog.length;
  const missGroup = fire(navEl, "click", { target: group });
  const missTasks = fire(navEl, "click", { target: tasks });
  const historyAfterMiss = historyLog.length === hist;
  const span = eventsA.querySelector("span");
  const hit = fire(navEl, "click", { target: span });
  const nav = {
    groupPrevented: missGroup.prevented,
    tasksPrevented: missTasks.prevented,
    historyAfterMiss: historyAfterMiss,
    hitPrevented: hit.prevented,
    url: historyLog[historyLog.length - 1] || "",
    hash: location.hash,
    eventsOn: document.getElementById("v-events").classList.contains("on"),
  };
  showView("orders");
  location.hash = "";

  document.documentElement.dataset.fold = "laptop";
  location.hash = "#events";
  fireWindow("hashchange");
  const hashEvents = {
    eventsOn: document.getElementById("v-events").classList.contains("on"),
    ordersOn: document.getElementById("v-orders").classList.contains("on"),
    fold: document.documentElement.dataset.fold || "",
  };
  location.hash = "#orders";
  fireWindow("hashchange");
  const hashOrders = document.getElementById("v-orders").classList.contains("on");
  location.hash = "";

  link.offsetTop = 21;
  fireWindow("resize");
  const resizeY = markerEl._style["--y"] || "";
  document.querySelectorAll("#section-nav a").forEach((anchor) => anchor.classList.remove("active"));
  markerEl._style["--y"] = "keep";
  fireWindow("resize");
  const resizeNoActive = markerEl._style["--y"] || "";
  showView("orders");
  markerEl.remove();
  let resizeThrew = false;
  try { fireWindow("resize"); } catch (error) { resizeThrew = true; }
  navEl.appendChild(markerEl);
  showView("orders");

  const baseKey = layoutKey();
  document.documentElement.dataset.fold = "book";
  const foldKey = layoutKey();
  delete document.documentElement.dataset.fold;
  computed.feed.overflowY = "auto";
  const flowKey = layoutKey();
  computed.feed.overflowY = "visible";
  computed.composer.position = "sticky";
  const stickyKey = layoutKey();
  computed.composer.position = "static";
  computed.orders.display = "flex";
  const displayKey = layoutKey();
  computed.orders.display = "block";
  syncLayout();
  flushRaf();
  const sameBefore = rafs.length;
  relayout();
  const sameRaf = rafs.length - sameBefore;
  computed.orders.display = "flex";
  relayout();
  const changedRaf = rafs.length - sameBefore;
  relayout();
  const secondRaf = rafs.length - sameBefore;
  flushRaf();
  syncLayout();

  nowMs += 1000;
  parkNode.hidden = true;
  host.innerHTML = '<article data-event-id="top"></article>';
  feedHtml = host.innerHTML;
  const article = host.children[0];
  box(article, 0, 10, 30, 0);
  box(document.getElementById("feed"), 0, 0, 0, 0);
  box(composer, 0, 0, 0, 0);
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 0, 0, 0);
  box(parkNode, 0, 0, 200, 0);
  const topPlain = topMessage();
  box(article, 0, 10, 5, 0);
  const topNone = topMessage();
  computed.composer.position = "sticky";
  box(composer, 0, 0, 100, 0);
  box(article, 0, 10, 105, 0);
  const topStickyMiss = topMessage();
  box(article, 0, 10, 120, 0);
  const topStickyHit = topMessage();
  computed.composer.position = "static";
  box(article, 0, 10, 0, 0);
  box(odBody, 0, 0, 40, 0);
  const topBody = topMessage();
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 0, 40, 0);
  const topReport = topMessage();
  box(orderReport, 0, 0, 0, 0);
  parkNode.hidden = false;
  box(parkNode, 0, 0, 80, 0);
  const topPark = topMessage();
  parkNode.hidden = true;
  box(parkNode, 0, 0, 80, 0);
  const topParkHidden = topMessage();
  const tops = {
    plain: topPlain && topPlain.dataset.eventId,
    none: topNone,
    stickyMiss: topStickyMiss,
    stickyHit: topStickyHit && topStickyHit.dataset.eventId,
    body: topBody && topBody.id,
    report: topReport && topReport.id,
    park: topPark && topPark.id,
    parkHidden: topParkHidden,
  };
  syncLayout();

  host.innerHTML = "";
  feedHtml = "";
  box(article, 0, 0, 0, 0);
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 0, 0, 0);
  parkNode.hidden = true;
  nowMs += 1000;
  fireDoc("scroll");
  const list = document.getElementById("orders");
  list.innerHTML = '<li class="order sel"></li>';
  shownHtml.delete(list);
  const item = list.querySelector(".order.sel");
  box(list, 50, 300, 200, 100);
  function placeItem(top, right, bottom, left) {
    box(item, top, right, bottom, left);
    list.scrollLeft = 0;
    list.scrollTop = 0;
    const before = scrolled.length;
    keepPlace();
    return { left: list.scrollLeft, top: list.scrollTop, scrolled: scrolled.length - before };
  }
  const leftOf = placeItem(60, 140, 90, 80);
  const rightOf = placeItem(60, 360, 90, 280);
  const above = placeItem(10, 200, 40, 120);
  const below = placeItem(180, 200, 250, 120);
  const inside = placeItem(60, 200, 90, 120);
  item.className = "order";
  box(item, 10, 400, 300, 0);
  list.scrollLeft = 5;
  list.scrollTop = 5;
  const beforeNone = scrolled.length;
  keepPlace();
  const noSel = { left: list.scrollLeft, top: list.scrollTop, scrolled: scrolled.length - beforeNone };
  item.className = "order sel";

  box(odBody, 0, 10, 40, 0);
  box(orderReport, 0, 0, 0, 0);
  parkNode.hidden = true;
  host.innerHTML = "";
  nowMs += 1000;
  fireDoc("scroll");
  const beforeBody = scrolled.length;
  keepPlace();
  const bodyScroll = scrolled[scrolled.length - 1];
  computed.composer.position = "sticky";
  keepPlace();
  const stickyScroll = scrolled[scrolled.length - 1];
  computed.composer.position = "static";
  const bodyScrolled = scrolled.length - beforeBody;

  relayout();
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 10, 40, 0);
  nowMs += 400;
  fireDoc("scroll");
  keepPlace();
  const duringGuard = scrolled[scrolled.length - 1];
  nowMs += 1;
  fireDoc("scroll");
  keepPlace();
  const afterGuard = scrolled[scrolled.length - 1];
  syncLayout();

  nowMs = NativeDate.parse("2026-11-14T00:05:00Z");
  const feedEvents = [
    { id: "e1", seq: 2, kind: "question", phase: "ask", round: 2, check: "lint", digest: "What <now>", at: "2026-11-14T00:00:00Z" },
    { id: "e0", seq: "x", kind: "note", phase: "", round: "", check: "", digest: "", at: "" },
    { id: "e2", seq: 1, kind: "note", phase: "work", round: 1, check: "", digest: "a<b", at: "2026-11-14T00:00:00Z" },
  ];
  const checkpoint = { event_id: "e1", seq: 2, check: "lint", answers: ["reply", "a<b"] };
  renderFeed(feedEvents, checkpoint);
  const firstHtml = host.innerHTML;
  const firstWrites = host._writes;
  const firstParent = askNode.parentElement;
  const firstFoot = document.getElementById("ask-foot").innerHTML;
  renderFeed(feedEvents, checkpoint);
  const skipWrites = host._writes === firstWrites;
  const skipParent = askNode.parentElement === firstParent;
  nowMs += 60000;
  const footBefore = document.getElementById("ask-foot")._writes;
  renderFeed(feedEvents, checkpoint);
  const footHtml = document.getElementById("ask-foot").innerHTML;
  const footWrote = document.getElementById("ask-foot")._writes === footBefore + 1;
  const stillWrites = host._writes === firstWrites;
  const e2article = [...host.children].find((el) => el.dataset.eventId === "e2");
  e2article.appendChild(askNode);
  const placedElsewhere = askNode.closest("article").dataset.eventId;
  const movedFrom = host._writes;
  renderFeed(feedEvents, checkpoint);
  const movedQuestion = {
    placed: placedElsewhere,
    wrote: host._writes === movedFrom + 1,
    eventId: askNode.closest("article").dataset.eventId,
  };
  const beforeExtra = host._writes;
  const beforeArticle = askNode.closest("article");
  renderFeed(feedEvents.concat([{ id: "e9", seq: 5, kind: "note", digest: "new", phase: "", round: "", check: "" }]), checkpoint);
  const rewroteArticle = askNode.closest("article");
  const rewrote = host._writes === beforeExtra + 1 && beforeArticle.isConnected === false && rewroteArticle.dataset.eventId === "e1";
  renderFeed([{ id: "e2", seq: 1, kind: "note", digest: "z", phase: "", round: "", check: "" }], { event_id: "missing\"id", seq: 3, check: "", answers: [] });
  const missingHtml = host.innerHTML;
  const missingArticle = askNode.closest("article");
  const missingParent = missingArticle && missingArticle.dataset.eventId;
  const missingFoot = document.getElementById("ask-foot").innerHTML;
  const quietWrites = host._writes;
  renderFeed([{ id: "e2", seq: 1, kind: "note", digest: "z", phase: "", round: "", check: "" }], null);
  const noCheckpoint = {
    parent: askNode.parentElement.id,
    askClass: host.innerHTML.indexOf("msg ask") >= 0,
    foot: document.getElementById("ask-foot").innerHTML,
    wrote: host._writes > quietWrites,
  };
  const quiet2 = host._writes;
  renderFeed([{ id: "e2", seq: 1, kind: "note", digest: "z", phase: "", round: "", check: "" }], null);
  const noCheckpointSkip = host._writes === quiet2;

  syncLayout();
  nowMs += 1000;
  const remembered = [
    { id: "m1", seq: 2, kind: "note", digest: "one", phase: "", round: "", check: "" },
    { id: "m2", seq: 1, kind: "note", digest: "two", phase: "", round: "", check: "" },
  ];
  renderFeed(remembered, null);
  const m1 = [...host.children].find((el) => el.dataset.eventId === "m1");
  box(document.getElementById("feed"), 0, 0, 0, 0);
  box(composer, 0, 0, 0, 0);
  box(m1, 0, 20, 40, 0);
  [...host.children].forEach((el) => { if (el !== m1) box(el, 0, 0, 0, 0); });
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 0, 0, 0);
  parkNode.hidden = true;
  fireDoc("scroll");
  const stableWrites = host._writes;
  const stableParent = askNode.parentElement;
  renderFeed(remembered, null);
  const stable = host._writes === stableWrites && askNode.parentElement === stableParent;
  renderFeed(remembered.concat([{ id: "m3", seq: 4, kind: "note", digest: "three", phase: "", round: "", check: "" }]), null);
  const oldGone = m1.isConnected === false;
  computed.composer.position = "sticky";
  composer.offsetHeight = 40;
  flushRaf();
  fireWindow("resize");
  const queued = rafs.length;
  flushRaf();
  const landed = scrolled[scrolled.length - 1];
  renderFeed([{ id: "m2", seq: 1, kind: "note", digest: "two", phase: "", round: "", check: "" }], null);
  const scrollCount = scrolled.length;
  keepPlace();
  const keyGone = scrolled.length === scrollCount;

  parkNode.hidden = false;
  host.innerHTML = "";
  feedHtml = "";
  box(document.getElementById("feed"), 0, 0, 0, 0);
  box(composer, 0, 0, 0, 0);
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 0, 0, 0);
  box(parkNode, 0, 0, 80, 0);
  nowMs += 1000;
  fireDoc("scroll");
  const parkAnchor = anchor;
  computed.orders.display = "grid";
  fireWindow("resize");
  flushRaf();
  const parkLanded = scrolled[scrolled.length - 1];
  const idScrolls = scrolled.length;
  parkNode.remove();
  keepPlace();
  const idGone = scrolled.length === idScrolls;
  detail.appendChild(parkNode);
  parkNode.hidden = true;
  box(odBody, 0, 0, 0, 0);
  box(orderReport, 0, 0, 0, 0);
  box(parkNode, 0, 0, 0, 0);
  host.innerHTML = "";
  feedHtml = "";
  nowMs += 1000;
  fireDoc("scroll");
  const noKey = anchor === null;
  box(odBody, 0, 0, 80, 0);
  const noneScrolls = scrolled.length;
  keepPlace();
  const noScroll = scrolled.length === noneScrolls;
  syncLayout();

  flushRaf();
  const foldList = document.getElementById("orders");
  foldList.innerHTML = '<li class="order sel"></li>';
  shownHtml.delete(foldList);
  const foldItem = foldList.querySelector(".order.sel");
  box(foldList, 0, 100, 40, 0);
  box(foldItem, 0, 200, 20, 150);
  foldList.scrollLeft = 0;
  location.search = "?fold=book";
  const foldFramesBefore = rafs.length;
  fireWindow("resize");
  const foldFrames = rafs.length - foldFramesBefore;
  flushRaf();
  const foldWatch = {
    frames: foldFrames,
    scrollLeft: foldList.scrollLeft,
    fold: document.documentElement.dataset.fold || "",
    lastLayout: lastLayout,
    layoutKey: layoutKey(),
  };
  location.search = "";

  ordersStickyNote = "";
  orderStickyError = "";
  document.getElementById("ev-wake").checked = false;
  document.getElementById("ev-unacked").checked = false;
  showExtra = {};
  selectedOrderId = null;
  const listBeforeAll = since("/api/orders/list");
  fire(document.getElementById("seg"), "click", { target: document.querySelector("#seg .knob") });
  const knobCalls = listBeforeAll().length;
  allButton.dataset.f = "";
  fire(document.getElementById("seg"), "click", { target: allButton });
  const blankCalls = listBeforeAll().length;
  allButton.dataset.f = "all";
  fire(document.getElementById("seg"), "click", { target: allButton });
  await drain();
  const allCalls = listBeforeAll();
  const allHtml = document.getElementById("orders").innerHTML;
  const allEntered = [...document.getElementById("orders").children].every((el) => el.classList.contains("jk-enter"));
  const allKnob = document.querySelector("#seg .knob")._style["--x"] || "";
  const allOn = allButton.classList.contains("on") && !openButton.classList.contains("on");
  fire(document.getElementById("seg"), "click", { target: openButton });
  await drain();
  const openCalls = calls.filter((item) => item.path === "/api/orders/list").slice(-1)[0];
  const openHtml = document.getElementById("orders").innerHTML;
  const openKnob = document.querySelector("#seg .knob")._style["--x"] || "";

  document.getElementById("ev-wake").checked = true;
  document.getElementById("ev-unacked").checked = true;
  const bothFrom = since("/api/orders/events");
  await ordersTick();
  const bothEvents = bothFrom();
  document.getElementById("ev-unacked").checked = false;
  const wakeFrom = since("/api/orders/events");
  await ordersTick();
  const wakeEvents = wakeFrom();
  document.getElementById("ev-wake").checked = false;
  document.getElementById("ev-unacked").checked = true;
  const unackedFrom = since("/api/orders/events");
  await ordersTick();
  const unackedEvents = unackedFrom();
  document.getElementById("ev-unacked").checked = false;

  openMode = "none";
  await ordersTick();
  const emptyBadge = badge("orders");
  const emptyHtml = document.getElementById("orders").innerHTML;
  openMode = "running";
  await ordersTick();
  const runningBadge = badge("orders");
  openMode = "normal";
  await ordersTick();

  failSpec = { path: "/api/orders/list", kind: "throw", error: "list down", once: true };
  await ordersTick();
  const listFail = document.getElementById("orders-note").textContent;
  failSpec = { path: "/api/orders/events", kind: "http", error: "events down", status: 503, once: true };
  await ordersTick();
  const eventsFail = document.getElementById("orders-note").textContent;

  orderSchema = null;
  failSpec = { path: "/api/orders/schema", kind: "http", error: "schema broke", status: 503, once: true };
  await ordersTick();
  const schemaHttp = { note: document.getElementById("orders-note").textContent, schema: orderSchema };
  orderSchema = null;
  failSpec = { path: "/api/orders/schema", kind: "badjson", once: true };
  await ordersTick();
  const schemaBad = document.getElementById("orders-note").textContent;
  orderSchema = null;
  failSpec = { path: "/api/orders/schema", kind: "notok", error: "nope", once: true };
  await ordersTick();
  const schemaNotOk = document.getElementById("orders-note").textContent;
  orderSchema = null;
  failSpec = { path: "/api/orders/schema", kind: "nulljson", once: true };
  await ordersTick();
  const schemaNull = document.getElementById("orders-note").textContent;
  orderSchema = null;
  schemaBody = { enabled: false };
  await ordersTick();
  const disabledNote = document.getElementById("orders-note").textContent;
  orderSchema = null;
  failSpec = { path: "/api/orders/list", kind: "throw", error: "list down", once: true };
  await ordersTick();
  const failureWins = document.getElementById("orders-note").textContent;
  schemaBody = { enabled: true };
  orderSchema = null;
  const schemaCount = calls.filter((item) => item.path === "/api/orders/schema").length;
  await ordersTick();
  const schemaFetched = calls.filter((item) => item.path === "/api/orders/schema").length === schemaCount + 1;
  const enabledNote = document.getElementById("orders-note").textContent;
  await ordersTick();
  const schemaSkipped = calls.filter((item) => item.path === "/api/orders/schema").length === schemaCount + 1;

  await ordersTick();
  const beforeClick = selectedOrderId;
  fire(document.getElementById("orders"), "click", { target: document.getElementById("orders") });
  const ignoredClick = selectedOrderId === beforeClick;
  flushRaf();
  flushRaf();
  const ringMark = rafs.length;
  const bold = document.getElementById("orders").querySelector("b");
  fire(document.getElementById("orders"), "click", { target: bold });
  await drain();
  const pickedRows = [...document.getElementById("orders").children].map((row) => ({
    id: row.dataset.id || "",
    className: row.className,
  }));
  const pickedArcs = [...document.getElementById("orders").querySelectorAll(".ring .arc")];
  const arcsBefore = pickedArcs.map((arc) => ({
    transition: arc.style.transition,
    dash: arc.style.strokeDasharray,
  }));
  const ringFrames = rafs.length - ringMark;
  flushRaf();
  flushRaf();
  const arcsAfter = pickedArcs.map((arc) => ({
    transition: arc.style.transition,
    dash: arc.style.strokeDasharray,
  }));
  const picked = {
    rows: pickedRows,
    ringFrames: ringFrames,
    arcsBefore: arcsBefore,
    arcsAfter: arcsAfter,
  };
  const selected = {
    id: selectedOrderId,
    title: document.getElementById("conv-title").textContent,
    repo: document.getElementById("conv-repo").textContent,
    round: document.getElementById("conv-round").textContent,
    phase: document.getElementById("conv-phase").textContent,
    state: document.getElementById("conv-state").innerHTML,
    body: document.getElementById("od-body").innerHTML,
    feed: host.innerHTML,
    question: document.getElementById("od-question").textContent,
    questionHidden: document.getElementById("od-question").hidden,
    opinion: document.getElementById("od-opinion").innerHTML,
    replyDisabled: document.getElementById("od-reply").disabled,
    bodyDisabled: document.getElementById("od-reply-body").disabled,
    cancelDisabled: document.getElementById("od-cancel").disabled,
    doneDisabled: document.getElementById("od-done").disabled,
    overrideDisabled: document.getElementById("od-override").disabled,
    detailHidden: document.getElementById("order-detail").hidden,
    composerHidden: document.getElementById("composer").hidden,
    emptyHidden: document.getElementById("conv-empty").hidden,
    odId: document.getElementById("od-id").innerHTML,
    after: calls.filter((item) => item.path === "/api/orders/events" && item.query.after).slice(-1)[0],
  };
  flushRaf();
  flushRaf();
  const listWrites = list._writes;
  const stableArc = list.querySelector(".ring .arc");
  await ordersTick();
  const stableRow = [...list.children].find((row) => row.dataset.id === "o-open");
  const stableArcNow = list.querySelector(".ring .arc");
  const unchangedList = {
    writes: list._writes - listWrites,
    className: stableRow.className,
    transition: stableArcNow.style.transition,
    dash: stableArcNow.style.strokeDasharray,
    sameArc: stableArcNow === stableArc,
  };
  flushRaf();
  flushRaf();
  const savedSelection = selectedOrderId;
  selectedOrderId = null;
  openMode = "running";
  flushRaf();
  flushRaf();
  const changedWrites = list._writes;
  const changedRafs = rafs.length;
  await ordersTick();
  const changedArc = list.querySelector(".ring .arc");
  const changedFrames = rafs.length - changedRafs;
  const changedTransition = changedArc.style.transition;
  const changedDash = changedArc.style.strokeDasharray;
  flushRaf();
  const changedNested = rafs.length;
  flushRaf();
  const changedList = {
    writes: list._writes - changedWrites,
    frames: changedFrames,
    nested: changedNested,
    transition: changedTransition,
    dash: changedDash,
  };
  openMode = "normal";
  selectedOrderId = savedSelection;
  await ordersTick();
  flushRaf();
  flushRaf();
  const elapsedSavedAt = OPEN[0].updated_at;
  const elapsedSavedTitle = OPEN[0].title;
  OPEN[0].updated_at = new NativeDate(nowMs - 30000).toISOString();
  await ordersTick();
  const elapsedBefore = list.querySelector("span.since").textContent;
  flushRaf();
  flushRaf();
  const elapsedWrites = list._writes;
  const elapsedRafs = rafs.length;
  nowMs += 2000;
  await ordersTick();
  const elapsedTick = {
    before: elapsedBefore,
    after: list.querySelector("span.since").textContent,
    writes: list._writes - elapsedWrites,
    frames: rafs.length - elapsedRafs,
  };
  nowMs -= 2000;
  OPEN[0].updated_at = elapsedSavedAt;
  await ordersTick();
  flushRaf();
  flushRaf();
  const titleWrites = list._writes;
  const titleRafs = rafs.length;
  OPEN[0].title = "Renamed title";
  await ordersTick();
  const titleTick = {
    writes: list._writes - titleWrites,
    frames: rafs.length - titleRafs,
  };
  OPEN[0].title = elapsedSavedTitle;
  await ordersTick();
  flushRaf();
  flushRaf();
  const headRafs = rafs.length;
  await ordersTick();
  const headUnchangedFrames = rafs.length - headRafs;
  showExtra = { state: "running" };
  const headChangeRafs = rafs.length;
  await ordersTick();
  const headChangedFrames = rafs.length - headChangeRafs;
  showExtra = {};
  await ordersTick();
  flushRaf();
  flushRaf();
  document.getElementById("od-reply-body").value = "kept";
  shownHtml.delete(document.getElementById("od-override-check"));
  document.getElementById("od-override-check").value = "lint";
  selectOrder("o-open");
  await drain();
  const sameId = {
    reply: document.getElementById("od-reply-body").value,
    check: document.getElementById("od-override-check").value,
    options: document.getElementById("od-override-check").innerHTML,
  };
  document.getElementById("od-reply-body").value = "gone";
  document.getElementById("od-cancel-reason").value = "why";
  fire(cancelButton, "pointerdown");
  const holdId = cancelHoldTimer;
  let clearedDuring = null;
  onCall = (record) => {
    if (clearedDuring === null && record.path === "/api/orders/list") {
      clearedDuring = { html: host.innerHTML, mark: feedHtml };
    }
  };
  selectOrder("o-run");
  await drain();
  onCall = null;
  const otherId = {
    id: selectedOrderId,
    reply: document.getElementById("od-reply-body").value,
    reason: document.getElementById("od-cancel-reason").value,
    title: document.getElementById("conv-title").textContent,
    holding: cancelButton.classList.contains("is-holding"),
    timerCleared: timers.get(holdId).cleared === true,
    feed: host.innerHTML,
    clearedHtml: clearedDuring.html,
    clearedMark: clearedDuring.mark,
  };
  list.innerHTML = '<table><tr class="order-row" data-id="o-row"><td>row</td></tr></table>';
  shownHtml.delete(list);
  const row = list.querySelector("tr.order-row");
  fire(list, "click", { target: row });
  await drain();
  const rowSelected = selectedOrderId;

  selectedOrderId = "o-open";
  showExtra = {};
  document.getElementById("conv-title").textContent = "stale";
  onCall = (record) => { if (record.path === "/api/orders/show") selectedOrderId = "gone"; };
  await ordersTick();
  const showDropped = document.getElementById("conv-title").textContent;
  onCall = null;

  selectedOrderId = "o-open";
  document.getElementById("ev-wake").checked = false;
  document.getElementById("ev-unacked").checked = false;
  let bare = 0;
  const feedWrites = host._writes;
  onCall = (record) => {
    if (record.path === "/api/orders/events" && !record.query.wake_only && !record.query.unacked && record.query.after == null) {
      bare += 1;
      if (bare === 2) selectedOrderId = "gone";
    }
  };
  await ordersTick();
  const feedDropped = { bare: bare, writes: host._writes === feedWrites };
  onCall = null;

  selectedOrderId = "o-open";
  document.getElementById("od-question").textContent = "keep";
  await ordersTick();
  const questionUpdated = document.getElementById("od-question").textContent;
  document.getElementById("od-question").textContent = "keep";
  const savedDigest = EVENTS[1].digest;
  EVENTS[1].digest = "changed";
  onCall = (record) => { if (record.path === "/api/orders/events" && record.query.after != null) selectedOrderId = "gone"; };
  const beforeAfter = host._writes;
  await ordersTick();
  EVENTS[1].digest = savedDigest;
  const afterDropped = {
    question: document.getElementById("od-question").textContent,
    wrote: host._writes > beforeAfter,
  };
  onCall = null;

  selectedOrderId = "o-open";
  const savedOpinion = EVENTS[0].opinion;
  EVENTS[0].opinion = null;
  await ordersTick();
  const opinionNull = document.getElementById("od-opinion").innerHTML;
  EVENTS[0].opinion = savedOpinion;
  showExtra = { checkpoint: { event_id: "nope", seq: 9, kind: "question", check: "", acked: false, answers: ["reply"] } };
  await ordersTick();
  const missingEvent = {
    question: document.getElementById("od-question").textContent,
    hidden: document.getElementById("od-question").hidden,
    opinion: document.getElementById("od-opinion").innerHTML,
    reply: document.getElementById("od-reply").disabled,
    cancel: document.getElementById("od-cancel").disabled,
  };
  showExtra = { checkpoint: null, state: "running", overridable_checks: [], phase: "", round: "" };
  cancelButton.classList.add("is-holding", "is-done");
  await ordersTick();
  const holdingKeepsDone = cancelButton.classList.contains("is-done");
  const runningFlags = {
    cancel: cancelButton.disabled,
    done: document.getElementById("od-done").disabled,
    reply: document.getElementById("od-reply").disabled,
    override: document.getElementById("od-override").disabled,
    questionHidden: document.getElementById("od-question").hidden,
  };
  cancelButton.classList.remove("is-holding");
  await ordersTick();
  const clearedDone = cancelButton.classList.contains("is-done");
  showExtra = { checkpoint: null, state: "merged", overridable_checks: [], phase: "", round: null };
  await ordersTick();
  const mergedFlags = {
    cancel: cancelButton.disabled,
    done: document.getElementById("od-done").disabled,
    round: document.getElementById("conv-round").textContent,
    phase: document.getElementById("conv-phase").textContent,
  };
  showExtra = { checkpoint: { event_id: "e1", seq: 2, kind: "question", check: "lint", acked: false, answers: ["reply"] }, state: "waiting", overridable_checks: [] };
  await ordersTick();
  const noCancelAnswer = {
    cancel: cancelButton.disabled,
    reply: document.getElementById("od-reply").disabled,
    override: document.getElementById("od-override").disabled,
  };
  showExtra = {};
  document.getElementById("od-override-check").value = "";
  await ordersTick();
  const emptyKeep = document.getElementById("od-override-check").value;

  selectedOrderId = "o-open";
  orderLiveError = "";
  failSpec = { path: "/api/orders/show", kind: "http", error: "missing", status: 503, once: true };
  await ordersTick();
  const liveError = {
    text: document.getElementById("od-err").textContent,
    hidden: document.getElementById("od-err").hidden,
    value: orderLiveError,
  };
  orderLiveError = "";
  orderStickyError = "";
  selectedOrderId = "o-open";
  onCall = (record) => { if (record.path === "/api/orders/show") selectedOrderId = "gone"; };
  failSpec = { path: "/api/orders/show", kind: "throw", error: "missing", once: true };
  await ordersTick();
  const droppedError = orderLiveError;
  onCall = null;
  showOrderNotes();

  function resetHold() {
    cancelButton.disabled = false;
    cancelButton.classList.remove("is-holding", "is-done");
    cancelButton.dataset.held = "";
    return cancelButton;
  }
  selectedOrderId = "o-open";
  const cancelFrom = since("/api/orders/cancel");
  resetHold();
  fire(cancelButton, "pointerdown");
  const started = cancelButton.classList.contains("is-holding");
  const timerDelay = timers.get(cancelHoldTimer).delay;
  fire(cancelButton, "pointerup");
  const upCleared = timers.get(cancelHoldTimer).cleared === true && !cancelButton.classList.contains("is-holding");
  fire(cancelButton, "click");
  await drain();
  const shortCancels = cancelFrom().length;

  function stoppedBy(name) {
    resetHold();
    fire(cancelButton, "pointerdown");
    const id = cancelHoldTimer;
    if (name === "keyup") {
      fire(cancelButton, "keydown", { key: " ", repeat: false });
      fire(cancelButton, "keyup");
    } else fire(cancelButton, name);
    return { cleared: timers.get(id).cleared === true, fired: fireTimer(id), holding: cancelButton.classList.contains("is-holding") };
  }
  const stops = {
    pointerup: { cleared: upCleared },
    pointerleave: stoppedBy("pointerleave"),
    pointercancel: stoppedBy("pointercancel"),
    keyup: stoppedBy("keyup"),
  };

  resetHold();
  const space = fire(cancelButton, "keydown", { key: " ", repeat: false });
  const spaceHolding = cancelButton.classList.contains("is-holding");
  fire(cancelButton, "keyup");
  const enterKey = fire(cancelButton, "keydown", { key: "Enter", repeat: false });
  const enterHolding = cancelButton.classList.contains("is-holding");
  fire(cancelButton, "keyup");
  const otherKey = fire(cancelButton, "keydown", { key: "a", repeat: false });
  const otherHolding = cancelButton.classList.contains("is-holding");
  const repeatKey = fire(cancelButton, "keydown", { key: " ", repeat: true });
  const repeatHolding = cancelButton.classList.contains("is-holding");
  const keys = {
    spacePrevented: space.prevented,
    spaceHolding: spaceHolding,
    enterPrevented: enterKey.prevented,
    enterHolding: enterHolding,
    otherPrevented: otherKey.prevented,
    otherHolding: otherHolding,
    repeatPrevented: repeatKey.prevented,
    repeatHolding: repeatHolding,
  };

  resetHold();
  cancelButton.disabled = true;
  const disabledSeq = timerSeq;
  fire(cancelButton, "pointerdown");
  const disabledStart = timerSeq === disabledSeq;
  cancelButton.disabled = false;
  cancelButton.classList.add("is-done");
  fire(cancelButton, "pointerdown");
  const doneStart = timerSeq === disabledSeq;
  cancelButton.classList.remove("is-done");
  fire(cancelButton, "pointerdown");
  const holdingSeq = timerSeq;
  fire(cancelButton, "pointerdown");
  const holdingStart = timerSeq === holdingSeq;
  fire(cancelButton, "pointerup");

  resetHold();
  fire(cancelButton, "pointerdown");
  cancelButton.disabled = true;
  const disabledFire = fireTimer(cancelHoldTimer);
  const disabledClick = {
    fired: disabledFire,
    done: cancelButton.classList.contains("is-done"),
    holding: cancelButton.classList.contains("is-holding"),
    cancels: cancelFrom().length,
  };

  resetHold();
  selectedOrderId = "o-open";
  document.getElementById("od-cancel-reason").value = "because";
  showExtra = {};
  schemaBody = { enabled: true };
  fire(cancelButton, "pointerdown");
  fireTimer(cancelHoldTimer);
  await drain();
  const heldCancel = cancelFrom();
  const clearedReason = document.getElementById("od-cancel-reason").value;

  resetHold();
  selectedOrderId = "o-open";
  document.getElementById("od-cancel-reason").value = "because";
  orderStickyError = "";
  failSpec = { path: "/api/orders/cancel", kind: "http", error: "no cancel", status: 409, once: true };
  fire(cancelButton, "pointerdown");
  fireTimer(cancelHoldTimer);
  await drain();
  const failedCancel = {
    reason: document.getElementById("od-cancel-reason").value,
    error: document.getElementById("od-err").textContent,
    sticky: orderStickyError,
    count: cancelFrom().length,
  };

  resetHold();
  selectedOrderId = "o-open";
  document.getElementById("od-cancel-reason").value = "stay";
  orderStickyError = "";
  onCall = (record) => { if (record.path === "/api/orders/cancel") selectedOrderId = "other"; };
  fire(cancelButton, "pointerdown");
  fireTimer(cancelHoldTimer);
  await drain();
  const switchedCancel = {
    reason: document.getElementById("od-cancel-reason").value,
    sticky: orderStickyError,
    count: cancelFrom().length,
  };
  onCall = null;
  orderStickyError = "";
  ordersStickyNote = "";

  const boxEl = document.getElementById("od-reply-body");
  const send = document.getElementById("od-reply");
  selectedOrderId = "o-open";
  send.disabled = true;
  boxEl.value = "hello <";
  const replyFrom = since("/api/orders/reply");
  const disabledSend = fire(boxEl, "keydown", { key: "Enter", metaKey: true, ctrlKey: false });
  await drain();
  const disabledSendCalls = replyFrom().length;
  const plainEnter = fire(boxEl, "keydown", { key: "Enter", metaKey: false, ctrlKey: false });
  const letter = fire(boxEl, "keydown", { key: "a", metaKey: true });
  send.disabled = false;
  boxEl.value = "ctrl";
  const ctrlSend = fire(boxEl, "keydown", { key: "Enter", metaKey: false, ctrlKey: true });
  await drain();
  const ctrlCall = replyFrom()[0];
  boxEl.value = "meta";
  const metaSend = fire(boxEl, "keydown", { key: "Enter", metaKey: true, ctrlKey: false });
  await drain();
  const metaCall = replyFrom()[1];
  const replyKeys = {
    disabledPrevented: disabledSend.prevented,
    disabledCalls: disabledSendCalls,
    plainPrevented: plainEnter.prevented,
    letterPrevented: letter.prevented,
    ctrlPrevented: ctrlSend.prevented,
    ctrl: ctrlCall || null,
    metaPrevented: metaSend.prevented,
    meta: metaCall || null,
    cleared: boxEl.value,
  };
  const beforeSubmit = calls.length;
  const submitted = fire(composer, "submit");
  const submit = { prevented: submitted.prevented, calls: calls.length === beforeSubmit };

  return {
    boot: boot,
    icons: icons,
    ring: ring,
    bits: bits,
    fact: fact,
    factEmpty: factEmpty,
    articles: articles,
    feet: feet,
    listFull: listFull,
    listBare: listBare,
    listEmpty: listEmpty,
    counts: { missing: missingCount, attn: attnCount, plain: plainCount, zero: zeroCount },
    heads: heads,
    clearedHead: clearedHead,
    park: { moved: moved, parked: parked, again: parkedAgain, appends: appendsAfterMove, againAppends: appendsAgain },
    replace: { skipped: skippedWrite, forced: forcedWrite },
    eventsView: eventsView,
    ordersViewOn: ordersViewOn,
    markerMissing: markerMissing,
    activeMissing: activeMissing,
    yBefore: yBefore,
    nav: nav,
    hashEvents: hashEvents,
    hashOrders: hashOrders,
    resize: { y: resizeY, noActive: resizeNoActive, threw: resizeThrew },
    keysLayout: { base: baseKey, fold: foldKey, flow: flowKey, sticky: stickyKey, display: displayKey },
    relayout: { same: sameRaf, changed: changedRaf, second: secondRaf },
    foldWatch: foldWatch,
    tops: tops,
    place: { leftOf: leftOf, rightOf: rightOf, above: above, below: below, inside: inside, noSel: noSel, body: bodyScroll, sticky: stickyScroll, bodyScrolled: bodyScrolled, duringGuard: duringGuard, afterGuard: afterGuard },
    feed: {
      html: firstHtml,
      foot: firstFoot,
      skipWrites: skipWrites,
      skipParent: skipParent,
      footHtml: footHtml,
      footWrote: footWrote,
      stillWrites: stillWrites,
      movedQuestion: movedQuestion,
      rewrote: rewrote,
      missingHtml: missingHtml,
      missingParent: missingParent,
      missingFoot: missingFoot,
      noCheckpoint: noCheckpoint,
      noCheckpointSkip: noCheckpointSkip,
    },
    regression: { stable: stable, oldGone: oldGone, queued: queued, landed: landed, keyGone: keyGone },
    reading: {
      parkAnchor: parkAnchor,
      parkId: parkLanded.id,
      parkConnected: parkLanded.connected,
      idGone: idGone,
      noKey: noKey,
      noScroll: noScroll,
    },
    seg: {
      knobCalls: knobCalls,
      blankCalls: blankCalls,
      allCalls: allCalls,
      allHtml: allHtml,
      allEntered: allEntered,
      allKnob: allKnob,
      allOn: allOn,
      openQuery: openCalls.query,
      openHtml: openHtml,
      openKnob: openKnob,
      openOnly: ordersOpenOnly,
    },
    filters: { both: bothEvents, wake: wakeEvents, unacked: unackedEvents },
    emptyBadge: emptyBadge,
    emptyHtml: emptyHtml,
    runningBadge: runningBadge,
    listFail: listFail,
    eventsFail: eventsFail,
    schemaHttp: schemaHttp,
    schemaBad: schemaBad,
    schemaNotOk: schemaNotOk,
    schemaNull: schemaNull,
    disabledNote: disabledNote,
    failureWins: failureWins,
    schemaFetched: schemaFetched,
    enabledNote: enabledNote,
    schemaSkipped: schemaSkipped,
    ignoredClick: ignoredClick,
    picked: picked,
    listPaint: { unchanged: unchangedList, changed: changedList },
    elapsedTick: elapsedTick,
    titleTick: titleTick,
    headTick: { unchanged: headUnchangedFrames, changed: headChangedFrames },
    selected: selected,
    sameId: sameId,
    otherId: otherId,
    rowSelected: rowSelected,
    showDropped: showDropped,
    feedDropped: feedDropped,
    questionUpdated: questionUpdated,
    afterDropped: afterDropped,
    opinionNull: opinionNull,
    missingEvent: missingEvent,
    holdingKeepsDone: holdingKeepsDone,
    runningFlags: runningFlags,
    clearedDone: clearedDone,
    mergedFlags: mergedFlags,
    noCancelAnswer: noCancelAnswer,
    emptyKeep: emptyKeep,
    liveError: liveError,
    droppedError: droppedError,
    hold: {
      started: started,
      delay: timerDelay,
      stops: stops,
      shortCancels: shortCancels,
      keys: keys,
      disabledStart: disabledStart,
      doneStart: doneStart,
      holdingStart: holdingStart,
      disabledClick: disabledClick,
      held: heldCancel,
      clearedReason: clearedReason,
      failed: failedCancel,
      switched: switchedCancel,
    },
    replyKeys: replyKeys,
    submit: submit,
  };
}
scenario();
`;
const source = [stub, payload.work, payload.first, payload.second, scenario].join("\n");
let result;
try {
  result = vm.runInContext(source, context, { timeout: 20000 });
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
Promise.resolve(result).then((report) => {
  console.log(JSON.stringify(report));
}).catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def _sources() -> tuple[str, str, str]:
    index = PAGE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", index, flags=re.S)
    if len(scripts) != 2:
        raise AssertionError(f"expected 2 inline scripts, found {len(scripts)}")
    return WORK.read_text(encoding="utf-8"), scripts[0], scripts[1]


def _run_page() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("node is not installed")
    work, first, second = _sources()
    source = _HARNESS.replace("__PAYLOAD__", json.dumps({"work": work, "first": first, "second": second}))
    completed = subprocess.run(
        [NODE, "-"],
        input=source,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _report() -> dict[str, Any]:
    global _REPORT
    if _REPORT is None:
        _REPORT = _run_page()
    return _REPORT


def test_boot_lists_only_open_orders_and_counts_events() -> None:
    boot = _report()["boot"]
    paths = [item["path"] for item in boot["calls"]]
    assert paths[0] == "/api/state"
    assert "/api/orders/schema" in paths
    lists = [item for item in boot["calls"] if item["path"] == "/api/orders/list"]
    assert len(lists) == 1
    assert lists[0]["query"] == {"open_only": "1"}
    assert lists[0]["method"] == "GET"
    assert "Open &lt;one&gt;" in boot["ordersHtml"]
    assert "Running" in boot["ordersHtml"]
    assert "Done" not in boot["ordersHtml"]
    assert boot["ordersElapsed"] == ["5 min"]
    assert 'data-at="2026-11-14T00:00:00Z"' in boot["ordersHtml"]
    assert "5 min" not in boot["ordersHtml"]
    assert 'aria-label="Waiting"' in boot["ordersHtml"]
    assert 'data-event="e1"' in boot["eventsHtml"]
    assert 'data-event="e2"' not in boot["eventsHtml"]
    assert 'data-event="e3"' not in boot["eventsHtml"]
    events = [item for item in boot["calls"] if item["path"] == "/api/orders/events"]
    assert events[0]["query"] == {}
    assert events[1]["query"] == {"wake_only": "1", "unacked": "1"}
    assert boot["ordersBadge"] == {"text": "2", "className": "count attn", "label": "2 waiting"}
    assert boot["eventsBadge"] == {"text": "1", "className": "count", "label": "1"}
    assert boot["note"] == {"hidden": True, "text": ""}
    assert boot["login"] == "octocat"
    assert boot["usage"] == "\u2014"
    assert boot["dev"] == "device-1"
    assert boot["stamp"] == "2026-11-14T00:00:00Z"
    assert boot["errHidden"] is True
    assert boot["intervals"] == [2000, 2000]
    assert boot["ordersOn"] is True and boot["eventsOn"] is False
    assert boot["ordersActive"] is True
    assert boot["marker"] == "12px"
    assert boot["layout"] == "visible,static,block,"
    assert boot["bootLayoutKey"] == boot["bootLastLayout"] == boot["layout"]
    assert boot["openOnly"] is True


def test_shared_and_feed_functions_cover_each_branch() -> None:
    page = _report()
    icons = page["icons"]
    assert icons["none"] == icons["undef"] == icons["empty"] == "\u2014"
    assert icons["merged"] == icons["mergedCase"]
    assert 'aria-label="merged"' in icons["merged"]
    assert "state-unknown" not in icons["merged"]
    assert "state-unknown" in icons["mystery"]
    assert 'aria-label="mystery"' in icons["mystery"]
    assert "state-unknown" not in icons["ja"] and 'aria-label="ja"' in icons["ja"]
    assert "state-unknown" not in icons["localCheck"] and 'aria-label="local-check"' in icons["localCheck"]
    assert page["ring"] == {
        "waiting": "Waiting",
        "none": "",
        "undef": "",
        "empty": "",
        "other": "running",
        "number": "2",
    }
    assert page["bits"] == {"none": "", "undef": "", "empty": "", "object": "", "text": "a&amp;b", "number": "2"}
    assert page["fact"] == "question \u00b7 2"
    assert page["factEmpty"] == ""
    ask = page["articles"]["ask"]
    assert 'data-event-id="a&quot;b&lt;c"' in ask
    assert 'class="msg ask"' in ask and 'data-ask="1"' in ask and "<pre>" not in ask
    assert 'data-event-id="e1"' in page["articles"]["digest"] and "<pre>x&lt;y</pre>" in page["articles"]["digest"]
    assert "<pre>" not in page["articles"]["emptyDigest"]
    assert "<pre>" not in page["articles"]["objectDigest"]
    assert "<span>" not in page["articles"]["noFact"].split("</b>", 1)[1]
    assert "5 min" in page["feet"]["full"] and "a&lt;b" in page["feet"]["full"] and "y&lt;z ok" in page["feet"]["full"]
    assert page["feet"]["noEv"].count("since") == 1 and "5 min" not in page["feet"]["noEv"]
    assert '<span class="since"></span>' in page["feet"]["badTime"]
    assert "5 min" not in page["feet"]["badTime"]
    assert "check" not in page["feet"]["noCheck"] and "5 min" in page["feet"]["noCheck"]
    assert "check" not in page["feet"]["numberCheck"]
    assert "Open" not in page["listBare"] and "OWNER/NAME" not in page["listBare"] and "ring" not in page["listBare"]
    assert "A&lt;B" not in page["listBare"] and "5 min" not in page["listBare"]
    assert page["listEmpty"] == ""
    assert "A&lt;B" in page["listFull"] and "OWNER/NAME" in page["listFull"]
    assert 'aria-label="Waiting"' in page["listFull"]
    assert '<span class="since" data-at="2026-11-14T00:00:00Z"></span>' in page["listFull"]
    assert "5 min" not in page["listFull"]
    assert "nope" not in page["listFull"]
    nope_row = page["listFull"].rsplit('<li class="order">', 1)[-1]
    assert '<span class="repo">OWNER/NAME</span><span></span>' in nope_row
    assert 'class="since"' not in nope_row and "data-at" not in nope_row
    assert page["counts"]["missing"] is None
    assert page["counts"]["attn"] == {"text": "2", "className": "count attn", "label": "2 waiting"}
    assert page["counts"]["plain"] == {"text": "4", "className": "count", "label": "4"}
    assert page["counts"]["zero"] is None
    hidden = page["heads"]["hidden"]
    assert hidden["rafs"] == 0
    assert hidden["title"] == hidden["repo"] == hidden["round"] == hidden["phase"] == hidden["state"] == hidden["pr"] == ""
    cleared = page["clearedHead"]
    assert cleared["title"] == cleared["repo"] == cleared["round"] == cleared["phase"] == cleared["state"] == cleared["pr"] == ""
    assert page["heads"]["roundNull"]["round"] == "" and page["heads"]["roundNull"]["state"] == ""
    assert page["heads"]["roundUndef"]["round"] == "" and page["heads"]["roundUndef"]["phase"] == ""
    assert page["heads"]["roundEmpty"]["round"] == ""
    assert page["heads"]["roundZero"]["round"] == "Round 0"
    assert page["heads"]["roundZero"]["phase"] == "Phase: ask"
    assert page["heads"]["roundZero"]["rafs"] == 1
    assert page["heads"]["waiting"]["title"] == "Order o-open"
    assert page["heads"]["waiting"]["state"].find('aria-label="Waiting"') >= 0
    assert page["park"] == {"moved": True, "parked": True, "again": True, "appends": 1, "againAppends": 0}
    assert page["replace"] == {"skipped": True, "forced": True}
    feed = page["feed"]
    assert feed["html"].find('data-event-id="e1"') < feed["html"].find('data-event-id="e2"') < feed["html"].find('data-event-id="e0"')
    ask_html = feed["html"][:feed["html"].find('data-event-id="e2"')]
    assert 'class="msg ask"' in ask_html and 'data-event-id="e1"' in ask_html and "<pre>" not in ask_html
    assert '<article class="msg" data-event-id="e2"' in feed["html"] and "<pre>a&lt;b</pre>" in feed["html"]
    assert "5 min" in feed["foot"] and "lint" in feed["foot"] and "a&lt;b" in feed["foot"]
    assert feed["skipWrites"] is True and feed["skipParent"] is True
    assert feed["footWrote"] is True and feed["stillWrites"] is True and "6 min" in feed["footHtml"]
    assert feed["movedQuestion"] == {"placed": "e2", "wrote": True, "eventId": "e1"}
    assert feed["rewrote"] is True
    assert feed["missingHtml"].startswith('<article class="msg ask" data-event-id="missing&quot;id"')
    assert feed["missingParent"] == 'missing"id'
    assert "5 min" not in feed["missingFoot"]
    assert feed["noCheckpoint"]["parent"] == "feed-park"
    assert feed["noCheckpoint"]["askClass"] is False
    assert feed["noCheckpoint"]["foot"] == ""
    assert feed["noCheckpoint"]["wrote"] is True
    assert feed["noCheckpointSkip"] is True


def test_reading_position_survives_refresh_and_layout() -> None:
    page = _report()
    assert page["tops"] == {
        "plain": "top",
        "none": None,
        "stickyMiss": None,
        "stickyHit": "top",
        "body": "od-body",
        "report": "order-report",
        "park": "feed-park",
        "parkHidden": None,
    }
    assert page["keysLayout"] == {
        "base": "visible,static,block,",
        "fold": "visible,static,block,book",
        "flow": "auto,static,block,",
        "sticky": "visible,sticky,block,",
        "display": "visible,static,flex,",
    }
    assert page["relayout"] == {"same": 0, "changed": 1, "second": 1}
    assert page["foldWatch"] == {
        "frames": 1,
        "scrollLeft": 142,
        "fold": "book",
        "lastLayout": "visible,static,block,book",
        "layoutKey": "visible,static,block,book",
    }
    place = page["place"]
    assert place["leftOf"] == {"left": -28, "top": 0, "scrolled": 0}
    assert place["rightOf"] == {"left": 172, "top": 0, "scrolled": 0}
    assert place["above"] == {"left": 0, "top": -48, "scrolled": 0}
    assert place["below"] == {"left": 0, "top": 122, "scrolled": 0}
    assert place["inside"] == {"left": 0, "top": 0, "scrolled": 0}
    assert place["noSel"] == {"left": 5, "top": 5, "scrolled": 0}
    assert place["body"]["id"] == "od-body" and place["body"]["margin"] == "" and place["body"]["block"] == "start"
    assert place["sticky"]["id"] == "od-body" and place["sticky"]["margin"] == "48px"
    assert place["bodyScrolled"] == 2
    assert place["duringGuard"]["id"] == "od-body"
    assert place["afterGuard"]["id"] == "order-report"
    regression = page["regression"]
    assert regression["stable"] is True
    assert regression["oldGone"] is True
    assert regression["queued"] == 1
    assert regression["landed"]["eventId"] == "m1"
    assert regression["landed"]["connected"] is True
    assert regression["landed"]["margin"] == "48px"
    assert regression["landed"]["block"] == "start"
    assert regression["keyGone"] is True
    reading = page["reading"]
    assert reading["parkAnchor"] == "id:feed-park"
    assert reading["parkId"] == "feed-park" and reading["parkConnected"] is True
    assert reading["idGone"] is True
    assert reading["noKey"] is True and reading["noScroll"] is True


def test_views_filters_failures_selection_and_hold() -> None:
    page = _report()
    assert page["eventsView"]["eventsOn"] is True and page["eventsView"]["ordersOn"] is False
    assert page["eventsView"]["eventsActive"] is True and page["eventsView"]["ordersActive"] is False
    assert page["eventsView"]["marker"] == "48px"
    assert page["ordersViewOn"]["entered"] == [True] * 14 + [False]
    assert page["ordersViewOn"]["feedEntered"] == [False, False]
    assert page["ordersViewOn"]["rafs"] >= 1
    assert page["ordersViewOn"]["marker"] == "12px"
    assert page["markerMissing"]["found"] is None
    assert page["markerMissing"]["y"] == page["ordersViewOn"]["marker"]
    assert page["activeMissing"] == "stay"
    nav = page["nav"]
    assert nav["groupPrevented"] is False and nav["tasksPrevented"] is False and nav["historyAfterMiss"] is True
    assert nav["hitPrevented"] is True and nav["url"] == "#events" and nav["hash"] == "#events"
    assert nav["eventsOn"] is True
    assert page["hashEvents"] == {"eventsOn": True, "ordersOn": False, "fold": ""}
    assert page["hashOrders"] is True
    assert page["resize"]["y"] == "21px"
    assert page["resize"]["noActive"] == "keep"
    assert page["resize"]["threw"] is False
    seg = page["seg"]
    assert seg["knobCalls"] == 0 and seg["blankCalls"] == 0
    assert [item["query"] for item in seg["allCalls"]] == [{"open_only": "1"}, {}]
    assert "Done" in seg["allHtml"] and "Open &lt;one&gt;" in seg["allHtml"]
    assert seg["allEntered"] is True and seg["allKnob"] == "100%" and seg["allOn"] is True
    assert seg["openQuery"] == {"open_only": "1"}
    assert "Done" not in seg["openHtml"] and seg["openKnob"] == "0" and seg["openOnly"] is True
    assert len(page["filters"]["both"]) == 1
    assert page["filters"]["both"][0]["query"] == {"wake_only": "1", "unacked": "1"}
    assert [item["query"] for item in page["filters"]["wake"]] == [
        {"wake_only": "1"},
        {"wake_only": "1", "unacked": "1"},
    ]
    assert [item["query"] for item in page["filters"]["unacked"]] == [
        {"unacked": "1"},
        {"wake_only": "1", "unacked": "1"},
    ]
    assert page["emptyBadge"] is None and page["emptyHtml"] == ""
    assert page["runningBadge"]["className"] == "count" and page["runningBadge"]["text"] == "2"
    assert page["listFail"] == "list down"
    assert page["eventsFail"] == "events down"
    assert page["schemaHttp"] == {"note": "schema broke", "schema": None}
    assert page["schemaBad"] == "HTTP 200"
    assert page["schemaNotOk"] == "nope"
    assert page["schemaNull"] == "HTTP 200"
    assert page["disabledNote"] == "Orders are not enabled on this device."
    assert page["failureWins"] == "list down"
    assert page["schemaFetched"] is True and page["schemaSkipped"] is True
    assert page["enabledNote"] == ""
    assert page["ignoredClick"] is True
    selected = page["selected"]
    assert selected["id"] == "o-open"
    picked = page["picked"]
    assert picked["rows"] == [
        {"id": "o-open", "className": "order sel"},
        {"id": "o-run", "className": "order"},
    ]
    assert picked["ringFrames"] == 1
    assert picked["arcsBefore"] == [
        {"transition": "", "dash": "18.85 37.70"},
        {"transition": "", "dash": "18.85 37.70"},
    ]
    assert picked["arcsAfter"] == picked["arcsBefore"]
    assert page["listPaint"]["unchanged"] == {
        "writes": 0,
        "className": "order sel",
        "transition": "",
        "dash": "18.85 37.70",
        "sameArc": True,
    }
    assert page["listPaint"]["changed"] == {
        "writes": 1,
        "frames": 1,
        "nested": 1,
        "transition": "none",
        "dash": "0 37.70",
    }
    assert page["elapsedTick"] == {"before": "30 s", "after": "32 s", "writes": 0, "frames": 0}
    assert page["titleTick"] == {"writes": 1, "frames": 1}
    assert page["headTick"] == {"unchanged": 0, "changed": 1}
    assert selected["title"] == "Order o-open" and selected["repo"] == "OWNER/NAME"
    assert selected["round"] == "Round 2" and selected["phase"] == "Phase: ask"
    assert 'aria-label="Waiting"' in selected["state"]
    assert "OWNER/NAME" in selected["body"] and "#3" in selected["body"]
    assert 'data-event-id="e1"' in selected["feed"] and 'data-event-id="e2"' in selected["feed"]
    assert 'data-event-id="e3"' not in selected["feed"] and 'class="msg ask"' in selected["feed"]
    assert selected["question"] == "What now?" and selected["questionHidden"] is False
    assert "lane" in selected["opinion"]
    assert selected["replyDisabled"] is False and selected["bodyDisabled"] is False
    assert selected["cancelDisabled"] is False and selected["doneDisabled"] is False
    assert selected["overrideDisabled"] is False
    assert selected["detailHidden"] is False and selected["composerHidden"] is False
    assert selected["emptyHidden"] is True and selected["odId"] == "o-open"
    assert selected["after"]["query"]["after"] == "1"
    assert page["sameId"]["reply"] == "kept" and page["sameId"]["check"] == "lint"
    assert 'value="lint"' in page["sameId"]["options"]
    assert page["otherId"]["id"] == "o-run" and page["otherId"]["reply"] == ""
    assert page["otherId"]["reason"] == "" and page["otherId"]["title"] == "Order o-run"
    assert page["otherId"]["holding"] is False and page["otherId"]["timerCleared"] is True
    assert page["otherId"]["clearedHtml"] == "" and page["otherId"]["clearedMark"] == ""
    assert 'data-event-id="e3"' in page["otherId"]["feed"]
    assert 'data-event-id="e1"' in page["otherId"]["feed"]
    assert page["rowSelected"] == "o-row"
    assert page["showDropped"] == "stale"
    assert page["feedDropped"] == {"bare": 2, "writes": True}
    assert page["questionUpdated"] == "What now?"
    assert page["afterDropped"] == {"question": "keep", "wrote": True}
    assert page["opinionNull"] == ""
    assert page["missingEvent"]["question"] == "" and page["missingEvent"]["hidden"] is True
    assert page["missingEvent"]["opinion"] == ""
    assert page["missingEvent"]["reply"] is False and page["missingEvent"]["cancel"] is True
    assert page["holdingKeepsDone"] is True
    assert page["runningFlags"] == {
        "cancel": False,
        "done": False,
        "reply": True,
        "override": True,
        "questionHidden": True,
    }
    assert page["clearedDone"] is False
    assert page["mergedFlags"]["cancel"] is True and page["mergedFlags"]["done"] is True
    assert page["mergedFlags"]["round"] == "" and page["mergedFlags"]["phase"] == ""
    assert page["noCancelAnswer"] == {"cancel": True, "reply": False, "override": True}
    assert page["emptyKeep"] == ""
    assert page["liveError"] == {"text": "missing", "hidden": False, "value": "missing"}
    assert page["droppedError"] == ""
    hold = page["hold"]
    assert hold["started"] is True and hold["delay"] == 1200 and hold["shortCancels"] == 0
    assert hold["stops"]["pointerup"] == {"cleared": True}
    for name in ("pointerleave", "pointercancel", "keyup"):
        assert hold["stops"][name] == {"cleared": True, "fired": False, "holding": False}
    assert hold["keys"] == {
        "spacePrevented": True,
        "spaceHolding": True,
        "enterPrevented": True,
        "enterHolding": True,
        "otherPrevented": False,
        "otherHolding": False,
        "repeatPrevented": False,
        "repeatHolding": False,
    }
    assert hold["disabledStart"] is True and hold["doneStart"] is True and hold["holdingStart"] is True
    assert hold["disabledClick"]["fired"] is True
    assert hold["disabledClick"]["done"] is False and hold["disabledClick"]["holding"] is False
    assert hold["disabledClick"]["cancels"] == 0
    assert len(hold["held"]) == 1
    assert hold["held"][0]["method"] == "POST"
    assert hold["held"][0]["body"] == {"order_id": "o-open", "reason": "because"}
    assert hold["clearedReason"] == ""
    assert hold["failed"]["reason"] == "because"
    assert hold["failed"]["error"] == "no cancel" and hold["failed"]["sticky"] == "no cancel"
    assert hold["failed"]["count"] == 2
    assert hold["switched"]["reason"] == "stay" and hold["switched"]["sticky"] == ""
    assert hold["switched"]["count"] == 3
    reply = page["replyKeys"]
    assert reply["disabledPrevented"] is True and reply["disabledCalls"] == 0
    assert reply["plainPrevented"] is False and reply["letterPrevented"] is False
    assert reply["ctrlPrevented"] is True and reply["ctrl"]["body"] == {"order_id": "o-open", "body": "ctrl"}
    assert reply["metaPrevented"] is True and reply["meta"]["body"] == {"order_id": "o-open", "body": "meta"}
    assert reply["cleared"] == ""
    assert page["submit"] == {"prevented": True, "calls": True}
