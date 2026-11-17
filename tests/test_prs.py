from __future__ import annotations

import json
from base64 import b64decode
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

import httpx
import pytest

from agent_core.app import create_app
from agent_core.db import Store
from agent_core.github import FakeGitHub, GitHubError, RealGitHub, _pr_from_search_item
from tests.conftest import sign_in


def test_pr_from_search_item() -> None:
    row = _pr_from_search_item(
        {
            "html_url": "https://github.com/DFXswiss/agent/pull/11",
            "title": "Add allow",
            "state": "open",
            "draft": True,
            "user": {"login": "TaprootFreakAI"},
        }
    )
    assert row == {
        "author": "taprootfreakai",
        "org": "DFXswiss",
        "repo": "agent",
        "number": 11,
        "title": "Add allow",
        "status": "draft",
        "url": "https://github.com/DFXswiss/agent/pull/11",
    }


def test_api_prs_requires_login(hub) -> None:
    res = hub.get("/api/prs")
    assert res.status_code == 401


def test_api_prs_team_filter(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "draft",
            "url": "https://github.com/acme/app/pull/7",
        },
        {
            "author": "bob",
            "org": "acme",
            "repo": "app",
            "number": 8,
            "title": "Add sync",
            "status": "open",
            "url": "https://github.com/acme/app/pull/8",
        },
        {
            "author": "cara",
            "org": "other",
            "repo": "x",
            "number": 1,
            "title": "secret",
            "status": "open",
            "url": "https://github.com/other/x/pull/1",
        },
    ]
    sign_in(hub, "code-alice")
    body = hub.get("/api/prs").json()
    assert body["source"] == "github"
    numbers = sorted(p["number"] for p in body["prs"])
    assert numbers == [7, 8]
    authors = {p["author"] for p in body["prs"]}
    assert authors == {"alice", "bob"}


def test_api_prs_self_only(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "dave",
            "org": "other",
            "repo": "x",
            "number": 1,
            "title": "secret",
            "status": "open",
            "url": "https://github.com/other/x/pull/1",
        },
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "draft",
            "url": "https://github.com/acme/app/pull/7",
        },
    ]
    sign_in(hub, "code-dave")
    body = hub.get("/api/prs").json()
    assert [p["number"] for p in body["prs"]] == [1]


def test_api_prs_includes_assignee(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "outside",
            "assignee": "alice",
            "org": "acme",
            "repo": "app",
            "number": 9,
            "title": "Help alice",
            "status": "open",
            "url": "https://github.com/acme/app/pull/9",
        }
    ]
    sign_in(hub, "code-alice")
    body = hub.get("/api/prs").json()
    assert [p["number"] for p in body["prs"]] == [9]


def test_prs_survive_new_hub(cfg, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "draft",
            "url": "https://github.com/acme/app/pull/7",
        }
    ]
    first_store = Store(cfg.database)
    try:
        with TestClient(create_app(cfg, github=github, store=first_store)) as first:
            sign_in(first, "code-alice")
            assert first.get("/api/prs").json()["source"] == "github"
            cookies = dict(first.cookies)
    finally:
        first_store.close()

    second_store = Store(cfg.database)
    try:
        with TestClient(create_app(cfg, github=github, store=second_store)) as second:
            second.cookies.update(cookies)
            body = second.get("/api/prs").json()
            assert body["source"] == "github"
            assert [p["number"] for p in body["prs"]] == [7]
    finally:
        second_store.close()


def test_auth_me_has_no_token(hub, cfg) -> None:
    sign_in(hub, "code-alice")
    me = hub.get("/auth/me").json()
    assert set(me) == {"login", "visible", "teams", "owned_agents"}
    assert "token" not in me and "github_token" not in me
    raw = hub.cookies["session"]
    data = TimestampSigner(cfg.session_secret).unsign(
        raw.encode("utf-8"), max_age=14 * 24 * 60 * 60
    )
    payload = json.loads(b64decode(data))
    assert payload == {"login": "alice"}
    assert hub.app.state.hub.store.get_oauth_token("alice") == "tok-alice"


def test_logout_drops_token(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "open",
            "url": "https://github.com/acme/app/pull/7",
        }
    ]
    sign_in(hub, "code-alice")
    assert hub.get("/api/prs").json()["source"] == "github"
    hub.post("/auth/logout")
    assert hub.get("/api/prs").status_code == 401
    assert hub.app.state.hub.store.get_oauth_token("alice") == ""


def test_expired_github_token_asks_reauth(hub, github: FakeGitHub) -> None:
    sign_in(hub, "code-alice")
    github.search_status = 401
    body = hub.get("/api/prs").json()
    assert body["source"] == "none"
    assert body["prs"] == []
    github.search_status = 200
    assert hub.get("/api/prs").json()["source"] == "none"


def test_rate_limit_does_not_drop_token(hub, github: FakeGitHub) -> None:
    sign_in(hub, "code-alice")
    github.search_status = 403
    res = hub.get("/api/prs")
    assert res.status_code == 502
    assert "HTTP 403" in res.json()["detail"]
    assert hub.app.state.hub.store.get_oauth_token("alice") == "tok-alice"


def test_malformed_search_items_is_502(hub, cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(hub, "code-alice")
    hub.app.state.hub.github = RealGitHub(cfg)

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"items": None}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    res = hub.get("/api/prs")
    assert res.status_code == 502
    assert "HTTP 200" in res.json()["detail"]
    assert hub.app.state.hub.store.get_oauth_token("alice") == "tok-alice"
    with pytest.raises(GitHubError, match="HTTP 200"):
        RealGitHub(cfg).search_open_prs("tok-alice", ["alice"])


def test_dashboard_state_comes_from_replica(hub) -> None:
    sign_in(hub, "code-alice")
    res = hub.get("/api/state")
    assert res.status_code == 200
    body = res.json()
    assert "session" in body and "task" in body and "devices" in body
    assert "prs" not in body


def test_index_lists_pr_columns() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    for col in ("Author", "Org", "Repo", "PR", "Description", "Status"):
        assert f"<th>{col}</th>" in html
    assert 'id="prs"' in html
    assert 'id="k-prs"' in html
    assert "Open PRs" in html
    assert "/api/prs" in html
    assert "fillPrs" in html
    assert "kickPrs" in html
    assert html.index('id="prs"') < html.index('id="usage"')
    assert html.index("<h2>Pull requests</h2>") < html.index("<h2>Usage</h2>")
    assert html.index('id="prs"') < html.index('id="sessions"')
    assert html.index('id="err"') < html.index('id="signed-in"')
    render_fn = html.split("async function render()", 1)[1].split("function kickState()", 1)[0]
    assert 'k-people").textContent' in render_fn
    assert "kickState();" in render_fn
    assert "kickPrs();" in render_fn
    assert "await kickState" not in render_fn
    assert "await kickPrs" not in render_fn
    assert html.index('k-people").textContent') < html.index("kickState();")
    assert html.index("kickState();") < html.index("kickPrs();")
    after_me = render_fn.split("await me()", 1)[1]
    assert "if (gen !== renderGen) return" in after_me.split("catch", 1)[0]
    assert html.index("if (stateInflight)") < html.index('jsonGet("/api/state"')
    assert html.index("if (prsInflight)") < html.index('jsonGet("/api/prs"')
    assert "stateInflight.gen !== gen" in html
    assert "prsInflight.gen !== gen" in html
    assert "if (stateCtl) stateCtl.abort()" not in html
    assert "if (prsCtl) prsCtl.abort()" not in html
    signed_out = html.split("if (!user)", 1)[1].split("window.__login", 1)[0]
    assert "abortHubFetches()" in signed_out
    assert "err.hidden = true" in signed_out
    switch_fn = html.split("window.__login !== user.login", 1)[1].split("window.__login = user.login", 1)[0]
    assert "abortHubFetches()" in switch_fn
    assert "clearHubTables()" in switch_fn
    clear_fn = html.split("function clearHubTables()", 1)[1].split("function jsonGet", 1)[0]
    assert 'id="prs-sub"' in html
    assert "prs-sub" in clear_fn
    assert "Open PRs authored or assigned to people you can see." in clear_fn
    assert "k-prs" in clear_fn
    assert "k-usage" in clear_fn
    catch_fn = html.split("async function render()", 1)[1].split("function kickState()", 1)[0].split("} catch (e)", 1)[1]
    assert "abortHubFetches()" in catch_fn
    assert "err.hidden = false" in catch_fn
    assert 'jsonGet("/api/state", stateCtl, 15000)' in html
    assert 'jsonGet("/api/prs", prsCtl, 25000)' in html
    assert "Timed out loading sessions." in html
    assert "Timed out loading PRs." in html
    assert "GitHub access is missing" in html
    assert "source === \"none\"" in html or "source === 'none'" in html
    assert "No open PRs for visible people." in html
    assert 'id="usage"' in html
    assert 'id="k-usage"' in html
    assert "Total tokens (input + output + cache read + cache create)" in html
    state_tick = html.split("function kickState()", 1)[1].split(
        "function kickUsage()", 1
    )[0]
    assert "kickUsage();" in state_tick


def test_index_agent_render_preserves_forms_and_handles_fetch_lifecycle() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    # renderAgents delegates painting to renderAgentCards; check both together.
    render_agents = html.split("async function renderAgents", 1)[1].split(
        "function repaintAgents", 1
    )[0] + html.split("function renderAgentCards", 1)[1].split("async function renderAgents", 1)[0]
    submit_handler = html.split('document.addEventListener("submit"', 1)[1].split(
        'document.addEventListener("click"', 1
    )[0]
    assign_handler = submit_handler.split('form.classList.contains("assign-form")', 1)[
        1
    ].split('form.classList.contains("reply-form")', 1)[0]
    reply_handler = submit_handler.split('form.classList.contains("reply-form")', 1)[1]
    abort_fn = html.split("function abortHubFetches()", 1)[1].split(
        "function abortAssignmentLoads()", 1
    )[0]
    clear_fn = html.split("function clearHubTables()", 1)[1].split("function jsonGet", 1)[0]

    assert "section.contains(document.activeElement)" not in render_agents
    assert "captureAgentDrafts()" in render_agents
    assert "restoreAgentFormState(drafts)" in render_agents
    assert "captureAgentDraft(" in html  # behaviour is executed in tests/test_index_page_js.py
    assert "if (values.__requestId) form.dataset.requestId = values.__requestId;" in html
    assert "new AbortController()" not in render_agents
    assert "abortAssignmentLoads()" not in render_agents
    assert "captureAgentFocus()" in render_agents
    assert "restoreAgentFocus(focus)" in render_agents
    assert "selectionStart" in html
    assert "selectionEnd" in html
    assert "setSelectionRange" in html
    assert "assignmentLoads" not in abort_fn
    assert html.count("abortAssignmentLoads();") == 3
    assert "assignmentOwnerSetKey(user.owned_agents || [])" in html
    assert "assignmentOwnersKey !== nextOwnerKey" in html
    assert "return [user.login].concat(user.owned_agents || []);" in html
    assert 'fillPairSelect(document.getElementById("pair-as"), user);' in html
    assert "++agentRenderGen" in clear_fn
    assert "++agentSessionEpoch" in clear_fn
    assert html.count("++agentSessionEpoch") == 1
    assert "agentPostControllers.forEach(ctl => ctl.abort())" in clear_fn
    assert "agentPostControllers.clear()" in clear_fn
    assert "agentFormsInFlight.clear()" in clear_fn
    assert 'JSON.stringify(["reply", form.dataset.agent || "", form.dataset.repo || "", form.dataset.number || ""])' in html
    assert 'class="reply-form" data-agent="${esc(agent)}"' in render_agents
    for handler in (assign_handler, reply_handler):
        assert "const sessionEpoch = agentSessionEpoch" in handler
        assert "const generation = agentRenderGen" not in handler
        assert "const login = window.__login" in handler
        before_success = handler.split("succeeded = true", 1)[0].rsplit("}", 1)[1]
        assert "sessionEpoch !== agentSessionEpoch" in before_success
        assert "login !== window.__login" in before_success
        finally_block = handler.split("finally", 1)[1].split("if (succeeded)", 1)[0]
        assert "sessionEpoch === agentSessionEpoch" in finally_block
        assert "login === window.__login" in finally_block
        before_repaint = handler.split("if (succeeded)", 1)[1].split(
            "await repaintAgents()", 1
        )[0]
        assert "sessionEpoch !== agentSessionEpoch" in before_repaint
        assert "login !== window.__login" in before_repaint


def test_oauth_scope_is_read_user_repo(cfg) -> None:
    url = RealGitHub(cfg).authorize_url("st", "http://127.0.0.1/auth/github/callback")
    query = parse_qs(urlparse(url).query)
    assert query.get("scope") == ["read:user repo"]
