from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.ai_accounts import (
    ACCESS_VALUES,
    READ_ONLY_WORKFLOW_KINDS,
    SUPPORTED_PROVIDERS,
    AccountError,
    AIAccount,
    AIAccounts,
    AIRole,
    load_ai_accounts,
)

pytestmark = pytest.mark.no_pg


def write_config(home: Path, data: dict) -> None:
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def sample_config() -> dict:
    return {
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
def test_installation_defaults_to_empty(tmp_path: Path, data) -> None:
    if data is not None:
        write_config(tmp_path, data)
    accounts = load_ai_accounts(tmp_path)
    assert accounts.accounts == {}
    assert accounts.roles == {}
    assert accounts.sessions == {}
    with pytest.raises(AccountError, match="No AI session configured"):
        accounts.for_session("chosen-session")
    with pytest.raises(AccountError, match="No AI session configured"):
        accounts.for_lane("chosen-session", "implementer", "grok")


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
    assert SUPPORTED_PROVIDERS == frozenset({"grok", "codex"})
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
        data["accounts"]["grok-primary"]["provider"] = "claude"
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
    accounts = AIAccounts({"n": account}, {"r": role}, {})
    assert account.name == "n"
    assert role.model == "model-x"
    assert accounts.roles["r"].access == "read-only"
    with pytest.raises(Exception):
        account.name = "other"  # type: ignore[misc]
