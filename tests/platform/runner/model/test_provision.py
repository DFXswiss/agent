"""U-RUN-model: gateway credentials of every access kind (05-runner.md sections 10.2-10.3, 2.6; 03-api.md section 7.1).

Every body is validated against `contracts/guest/session-credential.request.json`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.model.provision import (
    budget_exhausted,
    owner_credential,
    owner_secret,
    platform_budget,
    platform_credential,
    read_platform_key,
    withdrawn_credential,
)
from agent_runner.model.selection import load_catalog

REPO = Path(__file__).resolve().parents[4]
ORG = "01930001-0000-7000-8000-000000000001"
KEY = "test-platform-key-0123456789abcdef"


def key_file(tmp_path: Path, content: bytes = KEY.encode() + b"\n", mode: int = 0o600) -> Path:
    path = tmp_path / "openrouter.key"
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_key_is_read_from_a_private_file(tmp_path: Path) -> None:
    assert read_platform_key(key_file(tmp_path)) == KEY


@pytest.mark.parametrize(
    ("content", "mode"),
    [
        (KEY.encode(), 0o640),  # readable by the group
        (KEY.encode(), 0o604),
        (b"", 0o600),
        (b"two words", 0o600),
        (b"\xff\xfe", 0o600),
        (b"x" * 4097, 0o600),
    ],
)
def test_unusable_key_files_give_none(tmp_path: Path, content: bytes, mode: int) -> None:
    assert read_platform_key(key_file(tmp_path, content, mode)) is None


def test_missing_link_and_directory_give_none(tmp_path: Path) -> None:
    assert read_platform_key(None) is None
    assert read_platform_key(tmp_path / "missing") is None
    target = key_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert read_platform_key(link) is None
    directory = tmp_path / "dir"
    directory.mkdir(mode=0o700)
    assert read_platform_key(directory) is None


def test_foreign_owner_gives_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = key_file(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    assert read_platform_key(path) is None


def test_platform_credential_body_matches_the_contract() -> None:
    schemas = SchemaSet(REPO / "contracts")
    name = "guest/session-credential.request.json"
    catalog = load_catalog(None)
    body = platform_credential(key=KEY, upstream="https://openrouter.ai/api/", catalog=catalog, budget=None)
    assert schemas.errors(name, body) == []
    assert body["upstream_base_url"] == "https://openrouter.ai/api"
    assert body["auth_value"] == "Bearer " + KEY
    assert body["access"] == "platform"
    assert "budget" not in body
    assert [item["model_id"] for item in body["models_allowed"]] == list(catalog.policy("platform").allowed)
    limited = platform_credential(
        key=KEY, upstream="http://127.0.0.1:9", catalog=catalog, budget={"usd_per_day": 5.0, "spent_usd": 1.5}
    )
    assert schemas.errors(name, limited) == []
    assert limited["budget"] == {"usd_per_day": 5.0, "spent_usd": 1.5}


def test_budget_from_the_desired_state() -> None:
    assert platform_budget(None, ORG) is None
    assert platform_budget({"platform_budget": None}, ORG) is None  # unlimited
    state = {"platform_budget": {"usd_per_org_day": 10, "spent_today_by_org": {ORG: 2.5}}}
    assert platform_budget(state, ORG) == {"usd_per_day": 10.0, "spent_usd": 2.5}
    assert platform_budget(state, "other") == {"usd_per_day": 10.0, "spent_usd": 0.0}


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        ([], {"usd_per_day": 0.0, "spent_usd": 0.0}),
        ({"usd_per_org_day": True}, {"usd_per_day": 0.0, "spent_usd": 0.0}),
        ({"usd_per_org_day": -1}, {"usd_per_day": 0.0, "spent_usd": 0.0}),
        ({"usd_per_org_day": float("nan")}, {"usd_per_day": 0.0, "spent_usd": 0.0}),
        ({"usd_per_org_day": 3, "spent_today_by_org": []}, {"usd_per_day": 3.0, "spent_usd": 3.0}),
        ({"usd_per_org_day": 3, "spent_today_by_org": {ORG: "x"}}, {"usd_per_day": 3.0, "spent_usd": 3.0}),
        ({"usd_per_org_day": 3, "spent_today_by_org": {ORG: float("inf")}}, {"usd_per_day": 3.0, "spent_usd": 3.0}),
    ],
)
def test_malformed_budgets_fail_closed(budget: object, expected: dict[str, float]) -> None:
    assert platform_budget({"platform_budget": budget}, ORG) == expected


@pytest.mark.parametrize(
    ("kind", "header", "value"),
    [("api_key", "x-api-key", KEY), ("subscription", "authorization", "Bearer " + KEY)],
)
def test_owner_credential_bodies_match_the_contract(kind: str, header: str, value: str) -> None:
    schemas = SchemaSet(REPO / "contracts")
    catalog = load_catalog(None)
    body = owner_credential(kind=kind, secret=KEY, upstream="https://api.anthropic.com/", catalog=catalog)
    assert schemas.errors("guest/session-credential.request.json", body) == []
    assert body["upstream_base_url"] == "https://api.anthropic.com"
    assert (body["auth_header"], body["auth_value"], body["access"]) == (header, value, kind)
    assert "budget" not in body
    assert "provider_allowlist" not in body
    assert [item["model_id"] for item in body["models_allowed"]] == list(catalog.policy(kind).allowed)


@pytest.mark.parametrize("kind", ["api_key", "subscription", "platform"])
def test_withdrawn_credentials_carry_no_secret_and_allow_no_model(kind: str) -> None:
    body = withdrawn_credential(kind=kind, upstream="http://127.0.0.1:9/")
    assert SchemaSet(REPO / "contracts").errors("guest/session-credential.request.json", body) == []
    assert body["models_allowed"] == []
    assert "withdrawn" in body["auth_value"]
    assert body["upstream_base_url"] == "http://127.0.0.1:9"


def test_owner_secrets_are_taken_from_the_credential_bodies() -> None:
    assert owner_secret("anthropic_api_key", {"key": KEY}) == KEY
    assert owner_secret("claude_subscription", {"token": KEY, "email": None, "kind": "setup_token"}) == KEY
    # a further subscription's body served as the primary record is not used for the owner's sessions (KOR-69)
    further = {"token": KEY, "email": None, "kind": "setup_token", "name": "Spare"}
    assert owner_secret("claude_subscription", further) is None
    assert owner_secret("anthropic_api_key", {"key": "two words"}) is None
    assert owner_secret("anthropic_api_key", {"key": 5}) is None
    assert owner_secret("claude_subscription", {}) is None


def test_budget_exhaustion() -> None:
    assert budget_exhausted(None) is False
    assert budget_exhausted({"usd_per_day": 5.0, "spent_usd": 4.99}) is False
    assert budget_exhausted({"usd_per_day": 5.0, "spent_usd": 5.0}) is True
    assert budget_exhausted({"usd_per_day": 0.0, "spent_usd": 0.0}) is True
