from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.ai_accounts import (
    ACCESS_VALUES,
    LANE_EXECUTION_ERROR,
    READ_ONLY_WORKFLOW_KINDS,
    SUPPORTED_PROVIDERS,
    AccountError,
    AIAccount,
    AIAccounts,
    AIRole,
    AISubscription,
    load_ai_accounts,
    load_ai_subscriptions,
)
from agent_cli.model_slots import ModelSlots

pytestmark = pytest.mark.no_pg

UNCONFIGURED_MODELS_MESSAGE = (
    "models is not configured: the device's ai-accounts.json must name "
    "models.first and models.second (set by the local harness)"
)


def write_config(home: Path, data: dict) -> None:
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def sample_config() -> dict:
    return {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "grok", "second": "codex"},
        "accounts": {
            "grok-primary": {
                "provider": "grok",
                "config_dir": "/operator/path/grok-primary",
            },
            "codex-primary": {
                "provider": "codex",
                "config_dir": "/operator/path/codex-primary",
            },
            "grok-secondary": {
                "provider": "grok",
                "config_dir": "/operator/path/grok-secondary",
            },
        },
        "roles": {
            "builder": {
                "account": "grok-primary",
                "model": "operator-selected-model",
                "access": "workspace-write",
            },
            "reader": {
                "account": "grok-primary",
                "model": "operator-selected-model",
                "access": "read-only",
            },
            "codex-builder": {
                "account": "codex-primary",
                "model": "operator-codex-model",
                "access": "workspace-write",
            },
            "codex-reader": {
                "account": "codex-primary",
                "model": "operator-codex-model",
                "access": "read-only",
            },
            "alt-builder": {
                "account": "grok-secondary",
                "model": "alt-model",
                "access": "workspace-write",
            },
        },
        "sessions": {
            "chosen-session": {
                "interactive": "builder",
                "lanes": {
                    "grok:implementer": "builder",
                    "grok:reviewer": "reader",
                    "grok:pr-reviewer-quality": "reader",
                    "grok:pr-reviewer-logic": "reader",
                    "codex:implementer": "codex-builder",
                    "codex:pr-reviewer-quality": "codex-reader",
                    "codex:pr-reviewer-logic": "codex-reader",
                },
            },
            "lanes-only": {
                "lanes": {
                    "grok:implementer": "alt-builder",
                },
            },
        },
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"accounts": None, "roles": None, "sessions": None},
        {"accounts": {}, "roles": {}, "sessions": {}},
    ],
)
def test_installation_without_models_is_unconfigured(tmp_path: Path, data) -> None:
    if data is not None:
        write_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert str(excinfo.value) == UNCONFIGURED_MODELS_MESSAGE


def test_many_profiles_and_roles_load_without_fixed_count(tmp_path: Path) -> None:
    write_config(tmp_path, sample_config())
    loaded = load_ai_accounts(tmp_path)
    assert set(loaded.accounts) == {"grok-primary", "codex-primary", "grok-secondary"}
    assert set(loaded.roles) == {
        "builder",
        "reader",
        "codex-builder",
        "codex-reader",
        "alt-builder",
    }
    assert loaded.accounts["grok-primary"].provider == "grok"
    assert loaded.accounts["codex-primary"].config_dir == "/operator/path/codex-primary"
    assert loaded.roles["builder"].access == "workspace-write"
    assert loaded.roles["reader"].account is loaded.accounts["grok-primary"]
    assert SUPPORTED_PROVIDERS == frozenset({"grok", "codex", "claude", "openrouter"})
    assert ACCESS_VALUES == frozenset({"read-only", "workspace-write"})


def test_explicit_session_and_lane_resolution(tmp_path: Path) -> None:
    write_config(tmp_path, sample_config())
    loaded = load_ai_accounts(tmp_path)
    interactive = loaded.for_session("chosen-session")
    assert interactive.name == "builder"
    assert interactive.account.provider == "grok"
    implementer = loaded.for_lane("chosen-session", "implementer", "grok")
    assert implementer.name == "builder"
    assert implementer.access == "workspace-write"
    reviewer = loaded.for_lane("chosen-session", "reviewer", "grok")
    assert reviewer.name == "reader"
    assert reviewer.access == "read-only"
    alt = loaded.for_lane("lanes-only", "implementer", "grok")
    assert alt.name == "alt-builder"
    with pytest.raises(AccountError, match="No interactive AI role"):
        loaded.for_session("lanes-only")
    with pytest.raises(AccountError, match="No AI lane configured"):
        loaded.for_lane("lanes-only", "reviewer", "grok")


def test_missing_lane_execution_preserves_reads_but_refuses_launch_resolution(
    tmp_path: Path,
) -> None:
    data = sample_config()
    data.pop("lane_execution")
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.for_session("chosen-session").name == "builder"
    with pytest.raises(AccountError) as excinfo:
        loaded.for_lane("chosen-session", "implementer", "grok")
    assert str(excinfo.value) == LANE_EXECUTION_ERROR
    with pytest.raises(AccountError) as load_exc:
        loaded.require_lane_execution()
    assert str(load_exc.value) == LANE_EXECUTION_ERROR


def test_remote_lane_execution_is_unsupported(tmp_path: Path) -> None:
    data = sample_config()
    data["lane_execution"] = {"mode": "remote", "host": "lane.example"}
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.for_session("chosen-session").name == "builder"
    with pytest.raises(AccountError, match="remote.*not supported"):
        loaded.for_lane("chosen-session", "implementer", "grok")
    with pytest.raises(AccountError, match="remote.*not supported"):
        loaded.require_lane_execution()


def test_session_omitted_interactive_and_empty_lanes(tmp_path: Path) -> None:
    data = sample_config()
    data["sessions"]["optional"] = {"interactive": None, "lanes": None}
    data["sessions"]["empty-lanes"] = {"lanes": {}}
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.sessions["optional"].interactive is None
    assert loaded.sessions["optional"].lanes == {}
    assert loaded.sessions["empty-lanes"].lanes == {}
    with pytest.raises(AccountError, match="No interactive AI role"):
        loaded.for_session("optional")


@pytest.mark.parametrize("kind", sorted(READ_ONLY_WORKFLOW_KINDS))
def test_read_only_workflow_kinds_reject_writable_bindings(
    tmp_path: Path, kind: str
) -> None:
    data = sample_config()
    data["sessions"]["chosen-session"]["lanes"][f"grok:{kind}"] = "builder"
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    with pytest.raises(AccountError, match="requires read-only access"):
        loaded.for_lane("chosen-session", kind, "grok")


def test_check_kind_resolves_a_read_only_binding(tmp_path: Path) -> None:
    data = sample_config()
    data["sessions"]["chosen-session"]["lanes"]["grok:check"] = "reader"
    write_config(tmp_path, data)
    assert (
        load_ai_accounts(tmp_path)
        .for_lane("chosen-session", "check", "grok")
        .name
        == "reader"
    )

    data["sessions"]["chosen-session"]["lanes"]["grok:check"] = "builder"
    write_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path).for_lane(
            "chosen-session", "check", "grok"
        )
    assert str(excinfo.value) == (
        "Workflow kind check requires read-only access; refusing writable binding"
    )


def test_wrong_provider_on_lane_resolution_fails(tmp_path: Path) -> None:
    data = sample_config()
    data["sessions"]["chosen-session"]["lanes"]["grok:implementer"] = "codex-builder"
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    with pytest.raises(AccountError, match="provider does not match"):
        loaded.for_lane("chosen-session", "implementer", "grok")


def test_env_prefix_clears_tokens_without_mutating_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, sample_config())
    loaded = load_ai_accounts(tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "parent-xai")
    monkeypatch.setenv("GROK_API_KEY", "parent-grok")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-openai")
    monkeypatch.setenv("CODEX_API_KEY", "parent-codex")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-anthropic")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("GROK_HOME", "/ambient/grok")
    monkeypatch.setenv("CODEX_HOME", "/ambient/codex")
    monkeypatch.setenv("UNRELATED_KEEP", "keep-me")

    grok_prefix = loaded.roles["builder"].env_prefix()
    assert grok_prefix[0] == "env"
    for key in (
        "XAI_API_KEY",
        "GROK_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "GROK_HOME",
        "CODEX_HOME",
    ):
        assert key in grok_prefix
        assert grok_prefix[grok_prefix.index(key) - 1] == "-u"
    assert "GROK_HOME=/operator/path/grok-primary" in grok_prefix
    assert not any(part.startswith("CODEX_HOME=") for part in grok_prefix)

    codex_prefix = loaded.roles["codex-builder"].env_prefix()
    assert "CODEX_HOME=/operator/path/codex-primary" in codex_prefix
    assert not any(part.startswith("GROK_HOME=") for part in codex_prefix)

    assert os.environ["XAI_API_KEY"] == "parent-xai"
    assert os.environ["GROK_API_KEY"] == "parent-grok"
    assert os.environ["OPENAI_API_KEY"] == "parent-openai"
    assert os.environ["CODEX_API_KEY"] == "parent-codex"
    assert os.environ["ANTHROPIC_API_KEY"] == "parent-anthropic"
    assert os.environ["CLAUDECODE"] == "1"
    assert os.environ["CLAUDE_CODE_ENTRYPOINT"] == "cli"
    assert os.environ["GROK_HOME"] == "/ambient/grok"
    assert os.environ["CODEX_HOME"] == "/ambient/codex"
    assert os.environ["UNRELATED_KEEP"] == "keep-me"


PROXY_AND_CA_ENV = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def test_env_prefix_clears_proxy_and_ca_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, sample_config())
    loaded = load_ai_accounts(tmp_path)
    for key in PROXY_AND_CA_ENV:
        monkeypatch.setenv(key, "/ambient/intercept")
    for role in ("builder", "codex-builder"):
        prefix = loaded.roles[role].env_prefix()
        for key in PROXY_AND_CA_ENV:
            assert prefix[prefix.index(key) - 1] == "-u", (role, key)
        assert "/ambient/intercept" not in prefix


@pytest.mark.parametrize(
    "change",
    [
        "unknown-top-level",
        "accounts-not-object",
        "roles-not-object",
        "sessions-not-object",
        "account-unknown-field",
        "role-unknown-field",
        "session-unknown-field",
        "missing-provider",
        "missing-config-dir",
        "missing-role-account",
        "missing-model",
        "missing-access",
        "unsupported-provider",
        "invalid-access",
        "relative-config-dir",
        "parent-traversal",
        "config-dir-newline",
        "config-dir-cr",
        "config-dir-nul",
        "empty-account-name",
        "empty-role-name",
        "empty-session-id",
        "empty-lane-slot",
        "dangling-role-account",
        "dangling-interactive",
        "dangling-lane-role",
        "lanes-not-object",
        "token-field",
        "invalid-json",
    ],
)
def test_malformed_configuration_is_rejected(tmp_path: Path, change: str) -> None:
    data = sample_config()
    if change == "unknown-top-level":
        data["extra"] = {}
    elif change == "accounts-not-object":
        data["accounts"] = []
    elif change == "roles-not-object":
        data["roles"] = "nope"
    elif change == "sessions-not-object":
        data["sessions"] = 1
    elif change == "account-unknown-field":
        data["accounts"]["grok-primary"]["token"] = "not-allowed"
    elif change == "role-unknown-field":
        data["roles"]["builder"]["temperature"] = 0.2
    elif change == "session-unknown-field":
        data["sessions"]["chosen-session"]["default"] = "builder"
    elif change == "missing-provider":
        del data["accounts"]["grok-primary"]["provider"]
    elif change == "missing-config-dir":
        del data["accounts"]["grok-primary"]["config_dir"]
    elif change == "missing-role-account":
        del data["roles"]["builder"]["account"]
    elif change == "missing-model":
        del data["roles"]["builder"]["model"]
    elif change == "missing-access":
        del data["roles"]["builder"]["access"]
    elif change == "unsupported-provider":
        data["accounts"]["grok-primary"]["provider"] = "gemini"
    elif change == "invalid-access":
        data["roles"]["builder"]["access"] = "full"
    elif change == "relative-config-dir":
        data["accounts"]["grok-primary"]["config_dir"] = "./relative"
    elif change == "parent-traversal":
        data["accounts"]["grok-primary"]["config_dir"] = "/operator/path/../secret"
    elif change == "config-dir-newline":
        data["accounts"]["grok-primary"]["config_dir"] = "/operator/path\n/other"
    elif change == "config-dir-cr":
        data["accounts"]["grok-primary"]["config_dir"] = "/operator/path\r/other"
    elif change == "config-dir-nul":
        data["accounts"]["grok-primary"]["config_dir"] = "/operator/path\x00/other"
    elif change == "empty-account-name":
        data["accounts"][""] = {
            "provider": "grok",
            "config_dir": "/operator/path/empty",
        }
    elif change == "empty-role-name":
        data["roles"][" "] = {
            "account": "grok-primary",
            "model": "m",
            "access": "read-only",
        }
    elif change == "empty-session-id":
        data["sessions"][""] = {"interactive": "builder"}
    elif change == "empty-lane-slot":
        data["sessions"]["chosen-session"]["lanes"][""] = "builder"
    elif change == "dangling-role-account":
        data["roles"]["builder"]["account"] = "missing"
    elif change == "dangling-interactive":
        data["sessions"]["chosen-session"]["interactive"] = "missing"
    elif change == "dangling-lane-role":
        data["sessions"]["chosen-session"]["lanes"]["grok:implementer"] = "missing"
    elif change == "lanes-not-object":
        data["sessions"]["chosen-session"]["lanes"] = ["builder"]
    elif change == "token-field":
        data["accounts"]["grok-primary"]["api_key"] = "secret-must-not-load"
    if change == "invalid-json":
        (tmp_path / "ai-accounts.json").write_text("{", encoding="utf-8")
    else:
        write_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    message = str(excinfo.value)
    assert "secret-must-not-load" not in message
    assert "parent-xai" not in message


def test_errors_do_not_echo_credential_like_values(tmp_path: Path) -> None:
    secret = "sk-live-should-never-appear"
    write_config(
        tmp_path,
        {
            "models": {"first": "grok", "second": "codex"},
            "accounts": {
                "probe": {
                    "provider": "grok",
                    "config_dir": "/operator/path",
                    "api_key": secret,
                }
            },
            "roles": {},
            "sessions": {},
        },
    )
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert secret not in str(excinfo.value)


def test_account_error_extends_store_error() -> None:
    from agent_cli.store import StoreError

    assert issubclass(AccountError, StoreError)
    assert issubclass(AccountError, SystemExit)


def test_frozen_dataclasses_expose_required_fields() -> None:
    account = AIAccount("n", "grok", "/absolute/config")
    role = AIRole("r", account, "model-x", "read-only")
    accounts = AIAccounts(
        {"n": account},
        {"r": role},
        {},
        ModelSlots("grok", "codex"),
        "grok",
    )
    assert account.name == "n"
    assert role.model == "model-x"
    assert accounts.roles["r"].access == "read-only"
    with pytest.raises(Exception):
        account.name = "other"  # type: ignore[misc]


def test_openrouter_always_sets_claude_config_dir_for_default_named_path(
    tmp_path: Path,
) -> None:
    default_dir = str(Path.home() / ".claude")
    write_config(
        tmp_path,
        {
            "models": {"first": "grok", "second": "codex"},
            "accounts": {
                "or": {"provider": "openrouter", "config_dir": default_dir}
            },
            "roles": {
                "or-reader": {
                    "account": "or", "model": "vendor/model", "access": "read-only"
                }
            },
            "sessions": {},
        },
    )
    prefix = load_ai_accounts(tmp_path).roles["or-reader"].env_prefix()
    assert f"CLAUDE_CONFIG_DIR={default_dir}" in prefix


def test_usage_accounts_validate_names_and_provider(tmp_path: Path) -> None:
    data = sample_config()
    data["accounts"]["or"] = {
        "provider": "openrouter",
        "config_dir": "/operator/path/openrouter",
    }
    data["usage_accounts"] = ["or"]
    write_config(tmp_path, data)
    assert load_ai_accounts(tmp_path).usage_accounts == ("or",)

    data["usage_accounts"] = ["missing"]
    write_config(tmp_path, data)
    with pytest.raises(AccountError, match="unknown account"):
        load_ai_accounts(tmp_path)

    data["accounts"]["seat"] = {
        "provider": "claude",
        "config_dir": "/operator/path/claude-seat",
    }
    data["usage_accounts"] = ["or", "seat"]
    write_config(tmp_path, data)
    assert load_ai_accounts(tmp_path).usage_accounts == ("or", "seat")

    for other in ("grok-primary", "codex-primary"):
        data["usage_accounts"] = [other]
        write_config(tmp_path, data)
        with pytest.raises(AccountError, match="must use provider openrouter or claude"):
            load_ai_accounts(tmp_path)

    data["usage_accounts"] = ["or", "or"]
    write_config(tmp_path, data)
    with pytest.raises(AccountError, match="duplicate account or"):
        load_ai_accounts(tmp_path)

    for empty in (None, []):
        data["usage_accounts"] = empty
        write_config(tmp_path, data)
        assert load_ai_accounts(tmp_path).usage_accounts == ()

    data["usage_accounts"] = "or"
    write_config(tmp_path, data)
    with pytest.raises(AccountError, match="must be a list or null"):
        load_ai_accounts(tmp_path)
    from agent_cli.ai_accounts import load_model_slots

    # Task creation validates models and implementer but ignores broken usage_accounts.
    assert load_model_slots(tmp_path).as_dict()["first"]


def write_subscriptions(home: Path, data: object) -> None:
    (home / "ai-subscriptions.json").write_text(json.dumps(data), encoding="utf-8")


def test_subscription_inventory_is_optional(tmp_path: Path) -> None:
    assert load_ai_subscriptions(tmp_path) is None
    write_subscriptions(tmp_path, {"subscriptions": []})
    assert load_ai_subscriptions(tmp_path) == ()


def test_subscription_inventory_reads_entries(tmp_path: Path) -> None:
    write_subscriptions(
        tmp_path,
        {
            "subscriptions": [
                {
                    "provider": "claude",
                    "account": "seat-1@example.com",
                    "label": "Team seat 1",
                    "status": "active",
                    "note": "free text",
                },
                {"provider": "openrouter", "account": "router-a"},
                {"provider": "other-plan-2", "account": "seat-1@example.com", "note": None},
            ]
        },
    )
    assert load_ai_subscriptions(tmp_path) == (
        AISubscription("claude", "seat-1@example.com", "Team seat 1", "active", "free text"),
        AISubscription("openrouter", "router-a"),
        AISubscription("other-plan-2", "seat-1@example.com"),
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "accepts only subscriptions"),
        ({}, "accepts only subscriptions"),
        ({"subscriptions": [], "extra": 1}, "accepts only subscriptions"),
        ({"subscriptions": {}}, "subscriptions must be a list"),
        ({"subscriptions": ["claude"]}, r"subscriptions\[0\] must be an object"),
        (
            {"subscriptions": [{"provider": "claude", "account": "a@example.com", "plan": "max"}]},
            r"subscriptions\[0\] has unknown fields: plan",
        ),
        ({"subscriptions": [{"account": "a@example.com"}]}, r"subscriptions\[0\]\.provider"),
        (
            {"subscriptions": [{"provider": "Claude", "account": "a@example.com"}]},
            r"subscriptions\[0\]\.provider must be a lowercase token",
        ),
        (
            {"subscriptions": [{"provider": "claude ai", "account": "a@example.com"}]},
            r"subscriptions\[0\]\.provider must be a lowercase token",
        ),
        ({"subscriptions": [{"provider": "claude"}]}, r"subscriptions\[0\]\.account"),
        (
            {"subscriptions": [{"provider": "claude", "account": " "}]},
            r"subscriptions\[0\]\.account must be a non-empty",
        ),
        (
            {"subscriptions": [{"provider": "claude", "account": "a@example.com", "label": 3}]},
            r"subscriptions\[0\]\.label",
        ),
        (
            {"subscriptions": [{"provider": "claude", "account": "a@example.com", "status": ""}]},
            r"subscriptions\[0\]\.status",
        ),
        (
            {"subscriptions": [{"provider": "claude", "account": "a@example.com", "note": "a\nb"}]},
            r"subscriptions\[0\]\.note",
        ),
        (
            {
                "subscriptions": [
                    {"provider": "claude", "account": "Seat@Example.com"},
                    {"provider": "claude", "account": "seat@example.com"},
                ]
            },
            r"subscriptions\[1\] repeats provider claude",
        ),
    ],
)
def test_subscription_inventory_rejects_invalid_entries(
    tmp_path: Path, data: object, message: str
) -> None:
    write_subscriptions(tmp_path, data)
    with pytest.raises(AccountError, match=message):
        load_ai_subscriptions(tmp_path)


def test_subscription_inventory_allows_one_account_under_two_providers(tmp_path: Path) -> None:
    write_subscriptions(
        tmp_path,
        {
            "subscriptions": [
                {"provider": "claude", "account": "seat@example.com"},
                {"provider": "grok", "account": "seat@example.com"},
            ]
        },
    )
    assert len(load_ai_subscriptions(tmp_path) or ()) == 2


def test_subscription_inventory_rejects_unreadable_json(tmp_path: Path) -> None:
    (tmp_path / "ai-subscriptions.json").write_text("{", encoding="utf-8")
    with pytest.raises(AccountError, match="Cannot read ai-subscriptions.json"):
        load_ai_subscriptions(tmp_path)
