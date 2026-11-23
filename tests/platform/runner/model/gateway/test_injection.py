"""U-RUN-model: the runner's provisioning bodies through the real guest model gateway (GST-04, in the test process)
against the real model double (DBL-01), per access kind (05-runner.md sections 10.2-10.4; ADR-010).

The double accepts a request only with exactly the credential header of its kind (`x-api-key` / `Authorization:
Bearer` / bearer plus `anthropic-beta: oauth-2025-04-20`) and refuses any other credential header, so `state.ok`
proves that the body the runner built makes the gateway inject the right header and drop the session token. The
catalog allow-list, a withdrawn credential and a spent platform budget are enforced by the gateway from the body.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import pytest

from agent_guest.control import ControlContext
from agent_runner.model.catalog import load_catalog
from agent_runner.model.provision import owner_credential, platform_credential, withdrawn_credential
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN
from tests.platform.guest.gateway.conftest import KINDS, SECRETS, Api, Started, journal_bodies, messages

MESSAGES = "/model/v1/messages"
CATALOG = load_catalog(None)


def body_for(kind: str, upstream: str, budget: dict[str, float] | None = None) -> dict[str, Any]:
    """The body the runner builds for the kind (`model.provision`) with the gateway suite's upstream secrets."""
    secret = KINDS[kind][1]
    if kind == "platform":
        return platform_credential(key=secret, upstream=upstream, catalog=CATALOG, budget=budget)
    return owner_credential(kind=kind, secret=secret, upstream=upstream, catalog=CATALOG)


def provision(client: Any, body: dict[str, Any]) -> None:
    reply = client.request("PUT", f"/v1/sessions/{SESSION_ID}/credential", body)
    assert (reply.status, reply.json()) == (200, {}), reply.body


def usage_records(context: ControlContext, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = [body for sid, body in journal_bodies(context, "usage") if sid == SESSION_ID]
        if len(found) >= count:
            return found
        time.sleep(0.02)
    raise AssertionError("usage records missing")


StartDouble = Callable[..., Started]


@pytest.mark.parametrize("kind", ["api_key", "platform", "subscription"])
def test_each_kind_reaches_the_double_with_its_header(
    kind: str, start_double: StartDouble, control_client: Any, api: Api, context: ControlContext
) -> None:
    started = start_double(kind)
    provision(control_client, body_for(kind, started.url))
    model = "deepseek/deepseek-v4.1-flash-1017" if kind == "platform" else "claude-sonnet-5-5"
    answer = api.request("POST", MESSAGES, messages(model))
    assert answer.status == 200, answer.body
    assert started.client.state()["ok"], started.client.state()
    [usage] = usage_records(context, 1)
    assert usage["model"] == model
    assert usage["context_window_tokens"] == CATALOG.models[model].context_window
    text = json.dumps(started.client.journal()) + json.dumps(journal_bodies(context, "usage"))
    for secret in (*SECRETS, SESSION_TOKEN):
        assert secret not in text


@pytest.mark.parametrize("kind", ["api_key", "subscription"])
def test_a_model_outside_the_catalog_of_the_kind_is_refused(
    kind: str, start_double: StartDouble, control_client: Any, api: Api
) -> None:
    started = start_double(kind)
    provision(control_client, body_for(kind, started.url))
    answer = api.request("POST", MESSAGES, messages("deepseek/deepseek-v4.1-flash-1017"))
    assert answer.status == 400
    assert answer.json()["error"]["type"] == "model_not_allowed"
    assert started.client.journal() == []


@pytest.mark.parametrize("kind", ["api_key", "platform", "subscription"])
def test_a_withdrawn_credential_refuses_every_request(
    kind: str, start_double: StartDouble, control_client: Any, api: Api
) -> None:
    started = start_double(kind)
    provision(control_client, body_for(kind, started.url))
    provision(control_client, withdrawn_credential(kind=kind, upstream=started.url))
    model = "deepseek/deepseek-v4.1-flash-1017" if kind == "platform" else "claude-opus-5-5"
    answer = api.request("POST", MESSAGES, messages(model))
    assert answer.status == 400
    assert answer.json()["error"]["type"] == "model_not_allowed"
    assert started.client.journal() == []


def test_a_spent_platform_budget_is_refused_by_the_gateway(
    start_double: StartDouble, control_client: Any, api: Api
) -> None:
    started = start_double("platform")
    provision(control_client, body_for("platform", started.url, budget={"usd_per_day": 1.0, "spent_usd": 1.0}))
    answer = api.request("POST", MESSAGES, messages("deepseek/deepseek-v4.1-flash-1017"))
    assert answer.status == 429
    assert answer.json()["error"]["type"] == "budget_exceeded"
    assert started.client.journal() == []
