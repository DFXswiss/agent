from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _session_html() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_core"
        / "static"
        / "session.html"
    ).read_text(encoding="utf-8")


def _run_session_page(
    status: int,
    payload: dict[str, object] | None = None,
    *,
    search: str = "?id=session-1",
) -> dict[str, object]:
    node = shutil.which("node")
    assert node is not None, "node is required to run the session page test"
    html = _session_html()
    start = html.index("    function esc(")
    end = html.index("    load();", start)
    functions = html[start:end]
    script = functions + r'''
class StubElement {
  constructor(id) {
    this.id = id;
    this.textContent = "";
    this.href = "";
    this.children = [];
    this.innerHTML = "";
    this.classes = new Set();
    this.classList = {
      add: value => this.classes.add(value),
      remove: value => this.classes.delete(value),
    };
  }
  replaceChildren(...children) {
    this.children = children;
    this.textContent = children.map(child => typeof child === "string" ? child : child.textContent).join("");
  }
}
const elements = new Map([
  ["notice", new StubElement("notice")],
  ["session-id", new StubElement("session-id")],
  ["timeline-body", new StubElement("timeline-body")],
]);
global.document = {
  getElementById: id => elements.get(id),
  createElement: tag => new StubElement(tag),
  querySelector: selector => selector === "#timeline tbody" ? elements.get("timeline-body") : null,
};
const responseStatus = __STATUS__;
const responsePayload = __PAYLOAD__;
let fetchedUrl = "";
global.fetch = async url => {
  fetchedUrl = url;
  return {
    status: responseStatus,
    ok: responseStatus >= 200 && responseStatus < 300,
    json: async () => responsePayload,
  };
};
global.location = {pathname: "/session", search: __SEARCH__};
load().then(() => {
  const notice = elements.get("notice");
  process.stdout.write(JSON.stringify({
    notice: notice.textContent,
    noticeClasses: [...notice.classes],
    body: elements.get("timeline-body").innerHTML,
    sessionId: elements.get("session-id").textContent,
    fetchedUrl,
  }));
}).catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
'''
    script = (
        script.replace("__STATUS__", str(status))
        .replace("__PAYLOAD__", json.dumps(payload or {}))
        .replace("__SEARCH__", json.dumps(search))
    )
    completed = subprocess.run(
        [node, "-"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_session_page_shows_signed_out_notice_and_decodes_session_id() -> None:
    node = shutil.which("node")
    # Node is a test requirement (README); a missing node must fail, not skip.
    assert node is not None, "node is required to run the session page test"

    html = _session_html()
    start = html.index("    function showSignedOut()")
    end = html.index("    load();", start)
    functions = html[start:end]
    script = f"""
class StubElement {{
  constructor(id) {{
    this.id = id;
    this.textContent = "";
    this.href = "";
    this.children = [];
    this.classes = new Set();
    this.classList = {{
      add: value => this.classes.add(value),
      remove: value => this.classes.delete(value),
    }};
  }}
  replaceChildren(...children) {{
    this.children = children;
    this.textContent = children.map(child => typeof child === "string" ? child : child.textContent).join("");
  }}
}}

const elements = new Map([
  ["notice", new StubElement("notice")],
  ["session-id", new StubElement("session-id")],
]);
global.document = {{
  getElementById: id => elements.get(id),
  createElement: tag => new StubElement(tag),
}};
let fetchedUrl = "";
global.fetch = async url => {{
  fetchedUrl = url;
  return {{status: 401, ok: false}};
}};
global.location = {{pathname: "/session", search: "?id=a%2F..%2Fb"}};

{functions}

load().then(() => {{
  const notice = elements.get("notice");
  const link = notice.children[1];
  process.stdout.write(JSON.stringify({{
    notice: notice.textContent,
    href: link.href,
    sessionId: elements.get("session-id").textContent,
    fetchedUrl,
  }}));
}}).catch(error => {{
  process.stderr.write(String(error));
  process.exitCode = 1;
}});
"""

    completed = subprocess.run(
        [node],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "notice": "Signed out — sign in on the dashboard",
        "href": "/",
        "sessionId": "a/../b",
        "fetchedUrl": "/api/session-timeline?id=a%2F..%2Fb",
    }


def test_session_page_renders_existing_error_for_missing_id() -> None:
    out = _run_session_page(200, search="")

    assert out["notice"] == "Error: Invalid session id."
    assert out["noticeClasses"] == ["error"]
    assert out["sessionId"] == ""
    assert out["fetchedUrl"] == ""


def test_session_page_renders_timeline_rows_and_safe_assignment_links() -> None:
    out = _run_session_page(
        200,
        {
            "timeline": [
                {
                    "table": "activity",
                    "at": "2026-11-05T10:00:00Z",
                    "payload": {
                        "type": "lane.run",
                        "payload": {
                            "role": "worker<one>",
                            "vendor": "provider&one",
                            "model": "model\"one",
                            "status": "error",
                            "usage": {"input_tokens": 12, "output_tokens": 3},
                        },
                    },
                    "tools": {
                        "calls": [
                            {"tool": "Read<file>", "target": "a&b", "error": False},
                            {
                                "tool": "Run",
                                "target": "command",
                                "error": "failed <bad>",
                            },
                        ],
                        "truncated": True,
                    },
                },
                {
                    "table": "task",
                    "at": "2026-11-05T10:01:00Z",
                    "payload": {
                        "state": "working",
                        "coordinator": {"phase": "implement", "blocker": "none"},
                        "a38": {"guard_status": "green"},
                    },
                },
                {
                    "table": "local_check",
                    "at": "2026-11-05T10:02:00Z",
                    "payload": {"result": "pass", "head": "abc"},
                },
                {
                    "table": "review_gate",
                    "at": "2026-11-05T10:03:00Z",
                    "payload": {"status": "blocked", "head_sha": "def"},
                },
                {
                    "table": "checklist_item",
                    "at": "2026-11-05T10:04:00Z",
                    "payload": {"state": "done", "commit": "ghi"},
                },
                {
                    "table": "activity",
                    "at": "2026-11-05T10:05:00Z",
                    "payload": {
                        "type": "assignment.created",
                        "payload": {
                            "repo": "Acme/Widget",
                            "number": 7,
                            "url": "https://github.com/Acme/Widget/issues/7",
                        },
                    },
                },
                {
                    "table": "activity",
                    "at": "2026-11-05T10:06:00Z",
                    "payload": {
                        "type": "assignment.reply",
                        "payload": {
                            "repo": "Unsafe/Widget",
                            "number": 8,
                            "url": "javascript:alert(1)",
                        },
                    },
                },
            ],
            "truncated": False,
        },
    )
    body = str(out["body"])
    assert out["sessionId"] == "session-1"
    assert out["notice"] == "7 rows, oldest first."
    assert out["noticeClasses"] == []
    assert "role=worker&lt;one&gt; provider=provider&amp;one" in body
    assert "model=model&quot;one status=error tokens=12/3" in body
    assert 'class="error">Run command — failed &lt;bad&gt;' in body
    assert "Tool list truncated." in body
    assert "state=working phase=implement blocker=none guard=green" in body
    assert "result=pass head=abc" in body
    assert "result=blocked head=def" in body
    assert "result=done head=ghi" in body
    assert '<a href="https://github.com/Acme/Widget/issues/7"' in body
    assert "Acme/Widget#7" in body
    assert "Unsafe/Widget#8" in body
    assert "javascript:" not in body
    assert body.count("<a href=") == 1


def test_session_page_renders_empty_and_truncated_timelines() -> None:
    empty = _run_session_page(200, {"timeline": [], "truncated": False})
    assert empty["body"] == "<tr><td colspan='3'>No timeline rows.</td></tr>"
    assert empty["notice"] == "0 rows, oldest first."

    truncated = _run_session_page(
        200,
        {
            "timeline": [
                {"table": "activity", "at": "2026-11-05T10:00:00Z", "payload": {}}
            ],
            "truncated": True,
        },
    )
    assert truncated["notice"] == "Showing the newest 5,000 rows (oldest first)"


@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "Error: Session not found."), (500, "Error: HTTP 500")],
)
def test_session_page_renders_fetch_errors(status: int, message: str) -> None:
    out = _run_session_page(status)
    assert out["notice"] == message
    assert out["noticeClasses"] == ["error"]
