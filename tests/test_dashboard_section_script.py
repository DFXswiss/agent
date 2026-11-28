"""Line and branch coverage for the section and new-order page scripts.

The shared block is out of scope. Covered functions outside it:

section.html
- dashboard-section forEach callback
- section-nav forEach callback
- placeMarker
- renderDetail, including showErr and the six control onclick handlers
- selectSession, including its row forEach callback
- tick, including the session-row map callback and the draw block

orders_new.html
- section-nav forEach callback
- placeMarker
- os-add-skip click handler

The top-level currentLink arm is not a function. Both arms run.
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

_REPORT: dict[str, Any] | None = None

_EMPTY_MARK = "\u2014"
_WATCH = '<p class="watch-note">Watching \u2014 this session is on another device.</p>'
_MUTED = '<p class="usage-muted">' + _EMPTY_MARK + "</p>"
_INVENTORY = (
    "<tr><td colspan='12'>No subscription inventory: "
    "$AGENT_HOME/ai-subscriptions.json is absent</td></tr>"
)
_USAGE_ERROR = "ai-subscriptions.json: bad file"
_FORM = (
    '<button type="button" class="btn btn-primary btn-sm jk-press" id="ctl-start">Start</button>',
    '<button type="button" class="btn btn-ghost btn-sm jk-press" id="ctl-start-ai">Start AI session</button>',
    '<button type="button" class="btn btn-ghost btn-sm jk-press" id="ctl-stop">Stop</button>',
    '<input type="text" id="ctl-data" placeholder="input text" aria-label="Input text" />',
    '<button type="button" class="btn btn-ghost btn-sm jk-press" id="ctl-send">Send</button>',
    '<button type="button" class="btn btn-ghost btn-sm jk-press" id="ctl-enter">Enter</button>',
    '<button type="button" class="btn btn-ghost btn-sm jk-press" id="ctl-ctrlc">Ctrl-C</button>',
    '<p id="ctl-err" class="failed" hidden></p>',
)
_SKIP_ROW = (
    '<select class="os-skip-key"><option value="plain">plain</option>'
    '<option value="a&lt;b">a&lt;b</option><option value="c&quot;d">c&quot;d</option></select>'
    '<input type="text" class="os-skip-reason" placeholder="reason" />'
    '<button type="button" class="os-skip-remove btn btn-ghost btn-sm">Remove</button>'
)
_POSTS = (
    {"action": "start", "payload": {}},
    {"action": "start", "payload": {"interactive": True}},
    {"action": "stop", "payload": {}},
    {"action": "input", "payload": {"data": "hello <"}},
    {"action": "input", "payload": {"key": "enter"}},
    {"action": "input", "payload": {"key": "ctrl-c"}},
)

_HARNESS = r"""
const prelude = String.raw`
const rafs = [];
const calls = [];
const intervals = [];
const resizeListeners = [];
const queue = [];
const docListeners = [];

class Element {}
class HTMLDetailsElement extends Element {}

function tokens(el) {
  return el.className.split(/\s+/).filter(Boolean);
}
function compound(part) {
  const spec = { tag: "", id: "", classes: [] };
  const re = /#([A-Za-z0-9_-]+)|\.([A-Za-z0-9_-]+)|([A-Za-z][A-Za-z0-9-]*)/g;
  let match;
  while ((match = re.exec(part))) {
    if (match[1]) spec.id = match[1];
    else if (match[2]) spec.classes.push(match[2]);
    else if (match[3]) spec.tag = match[3].toLowerCase();
  }
  return spec;
}
function matchesCompound(el, spec) {
  if (spec.tag && el.tag !== spec.tag) return false;
  if (spec.id && el.id !== spec.id) return false;
  for (let i = 0; i < spec.classes.length; i += 1) {
    if (tokens(el).indexOf(spec.classes[i]) < 0) return false;
  }
  return true;
}
function walk(node, visit) {
  node.children.forEach((child) => {
    visit(child);
    walk(child, visit);
  });
}
function queryAll(rootNode, selector) {
  const parts = selector.trim().split(/\s+/).map(compound);
  let current = [rootNode];
  parts.forEach((spec) => {
    const next = [];
    current.forEach((node) => {
      walk(node, (el) => {
        if (matchesCompound(el, spec)) next.push(el);
      });
    });
    current = next;
  });
  return current;
}
function applyAttrs(el, raw) {
  const re = /([:@A-Za-z_][\w:.-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+)))?/g;
  let match;
  while ((match = re.exec(raw))) {
    const name = match[1].toLowerCase();
    const value = match[2] !== undefined ? match[2] : match[3] !== undefined ? match[3] : match[4] !== undefined ? match[4] : "";
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
        const styleValue = part.slice(cut + 1).trim();
        if (key === "stroke-dasharray") el.style.strokeDasharray = styleValue;
        else el._style[key] = styleValue;
      });
    }
  }
}
function parseInto(parent, html) {
  const re = /<(\/?)([A-Za-z][A-Za-z0-9]*)([^>]*)>/g;
  const voidTags = { br: 1, img: 1, input: 1, hr: 1, meta: 1, link: 1 };
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
    const top = stack[stack.length - 1];
    top.children.push(el);
    el.parent = top;
    el.isConnected = true;
    const selfClose = /\/\s*$/.test(attrs) || voidTags[tag] === 1;
    if (!selfClose) stack.push(el);
  }
}
function make(tag) {
  const el = tag === "details" ? new HTMLDetailsElement() : new Element();
  el.tag = tag;
  el.children = [];
  el.parent = null;
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
  el._offsetReads = 0;
  el._writes = 0;
  el._listeners = {};
  el.onclick = null;
  el.isConnected = false;
  el.offsetTop = 0;
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
  el.style = {
    setProperty(name, value) { el._style[name] = String(value); },
    removeProperty(name) { delete el._style[name]; },
    get transition() { return el._style.transition == null ? "" : el._style.transition; },
    set transition(value) { el._style.transition = String(value); },
    get strokeDasharray() {
      return el._style["stroke-dasharray"] == null ? "" : el._style["stroke-dasharray"];
    },
    set strokeDasharray(value) { el._style["stroke-dasharray"] = String(value); },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    set(html) {
      el._html = String(html);
      if (el.id === "detail-controls") el._writes += 1;
      el.children.splice(0, el.children.length).forEach((child) => {
        child.isConnected = false;
        child.parent = null;
      });
      parseInto(el, el._html);
    },
  });
  Object.defineProperty(el, "offsetWidth", {
    get() { el._offsetReads += 1; return 8; },
  });
  el.getAttribute = (name) => (Object.prototype.hasOwnProperty.call(el._attr, name) ? el._attr[name] : null);
  el.setAttribute = (name, value) => { el._attr[name] = String(value); };
  el.addEventListener = (type, handler) => {
    if (!el._listeners[type]) el._listeners[type] = [];
    el._listeners[type].push(handler);
  };
  el.appendChild = (child) => {
    if (child.parent && child.parent !== el) {
      child.parent.children = child.parent.children.filter((item) => item !== child);
    }
    child.parent = el;
    child.isConnected = true;
    if (el.children.indexOf(child) < 0) el.children.push(child);
    return child;
  };
  el.remove = () => {
    if (el.parent) el.parent.children = el.parent.children.filter((item) => item !== el);
    el.parent = null;
    el.isConnected = false;
  };
  el.focus = () => {};
  el.matches = (selector) => matchesCompound(el, compound(selector));
  el.closest = (selector) => {
    const spec = compound(selector);
    let node = el;
    while (node) {
      if (node.tag && matchesCompound(node, spec)) return node;
      node = node.parent;
    }
    return null;
  };
  el.querySelectorAll = (selector) => queryAll(el, selector);
  el.querySelector = (selector) => {
    const found = queryAll(el, selector);
    return found.length ? found[0] : null;
  };
  return el;
}

function matchMedia() { return { matches: false }; }
function requestAnimationFrame(fn) { rafs.push(fn); return rafs.length; }
function setTimeout() { return 0; }
function clearTimeout() {}
function setInterval(fn, delay) { intervals.push({ delay: delay }); return intervals.length; }
function addEventListener(type, fn) {
  if (type !== "resize") throw new Error("unexpected window listener " + type);
  resizeListeners.push(fn);
}
function fetch(url, opts) {
  const record = {
    url: String(url),
    method: opts && opts.method ? opts.method : "GET",
    body: opts && opts.body ? JSON.parse(opts.body) : null,
    jsonCalls: 0,
  };
  calls.push(record);
  const item = queue.shift();
  if (!item) return Promise.reject(new Error("empty queue for " + url));
  if (item.throw) return Promise.reject(new Error(item.throw));
  return Promise.resolve({
    ok: item.ok !== false,
    status: item.status || 200,
    json() {
      record.jsonCalls += 1;
      return Promise.resolve(item.json === undefined ? null : item.json);
    },
  });
}
function settle() { return new Promise((resolve) => setImmediate(resolve)); }
function flushRaf() {
  const batch = rafs.splice(0, rafs.length);
  batch.forEach((fn) => fn());
}
function httpOk(data) { return { ok: true, status: 200, json: data }; }
function baseState(over) {
  return Object.assign({
    generated_at: "2026-11-14T00:00:00Z",
    device_id: "device-1",
    login: "octocat",
    usage_overview: [],
    subscriptions: { configured: true, error: null, rows: [] },
    sessions: [],
    tasks: [],
    rounds: [],
    agents: [],
    checks: [],
    gates: [],
    work: [],
    pings: [],
  }, over || {});
}
function oneSession(id, canControl, seen, pane) {
  return {
    id: id,
    kind: "lane",
    status: "active",
    can_control: canControl,
    last_seen_at: seen,
    _origin_device_id: "dev",
    runtime: { pane: pane },
    control_connected: true,
  };
}
function sessionList(prefix, count) {
  const list = [];
  for (let i = 0; i < count; i += 1) list.push(oneSession("s" + i, i % 2 === 0, prefix + "-" + i, i));
  return list;
}

const root = make("div");
root.isConnected = true;
function mount(parent, el) {
  el.parent = parent;
  el.isConnected = true;
  parent.children.push(el);
  return el;
}
function leaf(parent, tag, id) {
  const el = make(tag);
  if (id) {
    el.id = id;
    el._attr.id = id;
  }
  return mount(parent, el);
}
function byId(id) {
  let found = null;
  walk(root, (el) => { if (el.id === id) found = el; });
  return found;
}
const ordersMode = modeName === "orders-new" || modeName === "orders-tasks";
const nav = leaf(root, "nav", "section-nav");
const markerEl = leaf(nav, "div", "marker");
const hrefs = ["/", "/#events", "/tasks", "/work", "/pings", "/sessions", "/rounds", "/agents", "/checks", "/gates", "/usage"];
const tops = { "/sessions": 64, "/usage": 40, "/tasks": 48 };
hrefs.forEach((href) => {
  const link = leaf(nav, "a", "");
  link._attr.href = href;
  link.offsetTop = tops[href] || 0;
});
if (!ordersMode) {
  leaf(root, "span", "login");
  leaf(root, "code", "dev");
  leaf(root, "span", "stamp");
  leaf(root, "p", "err").hidden = true;
  ["usage", "sessions", "tasks", "rounds", "agents", "checks", "gates", "work", "pings"].forEach((name) => {
    const sectionEl = leaf(root, "section", "");
    sectionEl.className = "dashboard-section";
    sectionEl.dataset.section = name;
    const table = leaf(sectionEl, "table", name);
    const head = leaf(table, "thead", "");
    leaf(head, "tr", "").className = "head-row";
    leaf(table, "tbody", "");
    if (name === "usage") {
      leaf(sectionEl, "input", "usage-detailed").checked = false;
      leaf(sectionEl, "input", "usage-sort").checked = false;
      leaf(sectionEl, "div", "usage-overview");
    }
    if (name === "sessions") {
      const detail = leaf(sectionEl, "div", "session-detail");
      detail.hidden = true;
      leaf(detail, "code", "detail-id");
      leaf(detail, "pre", "detail-pre");
      leaf(detail, "div", "detail-controls");
    }
  });
} else {
  ["order-submit", "orders-note", "os-repo", "os-title", "os-body", "os-base", "os-worker", "os-notify", "os-max-rounds", "os-skips", "os-add-skip", "os-send", "os-result"].forEach((id) => {
    leaf(root, "div", id);
  });
}
const pathByMode = {
  sessions: "/sessions",
  usage: "/usage",
  missing: "/nope",
  "orders-new": "/orders/new",
  "orders-tasks": "/tasks",
};
const location = { pathname: pathByMode[modeName], search: "" };
const navigator = { clipboard: { writeText() { return Promise.resolve(); } } };
const document = {
  activeElement: null,
  documentElement: root,
  getElementById(id) { return byId(id); },
  querySelectorAll(selector) { return queryAll(root, selector); },
  querySelector(selector) {
    const found = queryAll(root, selector);
    return found.length ? found[0] : null;
  },
  createElement(tag) { return make(tag); },
  addEventListener(type, handler, capture) {
    docListeners.push({ type: type, handler: handler, capture: capture === true });
  },
};
function markerValue() {
  const el = document.getElementById("marker");
  return Object.prototype.hasOwnProperty.call(el._style, "--y") ? el._style["--y"] : "";
}
function controlWrites() {
  return document.getElementById("detail-controls")._writes;
}
function tbody(id) {
  const found = document.querySelector("#" + id + " tbody");
  if (!found) throw new Error("missing tbody " + id);
  return found.innerHTML;
}
function snapRows() {
  const view = document.querySelector(".dashboard-section.on");
  if (!view) return [];
  return view.querySelectorAll("tbody tr").map((tr) => {
    const arc = tr.querySelector(".arc");
    return {
      id: tr.dataset.id || "",
      className: tr.className,
      index: Object.prototype.hasOwnProperty.call(tr._style, "--jk-i") ? tr._style["--jk-i"] : null,
      reads: tr._offsetReads,
      dash: arc ? arc.style.strokeDasharray : "",
      transition: arc ? arc.style.transition : "",
      to: arc ? (arc.dataset.to || "") : "",
    };
  });
}
function sectionSnap() {
  return document.querySelectorAll(".dashboard-section").map((el) => ({
    section: el.dataset.section,
    on: el.classList.contains("on"),
    hidden: el.hidden,
    className: el.className,
  }));
}
function navSnap() {
  return document.querySelectorAll("#section-nav a").map((link) => ({
    href: link.getAttribute("href"),
    current: link.getAttribute("aria-current"),
    className: link.className,
  }));
}
function rowClass(id) {
  const rows = document.querySelectorAll("tr.session-row");
  for (let i = 0; i < rows.length; i += 1) {
    if (rows[i].dataset.id === id) return rows[i].className;
  }
  return null;
}
function clickId(id) {
  const el = document.getElementById(id);
  if (!el || typeof el.onclick !== "function") throw new Error("missing onclick " + id);
  return el.onclick();
}
function errState() {
  const err = document.getElementById("err");
  return { hidden: err.hidden, text: err.textContent };
}
function linkByHref(href) {
  const links = document.querySelectorAll("#section-nav a");
  for (let i = 0; i < links.length; i += 1) {
    if (links[i].getAttribute("href") === href) return links[i];
  }
  throw new Error("missing link " + href);
}

if (modeName === "sessions") {
  queue.push(httpOk(baseState({
    usage_overview: [{
      provider: "driver-a", name: "Driver A", status: "no data", resets_in: "",
      used_percent: null, text: "", windows: [], accounts: [], recorded: null,
    }],
    subscriptions: {
      configured: true, error: null,
      rows: [{
        provider: "driver-a", account: "seat@example.com", label: null, status: "no data",
        note: null, listed: true, checked_at: null, usage: null,
      }],
    },
    sessions: sessionList("t1", 15),
    tasks: [{ id: "t1", title: "Title", workflow: "wf", state: "open", current_round: 1, _origin_device_id: "dev" }],
    rounds: [{ task_id: "t1", round: 1, implementer_verdict: "ja", reviewer_verdict: "nein", started_at: "s", finished_at: "f" }],
    agents: [{ id: "a1", role: "impl", vendor: "driver-a", status: "working", task_id: "t1", round: 1 }],
    checks: [{ task_id: "t1", name: "lint", result: "green", command: "pytest", ran_at: "t" }],
    gates: [{ task_id: "t1", stage: "pr", dimension: "logic", vendor: "driver-a", verdict: "ja", head_sha: "abc" }],
    work: [{ session_id: "s0", key: "k", status: "open", closable_by: "me", note: "n" }],
    pings: [{ from_login: "a", to_login: "b", kind: "ping", acked_at: "" }],
  })));
} else if (modeName === "usage") {
  queue.push(httpOk(baseState({
    subscriptions: { configured: true, error: "bad file", rows: [] },
  })));
} else if (modeName === "missing") {
  queue.push(httpOk(baseState({})));
} else {
  const skipKeys = modeName === "orders-new" ? ["plain", "a<b", 'c"d'] : [];
  queue.push({ ok: true, status: 200, json: { ok: true, result: { enabled: true, skippable: skipKeys } } });
}
void markerEl;
`;

const scenario = String.raw`
await settle();
await settle();
let out;
if (modeName === "sessions") {
  if (calls.length !== 1 || calls[0].url !== "/api/state") throw new Error("boot fetches");
  const boot = {
    sections: sectionSnap(),
    nav: navSnap(),
    markerY: markerValue(),
    markerHidden: document.getElementById("marker").hidden,
    head: document.querySelector("#sessions thead tr").className,
    intervals: intervals.map((item) => item.delay),
    calls: calls.length,
    err: errState(),
    login: document.getElementById("login").textContent,
    stamp: document.getElementById("stamp").textContent,
    dev: document.getElementById("dev").innerHTML,
    drew: document.querySelector(".dashboard-section.on").dataset.drew,
    overview: document.getElementById("usage-overview").innerHTML,
    usage: tbody("usage"),
    sessions: tbody("sessions"),
    tasks: tbody("tasks"),
    rounds: tbody("rounds"),
    agents: tbody("agents"),
    checks: tbody("checks"),
    gates: tbody("gates"),
    work: tbody("work"),
    pings: tbody("pings"),
    rafs: rafs.length,
    rows: snapRows(),
  };
  flushRaf();
  const midRafs = rafs.length;
  flushRaf();
  const drawn = { rows: snapRows(), midRafs: midRafs, rafs: rafs.length };
  queue.push(httpOk(baseState({
    login: "",
    generated_at: "2026-11-14T00:00:01Z",
    usage_overview: null,
    subscriptions: null,
    sessions: sessionList("t2", 15),
    tasks: null,
    rounds: [],
    agents: null,
    checks: [],
    gates: null,
    work: [],
    pings: null,
  })));
  await tick();
  const tick2 = {
    login: document.getElementById("login").textContent,
    stamp: document.getElementById("stamp").textContent,
    overview: document.getElementById("usage-overview").innerHTML,
    usage: tbody("usage"),
    sessions: tbody("sessions"),
    tasks: tbody("tasks"),
    rounds: tbody("rounds"),
    agents: tbody("agents"),
    checks: tbody("checks"),
    gates: tbody("gates"),
    work: tbody("work"),
    pings: tbody("pings"),
    rafs: rafs.length,
    drew: document.querySelector(".dashboard-section.on").dataset.drew,
    rows: snapRows(),
    err: errState(),
  };
  selectSession("s0");
  const selectedOn = {
    row: rowClass("s0"),
    other: rowClass("s1"),
    hidden: document.getElementById("session-detail").hidden,
    controls: document.getElementById("detail-controls").innerHTML,
    writes: controlWrites(),
    id: document.getElementById("detail-id").innerHTML,
  };
  selectSession("s1");
  const selectedOff = {
    row: rowClass("s1"),
    other: rowClass("s0"),
    hidden: document.getElementById("session-detail").hidden,
    controls: document.getElementById("detail-controls").innerHTML,
    writes: controlWrites(),
  };
  queue.push(httpOk(baseState({
    sessions: [oneSession("s0", true, "t3-0", 1), oneSession("s1", false, "t3-1", 1)],
    subscriptions: { configured: true, error: "bad file", rows: [] },
  })));
  await tick();
  const held = {
    err: errState(),
    usage: tbody("usage"),
    writes: controlWrites(),
    controls: document.getElementById("detail-controls").innerHTML,
    row: rowClass("s1"),
    other: rowClass("s0"),
  };
  selectSession("a/b");
  const absent = {
    selected: selectedSessionId,
    hidden: document.getElementById("session-detail").hidden,
    writes: controlWrites(),
    s0: rowClass("s0"),
    s1: rowClass("s1"),
  };
  queue.push(httpOk(baseState({ sessions: [oneSession("a/b", true, "t4", 1)] })));
  await tick();
  const opened = {
    hidden: document.getElementById("session-detail").hidden,
    writes: controlWrites(),
    row: rowClass("a/b"),
    controls: document.getElementById("detail-controls").innerHTML,
    id: document.getElementById("detail-id").innerHTML,
    selected: selectedSessionId,
  };
  for (let n = 0; n < 6; n += 1) queue.push({ ok: false, status: 422, json: { error: "nope" } });
  document.getElementById("ctl-data").value = "hello <";
  const postAt = calls.length;
  await clickId("ctl-start");
  await clickId("ctl-start-ai");
  await clickId("ctl-stop");
  await clickId("ctl-send");
  await clickId("ctl-enter");
  await clickId("ctl-ctrlc");
  const posts = {
    calls: calls.slice(postAt),
    errHidden: document.getElementById("ctl-err").hidden,
    errText: document.getElementById("ctl-err").textContent,
    writes: controlWrites(),
  };
  queue.push(httpOk(baseState({ sessions: [oneSession("a/b", true, "t5", 9)] })));
  await tick();
  const sameForm = {
    writes: controlWrites(),
    pre: document.getElementById("detail-pre").innerHTML,
    controls: document.getElementById("detail-controls").innerHTML,
  };
  queue.push(httpOk(baseState({ sessions: [oneSession("a/b", false, "t6", 9)] })));
  await tick();
  const watched = {
    writes: controlWrites(),
    controls: document.getElementById("detail-controls").innerHTML,
    hidden: document.getElementById("session-detail").hidden,
  };
  queue.push(httpOk(baseState({
    sessions: null,
    tasks: [],
    rounds: null,
    agents: [],
    checks: null,
    gates: [],
    work: null,
    pings: [],
  })));
  await tick();
  const cleared = {
    selected: selectedSessionId,
    hidden: document.getElementById("session-detail").hidden,
    writes: controlWrites(),
    sessions: tbody("sessions"),
    tasks: tbody("tasks"),
    rounds: tbody("rounds"),
    agents: tbody("agents"),
    checks: tbody("checks"),
    gates: tbody("gates"),
    work: tbody("work"),
    pings: tbody("pings"),
    err: errState(),
  };
  document.getElementById("session-detail").hidden = false;
  const backWrites = controlWrites();
  queue.push(httpOk(baseState({ sessions: [oneSession("a/b", true, "t8", 1)] })));
  await tick();
  const back = {
    selected: selectedSessionId,
    hidden: document.getElementById("session-detail").hidden,
    writes: controlWrites(),
    writeDelta: controlWrites() - backWrites,
    row: rowClass("a/b"),
    sessions: tbody("sessions"),
  };
  queue.push({ ok: false, status: 503 });
  await tick();
  const httpErr = errState();
  httpErr.jsonCalls = calls[calls.length - 1].jsonCalls;
  httpErr.url = calls[calls.length - 1].url;
  queue.push({ throw: "down" });
  await tick();
  const down = errState();
  down.url = calls[calls.length - 1].url;
  linkByHref("/sessions").offsetTop = 120;
  resizeListeners[0]();
  out = {
    boot: boot,
    drawn: drawn,
    tick2: tick2,
    selectedOn: selectedOn,
    selectedOff: selectedOff,
    held: held,
    absent: absent,
    opened: opened,
    posts: posts,
    sameForm: sameForm,
    watched: watched,
    cleared: cleared,
    back: back,
    httpErr: httpErr,
    down: down,
    moved: markerValue(),
    resize: resizeListeners.length,
  };
} else if (modeName === "usage") {
  const first = {
    err: errState(),
    sections: sectionSnap(),
    nav: navSnap(),
    markerY: markerValue(),
    markerHidden: document.getElementById("marker").hidden,
    rafs: rafs.length,
    drew: document.querySelector(".dashboard-section.on").dataset.drew,
    resize: resizeListeners.length,
  };
  queue.push(httpOk(baseState({
    subscriptions: { configured: true, error: null, rows: [] },
  })));
  await tick();
  const second = { err: errState() };
  linkByHref("/usage").offsetTop = 90;
  resizeListeners[0]();
  out = { first: first, second: second, moved: markerValue() };
} else if (modeName === "missing") {
  out = {
    sections: sectionSnap(),
    nav: navSnap(),
    markerHidden: document.getElementById("marker").hidden,
    markerY: markerValue(),
    resize: resizeListeners.length,
    rafs: rafs.length,
    err: errState(),
    calls: calls.length,
    drewOn: document.querySelector(".dashboard-section.on"),
  };
} else if (modeName === "orders-new") {
  const button = document.getElementById("os-add-skip");
  if (!button._listeners.click || button._listeners.click.length !== 1) throw new Error("skip handler");
  button._listeners.click[0]();
  const row = document.getElementById("os-skips").children[0];
  out = {
    markerHidden: document.getElementById("marker").hidden,
    markerY: markerValue(),
    resize: resizeListeners.length,
    nav: navSnap(),
    calls: calls.slice(),
    resultText: document.getElementById("os-result").textContent,
    resultClass: document.getElementById("os-result").className,
    rowClass: row.className,
    rowHtml: row.innerHTML,
  };
} else if (modeName === "orders-tasks") {
  const markerY = markerValue();
  linkByHref("/tasks").offsetTop = 77;
  resizeListeners[0]();
  out = {
    markerHidden: document.getElementById("marker").hidden,
    markerY: markerY,
    moved: markerValue(),
    resize: resizeListeners.length,
    nav: navSnap(),
    calls: calls.slice(),
    resultText: document.getElementById("os-result").textContent,
  };
} else {
  throw new Error("unknown mode " + modeName);
}
return out;
`;

function run(mode) {
  const pageSource = mode === "orders-new" || mode === "orders-tasks" ? ordersSource : sectionSource;
  const body = prelude + "\n" + workSource + "\n" + pageSource + "\n" + scenario;
  const fn = new Function("modeName", "return (async () => {\n" + body + "\n})();");
  return fn(mode);
}

(async () => {
  const modes = ["sessions", "usage", "missing", "orders-new", "orders-tasks"];
  const out = {};
  for (const mode of modes) {
    try {
      out[mode] = await run(mode);
    } catch (error) {
      console.error(mode);
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    }
  }
  console.log(JSON.stringify(out));
})();
"""


def _inline(name: str) -> str:
    html = (STATIC / name).read_text(encoding="utf-8")
    found = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    if len(found) != 1:
        raise AssertionError(name + " inline scripts: " + str(len(found)))
    return found[0]


def _empty(cols: int) -> str:
    return "<tr><td colspan='" + str(cols) + "'>" + _EMPTY_MARK + "</td></tr>"


def _report() -> dict[str, Any]:
    global _REPORT
    if NODE is None:
        pytest.skip("node is not installed; the static tests above cover the source")
    if _REPORT is None:
        _REPORT = _run()
    return _REPORT


def _run() -> dict[str, Any]:
    script = (
        "const workSource = "
        + json.dumps(WORK.read_text(encoding="utf-8"))
        + ";\nconst sectionSource = "
        + json.dumps(_inline("section.html"))
        + ";\nconst ordersSource = "
        + json.dumps(_inline("orders_new.html"))
        + ";\n"
        + _HARNESS
    )
    completed = subprocess.run(
        [NODE, "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report: dict[str, Any] = json.loads(completed.stdout)
    return report


def _link(nav: list[dict[str, Any]], href: str) -> dict[str, Any]:
    found = [item for item in nav if item["href"] == href]
    assert len(found) == 1
    return found[0]


def _assert_nav(nav: list[dict[str, Any]], current: str | None) -> None:
    assert [item["href"] for item in nav] == [
        "/",
        "/#events",
        "/tasks",
        "/work",
        "/pings",
        "/sessions",
        "/rounds",
        "/agents",
        "/checks",
        "/gates",
        "/usage",
    ]
    marked = [item["href"] for item in nav if item["current"] is not None]
    assert marked == ([] if current is None else [current])
    for item in nav:
        if item["href"] == current:
            assert item["current"] == "page"
            assert item["className"] == "active"
        else:
            assert item["current"] is None
            assert item["className"] == ""


def _assert_form(html: str) -> None:
    for snip in _FORM:
        assert snip in html
    assert "control-form" not in html


def _assert_rows(rows: list[dict[str, Any]], dash: str, transition: str, entered: bool) -> None:
    assert len(rows) == 15
    for index, row in enumerate(rows):
        assert row["id"] == "s" + str(index)
        assert row["to"] == "18.85"
        assert row["dash"] == dash
        assert row["transition"] == transition
        if entered and index < 14:
            assert row["className"] == "session-row click jk-enter"
            assert row["index"] == str(index)
            assert row["reads"] == 1
        else:
            assert row["className"] == "session-row click"
            assert row["index"] is None
            assert row["reads"] == 0


def test_section_boot_marks_the_open_section_and_nav() -> None:
    page = _report()["sessions"]
    boot = page["boot"]
    by_name = {item["section"]: item for item in boot["sections"]}
    assert list(by_name) == [
        "usage",
        "sessions",
        "tasks",
        "rounds",
        "agents",
        "checks",
        "gates",
        "work",
        "pings",
    ]
    for name, item in by_name.items():
        if name == "sessions":
            assert item["on"] is True
            assert item["hidden"] is False
            assert item["className"] == "dashboard-section on"
        else:
            assert item["on"] is False
            assert item["hidden"] is True
            assert item["className"] == "dashboard-section"
    _assert_nav(boot["nav"], "/sessions")
    assert boot["markerHidden"] is False
    assert boot["markerY"] == "64px"
    assert page["moved"] == "120px"
    assert page["resize"] == 1
    assert boot["head"] == "head-row"
    assert boot["intervals"] == [2000]
    assert boot["calls"] == 1
    assert boot["err"] == {"hidden": True, "text": ""}
    assert boot["login"] == "octocat"
    assert boot["stamp"] == "2026-11-14T00:00:00Z"
    assert boot["dev"] == "device-1"
    assert boot["drew"] == "1"


def test_section_tick_draws_once_and_renders_every_table() -> None:
    boot = _report()["sessions"]["boot"]
    drawn = _report()["sessions"]["drawn"]
    tick2 = _report()["sessions"]["tick2"]
    assert boot["rafs"] == 1
    _assert_rows(boot["rows"], "0 37.70", "none", True)
    assert drawn["midRafs"] == 1
    assert drawn["rafs"] == 0
    _assert_rows(drawn["rows"], "18.85 37.70", "", True)
    assert boot["sessions"].count('data-to="18.85"') == 15
    assert boot["sessions"].count(">control<") == 8
    assert boot["sessions"].count(">watch<") == 7
    assert "selected" not in boot["sessions"]
    assert 'data-id="s0"' in boot["sessions"]
    assert "t1-14" in boot["sessions"]
    assert "<b>Driver A</b>" in boot["overview"]
    assert 'data-provider="driver-a"' in boot["overview"]
    assert "seat@example.com" in boot["usage"]
    assert "ai-subscriptions.json is absent" not in boot["usage"]
    assert "Title" in boot["tasks"]
    assert "ja" in boot["rounds"] and "nein" in boot["rounds"]
    assert "impl" in boot["agents"]
    assert "pytest" in boot["checks"]
    assert "logic" in boot["gates"]
    assert ">me<" in boot["work"]
    assert ">ping<" in boot["pings"]
    assert tick2["login"] == "not paired"
    assert tick2["stamp"] == "2026-11-14T00:00:01Z"
    assert tick2["overview"] == _MUTED
    assert tick2["usage"] == _INVENTORY
    assert tick2["rafs"] == 0
    assert tick2["drew"] == "1"
    assert tick2["err"] == {"hidden": True, "text": ""}
    assert tick2["sessions"].count('class="session-row click"') == 15
    assert "jk-enter" not in tick2["sessions"]
    assert "t2-3" in tick2["sessions"]
    _assert_rows(tick2["rows"], "18.85 37.70", "", False)
    assert tick2["tasks"] == _empty(6)
    assert tick2["rounds"] == _empty(6)
    assert tick2["agents"] == _empty(6)
    assert tick2["checks"] == _empty(5)
    assert tick2["gates"] == _empty(6)
    assert tick2["work"] == _empty(5)
    assert tick2["pings"] == _empty(4)


def test_section_select_and_control_posts() -> None:
    page = _report()["sessions"]
    selected = page["selectedOn"]
    other = page["selectedOff"]
    assert selected["row"] == "session-row click selected sel"
    assert selected["other"] == "session-row click"
    assert selected["hidden"] is False
    assert selected["writes"] == 1
    assert selected["id"] == "s0"
    _assert_form(selected["controls"])
    assert other["row"] == "session-row click selected sel"
    assert other["other"] == "session-row click"
    assert other["hidden"] is False
    assert other["writes"] == 2
    assert other["controls"] == _WATCH
    held = page["held"]
    assert held["err"] == {"hidden": True, "text": ""}
    assert held["usage"] == _empty(12)
    assert held["writes"] == 2
    assert held["controls"] == _WATCH
    assert held["row"] == "session-row click selected sel"
    assert held["other"] == "session-row click"
    absent = page["absent"]
    assert absent == {
        "selected": "a/b",
        "hidden": True,
        "writes": 2,
        "s0": "session-row click",
        "s1": "session-row click",
    }
    opened = page["opened"]
    assert opened["hidden"] is False
    assert opened["writes"] == 3
    assert opened["row"] == "session-row click selected sel"
    assert opened["selected"] == "a/b"
    assert opened["id"] == "a/b"
    _assert_form(opened["controls"])
    posts = page["posts"]
    assert posts["writes"] == 3
    assert posts["errHidden"] is False
    assert posts["errText"] == "Error: nope"
    assert [call["url"] for call in posts["calls"]] == ["/api/sessions/a%2Fb/control"] * 6
    assert [call["method"] for call in posts["calls"]] == ["POST"] * 6
    assert [call["body"] for call in posts["calls"]] == list(_POSTS)
    assert [call["jsonCalls"] for call in posts["calls"]] == [1, 1, 1, 1, 1, 1]


def test_section_tick_keeps_or_replaces_the_open_detail() -> None:
    page = _report()["sessions"]
    same = page["sameForm"]
    assert same["writes"] == 3
    assert "&quot;pane&quot;: 9" in same["pre"]
    _assert_form(same["controls"])
    watched = page["watched"]
    assert watched["writes"] == 4
    assert watched["hidden"] is False
    assert watched["controls"] == _WATCH
    cleared = page["cleared"]
    assert cleared["selected"] is None
    assert cleared["hidden"] is True
    assert cleared["writes"] == 4
    assert cleared["err"] == {"hidden": True, "text": ""}
    assert cleared["sessions"] == _empty(6)
    assert cleared["tasks"] == _empty(6)
    assert cleared["rounds"] == _empty(6)
    assert cleared["agents"] == _empty(6)
    assert cleared["checks"] == _empty(5)
    assert cleared["gates"] == _empty(6)
    assert cleared["work"] == _empty(5)
    assert cleared["pings"] == _empty(4)
    back = page["back"]
    assert back["selected"] is None
    assert back["hidden"] is False
    assert back["writes"] == 4
    assert back["writeDelta"] == 0
    assert back["row"] == "session-row click"
    assert "selected" not in back["sessions"]
    assert 'data-id="a/b"' in back["sessions"]
    assert page["httpErr"] == {
        "hidden": False,
        "text": "Error: HTTP 503",
        "jsonCalls": 0,
        "url": "/api/state",
    }
    assert page["down"] == {"hidden": False, "text": "Error: down", "url": "/api/state"}


def test_usage_section_shows_the_inventory_error_only_while_it_is_set() -> None:
    page = _report()["usage"]
    first = page["first"]
    assert first["err"] == {"hidden": False, "text": _USAGE_ERROR}
    assert first["markerHidden"] is False
    assert first["markerY"] == "40px"
    assert first["rafs"] == 1
    assert first["drew"] == "1"
    assert first["resize"] == 1
    assert page["moved"] == "90px"
    by_name = {item["section"]: item for item in first["sections"]}
    assert by_name["usage"]["on"] is True and by_name["usage"]["hidden"] is False
    assert by_name["sessions"]["on"] is False and by_name["sessions"]["hidden"] is True
    _assert_nav(first["nav"], "/usage")
    assert page["second"]["err"] == {"hidden": True, "text": _USAGE_ERROR}


def test_unknown_section_hides_the_marker_and_skips_the_draw() -> None:
    page = _report()["missing"]
    assert page["markerHidden"] is True
    assert page["markerY"] == ""
    assert page["resize"] == 0
    assert page["rafs"] == 0
    assert page["drewOn"] is None
    assert page["calls"] == 1
    assert page["err"] == {"hidden": True, "text": ""}
    assert page["sections"]
    for item in page["sections"]:
        assert item["on"] is False
        assert item["hidden"] is True
        assert item["className"] == "dashboard-section"
    _assert_nav(page["nav"], None)


def test_new_order_skip_row_escapes_keys_and_tasks_link_moves() -> None:
    fresh = _report()["orders-new"]
    assert fresh["markerHidden"] is True
    assert fresh["markerY"] == ""
    assert fresh["resize"] == 0
    assert fresh["resultText"] == ""
    assert fresh["resultClass"] == ""
    assert fresh["calls"] == [
        {"url": "/api/orders/schema", "method": "GET", "body": None, "jsonCalls": 1}
    ]
    assert fresh["rowClass"] == "os-skip controls"
    assert fresh["rowHtml"] == _SKIP_ROW
    _assert_nav(fresh["nav"], None)
    tasks = _report()["orders-tasks"]
    assert tasks["markerHidden"] is False
    assert tasks["markerY"] == "48px"
    assert tasks["moved"] == "77px"
    assert tasks["resize"] == 1
    assert tasks["resultText"] == ""
    assert tasks["calls"] == [
        {"url": "/api/orders/schema", "method": "GET", "body": None, "jsonCalls": 1}
    ]
    _assert_nav(tasks["nav"], "/tasks")
