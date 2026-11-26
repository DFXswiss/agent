"""U-HUB-models (fallback): the wait of a session paused for model quota (`session_transition` with `wait_until` and
`dfx_choice`), the attention reasons `working/quota_wait` and `needs_decision/quota_choice`, the task's consent
(`POST /v1/tasks/{task}/model-consent`) and the audit action `session.model_switched` (ADR-081 sections 4 and 5,
KOR-50)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from agent_hub.billing import binding
from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.notifications import rules
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.events.support import Json
from tests.platform.hub.models.support import Keys
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import audit_rows

QUOTA = "model_quota_exhausted"


def _transition(keys: Keys, session: str, state: str, reason: str | None = None, **fields: Any) -> None:
    body: Json = {"session_id": session, "state": state, "reason": reason, "at": keys.bill.meter.time(-0.1), **fields}
    assert keys.status("session_transition", body, keys.device) == ("ok", None)


def _paused(keys: Keys, *, choice: bool = True) -> tuple[str, str, str]:
    """A task whose main session waits for model quota; `(task, session, wait_until)`."""
    task, session = keys.bill.meter.session("subscription")
    until = format_time(keys.hub.clock() + timedelta(hours=2))
    _transition(keys, session, "idle")
    _transition(keys, session, "suspended", QUOTA, wait_until=until, dfx_choice=choice)
    return task, session, until


def _attention(keys: Keys, task: str) -> tuple[Any, ...]:
    return keys.rows(
        "SELECT category, reason, ref_kind, ref_id::text, wait_until FROM hub.task_attention WHERE task_id = %s", task
    )[0]


def _listed(keys: Keys, category: str) -> list[Json]:
    response = keys.bill.scene.writer.browser.get(f"/v1/orgs/{keys.org}/attention", params={"category": category})
    assert response.status_code == 200, response.text
    return [item["attention"] for item in response.json()["items"]]


def test_a_paused_session_keeps_its_wait_and_offers_the_choice(keys: Keys) -> None:
    binding.bind(keys.hub.config)
    task, session, until = _paused(keys)
    assert keys.rows("SELECT state, state_reason, wait_until, dfx_choice FROM hub.session WHERE id = %s", session) == [
        ("suspended", QUOTA, parse_time(until), True)
    ]
    assert _attention(keys, task) == ("needs_decision", "quota_choice", "session", session, parse_time(until))
    assert [(row["reason"], row["wait_until"]) for row in _listed(keys, "needs_decision")] == [("quota_choice", until)]
    # the runner reports the same pause with a new release time: only the wait changes
    sooner = format_time(keys.hub.clock() + timedelta(minutes=30))
    _transition(keys, session, "suspended", QUOTA, wait_until=sooner, dfx_choice=False)
    assert _attention(keys, task) == ("working", "quota_wait", None, None, parse_time(sooner))
    assert [(row["reason"], row["wait_until"]) for row in _listed(keys, "working")] == [("quota_wait", sooner)]
    _transition(keys, session, "suspended", QUOTA, wait_until=sooner, dfx_choice=False)  # the same again
    # the session continues: the wait is cleared and the record has no wait time
    _transition(keys, session, "resuming")
    assert keys.rows("SELECT wait_until, dfx_choice FROM hub.session WHERE id = %s", session) == [(None, False)]
    assert _attention(keys, task)[:2] == ("working", "starting")
    assert "wait_until" not in _listed(keys, "working")[0]


def test_another_pause_reason_keeps_no_wait(keys: Keys) -> None:
    task, session = keys.bill.meter.session("platform")
    _transition(keys, session, "idle")
    _transition(
        keys, session, "suspended", "credit_exhausted", wait_until=format_time(keys.hub.clock()), dfx_choice=True
    )
    assert keys.rows("SELECT wait_until, dfx_choice FROM hub.session WHERE id = %s", session) == [(None, False)]
    assert _attention(keys, task)[:2] == ("needs_decision", "credit_empty")
    # the same state with the quota reason does not overwrite the credit pause (credit first, ADR-081 section 4)
    _transition(keys, session, "suspended", QUOTA, wait_until=format_time(keys.hub.clock()), dfx_choice=True)
    assert keys.rows("SELECT state_reason, wait_until FROM hub.session WHERE id = %s", session) == [
        ("credit_exhausted", None)
    ]


def test_a_failed_session_reports_no_wait(keys: Keys) -> None:
    _task, session = keys.bill.meter.session("subscription")
    body = {"session_id": session, "state": "failed", "reason": QUOTA, "at": keys.bill.meter.time(-0.1)}
    assert keys.raw_status("session_transition", {**body, "dfx_choice": True}, keys.device) == (
        "rejected",
        "request_invalid",
    )
    assert keys.status("session_transition", body, keys.device) == ("ok", None)
    assert keys.rows("SELECT state, state_reason FROM hub.session WHERE id = %s", session) == [("failed", QUOTA)]


def test_an_exhausted_org_offers_no_choice(keys: Keys) -> None:
    binding.bind(keys.hub.config)
    keys.bill.add_prices()
    keys.execute(
        "INSERT INTO hub.credit_account (org_id, balance_micro_usd, booked_micro_usd) VALUES (%s, 0, 0)", keys.org
    )
    task, _session, _until = _paused(keys)
    assert _attention(keys, task)[:2] == ("working", "quota_wait")


def test_the_consent_turns_the_choice_into_a_wait(keys: Keys, published: MemoryPublisher) -> None:
    binding.bind(keys.hub.config)
    task, _session, until = _paused(keys)
    writer = keys.bill.scene.writer
    published.clear()
    response = writer.browser.post(f"/v1/tasks/{task}/model-consent", json={})
    assert (response.status_code, response.json()) == (200, {"task_id": task, "model_consent": True})
    assert keys.rows("SELECT model_consent FROM hub.task WHERE id = %s", task) == [(True,)]
    assert _attention(keys, task) == ("working", "quota_wait", None, None, parse_time(until))
    hinted = {str(entry.audience.id) for entry in published.items() if entry.frame == "reconcile"}
    assert hinted == {keys.device.id}
    assert keys.state(keys.device)["sessions"][0]["model_consent"] is True
    # again: nothing changes, the row says so
    published.clear()
    assert writer.browser.post(f"/v1/tasks/{task}/model-consent", json={}).status_code == 200
    assert [entry for entry in published.items() if entry.frame == "reconcile"] == []
    rows = audit_rows(keys.bill.meter.desk, "task.model_consent")
    assert [(row[0], str(row[1]), row[3]) for row in rows] == [
        ("user", writer.id, {"changed": True}),
        ("user", writer.id, {"changed": False}),
    ]


def test_the_consent_needs_the_write_right(keys: Keys) -> None:
    task, _session = keys.bill.meter.session("subscription")
    scene = keys.bill.scene
    assert scene.reader.browser.post(f"/v1/tasks/{task}/model-consent", json={}).status_code == 403
    assert scene.stranger.browser.post(f"/v1/tasks/{task}/model-consent", json={}).status_code == 403
    assert scene.writer.browser.post(f"/v1/tasks/{new_id()}/model-consent", json={}).status_code == 404
    assert keys.rows("SELECT model_consent FROM hub.task WHERE id = %s", task) == [(False,)]


def test_a_model_switch_is_audited_for_the_session(keys: Keys, published: MemoryPublisher) -> None:
    task, session = keys.bill.meter.session("subscription")
    detail = {
        "from_access": "subscription",
        "to_access": "platform",
        "from_model": "claude-sonnet-5-5",
        "to_model": None,
        "cause": "exhausted",
        "until": keys.bill.meter.time(3600),
    }
    body = {
        "action": "session.model_switched",
        "result": "ok",
        "session_id": session,
        "target_kind": "session",
        "target_id": session,
        "detail": detail,
    }
    assert keys.status("audit", body, keys.device) == ("ok", None)
    assert keys.raw_status("audit", {**body, "detail": {**detail, "cause": "rate_limited"}}, keys.device) == (
        "rejected",
        "request_invalid",
    )
    rows = keys.rows("SELECT task_id::text, detail FROM hub.audit WHERE action = 'session.model_switched'")
    assert rows == [(task, detail)]
    # the task's owner is notified (code and metadata only, ADR-020)
    writer, wg = keys.bill.scene.writer, keys.bill.meter.desk.wg
    notices = keys.rows(
        "SELECT recipient_id::text, org_id::text, wg_id::text, task_id::text, ref FROM hub.notification"
        " WHERE kind = 'model.switched'"
    )
    assert notices == [(writer.id, keys.org, wg, task, f"subscription:platform:{detail['until']}")]
    frames = [entry for entry in published.items() if entry.frame == "notification"]
    assert [(str(entry.audience.id), entry.data["ref"]) for entry in frames] == [(writer.id, notices[0][4])]
    listed = writer.browser.get("/v1/notifications").json()
    assert [item["kind"] for item in listed["notifications"]] == ["model.switched"]
    # a release without a time
    released = {**detail, "from_access": "platform", "to_access": "subscription", "cause": "released", "until": None}
    assert keys.status("audit", {**body, "detail": released}, keys.device) == ("ok", None)
    refs = keys.rows("SELECT ref FROM hub.notification WHERE kind = 'model.switched' ORDER BY created_at")
    assert refs[-1] == ("platform:subscription:",)
    # a refused access continued on its renewal (HOT-26): audited, the task's quiet line, no notification
    renewed = {
        **detail,
        "to_access": "subscription",
        "to_model": "claude-sonnet-5-5",
        "cause": "renewed",
        "until": None,
    }
    assert keys.status("audit", {**body, "detail": renewed}, keys.device) == ("ok", None)
    stored = "SELECT detail FROM hub.audit WHERE action = 'session.model_switched' AND detail->>'cause' = 'renewed'"
    assert keys.rows(stored) == [(renewed,)]
    assert keys.rows("SELECT count(*) FROM hub.notification WHERE kind = 'model.switched'") == [(2,)]
    # a session that waits for a subscription with its model: audited and its owner is told
    unavailable = {**renewed, "cause": "model_unavailable"}
    assert keys.status("audit", {**body, "detail": unavailable}, keys.device) == ("ok", None)
    assert keys.rows("SELECT count(*) FROM hub.notification WHERE kind = 'model.switched'") == [(3,)]
    # an owner who no longer reads the workgroup is not notified; a row without a session names no task
    keys.execute(
        "UPDATE hub.wg_access SET revoked_seq = granted_seq + 100 WHERE wg_id = %s AND user_id = %s", wg, writer.id
    )
    assert keys.status("audit", body, keys.device) == ("ok", None)
    without = {key: value for key, value in body.items() if key != "session_id"}
    assert keys.status("audit", {**without, "task_id": task}, keys.device) == ("ok", None)
    assert keys.rows("SELECT count(*) FROM hub.notification WHERE kind = 'model.switched'") == [(3,)]
    # other runner audit rows notify nobody
    alert = {**body, "action": "runner.alert", "detail": {"alert": "disk_low", "value": None}}
    assert keys.status("audit", alert, keys.device) == ("ok", None)
    assert keys.rows("SELECT count(*) FROM hub.notification WHERE kind = 'model.switched'") == [(3,)]


def test_a_switch_of_an_unknown_task_notifies_nobody(keys: Keys) -> None:
    detail = {"from_access": "subscription", "to_access": "platform", "until": None}
    with keys.hub.pool.connection() as connection:
        assert rules.switch_rule(connection, UUID(new_id()), detail, keys.hub.clock()) == []


def _wait(keys: Keys, session: str, until: str | None, choice: bool, at: str, *, device: Any = None) -> tuple[str, Any]:
    body = {"session_id": session, "wait_until": until, "dfx_choice": choice, "at": at}
    return keys.status("session_wait", body, device or keys.device)


def test_a_session_wait_holds_any_live_main_session(keys: Keys) -> None:
    """ADR-081 sections 3 and 4: the runner reports the wait of a session that is not paused (a new main session in
    its start chain, a working one); a cleared wait gives the task back to the other rules."""
    binding.bind(keys.hub.config)
    task, session = keys.bill.meter.session("subscription")
    keys.execute("UPDATE hub.session SET state = 'agent_starting' WHERE id = %s", session)
    until = keys.bill.meter.time(7200)
    assert _wait(keys, session, until, True, keys.bill.meter.time(-1)) == ("ok", None)
    assert _attention(keys, task) == ("needs_decision", "quota_choice", "session", session, parse_time(until))
    # an older report changes nothing; the same report again neither
    assert _wait(keys, session, None, False, keys.bill.meter.time(-30)) == ("ok", None)
    assert _wait(keys, session, until, True, keys.bill.meter.time(-1)) == ("ok", None)
    assert _attention(keys, task)[:2] == ("needs_decision", "quota_choice")
    # a transition that is no quota pause keeps the wait
    _transition(keys, session, "working")
    assert keys.rows("SELECT wait_until, dfx_choice FROM hub.session WHERE id = %s", session) == [
        (parse_time(until), True)
    ]
    # without a wait time there is no choice; cleared, the task works again
    assert _wait(keys, session, None, True, keys.bill.meter.time()) == ("ok", None)
    assert keys.rows("SELECT wait_until, dfx_choice FROM hub.session WHERE id = %s", session) == [(None, False)]
    assert _attention(keys, task)[:2] == ("working", "working")
    # the end of the session clears a wait
    assert _wait(keys, session, until, False, keys.bill.meter.time()) == ("ok", None)
    assert _attention(keys, task)[:2] == ("working", "quota_wait")
    _transition(keys, session, "failed", "agent_exited")
    assert keys.rows("SELECT wait_until FROM hub.session WHERE id = %s", session) == [(None,)]


def test_a_session_wait_comes_from_the_session_s_device(keys: Keys) -> None:
    _task, session = keys.bill.meter.session("subscription")
    at = keys.bill.meter.time(-1)
    assert _wait(keys, session, None, False, at, device=keys.issuer) == ("rejected", "forbidden")
    assert _wait(keys, new_id(), None, False, at) == ("rejected", "not_found")


def test_a_changed_fallback_reaches_the_devices_of_the_owner(keys: Keys, published: MemoryPublisher) -> None:
    keys.bill.meter.session("subscription")
    writer = keys.bill.scene.writer
    settings = {"model_access": "auto", "model_id": None, "locale": "en"}

    def put(**fallback: bool) -> set[str]:
        published.clear()
        body = {**settings, "fallback": fallback} if fallback else settings
        assert writer.browser.put("/v1/me/settings", json=body).status_code == 200
        return {str(entry.audience.id) for entry in published.items() if entry.frame == "reconcile"}

    assert put() == set()  # a first row with the defaults changes nothing
    assert put(own_to_dfx=True, operator_to_platform=True) == {keys.device.id}
    assert put(own_to_dfx=True, operator_to_platform=True) == set()
    assert put() == set()


def test_an_operator_subscription_switch_is_named(keys: Keys) -> None:
    task, session = keys.bill.meter.session("subscription")
    detail = {
        "from_access": "operator",
        "to_access": "platform",
        "from_model": None,
        "to_model": None,
        "cause": "exhausted",
        "until": None,
    }
    body = {
        "action": "session.model_switched",
        "result": "ok",
        "session_id": session,
        "target_kind": "session",
        "target_id": session,
        "detail": detail,
    }
    assert keys.status("audit", body, keys.device) == ("ok", None)
    assert keys.rows("SELECT ref FROM hub.notification WHERE kind = 'model.switched' AND task_id = %s", task) == [
        ("operator:platform:",)
    ]
