from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _extract(html: str, name: str) -> str:
    marker = f"    function {name}("
    async_marker = f"    async function {name}("
    start = html.index(marker) if marker in html else html.index(async_marker)
    end = html.index("\n    }\n", start) + len("\n    }\n")
    return html[start:end]


def _index_html() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_core"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")


def test_request_id_final_statuses() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    function = _extract(_index_html(), "requestIdIsFinal")
    script = f"""
{function}
const cases = [
  [200, {{}}, true],
  [201, {{}}, true],
  [400, {{request_id_final: true}}, true],
  [403, {{request_id_final: true}}, true],
  [403, {{request_id_final: false}}, false],
  [404, {{request_id_final: true}}, true],
  [409, {{request_id_final: true}}, true],
  [409, {{request_id_final: false}}, false],
  [409, {{detail: "sign in on the website again"}}, false],
  [502, {{request_id_final: false, url: "https://github.test/issue/1"}}, false],
  [0, {{}}, false],
];
process.stdout.write(JSON.stringify(cases.map(([status, failure, expected]) => ({{
  status,
  failure,
  expected,
  actual: requestIdIsFinal(status, failure),
}}))));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    cases = json.loads(result.stdout)
    assert all(case["actual"] == case["expected"] for case in cases), cases


def test_assignment_and_reply_submit_handlers_execute_request_lifecycle() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in (
            "esc",
            "safeHref",
            "agentFormKey",
            "pendingRequestKey",
            "pendingRequestStorage",
            "pendingRequestId",
            "forgetPendingRequest",
            "clearRenderedRequestIds",
            "setFormBusy",
            "applyPartialAssignmentLock",
            "partialAssignmentLockUrl",
            "assignmentFailureText",
            "responseJson",
            "requestIdIsFinal",
        )
    )
    start = html.index('    document.addEventListener("submit"')
    end = html.index('    document.addEventListener("click"', start)
    submit_listener = html[start:end]
    script = functions + "\n" + r'''
const crypto = require("node:crypto");
class StubStorage {
  constructor() { this.items = new Map(); }
  getItem(key) { return this.items.has(key) ? this.items.get(key) : null; }
  setItem(key, value) { this.items.set(key, value); }
  removeItem(key) { this.items.delete(key); }
}
class StubResult {
  constructor() { this.hidden = true; this.textContent = ""; this.children = []; }
  replaceChildren(...children) { this.children = children; this.textContent = children.map(child => child.textContent || "").join(""); }
}
class StubForm {
  constructor(kind, dataset, values) {
    this.kind = kind;
    this.dataset = {...dataset};
    this.values = {...values};
    this.attributes = {};
    this.inert = false;
    this.controls = [
      ...Object.keys(values).map(name => name === "deviation"
        ? ({name, disabled: false, type: "checkbox", checked: values[name] === true, value: "authorized"})
        : ({name, disabled: false, value: values[name]})),
      {name: "submit", disabled: false, type: "submit"},
    ];
    this.deviation = this.controls.find(control => control.name === "deviation") || null;
    this.submit = this.controls[this.controls.length - 1];
    this.result = new StubResult();
    this.listeners = {};
    this.classList = {contains: value => value === kind};
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll(selector) {
    return selector === "input, textarea, select, button" ? this.controls : [];
  }
  querySelector(selector) {
    if (selector === ".assignment-failure") return this.result;
    if (selector === 'button[type="submit"]') return this.submit;
    if (selector === 'input[name="deviation"]') return this.deviation;
    if (selector === ".assignment-failure a") return this.result.children[0] || null;
    return null;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  removeEventListener(name, callback) { if (this.listeners[name] === callback) delete this.listeners[name]; }
  reset() { this.resetCount = (this.resetCount || 0) + 1; }
}
class StubFormData {
  constructor(form) { this.form = form; }
  get(name) { return this.form.values[name] ?? null; }
}
class StubAbortController {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(name, callback) { if (name === "abort") listeners.push(callback); },
      listeners,
    };
  }
  abort() {
    this.signal.aborted = true;
    this.signal.listeners.forEach(callback => callback());
  }
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}
const storage = new StubStorage();
global.sessionStorage = storage;
global.FormData = StubFormData;
global.AbortController = StubAbortController;
let timeoutId = 0;
const timeouts = new Map();
global.setTimeout = callback => { const id = ++timeoutId; timeouts.set(id, callback); return id; };
global.clearTimeout = id => timeouts.delete(id);
const actionError = {hidden: true, textContent: ""};
const assignForm = new StubForm("assign-form", {agent: "helperbot"}, {repo: "Acme/Widget", title: "Fix", body: "Details", deviation: false});
const replyForm = new StubForm("reply-form", {agent: "helperbot", repo: "Acme/Widget", number: "7"}, {body: "Reply"});
let submitHandler;
global.document = {
  addEventListener(name, callback) { if (name === "submit") submitHandler = callback; },
  getElementById(id) { if (id === "action-err") return actionError; throw new Error(id); },
  createElement() { return {href: "", target: "", rel: "", textContent: ""}; },
  querySelectorAll(selector) {
    if (selector === "#agents-list .assign-form") return [assignForm];
    if (selector === "#agents-list .reply-form") return [replyForm];
    if (selector === "#agents-list .assign-form, #agents-list .reply-form") return [assignForm, replyForm];
    return [];
  },
};
global.window = {__login: "alice"};
let agentSessionEpoch = 0;
const agentFormsInFlight = new Set();
const agentPostControllers = new Set();
const partialAssignmentLocks = new Map();
function clearActionError() { actionError.hidden = true; actionError.textContent = ""; }
const expiredAgents = [];
let repaintCount = 0;
function expireAssignmentLoad(agent) { expiredAgents.push(agent); }
async function repaintAgents() { repaintCount += 1; }
const posts = [];
global.fetch = (url, options) => {
  const next = deferred();
  options.signal.addEventListener("abort", () => {
    const error = new Error("aborted");
    error.name = "AbortError";
    next.reject(error);
  });
  posts.push({url, options, next});
  return next.promise;
};
''' + submit_listener + r'''
async function submit(form) {
  return submitHandler({target: form, preventDefault() {}});
}
(async () => {
  await submit(assignForm);
  const unchecked = {
    postCount: posts.length,
    message: actionError.textContent,
    visible: !actionError.hidden,
  };
  assignForm.deviation.checked = true;
  const firstRun = submit(assignForm);
  const busyDuringPost = {
    submitting: assignForm.dataset.submitting,
    ariaDisabled: assignForm.attributes["aria-disabled"],
    inert: assignForm.inert,
    controls: assignForm.controls.map(control => control.disabled),
  };
  const retryId = JSON.parse(posts[0].options.body).request_id;
  const assignmentPayload = JSON.parse(posts[0].options.body);
  posts[0].next.resolve({
    ok: false,
    status: 403,
    json: async () => ({detail: "verification failed", request_id_final: false, assignee_missing: true, url: "https://github.com/Acme/Widget/issues/7"}),
  });
  await firstRun;
  const retryState = {
    requestId: assignForm.dataset.requestId,
    stored: JSON.parse([...storage.items.values()][0]).request_id,
    link: assignForm.result.children[0].href,
    submitDisabled: assignForm.submit.disabled,
  };
  assignForm.listeners.input();

  const secondRun = submit(assignForm);
  const finalId = JSON.parse(posts[1].options.body).request_id;
  posts[1].next.resolve({
    ok: false,
    status: 400,
    json: async () => ({detail: "invalid", request_id_final: true}),
  });
  await secondRun;
  const finalState = {
    requestIdPresent: Object.prototype.hasOwnProperty.call(assignForm.dataset, "requestId"),
    storedCount: storage.items.size,
    error: actionError.textContent,
  };

  const replyRun = submit(replyForm);
  const replyBusy = replyForm.controls.every(control => control.disabled);
  const replyPayload = JSON.parse(posts[2].options.body);
  posts[2].next.resolve({ok: true, status: 201, json: async () => ({url: "https://github.com/Acme/Widget/issues/7#issuecomment-1"})});
  await replyRun;

  const timeoutRun = submit(assignForm);
  const timeoutRequestId = JSON.parse(posts[3].options.body).request_id;
  [...timeouts.values()][0]();
  await timeoutRun;
  process.stdout.write(JSON.stringify({
    unchecked,
    assignmentPayload,
    busyDuringPost,
    retryId,
    retryState,
    finalId,
    finalState,
    replyBusy,
    replyUrl: posts[2].url,
    replyPayload,
    replyRequestIdPresent: Object.prototype.hasOwnProperty.call(replyForm.dataset, "requestId"),
    replyResetCount: replyForm.resetCount,
    timeoutPayloadId: timeoutRequestId,
    timeoutFormId: assignForm.dataset.requestId,
    timeoutStoredCount: storage.items.size,
    timeoutMessage: actionError.textContent,
    expiredAgents,
    repaintCount,
  }));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
'''
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["unchecked"] == {
        "postCount": 0,
        "message": "Authorize no deviation from the repository's contribution rules before assigning.",
        "visible": True,
    }
    assert out["assignmentPayload"]["deviation"] == {"declared": False}
    assert out["busyDuringPost"] == {
        "submitting": "true",
        "ariaDisabled": "true",
        "inert": True,
        "controls": [True, True, True, True, True],
    }
    assert out["retryState"] == {
        "requestId": out["retryId"],
        "stored": out["retryId"],
        "link": "https://github.com/Acme/Widget/issues/7",
        "submitDisabled": True,
    }
    assert out["finalId"] == out["retryId"]
    assert out["finalState"] == {
        "requestIdPresent": False,
        "storedCount": 0,
        "error": "invalid",
    }
    assert out["replyBusy"] is True
    assert out["replyUrl"] == "/api/assignments/Acme/Widget/7/comments"
    assert out["replyPayload"]["body"] == "Reply"
    assert isinstance(out["replyPayload"]["request_id"], str)
    assert out["replyRequestIdPresent"] is False
    assert out["replyResetCount"] == 1
    assert out["timeoutFormId"] == out["timeoutPayloadId"]
    assert out["timeoutStoredCount"] == 1
    assert out["timeoutMessage"] == (
        "Request did not complete — check the assignment list before submitting again"
    )
    assert out["expiredAgents"] == ["helperbot", "helperbot"]
    assert out["repaintCount"] == 2


def test_pending_request_ids_survive_reload_and_storage_errors() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in (
            "pendingRequestKey",
            "pendingRequestStorage",
            "pendingRequestId",
            "forgetPendingRequest",
        )
    )
    script = f"""
const crypto = require("node:crypto");
{functions}
class StubStorage {{
  constructor() {{ this.items = new Map(); }}
  getItem(key) {{ return this.items.has(key) ? this.items.get(key) : null; }}
  setItem(key, value) {{ this.items.set(key, value); }}
  removeItem(key) {{ this.items.delete(key); }}
}}
class ThrowingStorage {{
  getItem() {{ throw new Error("unavailable"); }}
  setItem() {{ throw new Error("unavailable"); }}
  removeItem() {{ throw new Error("unavailable"); }}
}}
const storage = new StubStorage();
const formKey = JSON.stringify(["assign", "helperbot"]);
const key = pendingRequestKey("alice", formKey);
const payload = JSON.stringify({{agent: "helperbot", repo: "Acme/Widget", title: "T", body: "B", deviation: {{declared: false}}}});
const changedPayload = JSON.stringify({{agent: "helperbot", repo: "Acme/Widget", title: "Changed", body: "B", deviation: {{declared: false}}}});
const first = pendingRequestId(storage, key, payload);
const stored = JSON.parse(storage.getItem(key));
const reused = pendingRequestId(storage, key, payload);
const replaced = pendingRequestId(storage, key, changedPayload);
const replacement = JSON.parse(storage.getItem(key));
forgetPendingRequest(storage, key);
const forgotten = storage.getItem(key);
const throwing = new ThrowingStorage();
const fallback = pendingRequestId(throwing, "key", payload);
const keptFormId = pendingRequestId(throwing, "key", payload, "kept-form-id");
forgetPendingRequest(throwing, "key");
Object.defineProperty(globalThis, "sessionStorage", {{
  configurable: true,
  get() {{ throw new Error("blocked"); }},
}});
const unavailable = pendingRequestStorage();
process.stdout.write(JSON.stringify({{
  key,
  first,
  stored,
  reused,
  replaced,
  replacement,
  forgotten,
  fallback,
  keptFormId,
  unavailable,
}}));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["keptFormId"] == "kept-form-id"
    expected_payload = json.dumps(
        {
            "agent": "helperbot",
            "repo": "Acme/Widget",
            "title": "T",
            "body": "B",
            "deviation": {"declared": False},
        },
        separators=(",", ":"),
    )
    assert out["key"] == 'agent-core:pending:alice:["assign","helperbot"]'
    assert out["stored"] == {
        "request_id": out["first"],
        "payload": expected_payload,
    }
    assert out["reused"] == out["first"]
    assert out["replaced"] != out["first"]
    assert out["replacement"]["request_id"] == out["replaced"]
    assert out["replacement"]["payload"] != out["stored"]["payload"]
    assert out["forgotten"] is None
    assert isinstance(out["fallback"], str) and len(out["fallback"]) == 36
    assert out["unavailable"] is None


def test_agent_card_decisions_and_drafts() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in (
            "agentCardOrder",
            "agentCardState",
            "captureAgentDraft",
            "restoreAgentDraft",
            "agentDraftFieldValue",
            "restoreAgentDraftField",
            "partialAssignmentLockUrl",
            "assignmentFailureText",
        )
    )
    script = f"""
{functions}
const sourceOrder = ["zetabot", "alphabot"];
const ordered = agentCardOrder(sourceOrder);
const orderedBeforeMutation = ordered.slice();
ordered.push("otherbot");
const states = [
  agentCardState({{assignments: [{{number: 1}}], error: null}}),
  agentCardState({{assignments: [], error: "HTTP 502"}}),
  agentCardState({{assignments: [], error: "Timed out loading assignments."}}),
  agentCardState({{assignments: [], error: null, loading: true}}),
];
const alphaKey = JSON.stringify(["assign", "alphabot"]);
const zetaKey = JSON.stringify(["assign", "zetabot"]);
let drafts = {{}};
drafts = captureAgentDraft(drafts, alphaKey, {{repo: "Acme/One", title: "A", deviation: true}}, "request-a");
drafts = captureAgentDraft(drafts, zetaKey, {{repo: "Acme/Two", title: "Z", deviation: false}}, "request-z");
const alpha = restoreAgentDraft(drafts, alphaKey);
alpha.repo = "changed";
const checkbox = {{type: "checkbox", checked: true, value: "authorized"}};
const checkboxDraft = agentDraftFieldValue(checkbox);
restoreAgentDraftField(checkbox, false);
const restoredUnchecked = checkbox.checked;
restoreAgentDraftField(checkbox, true);
process.stdout.write(JSON.stringify({{
  orderedBeforeMutation,
  sourceOrder,
  nonArrayOrder: agentCardOrder(null),
  states,
  alpha: restoreAgentDraft(drafts, alphaKey),
  zeta: restoreAgentDraft(drafts, zetaKey),
  missing: restoreAgentDraft(drafts, JSON.stringify(["assign", "missingbot"])),
  checkboxDraft,
  restoredUnchecked,
  restoredChecked: checkbox.checked,
  lockUrls: [
    partialAssignmentLockUrl({{url: "https://github.test/issue/1", assignee_missing: true}}),
    partialAssignmentLockUrl({{url: "https://github.test/issue/1"}}),
    partialAssignmentLockUrl({{url: "", assignee_missing: true}}),
    partialAssignmentLockUrl({{detail: "failed"}}),
    partialAssignmentLockUrl({{url: 7}}),
  ],
  failureTexts: [
    assignmentFailureText({{detail: "sign in on the website again", url: "https://github.test/issue/1"}}),
    assignmentFailureText({{detail: "failed"}}),
    assignmentFailureText({{}}),
  ],
}}));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["orderedBeforeMutation"] == ["zetabot", "alphabot"]
    assert out["sourceOrder"] == ["zetabot", "alphabot"]
    assert out["nonArrayOrder"] == []
    assert out["states"] == [
        {"kind": "success", "message": ""},
        {"kind": "error", "message": "Could not load assignments: HTTP 502"},
        {
            "kind": "timeout",
            "message": "Could not load assignments: Timed out loading assignments.",
        },
        {"kind": "loading", "message": "Loading assignments…"},
    ]
    assert out["alpha"] == {
        "repo": "Acme/One",
        "title": "A",
        "deviation": True,
        "__requestId": "request-a",
    }
    assert out["zeta"] == {
        "repo": "Acme/Two",
        "title": "Z",
        "deviation": False,
        "__requestId": "request-z",
    }
    assert out["missing"] == {}
    assert out["checkboxDraft"] is True
    assert out["restoredUnchecked"] is False
    assert out["lockUrls"] == ["https://github.test/issue/1", None, None, None, None]
    assert out["failureTexts"] == [
        "sign in on the website again (https://github.test/issue/1)",
        "failed",
        "assignment failed",
    ]


def test_assignment_load_success_errors_timeout_cache_and_invalidation() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in ("responseJson", "expireAssignmentLoad", "loadAssignmentsForAgent")
    )
    script = functions + r'''
const ASSIGNMENT_REFRESH_MS = 15000;
const ASSIGNMENT_LOAD_TIMEOUT_MS = 20000;
const assignmentLoads = new Map();
const assignmentResults = new Map();
const assignmentCompletedAt = new Map();
const assignmentInvalidations = new Map();
let now = 1000;
Date.now = () => now;
let timerId = 0;
const timers = new Map();
global.setTimeout = callback => { const id = ++timerId; timers.set(id, callback); return id; };
global.clearTimeout = id => timers.delete(id);
class StubAbortController {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(name, callback) { if (name === "abort") listeners.push(callback); },
      listeners,
    };
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.signal.listeners.forEach(callback => callback());
  }
}
global.AbortController = StubAbortController;
const plans = [];
let fetchCount = 0;
global.fetch = (_url, options) => {
  fetchCount += 1;
  const plan = plans.shift();
  if (plan === "hang") {
    return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    }));
  }
  return Promise.resolve(plan);
};
const ok = assignments => ({ok: true, status: 200, json: async () => ({assignments})});
(async () => {
  plans.push(ok([{number: 2}, {number: 1}]));
  const first = await loadAssignmentsForAgent("alpha");
  await Promise.resolve();
  now = 2000;
  const cached = await loadAssignmentsForAgent("alpha");
  const afterCacheFetchCount = fetchCount;

  now = 17000;
  plans.push(ok([{number: 3}]));
  const refreshed = await loadAssignmentsForAgent("alpha");
  await Promise.resolve();
  expireAssignmentLoad("alpha");
  plans.push(ok([{number: 4}]));
  const invalidated = await loadAssignmentsForAgent("alpha");
  await Promise.resolve();

  plans.push({ok: false, status: 502, json: async () => ({detail: "upstream failed"})});
  const fetchError = await loadAssignmentsForAgent("beta");
  await Promise.resolve();

  plans.push("hang");
  const abortedPromise = loadAssignmentsForAgent("gamma");
  expireAssignmentLoad("gamma");
  const aborted = await abortedPromise;

  plans.push("hang");
  const timeoutPromise = loadAssignmentsForAgent("delta");
  const timeoutCallback = [...timers.values()][0];
  timeoutCallback();
  const timedOut = await timeoutPromise;
  process.stdout.write(JSON.stringify({
    first,
    cached,
    afterCacheFetchCount,
    refreshed,
    invalidated,
    fetchError,
    aborted,
    timedOut,
    totalFetchCount: fetchCount,
  }));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
'''
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["first"] == {"assignments": [{"number": 2}, {"number": 1}], "error": None}
    assert out["cached"] == out["first"]
    assert out["afterCacheFetchCount"] == 1
    assert out["refreshed"] == {"assignments": [{"number": 3}], "error": None}
    assert out["invalidated"] == {"assignments": [{"number": 4}], "error": None}
    assert out["fetchError"] == {"assignments": [], "error": "upstream failed"}
    assert out["aborted"] == {"assignments": [], "error": "request aborted"}
    assert out["timedOut"] == {
        "assignments": [],
        "error": "Timed out loading assignments.",
    }
    assert out["totalFetchCount"] == 6


def test_agent_cards_render_in_owner_and_assignment_order() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in (
            "esc",
            "safeHref",
            "sessionLink",
            "agentCardState",
            "agentTaskSummary",
            "assignmentCommentsHtml",
            "renderAgentCards",
        )
    )
    script = functions + r'''
const list = {innerHTML: ""};
global.document = {getElementById: id => { if (id === "agents-list") return list; throw new Error(id); }};
function captureAgentDrafts() { return {}; }
function captureAgentFocus() { return null; }
function restoreAgentFormState() {}
function restoreAgentFocus() {}
renderAgentCards(
  ["zetabot", "alphabot"],
  {
    session: [
      {_github_login: "alphabot", id: "alpha-session", status: "open"},
      {_github_login: "zetabot", id: "zeta-session", status: "closed"},
    ],
    task: [],
  },
  [
    {assignments: [
      {repo: "Acme/One", number: 2, title: "newer", state: "open", url: "https://github.com/Acme/One/issues/2", comments: []},
      {repo: "Acme/One", number: 1, title: "older", state: "closed", url: "https://github.com/Acme/One/issues/1", comments: []},
    ], error: null},
    {assignments: [], error: null},
  ],
);
process.stdout.write(JSON.stringify({html: list.innerHTML}));
'''
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    rendered = json.loads(result.stdout)["html"]
    assert rendered.index("@zetabot") < rendered.index("@alphabot")
    assert rendered.index("zeta-session") < rendered.index("alpha-session")
    assert rendered.index("#2 — newer") < rendered.index("#1 — older")
    assert 'href="https://github.com/Acme/One/issues/2"' in rendered
    assert "No assignments." in rendered


def test_agent_render_drops_stale_generation() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name) for name in ("agentCardOrder", "renderAgents")
    )
    script = functions + r'''
let agentRenderGen = 0;
let latestAgentState = {};
const assignmentResults = new Map();
const section = {classList: {toggle() {}}};
const list = {innerHTML: ""};
global.document = {getElementById: id => id === "agents-section" ? section : list};
const paints = [];
function renderAgentCards(owned, _state, sets) {
  paints.push({owned: owned.slice(), sets: sets.map(set => ({...set}))});
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
const loads = {oldbot: deferred(), newbot: deferred()};
function loadAssignmentsForAgent(agent) { return loads[agent].promise; }
(async () => {
  const oldRender = renderAgents(["oldbot"], {session: []});
  const newRender = renderAgents(["newbot"], {session: []});
  loads.newbot.resolve({assignments: [{number: 2}], error: null});
  await newRender;
  loads.oldbot.resolve({assignments: [{number: 1}], error: null});
  await oldRender;
  process.stdout.write(JSON.stringify(paints));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
'''
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    paints = json.loads(result.stdout)
    completed = [paint for paint in paints if not paint["sets"][0].get("loading")]
    assert completed == [
        {
            "owned": ["newbot"],
            "sets": [{"assignments": [{"number": 2}], "error": None}],
        }
    ]


def test_safe_href_escaping_and_usage_renderers() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in (
            "esc",
            "safeHref",
            "sessionLink",
            "fillLaneUsage",
            "usd",
            "fillAccountUsage",
        )
    )
    script = functions + r'''
const tables = {
  "#lane-usage tbody": {innerHTML: ""},
  "#account-usage tbody": {innerHTML: ""},
};
global.document = {querySelector: selector => tables[selector]};
fillLaneUsage([
  {login: '<owner&>', session_id: 's/1', provider: 'p"', model: '<m>', runs: 0, input_tokens: 12, output_tokens: 0},
  {},
]);
fillAccountUsage([
  {login: '<owner>', account: 'a&b', usage_usd: 1.25, usage_daily_usd: 0, limit_usd: 5, fetched_at: '<now>'},
  {},
]);
process.stdout.write(JSON.stringify({
  escaped: esc('<&">'),
  hrefs: [
    safeHref('https://github.com/Owner/Repo/issues/1'),
    safeHref('http://github.com/Owner/Repo'),
    safeHref('javascript:alert(1)'),
    safeHref('https://github.example/Owner/Repo'),
    safeHref('not a url'),
  ],
  lanes: tables["#lane-usage tbody"].innerHTML,
  accounts: tables["#account-usage tbody"].innerHTML,
}));
'''
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["escaped"] == "&lt;&amp;&quot;&gt;"
    assert out["hrefs"] == [
        "https://github.com/Owner/Repo/issues/1",
        None,
        None,
        None,
        None,
    ]
    assert "&lt;owner&amp;&gt;" in out["lanes"]
    assert 'p&quot;' in out["lanes"]
    assert ">0</td>" in out["lanes"]
    assert ">12</td>" in out["lanes"]
    assert '<a href="/session?id=s%2F1">s/1</a>' in out["lanes"]
    assert out["lanes"].count("<tr>") == 2
    assert "undefined" not in out["lanes"]
    assert "$1.2500" in out["accounts"]
    assert "$0.0000" in out["accounts"]
    assert "&lt;now&gt;" in out["accounts"]
    assert out["accounts"].count("<tr>") == 2
    assert "undefined" not in out["accounts"]


def test_kick_usage_throttles_reuses_inflight_and_aborts() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name) for name in ("abortHubFetches", "kickUsage")
    )
    script = functions + r'''
let stateInflight = null;
let prsInflight = null;
let usageInflight = null;
let usageGen = 0;
let usageLastStartedAt = 0;
let now = 20000;
Date.now = () => now;
class StubAbortController {
  constructor() { this.signal = {aborted: false}; }
  abort() { this.signal.aborted = true; }
}
global.AbortController = StubAbortController;
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
const requests = [];
function jsonGet(url, ctl, ms) {
  const request = deferred();
  requests.push({url, ctl, ms, request});
  return request.promise;
}
const painted = {lanes: [], accounts: [], grok: []};
function fillLaneUsage(rows) { painted.lanes.push(rows); }
function fillAccountUsage(rows) { painted.accounts.push(rows); }
function fillUsage(rows) { painted.grok.push(rows); }
const total = {textContent: ""};
global.document = {getElementById: id => { if (id === "k-usage") return total; throw new Error(id); }};
(async () => {
  const first = kickUsage();
  const reused = kickUsage();
  requests[0].request.resolve({
    lanes: [{input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4}],
    accounts: [{account: "one"}],
    grok: [{account: "two"}],
  });
  await first;
  await Promise.resolve();
  now = 34999;
  const throttled = kickUsage();
  now = 35000;
  const second = kickUsage();
  const secondController = requests[1].ctl;
  abortHubFetches();
  requests[1].request.resolve({lanes: [{input_tokens: 99}], accounts: [], grok: []});
  await second;
  await Promise.resolve();
  process.stdout.write(JSON.stringify({
    reused: reused === first,
    throttled,
    requestCount: requests.length,
    request: {url: requests[0].url, ms: requests[0].ms},
    painted,
    total: total.textContent,
    secondIsPromise: Boolean(second && typeof second.then === "function"),
    secondAborted: secondController.signal.aborted,
    inflightCleared: usageInflight === null,
  }));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
'''
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out == {
        "reused": True,
        "throttled": None,
        "requestCount": 2,
        "request": {"url": "/api/usage", "ms": 15000},
        "painted": {
            "lanes": [
                [
                    {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 4,
                    }
                ]
            ],
            "accounts": [[{"account": "one"}]],
            "grok": [[{"account": "two"}]],
        },
        "total": 10,
        "secondIsPromise": True,
        "secondAborted": True,
        "inflightCleared": True,
    }


def test_dashboard_uses_only_query_session_api_routes() -> None:
    html = _index_html()
    assert "/api/sessions/" not in html
    assert 'fetch("/api/session?id=" + encodeURIComponent(id))' in html
    assert 'new EventSource("/api/session/terminal?id=" + encodeURIComponent(id))' in html
    assert 'fetch("/api/session/control?id=" + encodeURIComponent(openSessionId)' in html


def test_clear_hub_tables_does_not_forget_pending_requests() -> None:
    assert "sessionStorage" not in _extract(_index_html(), "clearHubTables")


def test_pair_selector_lists_login_then_agents_keeps_selection_and_builds_payload() -> None:
    node = shutil.which("node")
    # Node is a test requirement (README); a missing node must fail, not skip.
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in ("esc", "pairChoices", "fillPairSelect", "pairConfirmPayload")
    )
    script = f"""
class StubSelect {{
  constructor() {{ this.options = []; this._value = ""; }}
  set innerHTML(markup) {{
    this.markup = markup;
    this.options = [...markup.matchAll(/<option value="([^"]*)">/g)].map(match => match[1]);
    this._value = this.options[0] || "";
  }}
  get value() {{ return this._value; }}
  set value(next) {{ if (this.options.includes(next)) this._value = next; }}
}}
{functions}
const user = {{login: "alice", owned_agents: ["alphabot", "zetabot"]}};
const select = new StubSelect();
fillPairSelect(select, user);
const first = {{options: select.options, value: select.value}};
select.value = "zetabot";
fillPairSelect(select, user);
const kept = select.value;
fillPairSelect(select, {{login: "alice", owned_agents: ["alphabot"]}});
const dropped = select.value;
fillPairSelect(select, {{login: "bob"}});
const noAgents = select.options;
process.stdout.write(JSON.stringify({{
  first,
  kept,
  dropped,
  noAgents,
  self: pairConfirmPayload("c1", "alice", "alice"),
  agent: pairConfirmPayload("c2", "zetabot", "alice"),
  empty: pairConfirmPayload("c3", "", "alice"),
}}));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["first"] == {"options": ["alice", "alphabot", "zetabot"], "value": "alice"}
    assert out["kept"] == "zetabot"
    assert out["dropped"] == "alice"
    assert out["noAgents"] == ["bob"]
    assert out["self"] == {"challenge": "c1"}
    assert out["agent"] == {"challenge": "c2", "as_login": "zetabot"}
    assert out["empty"] == {"challenge": "c3"}


def test_open_session_ignores_a_superseded_answer() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    function = _extract(_index_html(), "openSession")
    script = f"""
let termEs = null;
let openSessionId = null;
let sessionOpenGeneration = 0;
let openCanControl = false;
const streams = [];
class EventSource {{
  constructor(url) {{ this.url = url; this.closed = false; streams.push(this); }}
  close() {{ this.closed = true; }}
}}
const elements = {{}};
const document = {{
  getElementById(id) {{
    if (!elements[id]) {{
      elements[id] = {{
        id, textContent: "", disabled: false,
        classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
      }};
    }}
    return elements[id];
  }},
}};
function setControlEnabled() {{}}
function appendTermChunk() {{}}
const pending = {{}};
function fetch(url) {{
  const id = decodeURIComponent(url.split("id=")[1]);
  return new Promise(resolve => {{ pending[id] = resolve; }});
}}
function answer(id) {{
  pending[id]({{ ok: true, status: 200, json: async () => ({{
    id, payload: {{ host: "h", status: "active" }}, can_control: true, control_connected: true,
  }}) }});
}}
{function}
(async () => {{
  const first = openSession("session-a");
  const second = openSession("session-b");
  answer("session-b");
  await second;
  answer("session-a");
  await first;
  process.stdout.write(JSON.stringify({{
    openSessionId,
    meta: document.getElementById("session-meta").textContent,
    streams: streams.map(s => ({{ url: s.url, closed: s.closed }})),
  }}));
}})();
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state["openSessionId"] == "session-b"
    assert state["meta"].startswith("id=session-b ")
    assert state["streams"] == [
        {"url": "/api/session/terminal?id=session-b", "closed": False}
    ]


def test_assignment_comments_say_not_loaded_without_enrichment() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(_extract(html, name) for name in ("esc", "safeHref", "assignmentCommentsHtml"))
    script = f"""
{functions}
process.stdout.write(JSON.stringify([
  assignmentCommentsHtml({{enriched: false}}),
  assignmentCommentsHtml({{}}),
  assignmentCommentsHtml({{enriched: true, comments: []}}),
  assignmentCommentsHtml({{enriched: true, comments: [{{author: "alice", created_at: "t", body: "hi", url: "https://github.com/a/b/issues/1#c"}}]}}),
]));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    not_loaded, missing, empty, one = json.loads(result.stdout)
    assert not_loaded == missing == "<li>Comments not loaded.</li>"
    assert empty == "<li>No comments.</li>"
    assert "<b>alice</b>" in one and "hi" in one
