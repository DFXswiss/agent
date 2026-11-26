"""U-RUN-login (checks): `credential.check` against the model double's `count_tokens` (per credential kind) and
`mailbox.check` against the mail double's IMAPS (05-runner.md section 11, `contracts/requests/`); answers never hold
a secret."""

from __future__ import annotations

import imaplib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_cli.account_login import Mailbox
from agent_doubles.model.access import Access
from agent_doubles.model.scenario import parse_scenario
from agent_doubles.model.server import ModelDouble
from agent_runner.login import requests as login_requests

from .conftest import Login, Run, mailbox_body, run_request

API_KEY = "test-api-key-run13"  # not a secret: the model double only compares it
SUB_TOKEN = "test-subscription-token-run13"
SCENARIO = {"name": "run13-check", "steps": [{"reply": {"content": [{"type": "text", "text": "ok"}]}}]}


@pytest.fixture
def model() -> Iterator[Callable[[Access], ModelDouble]]:
    started: list[ModelDouble] = []

    def start(access: Access) -> ModelDouble:
        double = ModelDouble(
            parse_scenario(
                {"name": "run13-check", "steps": [{"reply": {"content": [{"type": "text", "text": "ok"}]}}]}
            ),
            access=access,
        ).start()
        started.append(double)
        return double

    yield start
    for double in started:
        double.stop()


def _check(dsn: str, tmp_path: Path, body: dict[str, Any], expected: dict[str, Any], **kw: Any) -> None:
    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [expected]
        assert run.finals(run.root) == [True]
        run.secrets_absent(API_KEY, SUB_TOKEN, "mailbox-password")

    run_request(dsn, tmp_path, kw.pop("kind", "credential.check"), body, check, **kw)


@pytest.mark.parametrize(
    "case",
    [
        ("anthropic_api_key", Access("api_key", API_KEY), {"key": API_KEY}),
        (
            "claude_subscription",
            Access("subscription", SUB_TOKEN),
            {"token": SUB_TOKEN, "email": None, "kind": "setup_token"},
        ),
    ],
)
def test_a_valid_credential_is_reported_valid(
    store_dsn: str, tmp_path: Path, model: Callable[[Access], ModelDouble], case: tuple[str, Access, dict[str, Any]]
) -> None:
    kind, access, record = case
    double = model(access)
    _check(
        store_dsn,
        tmp_path,
        {"kind": kind},
        {"step": "done", "valid": True},
        records={kind: record},
        config={"model_upstream_anthropic": double.url},
    )
    entries = [e for e in double.journal.records() if e["path"] == "/v1/messages/count_tokens"]
    assert len(entries) == 1
    assert entries[0]["body"]["model"] == login_requests.CHECK_MODEL


@pytest.mark.parametrize(
    ("kind", "record"),
    [
        ("anthropic_api_key", {"key": "test-wrong-key"}),
        ("claude_subscription", {"token": "test-wrong-token", "email": None, "kind": "setup_token"}),
    ],
)
def test_a_refused_credential_is_reported_not_valid(
    store_dsn: str, tmp_path: Path, model: Callable[[Access], ModelDouble], kind: str, record: dict[str, Any]
) -> None:
    double = model(Access("api_key", API_KEY) if kind == "anthropic_api_key" else Access("subscription", SUB_TOKEN))
    _check(
        store_dsn,
        tmp_path,
        {"kind": kind},
        {"step": "done", "valid": False},
        records={kind: record},
        config={"model_upstream_anthropic": double.url},
    )


def test_the_catalog_default_model_is_used_for_the_probe(
    store_dsn: str, tmp_path: Path, model: Callable[[Access], ModelDouble]
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "version": 1,
                "access": {
                    "platform": {"default": "p/m", "allowed": ["p/m"], "providers_allowed": []},
                    "api_key": {"default": "claude-check-model", "allowed": ["claude-check-model"]},
                    "subscription": {"default": None, "allowed": []},
                },
                "models": {
                    "p/m": {"context_window": 1000, "max_output": 100},
                    "claude-check-model": {"context_window": 1000, "max_output": 100},
                },
            }
        )
    )
    double = model(Access("api_key", API_KEY))
    _check(
        store_dsn,
        tmp_path,
        {"kind": "anthropic_api_key"},
        {"step": "done", "valid": True},
        records={"anthropic_api_key": {"key": API_KEY}},
        config={"model_upstream_anthropic": double.url, "model_catalog": catalog},
    )
    assert [e["body"]["model"] for e in double.journal.records() if e["path"].endswith("count_tokens")] == [
        "claude-check-model"
    ]


def test_an_unreadable_catalog_falls_back_to_the_probe_model(
    store_dsn: str, tmp_path: Path, model: Callable[[Access], ModelDouble]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    double = model(Access("api_key", API_KEY))
    _check(
        store_dsn,
        tmp_path,
        {"kind": "anthropic_api_key"},
        {"step": "done", "valid": True},
        records={"anthropic_api_key": {"key": API_KEY}},
        config={"model_upstream_anthropic": double.url, "model_catalog": broken},
    )


@pytest.mark.parametrize(
    "case",
    [
        ({"kind": "github"}, {}, None, "upstream_error"),
        ({"kind": "anthropic_api_key", "x": 1}, {}, None, "upstream_error"),
        ({"kind": "anthropic_api_key"}, {}, None, "credential_missing"),
        ({"kind": "anthropic_api_key"}, {"anthropic_api_key": {"other": "x"}}, None, "credential_missing"),
        ({"kind": "anthropic_api_key"}, {"anthropic_api_key": {"key": API_KEY}}, None, "upstream_unreachable"),
        ({"kind": "anthropic_api_key"}, {"anthropic_api_key": {"key": API_KEY}}, "/elsewhere", "upstream_error"),
    ],
)
def test_credential_check_failures(
    store_dsn: str, tmp_path: Path, model: Callable[[Access], ModelDouble], case: tuple[Any, ...]
) -> None:
    body, records, upstream, code = case
    config = {}
    if upstream is not None:
        config["model_upstream_anthropic"] = model(Access("api_key", API_KEY)).url + upstream
    _check(store_dsn, tmp_path, body, {"step": "failed", "code": code}, records=records, config=config)


# --- mailbox.check -------------------------------------------------------------------------------------------------


def test_a_working_mailbox_is_reported_valid(store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]) -> None:
    login = doubles()
    _check(
        store_dsn,
        tmp_path,
        {},
        {"step": "done", "valid": True},
        kind="mailbox.check",
        records={"mailbox": mailbox_body(login)},
    )


def test_a_refused_mailbox_login_is_reported_not_valid(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()
    _check(
        store_dsn,
        tmp_path,
        {},
        {"step": "done", "valid": False},
        kind="mailbox.check",
        records={"mailbox": mailbox_body(login, password="test-wrong-password")},
    )


def test_an_unreachable_mailbox_is_upstream_unreachable(store_dsn: str, tmp_path: Path) -> None:
    body = {"imap_host": "127.0.0.1", "imap_port": 9, "imap_user": "u", "imap_password": "p", "email": "u@x.test"}
    _check(
        store_dsn,
        tmp_path,
        {},
        {"step": "failed", "code": "upstream_unreachable"},
        kind="mailbox.check",
        records={"mailbox": body},
    )


def test_a_mailbox_sent_with_the_request_is_checked(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    # ADR-099 section 9: any mailbox of the store, sealed to the device inside the request; no stored record needed
    login = doubles()
    _check(
        store_dsn,
        tmp_path,
        {"mailbox": {**mailbox_body(login), "name": "signup", "use": {"wgs": []}}},
        {"step": "done", "valid": True},
        kind="mailbox.check",
    )


@pytest.mark.parametrize("mailbox", [{"imap_host": "x"}, "not a mailbox"])
def test_a_malformed_mailbox_of_the_request_is_refused(store_dsn: str, tmp_path: Path, mailbox: Any) -> None:
    _check(
        store_dsn,
        tmp_path,
        {"mailbox": mailbox},
        {"step": "failed", "code": "upstream_error"},
        kind="mailbox.check",
    )


@pytest.mark.parametrize(
    ("body", "records", "code"), [({"x": 1}, {}, "upstream_error"), ({}, {}, "credential_missing")]
)
def test_mailbox_check_refusals(
    store_dsn: str, tmp_path: Path, body: dict[str, Any], records: dict[str, Any], code: str
) -> None:
    _check(store_dsn, tmp_path, body, {"step": "failed", "code": code}, kind="mailbox.check", records=records)


class FakeImap:
    def __init__(self, list_status: str, logout_error: bool) -> None:
        self.list_status = list_status
        self.logout_error = logout_error
        self.logged_out = False

    def login(self, user: str, password: str) -> None:
        assert (user, password) == ("u", "p")

    def list(self) -> tuple[str, list[bytes]]:
        return self.list_status, []

    def logout(self) -> None:
        self.logged_out = True
        if self.logout_error:
            raise imaplib.IMAP4.error("bye")


def test_a_failed_list_is_upstream_error_and_logout_errors_are_ignored() -> None:
    box = Mailbox(host="h", user="u", password="p", port=993)
    made: list[FakeImap] = []

    def factory(status: str, error: bool) -> Callable[..., FakeImap]:
        def make(*_a: object, **_k: object) -> FakeImap:
            made.append(FakeImap(status, error))
            return made[-1]

        return make

    with pytest.raises(login_requests.Failed) as failed:
        login_requests.imap_login(box, factory("NO", True))
    assert failed.value.code == "upstream_error"
    assert login_requests.imap_login(box, factory("OK", False)) is True
    assert [m.logged_out for m in made] == [True, True]
