"""U-GST-gateway: a request without a credential waits for the runner's new provisioning (KOR-09, rest of AUD-01 A-04).

After a restart of ``agent-guest-api`` the credential (memory only) is gone. The runner provisions it again after
``guest.error credential_missing``, but only after its 10 s cooldown when it provisioned the session shortly before;
Claude Code gives up after three retries of a ``503`` (about 4 s, measured with the real TUI in
``guest_linux/test_units_linux.py``) and the turn ends with an API error. The gateway therefore holds the request
until the credential arrives (``CREDENTIAL_WAIT_SECONDS``) and answers ``503`` only when none does in time or the
session ends meanwhile.
"""

from __future__ import annotations

import email
import io
import json
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

from agent_guest import gateway, routing
from agent_guest.apisock import ApiClient
from agent_guest.control import ControlContext
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN
from tests.platform.guest.gateway.conftest import OPUS, journal_bodies, messages
from tests.platform.guest.gateway.test_gateway import MESSAGES, Provision, StartDouble, usage_records


def later(seconds: float, action: Any) -> threading.Thread:
    def run() -> None:
        time.sleep(seconds)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_request_without_a_credential_waits_for_the_new_provisioning(
    start_double: StartDouble, provision: Provision, api: Any, session: Session, context: ControlContext
) -> None:
    started = start_double()
    thread = later(1.0, lambda: provision("api_key", started.url))  # the runner, still in its cooldown
    began = time.monotonic()
    answer = api.request("POST", MESSAGES, messages(OPUS))
    thread.join(5)
    assert answer.status == 200, answer.body
    assert time.monotonic() - began >= 1.0
    assert journal_bodies(context, "guest.error") == [(SESSION_ID, {"code": "credential_missing"})]
    assert len(started.client.journal()) == 1  # forwarded once, after the credential arrived


def test_no_credential_in_time_is_503(
    api: Any, session: Session, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "CREDENTIAL_WAIT_SECONDS", 0.6)
    began = time.monotonic()
    answer = api.request("POST", MESSAGES, messages(OPUS))
    assert answer.status == 503
    assert time.monotonic() - began >= 0.6
    [usage] = usage_records(context)
    assert usage["status"] == 503


def test_a_session_that_ends_meanwhile_is_503_at_once(
    api: Any, session: Session, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "CREDENTIAL_WAIT_SECONDS", 30.0)
    thread = later(0.5, lambda: context.state.save_session(replace(session, state="stopped")))
    began = time.monotonic()
    answer = api.request("POST", MESSAGES, messages(OPUS))
    thread.join(5)
    assert answer.status == 503
    assert 0.5 <= time.monotonic() - began < 10  # held, then ended with the session (not after 30 s)


class FakeTime:
    """``Gateway.sleep``/``monotonic`` in simulated time; ``at(t, action)`` runs ``action`` once ``t`` passed."""

    def __init__(self) -> None:
        self.now = 0.0
        self.actions: list[tuple[float, Any]] = []

    def at(self, when: float, action: Any) -> None:
        self.actions.append((when, action))

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for item in [item for item in self.actions if item[0] <= self.now]:
            self.actions.remove(item)
            item[1]()


def direct(context: ControlContext, clock: FakeTime) -> gateway.Gateway:
    return gateway.Gateway(ApiClient(context.config.api_socket), sleep=clock.sleep, monotonic=clock.monotonic)


def model_request() -> routing.Request:
    return routing.Request(
        method="POST",
        path=MESSAGES,
        query={"beta": "true"},
        headers=email.message_from_string(f"Authorization: Bearer {SESSION_TOKEN}\n\n"),
        stream=io.BytesIO(json.dumps(messages(OPUS)).encode()),  # type: ignore[arg-type]
    )


def test_the_report_is_repeated_after_the_runners_cooldown(
    start_double: StartDouble, provision: Provision, session: Session, context: ControlContext
) -> None:
    """The runner ignored the first report (it provisioned the session less than 10 s before); the second one, after
    ``CREDENTIAL_REPORT_SECONDS``, is provisioned."""
    started = start_double()
    clock = FakeTime()
    clock.at(gateway.CREDENTIAL_REPORT_SECONDS + 0.5, lambda: provision("api_key", started.url))
    response = direct(context, clock).forward(model_request(), "/v1/messages", record_usage=True)
    assert response.status == 200
    assert journal_bodies(context, "guest.error") == [(SESSION_ID, {"code": "credential_missing"})] * 2
    list(response.body)


def test_other_requests_of_the_session_wait_for_the_first_one(session: Session, context: ControlContext) -> None:
    clock = FakeTime()
    found = direct(context, clock)
    waiting = threading.Event()
    found._waits[SESSION_ID] = [waiting, 0]
    waiting.set()  # the first request ended its wait
    assert found._await_credential(SESSION_ID) is None  # nothing stored: the store's answer
    assert journal_bodies(context, "guest.error") == []  # only the first request reports
    assert clock.now == 0


def test_more_waiting_requests_of_a_session_than_allowed_are_503_at_once(
    session: Session, context: ControlContext
) -> None:
    """Every waiting request holds a handler slot of the server: one session must not take them all."""
    found = direct(context, FakeTime())
    never = threading.Event()
    found._waits[SESSION_ID] = [never, gateway.CREDENTIAL_WAITERS - 1]
    began = time.monotonic()
    assert found._await_credential(SESSION_ID) is None
    assert time.monotonic() - began < 1
    assert found._waits[SESSION_ID][1] == gateway.CREDENTIAL_WAITERS - 1


def test_a_follower_gets_the_credential_the_first_request_took(
    start_double: StartDouble, provision: Provision, session: Session, context: ControlContext
) -> None:
    started = start_double()
    found = direct(context, FakeTime())
    waiting = threading.Event()
    found._waits[SESSION_ID] = [waiting, 0]
    results: list[Any] = []
    follower = threading.Thread(target=lambda: results.append(found._await_credential(SESSION_ID)), daemon=True)
    follower.start()
    time.sleep(0.2)
    assert results == []
    assert found._waits[SESSION_ID][1] == 1
    provision("api_key", started.url)
    assert found._credential(SESSION_ID) is not None  # the first request takes it over
    waiting.set()
    follower.join(5)
    assert results[0] is not None


def test_a_full_vm_after_the_wait_is_529(
    start_double: StartDouble, provision: Provision, session: Session, context: ControlContext
) -> None:
    started = start_double()
    clock = FakeTime()
    found = direct(context, clock)

    def fill() -> None:
        provision("api_key", started.url)
        while found._slots.acquire(blocking=False):
            pass  # other sessions took every model slot meanwhile

    clock.at(1.0, fill)
    response = found.forward(model_request(), "/v1/messages", record_usage=True)
    assert response.status == 529
    assert started.client.journal() == []
