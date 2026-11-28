"""Line and branch coverage for src/agent_cli/static/design/work.js.

Covered functions: esc, chip, fillRings, enter, foldSegments, fold, watchFold.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.no_pg

NODE = shutil.which("node")
WORK = Path(__file__).resolve().parents[1] / "src" / "agent_cli" / "static" / "design" / "work.js"

_REPORT: dict[str, Any] | None = None

_HARNESS = r"""
const vm = require("vm");
const tooling = String.raw`
const queries = [];
const rafs = [];
const resizeListeners = [];
const postureListeners = [];
function matchMedia(query) {
  queries.push(query);
  return { matches: REDUCE_MATCHES === true && query === "(prefers-reduced-motion: reduce)" };
}
function requestAnimationFrame(fn) {
  rafs.push(fn);
  return rafs.length;
}
function flushRaf() {
  const batch = rafs.splice(0, rafs.length);
  batch.forEach((fn) => fn());
}
function setTimeout() { throw new Error("setTimeout"); }
function addEventListener(type, fn) {
  if (type !== "resize") throw new Error("unexpected listener " + type);
  resizeListeners.push(fn);
}
function DOMRect(x, y, width, height) {
  this.x = x;
  this.y = y;
  this.width = width;
  this.height = height;
  this.left = x;
  this.top = y;
  this.right = x + width;
  this.bottom = y + height;
}
const innerWidth = 1000;
const innerHeight = 800;
const location = { search: "" };
const navigator = {};
const window = globalThis;
window.viewport = undefined;

function classTokens(el) {
  return el.className.split(/\s+/).filter(Boolean);
}
function compound(part) {
  const out = { tag: null, id: null, classes: [] };
  const re = /#([A-Za-z0-9_-]+)|\.([A-Za-z0-9_-]+)|([A-Za-z][A-Za-z0-9-]*)/g;
  let match;
  while ((match = re.exec(part))) {
    if (match[1]) out.id = match[1];
    else if (match[2]) out.classes.push(match[2]);
    else if (match[3]) out.tag = match[3].toLowerCase();
  }
  return out;
}
function matchesCompound(el, spec) {
  if (spec.tag && el.tag !== spec.tag) return false;
  if (spec.id && el.id !== spec.id) return false;
  for (const name of spec.classes) {
    if (!el.classList.contains(name)) return false;
  }
  return true;
}
function descendants(root) {
  const out = [];
  const stack = root.children.slice();
  while (stack.length) {
    const el = stack.shift();
    out.push(el);
    stack.unshift(...el.children);
  }
  return out;
}
function queryAll(root, selector) {
  const parts = selector.trim().split(/\s+/).filter(Boolean).map(compound);
  function find(starts, index) {
    const found = [];
    for (const start of starts) {
      for (const el of descendants(start)) {
        if (!matchesCompound(el, parts[index])) continue;
        if (index === parts.length - 1) found.push(el);
        else found.push(...find([el], index + 1));
      }
    }
    return found;
  }
  return find([root], 0);
}
function make(tag) {
  const el = {
    tag: tag,
    id: "",
    className: "",
    dataset: {},
    children: [],
    events: [],
    reads: 0,
    removed: [],
    props: {},
    rect: { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 },
    paddingLeft: "0",
    paddingTop: "0",
  };
  el.style = {
    setProperty(name, value) {
      el.props[name] = String(value);
      el.events.push("set:" + name + "=" + value);
    },
    removeProperty(name) {
      delete el.props[name];
      el.removed.push(name);
    },
  };
  Object.defineProperty(el.style, "transition", {
    get() { return Object.prototype.hasOwnProperty.call(el.props, "transition") ? el.props.transition : ""; },
    set(value) { el.props.transition = value; },
  });
  Object.defineProperty(el.style, "strokeDasharray", {
    get() {
      return Object.prototype.hasOwnProperty.call(el.props, "stroke-dasharray") ? el.props["stroke-dasharray"] : "";
    },
    set(value) { el.props["stroke-dasharray"] = value; },
  });
  el.classList = {
    add(...names) {
      const tokens = classTokens(el);
      names.forEach((name) => {
        el.events.push("add:" + name);
        if (tokens.indexOf(name) === -1) tokens.push(name);
      });
      el.className = tokens.join(" ");
    },
    remove(...names) {
      const drop = {};
      names.forEach((name) => { drop[name] = true; el.events.push("remove:" + name); });
      el.className = classTokens(el).filter((name) => !drop[name]).join(" ");
    },
    contains(name) { return classTokens(el).indexOf(name) !== -1; },
    toggle(name, force) {
      const has = el.classList.contains(name);
      const on = force === undefined ? !has : !!force;
      if (on) el.classList.add(name);
      else el.classList.remove(name);
      return on;
    },
  };
  Object.defineProperty(el, "offsetWidth", {
    get() { el.reads += 1; el.events.push("read"); return 8; },
  });
  el.getBoundingClientRect = function () { return el.rect; };
  el.querySelectorAll = function (selector) { return queryAll(el, selector); };
  el.appendChild = function (child) { el.children.push(child); return child; };
  return el;
}
function getComputedStyle(el) {
  return { paddingLeft: el.paddingLeft, paddingTop: el.paddingTop };
}
const rootEl = make("html");
const document = {
  documentElement: rootEl,
  querySelectorAll(selector) { return queryAll(rootEl, selector); },
};
`;

const scenario = String.raw`
const chips = {};
[
  "queued", "waiting", "implementing", "ready", "Done", "failed", "cancelled", "mystery",
  "Review 2/5", "constructor",
].forEach((word) => { chips[word] = chip(word); });
const escaped = { nullish: esc(null), missing: esc(undefined), marks: esc('& < > "') };
chips["a <\"b\""] = chip("a <\"b\"");
chips["a <\"b\" 1/2"] = chip("a <\"b\" 1/2");

const ring = make("span");
ring.className = "ring";
const arc = make("circle");
arc.className = "arc";
arc.dataset.to = "18.85";
arc.style.transition = "sentinel";
arc.style.strokeDasharray = "sentinel";
ring.appendChild(arc);
const host = make("div");
host.appendChild(ring);
fillRings(host);
const sync = {
  transition: arc.style.transition,
  dash: arc.style.strokeDasharray,
  rafs: rafs.length,
};
flushRaf();
const outer = {
  transition: arc.style.transition,
  dash: arc.style.strokeDasharray,
  rafs: rafs.length,
};
flushRaf();
const inner = {
  transition: arc.style.transition,
  dash: arc.style.strokeDasharray,
  rafs: rafs.length,
};
const empty = make("div");
const beforeEmpty = rafs.length;
fillRings(empty);
const emptyQueued = rafs.length - beforeEmpty;
flushRaf();
flushRaf();
const emptyLeft = rafs.length;

function enterNode() {
  const node = make("div");
  node.className = "row jk-enter";
  node.events = [];
  node.reads = 0;
  return node;
}
const entered = [enterNode(), enterNode()];
enter(entered);

function pack(rect) {
  if (!rect) return null;
  return [rect.x, rect.y, rect.width, rect.height, rect.left, rect.top, rect.right, rect.bottom];
}
function packList(list) {
  return list ? list.map(pack) : null;
}
window.viewport = undefined;
location.search = "?fold=book";
const book = packList(foldSegments());
location.search = "?fold=book&hinge=40";
const bookHinge = packList(foldSegments());
location.search = "?fold=laptop";
const laptop = packList(foldSegments());
location.search = "?fold=laptop&hinge=40";
const laptopHinge = packList(foldSegments());
location.search = "?fold=book";
window.viewport = { segments: [] };
const bookBeatsEmpty = packList(foldSegments());
const seg = [new DOMRect(1, 2, 3, 4), new DOMRect(5, 6, 7, 8)];
window.viewport = { segments: seg };
location.search = "";
const copied = foldSegments();
const viewportCopy = {
  sameArray: copied === seg,
  same0: copied[0] === seg[0],
  same1: copied[1] === seg[1],
  left: copied[0].left,
  right: copied[1].right,
};
const lengths = {};
[0, 1, 3].forEach((n) => {
  window.viewport = { segments: Array.from({ length: n }, () => new DOMRect(0, 0, 1, 1)) };
  location.search = "";
  lengths[String(n)] = foldSegments();
});
window.viewport = undefined;
location.search = "";
const noViewport = foldSegments();
window.viewport = {};
const noSegments = foldSegments();
const tabletSeg = [new DOMRect(8, 0, 1, 1), new DOMRect(9, 0, 1, 1)];
window.viewport = { segments: tabletSeg };
location.search = "?fold=tablet";
const tabletFound = foldSegments();
const tablet = {
  same0: tabletFound !== null && tabletFound[0] === tabletSeg[0],
  length: tabletFound ? tabletFound.length : 0,
};

const grids = [0, 1, 2, 3].map(() => {
  const grid = make("div");
  grid.className = "fold-grid";
  grid.props["--fold-first"] = "stale";
  rootEl.appendChild(grid);
  return grid;
});
rootEl.dataset.fold = "stale";
rootEl.props["--fold-gap"] = "sentinel";
location.search = "";
window.viewport = undefined;
const cleared = fold();
const clearedSnap = {
  value: cleared,
  hasFold: Object.prototype.hasOwnProperty.call(rootEl.dataset, "fold"),
  gap: rootEl.props["--fold-gap"],
  first: grids.map((grid) => grid.props["--fold-first"]),
  removed: grids.map((grid) => grid.removed.slice()),
};
function place(grid, left, top, padL, padT) {
  grid.rect = { left: left, top: top, right: left, bottom: top, width: 0, height: 0 };
  grid.paddingLeft = padL;
  grid.paddingTop = padT;
}
place(grids[0], 0, 0, "0", "0");
place(grids[1], 0, 0, "10.4", "0");
place(grids[2], 370, 0, "0", "0");
place(grids[3], 344, 0, "0", "0");
location.search = "?fold=book&hinge=40";
const bookValue = fold();
const bookSnap = {
  value: bookValue,
  fold: rootEl.dataset.fold,
  gap: rootEl.props["--fold-gap"],
  first: grids.map((grid) => grid.props["--fold-first"]),
  removed: grids.map((grid) => grid.removed.slice()),
};
place(grids[0], 0, 0, "0", "0");
place(grids[1], 0, 0, "0", "10.6");
place(grids[2], 0, 250, "0", "0");
place(grids[3], 0, 244, "0", "0");
location.search = "?fold=laptop&hinge=40";
const laptopValue = fold();
const laptopSnap = {
  value: laptopValue,
  fold: rootEl.dataset.fold,
  gap: rootEl.props["--fold-gap"],
  first: grids.map((grid) => grid.props["--fold-first"]),
  removed: grids.map((grid) => grid.removed.slice()),
};
location.search = "?fold=book";
const touchValue = fold();
const touchSnap = {
  value: touchValue,
  fold: rootEl.dataset.fold,
  gap: rootEl.props["--fold-gap"],
};

location.search = "";
window.viewport = undefined;
const calls = [];
watchFold((value) => { calls.push(value); });
const firstListener = resizeListeners[resizeListeners.length - 1];
firstListener();
location.search = "?fold=book";
firstListener();
firstListener();
location.search = "?fold=laptop";
firstListener();
location.search = "";
firstListener();
const callsAfter = calls.slice();
watchFold();
const bare = resizeListeners[resizeListeners.length - 1];
location.search = "?fold=book";
let bareThrew = false;
try { bare(); } catch (error) { bareThrew = true; }
const bareFold = rootEl.dataset.fold;
navigator.devicePosture = {
  addEventListener(type, fn) { postureListeners.push({ type: type, fn: fn }); },
};
location.search = "";
const postureCalls = [];
watchFold((value) => { postureCalls.push(value); });
location.search = "?fold=laptop";
const posture = postureListeners[postureListeners.length - 1];
posture.fn();
posture.fn();
const postureCount = postureListeners.length;
navigator.devicePosture = undefined;
watchFold((value) => { postureCalls.push("unexpected"); });

globalThis.report = {
  reduce: REDUCE_MATCHES,
  queries: queries.slice(),
  escaped: escaped,
  chips: chips,
  fill: { sync: sync, outer: outer, inner: inner, emptyQueued: emptyQueued, emptyLeft: emptyLeft },
  enter: {
    events: entered.map((node) => node.events.slice()),
    classNames: entered.map((node) => node.className),
    reads: entered.map((node) => node.reads),
    index: entered.map((node) => (Object.prototype.hasOwnProperty.call(node.props, "--jk-i") ? node.props["--jk-i"] : null)),
  },
  segments: {
    book: book,
    bookHinge: bookHinge,
    laptop: laptop,
    laptopHinge: laptopHinge,
    bookBeatsEmpty: bookBeatsEmpty,
    viewportCopy: viewportCopy,
    lengths: lengths,
    noViewport: noViewport,
    noSegments: noSegments,
    tablet: tablet,
  },
  fold: { cleared: clearedSnap, book: bookSnap, laptop: laptopSnap, touch: touchSnap },
  watch: {
    calls: callsAfter,
    bareThrew: bareThrew,
    bareFold: bareFold,
    callsUntouched: calls.length === callsAfter.length,
    listeners: resizeListeners.length,
    postureType: posture.type,
    postureCalls: postureCalls.slice(),
    withoutPosture: postureListeners.length === postureCount,
  },
};
`;

function run(flag) {
  const source = "const REDUCE_MATCHES = " + (flag ? "true" : "false") + ";\n" + tooling + "\n" + workSource + "\n" + scenario;
  const NativeURLSearchParams = URLSearchParams;
  const context = vm.createContext({
    URLSearchParams: function URLSearchParams(init) { return new NativeURLSearchParams(init); },
  });
  vm.runInContext(source, context, { timeout: 5000 });
  return context.report;
}
console.log(JSON.stringify({ off: run(false), on: run(true) }));
"""


def _design_report() -> dict[str, Any]:
    global _REPORT
    if NODE is None:
        pytest.skip("node is not installed")
    if _REPORT is None:
        _REPORT = _run_design()
    return _REPORT


def _run_design() -> dict[str, Any]:
    script = "const workSource = " + json.dumps(WORK.read_text(encoding="utf-8")) + ";\n" + _HARNESS
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


def _idle(label: str, aria: str, meta: str = "") -> str:
    graphic = (
        '<g class="dash-spin"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--neutral-500)" '
        'stroke-width="2" stroke-dasharray="2.4 2.3"/></g>'
    )
    tail = f'<span class="meta">{meta}</span>' if meta else ""
    return (
        f'<span class="ring" role="img" aria-label="{aria}">'
        f'<svg viewBox="0 0 16 16" aria-hidden="true">{graphic}</svg>'
        f'<span class="label">{label}</span>{tail}</span>'
    )


def test_chip_covers_every_kind_count_and_escape() -> None:
    report = _design_report()
    assert report["off"]["chips"] == report["on"]["chips"]
    chips = report["off"]["chips"]
    assert chips["queued"] == _idle("queued", "queued")
    assert chips["mystery"] == _idle("mystery", "mystery")
    assert chips['a <"b"'] == _idle("a &lt;&quot;b&quot;", "a &lt;&quot;b&quot;")
    assert chips['a <"b" 1/2'] == _idle("a &lt;&quot;b&quot;", "a &lt;&quot;b&quot; 1/2", "1/2")
    assert chips["waiting"] == (
        '<span class="ring" role="img" aria-label="waiting"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle class="track" cx="8" cy="8" r="6"/>'
        '<circle class="arc jk-value-arc-c" cx="8" cy="8" r="6" stroke="var(--warn)" data-to="18.85" '
        'style="stroke-dasharray:18.85 37.70"/>'
        '<circle cx="8" cy="8" r="2.5" fill="var(--warn)"/></svg>'
        '<span class="label">waiting</span></span>'
    )
    assert chips["implementing"] == (
        '<span class="ring" role="img" aria-label="implementing"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle class="track" cx="8" cy="8" r="6"/><g class="spin">'
        '<circle class="arc jk-value-arc-c" cx="8" cy="8" r="6" stroke="var(--blue-500)" data-to="9.42" '
        'style="stroke-dasharray:9.42 37.70"/></g></svg>'
        '<span class="label">implementing</span></span>'
    )
    assert chips["Review 2/5"] == (
        '<span class="ring" role="img" aria-label="Review 2/5"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle class="track" cx="8" cy="8" r="6"/><g class="spin">'
        '<circle class="arc jk-value-arc-c" cx="8" cy="8" r="6" stroke="var(--blue-500)" data-to="28.27" '
        'style="stroke-dasharray:28.27 37.70"/></g></svg>'
        '<span class="label">Review</span><span class="meta">2/5</span></span>'
    )
    assert chips["ready"] == (
        '<span class="ring" role="img" aria-label="ready"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle cx="8" cy="8" r="6" fill="none" stroke="var(--status-success)" stroke-width="1.5"/>'
        '<path d="M5.6 8.1l1.7 1.7 3.1-3.3" fill="none" stroke="var(--status-success)" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        '<span class="label">ready</span></span>'
    )
    assert chips["Done"] == (
        '<span class="ring" role="img" aria-label="Done"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle cx="8" cy="8" r="7" fill="var(--navy-800)"/>'
        '<path d="M5 8.2l2 2 4-4.2" fill="none" stroke="#fff" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        '<span class="label">Done</span></span>'
    )
    assert chips["failed"] == (
        '<span class="ring" role="img" aria-label="failed"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle cx="8" cy="8" r="7" fill="var(--red-500)"/>'
        '<path d="M5.5 5.5l5 5M10.5 5.5l-5 5" fill="none" stroke="#fff" stroke-width="1.8" '
        'stroke-linecap="round"/></svg><span class="label">failed</span></span>'
    )
    assert chips["cancelled"] == (
        '<span class="ring" role="img" aria-label="cancelled"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle cx="8" cy="8" r="6" fill="none" stroke="var(--neutral-500)" stroke-width="2"/>'
        '<path d="M4 12L12 4" stroke="var(--neutral-500)" stroke-width="2" stroke-linecap="round"/></svg>'
        '<span class="label">cancelled</span></span>'
    )
    assert chips["constructor"] == _idle("constructor", "constructor")


def test_esc_covers_null_undefined_and_markup() -> None:
    report = _design_report()
    assert report["off"]["escaped"] == report["on"]["escaped"]
    escaped = report["off"]["escaped"]
    assert escaped["nullish"] == ""
    assert escaped["missing"] == ""
    assert escaped["marks"] == "&amp; &lt; &gt; &quot;"


def test_fill_rings_and_enter_follow_reduce() -> None:
    report = _design_report()
    off = report["off"]
    on = report["on"]
    assert off["queries"] == ["(prefers-reduced-motion: reduce)"]
    assert on["queries"] == ["(prefers-reduced-motion: reduce)"]
    assert off["reduce"] is False
    assert on["reduce"] is True
    assert off["fill"] == {
        "sync": {"transition": "none", "dash": "0 37.70", "rafs": 1},
        "outer": {"transition": "none", "dash": "0 37.70", "rafs": 1},
        "inner": {"transition": "", "dash": "18.85 37.70", "rafs": 0},
        "emptyQueued": 1,
        "emptyLeft": 0,
    }
    assert on["fill"] == {
        "sync": {"transition": "sentinel", "dash": "sentinel", "rafs": 0},
        "outer": {"transition": "sentinel", "dash": "sentinel", "rafs": 0},
        "inner": {"transition": "sentinel", "dash": "sentinel", "rafs": 0},
        "emptyQueued": 0,
        "emptyLeft": 0,
    }
    assert off["enter"] == {
        "events": [
            ["remove:jk-enter", "read", "set:--jk-i=0", "add:jk-enter"],
            ["remove:jk-enter", "read", "set:--jk-i=1", "add:jk-enter"],
        ],
        "classNames": ["row jk-enter", "row jk-enter"],
        "reads": [1, 1],
        "index": ["0", "1"],
    }
    assert on["enter"] == {
        "events": [[], []],
        "classNames": ["row jk-enter", "row jk-enter"],
        "reads": [0, 0],
        "index": [None, None],
    }


def test_fold_segments_book_laptop_and_viewport() -> None:
    segments = _design_report()["off"]["segments"]
    assert segments == _design_report()["on"]["segments"]
    assert segments["book"] == [
        [0, 0, 500, 800, 0, 0, 500, 800],
        [500, 0, 500, 800, 500, 0, 1000, 800],
    ]
    assert segments["bookHinge"] == [
        [0, 0, 480, 800, 0, 0, 480, 800],
        [520, 0, 480, 800, 520, 0, 1000, 800],
    ]
    assert segments["laptop"] == [
        [0, 0, 1000, 400, 0, 0, 1000, 400],
        [0, 400, 1000, 400, 0, 400, 1000, 800],
    ]
    assert segments["laptopHinge"] == [
        [0, 0, 1000, 380, 0, 0, 1000, 380],
        [0, 420, 1000, 380, 0, 420, 1000, 800],
    ]
    assert segments["bookBeatsEmpty"] == segments["book"]
    assert segments["viewportCopy"] == {
        "sameArray": False,
        "same0": True,
        "same1": True,
        "left": 1,
        "right": 12,
    }
    assert segments["lengths"] == {"0": None, "1": None, "3": None}
    assert segments["noViewport"] is None
    assert segments["noSegments"] is None
    assert segments["tablet"] == {"same0": True, "length": 2}


def test_fold_sets_gap_and_first_or_clears() -> None:
    fold = _design_report()["off"]["fold"]
    assert fold == _design_report()["on"]["fold"]
    assert fold["cleared"] == {
        "value": None,
        "hasFold": False,
        "gap": "sentinel",
        "first": ["stale", "stale", "stale", "stale"],
        "removed": [[], [], [], []],
    }
    assert fold["book"] == {
        "value": "book",
        "fold": "book",
        "gap": "40px",
        "first": ["464px", "454px", "120px", "120px"],
        "removed": [["--fold-first"], ["--fold-first"], ["--fold-first"], ["--fold-first"]],
    }
    assert fold["laptop"] == {
        "value": "laptop",
        "fold": "laptop",
        "gap": "40px",
        "first": ["364px", "353px", "120px", "120px"],
        "removed": [
            ["--fold-first", "--fold-first"],
            ["--fold-first", "--fold-first"],
            ["--fold-first", "--fold-first"],
            ["--fold-first", "--fold-first"],
        ],
    }
    assert fold["touch"] == {"value": "book", "fold": "book", "gap": "0px"}


def test_watch_fold_fires_only_when_the_value_changes() -> None:
    watch = _design_report()["off"]["watch"]
    assert watch == _design_report()["on"]["watch"]
    assert watch["calls"] == ["book", "laptop", None]
    assert watch["bareThrew"] is False
    assert watch["bareFold"] == "book"
    assert watch["callsUntouched"] is True
    assert watch["listeners"] == 4
    assert watch["postureType"] == "change"
    assert watch["postureCalls"] == ["laptop"]
    assert watch["withoutPosture"] is True
