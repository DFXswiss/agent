from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_cli.ai_accounts import (
    ACCESS_VALUES,
    LANE_EXECUTION_ERROR,
    READ_ONLY_WORKFLOW_KINDS,
    AccountError,
    AIAccount,
    AIAccountPool,
    AIAccounts,
    AIRole,
    AISubscription,
    load_ai_accounts,
    load_ai_subscriptions,
    load_driver_configuration,
    load_model_configuration,
    load_recorded_names,
)
from agent_cli.drivers import (
    DRIVERS_NOT_CONFIGURED,
    NETWORK_OVERRIDE_ENV,
    RecordedNames,
    role_clear_env,
)
from agent_cli.model_slots import ModelSlots
from driver_fixtures import (
    DECLARED_DRIVERS,
    LANE_DRIVERS,
    WINDOWS_DRIVER_ID,
    accounts_document,
    driver_document,
    windows_driver,
    windows_usage,
    write_accounts,
    write_drivers,
)

pytestmark = pytest.mark.no_pg

UNCONFIGURED_MODELS_MESSAGE = (
    "models is not configured: the device's ai-accounts.json must name "
    "models.first and models.second (set by the local harness)"
)


def write_config(home: Path, data: dict, *, drivers: bool = True) -> None:
    """Write ``ai-accounts.json``, and the placeholder ``drivers.json`` unless told not to."""
    if drivers:
        write_drivers(home)
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def sample_config() -> dict:
    return {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "driver-a", "second": "driver-b"},
        "accounts": {
            "driver-a-primary": {
                "provider": "driver-a",
                "config_dir": "/operator/path/driver-a-primary",
            },
            "driver-b-primary": {
                "provider": "driver-b",
                "config_dir": "/operator/path/driver-b-primary",
            },
            "driver-a-secondary": {
                "provider": "driver-a",
                "config_dir": "/operator/path/driver-a-secondary",
            },
        },
        "roles": {
            "builder": {
                "account": "driver-a-primary",
                "model": "operator-selected-model",
                "access": "workspace-write",
            },
            "reader": {
                "account": "driver-a-primary",
                "model": "operator-selected-model",
                "access": "read-only",
            },
            "driver-b-builder": {
                "account": "driver-b-primary",
                "model": "operator-driver-b-model",
                "access": "workspace-write",
            },
            "driver-b-reader": {
                "account": "driver-b-primary",
                "model": "operator-driver-b-model",
                "access": "read-only",
            },
            "alt-builder": {
                "account": "driver-a-secondary",
                "model": "alt-model",
                "access": "workspace-write",
            },
        },
        "sessions": {
            "chosen-session": {
                "interactive": "builder",
                "lanes": {
                    "driver-a:implementer": "builder",
                    "driver-a:reviewer": "reader",
                    "driver-a:pr-reviewer-quality": "reader",
                    "driver-a:pr-reviewer-logic": "reader",
                    "driver-b:implementer": "driver-b-builder",
                    "driver-b:pr-reviewer-quality": "driver-b-reader",
                    "driver-b:pr-reviewer-logic": "driver-b-reader",
                },
            },
            "lanes-only": {
                "lanes": {
                    "driver-a:implementer": "alt-builder",
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
    assert set(loaded.accounts) == {"driver-a-primary", "driver-b-primary", "driver-a-secondary"}
    assert set(loaded.roles) == {
        "builder",
        "reader",
        "driver-b-builder",
        "driver-b-reader",
        "alt-builder",
    }
    assert loaded.accounts["driver-a-primary"].provider == "driver-a"
    assert loaded.accounts["driver-b-primary"].config_dir == "/operator/path/driver-b-primary"
    assert loaded.roles["builder"].access == "workspace-write"
    assert loaded.roles["reader"].account is loaded.accounts["driver-a-primary"]
    assert ACCESS_VALUES == frozenset({"read-only", "workspace-write"})


def test_explicit_session_and_lane_resolution(tmp_path: Path) -> None:
    write_config(tmp_path, sample_config())
    loaded = load_ai_accounts(tmp_path)
    interactive = loaded.for_session("chosen-session")
    assert interactive.name == "builder"
    assert interactive.account.provider == "driver-a"
    implementer = loaded.for_lane("chosen-session", "implementer", "driver-a")
    assert implementer.name == "builder"
    assert implementer.access == "workspace-write"
    reviewer = loaded.for_lane("chosen-session", "reviewer", "driver-a")
    assert reviewer.name == "reader"
    assert reviewer.access == "read-only"
    alt = loaded.for_lane("lanes-only", "implementer", "driver-a")
    assert alt.name == "alt-builder"
    with pytest.raises(AccountError, match="No interactive AI role"):
        loaded.for_session("lanes-only")
    with pytest.raises(AccountError, match="No AI lane configured"):
        loaded.for_lane("lanes-only", "reviewer", "driver-a")


def test_missing_lane_execution_preserves_reads_but_refuses_launch_resolution(
    tmp_path: Path,
) -> None:
    data = sample_config()
    data.pop("lane_execution")
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.for_session("chosen-session").name == "builder"
    with pytest.raises(AccountError) as excinfo:
        loaded.for_lane("chosen-session", "implementer", "driver-a")
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
        loaded.for_lane("chosen-session", "implementer", "driver-a")
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
    data["sessions"]["chosen-session"]["lanes"][f"driver-a:{kind}"] = "builder"
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    with pytest.raises(AccountError, match="requires read-only access"):
        loaded.for_lane("chosen-session", kind, "driver-a")


def test_check_kind_resolves_a_read_only_binding(tmp_path: Path) -> None:
    data = sample_config()
    data["sessions"]["chosen-session"]["lanes"]["driver-a:check"] = "reader"
    write_config(tmp_path, data)
    assert (
        load_ai_accounts(tmp_path)
        .for_lane("chosen-session", "check", "driver-a")
        .name
        == "reader"
    )

    data["sessions"]["chosen-session"]["lanes"]["driver-a:check"] = "builder"
    write_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path).for_lane(
            "chosen-session", "check", "driver-a"
        )
    assert str(excinfo.value) == (
        "Workflow kind check requires read-only access; refusing writable binding"
    )


def test_wrong_provider_on_lane_resolution_fails(tmp_path: Path) -> None:
    data = sample_config()
    data["sessions"]["chosen-session"]["lanes"]["driver-a:implementer"] = "driver-b-builder"
    write_config(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    with pytest.raises(AccountError, match="provider does not match"):
        loaded.for_lane("chosen-session", "implementer", "driver-a")


def test_env_prefix_clears_tokens_without_mutating_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, sample_config())
    loaded = load_ai_accounts(tmp_path)
    monkeypatch.setenv("TOOL_A_API_KEY", "parent-tool-a")
    monkeypatch.setenv("TOOL_B_API_KEY", "parent-tool-b")
    monkeypatch.setenv("TOOL_C_API_KEY", "parent-tool-c")
    monkeypatch.setenv("TOOL_C_TOKEN", "parent-tool-c-token")
    monkeypatch.setenv("TOOL_D_API_KEY", "parent-tool-d")
    monkeypatch.setenv("TOOL_A_HOME", "/ambient/driver-a")
    monkeypatch.setenv("TOOL_B_HOME", "/ambient/driver-b")
    monkeypatch.setenv("UNRELATED_KEEP", "keep-me")

    driver_a_prefix = loaded.roles["builder"].env_prefix(loaded.drivers)
    assert driver_a_prefix[0] == "env"
    for key in (
        "TOOL_A_API_KEY",
        "TOOL_B_API_KEY",
        "TOOL_C_API_KEY",
        "TOOL_C_TOKEN",
        "TOOL_D_API_KEY",
        "TOOL_A_HOME",
        "TOOL_B_HOME",
        "TOOL_C_CONFIG",
    ):
        assert key in driver_a_prefix
        assert driver_a_prefix[driver_a_prefix.index(key) - 1] == "-u"
    assert "TOOL_A_HOME=/operator/path/driver-a-primary" in driver_a_prefix
    assert not any(part.startswith("TOOL_B_HOME=") for part in driver_a_prefix)

    driver_b_prefix = loaded.roles["driver-b-builder"].env_prefix(loaded.drivers)
    assert "TOOL_B_HOME=/operator/path/driver-b-primary" in driver_b_prefix
    assert not any(part.startswith("TOOL_A_HOME=") for part in driver_b_prefix)

    assert os.environ["TOOL_A_API_KEY"] == "parent-tool-a"
    assert os.environ["TOOL_B_API_KEY"] == "parent-tool-b"
    assert os.environ["TOOL_C_API_KEY"] == "parent-tool-c"
    assert os.environ["TOOL_C_TOKEN"] == "parent-tool-c-token"
    assert os.environ["TOOL_D_API_KEY"] == "parent-tool-d"
    assert os.environ["TOOL_A_HOME"] == "/ambient/driver-a"
    assert os.environ["TOOL_B_HOME"] == "/ambient/driver-b"
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
    for role in ("builder", "driver-b-builder"):
        prefix = loaded.roles[role].env_prefix(loaded.drivers)
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
        data["accounts"]["driver-a-primary"]["token"] = "not-allowed"
    elif change == "role-unknown-field":
        data["roles"]["builder"]["temperature"] = 0.2
    elif change == "session-unknown-field":
        data["sessions"]["chosen-session"]["default"] = "builder"
    elif change == "missing-provider":
        del data["accounts"]["driver-a-primary"]["provider"]
    elif change == "missing-config-dir":
        del data["accounts"]["driver-a-primary"]["config_dir"]
    elif change == "missing-role-account":
        del data["roles"]["builder"]["account"]
    elif change == "missing-model":
        del data["roles"]["builder"]["model"]
    elif change == "missing-access":
        del data["roles"]["builder"]["access"]
    elif change == "unsupported-provider":
        data["accounts"]["driver-a-primary"]["provider"] = "driver-x"
    elif change == "invalid-access":
        data["roles"]["builder"]["access"] = "full"
    elif change == "relative-config-dir":
        data["accounts"]["driver-a-primary"]["config_dir"] = "./relative"
    elif change == "parent-traversal":
        data["accounts"]["driver-a-primary"]["config_dir"] = "/operator/path/../secret"
    elif change == "config-dir-newline":
        data["accounts"]["driver-a-primary"]["config_dir"] = "/operator/path\n/other"
    elif change == "config-dir-cr":
        data["accounts"]["driver-a-primary"]["config_dir"] = "/operator/path\r/other"
    elif change == "config-dir-nul":
        data["accounts"]["driver-a-primary"]["config_dir"] = "/operator/path\x00/other"
    elif change == "empty-account-name":
        data["accounts"][""] = {
            "provider": "driver-a",
            "config_dir": "/operator/path/empty",
        }
    elif change == "empty-role-name":
        data["roles"][" "] = {
            "account": "driver-a-primary",
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
        data["sessions"]["chosen-session"]["lanes"]["driver-a:implementer"] = "missing"
    elif change == "lanes-not-object":
        data["sessions"]["chosen-session"]["lanes"] = ["builder"]
    elif change == "token-field":
        data["accounts"]["driver-a-primary"]["api_key"] = "secret-must-not-load"
    if change == "invalid-json":
        (tmp_path / "ai-accounts.json").write_text("{", encoding="utf-8")
    else:
        write_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    message = str(excinfo.value)
    assert "secret-must-not-load" not in message
    assert "parent-tool-a" not in message


def test_errors_do_not_echo_credential_like_values(tmp_path: Path) -> None:
    secret = "sk-live-should-never-appear"
    write_config(
        tmp_path,
        {
            "models": {"first": "driver-a", "second": "driver-b"},
            "accounts": {
                "probe": {
                    "provider": "driver-a",
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
    account = AIAccount("n", "driver-a", "/absolute/config")
    role = AIRole("r", account, "model-x", "read-only")
    accounts = AIAccounts(
        {"n": account},
        {"r": role},
        {},
        ModelSlots("driver-a", "driver-b"),
        "driver-a",
    )
    assert account.name == "n"
    assert role.model == "model-x"
    assert accounts.roles["r"].access == "read-only"
    with pytest.raises(Exception):
        account.name = "other"  # type: ignore[misc]


def test_driver_d_always_sets_the_shared_config_dir_for_the_default_named_path(
    tmp_path: Path,
) -> None:
    default_dir = str(Path.home() / ".tool-c")
    write_config(
        tmp_path,
        {
            "models": {"first": "driver-a", "second": "driver-b"},
            "accounts": {
                "or": {"provider": "driver-d", "config_dir": default_dir}
            },
            "roles": {
                "or-reader": {
                    "account": "or", "model": "vendor/model", "access": "read-only"
                }
            },
            "sessions": {},
        },
    )
    loaded = load_ai_accounts(tmp_path)
    prefix = loaded.roles["or-reader"].env_prefix(loaded.drivers)
    assert f"TOOL_C_CONFIG={default_dir}" in prefix


def test_usage_accounts_validate_names_and_provider(tmp_path: Path) -> None:
    data = sample_config()
    data["accounts"]["or"] = {
        "provider": "driver-d",
        "config_dir": "/operator/path/driver-d",
    }
    data["usage_accounts"] = ["or"]
    write_config(tmp_path, data)
    assert load_ai_accounts(tmp_path).usage_accounts == ("or",)

    data["usage_accounts"] = ["missing"]
    write_config(tmp_path, data)
    with pytest.raises(AccountError, match="unknown account"):
        load_ai_accounts(tmp_path)

    document = driver_document()
    document["drivers"][WINDOWS_DRIVER_ID] = windows_driver()
    data["accounts"]["seat"] = {
        "provider": WINDOWS_DRIVER_ID,
        "config_dir": "/operator/path/driver-w-seat",
    }
    data["usage_accounts"] = ["or", "seat"]
    write_drivers(tmp_path, document)
    write_config(tmp_path, data, drivers=False)
    assert load_ai_accounts(tmp_path).usage_accounts == ("or", "seat")
    del data["accounts"]["seat"]

    data["usage_accounts"] = ["driver-a-primary"]
    write_config(tmp_path, data)
    with pytest.raises(AccountError, match="has no spend usage section"):
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


@pytest.mark.parametrize("value", ["absent", None])
def test_token_keepalive_is_off_by_default(tmp_path: Path, value: object) -> None:
    data = sample_config()
    if value != "absent":
        data["token_keepalive"] = value
    write_config(tmp_path, data)
    assert load_ai_accounts(tmp_path).token_keepalive_seconds is None


@pytest.mark.parametrize("minutes", [5, 30, 480])
def test_token_keepalive_minutes_become_seconds(tmp_path: Path, minutes: int) -> None:
    data = sample_config()
    data["token_keepalive"] = {"refresh_before_minutes": minutes}
    write_config(tmp_path, data)
    assert load_ai_accounts(tmp_path).token_keepalive_seconds == minutes * 60


def test_token_keepalive_is_available_with_deferred_models(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {"models": {"first": "nope"}, "token_keepalive": {"refresh_before_minutes": 45}},
    )
    accounts = load_ai_accounts(tmp_path, defer_model_configuration=True)
    assert accounts.token_keepalive_seconds == 45 * 60
    assert accounts.model_configuration_error is not None


@pytest.mark.parametrize(
    "value",
    [
        {"refresh_before_minutes": 4},
        {"refresh_before_minutes": 481},
        {"refresh_before_minutes": 0},
        {"refresh_before_minutes": -5},
        {"refresh_before_minutes": 30.0},
        {"refresh_before_minutes": "30"},
        {"refresh_before_minutes": True},
        {"refresh_before_minutes": None},
        {},
        {"refresh_before_minutes": 30, "accounts": ["seat"]},
        {"minutes": 30},
        30,
        True,
        "30",
        [30],
    ],
)
def test_token_keepalive_rejects_invalid_values(tmp_path: Path, value: object) -> None:
    data = sample_config()
    data["token_keepalive"] = value
    write_config(tmp_path, data)
    with pytest.raises(AccountError, match="token_keepalive"):
        load_ai_accounts(tmp_path)


def test_unknown_top_level_key_error_lists_token_keepalive(tmp_path: Path) -> None:
    data = sample_config()
    data["extra"] = {}
    write_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert "token_keepalive" in str(excinfo.value)

    with pytest.raises(AccountError) as excinfo:
        load_model_configuration(tmp_path)
    assert "token_keepalive" in str(excinfo.value)


def test_token_keepalive_does_not_stop_task_creation(tmp_path: Path) -> None:
    from agent_cli.ai_accounts import load_model_slots

    data = sample_config()
    data["token_keepalive"] = {"refresh_before_minutes": 1}
    write_config(tmp_path, data)
    # Task creation reads only models and implementer; the key itself is known.
    assert load_model_slots(tmp_path).as_dict()["first"] == "driver-a"
POOL_DRIVER = "driver-p"


def pool_driver_document() -> dict:
    """The placeholder drivers plus ``driver-p``: a lane driver with a windows adapter."""
    document = driver_document()
    driver = copy.deepcopy(document["drivers"]["driver-c"])
    usage_section = windows_usage()
    usage_section["account_file"].pop("default_name")
    driver.pop("home_default_dir")
    driver.update(
        display_name="Vendor P",
        home_env="TOOL_P_CONFIG",
        credential_env=["TOOL_P_API_KEY"],
        usage=usage_section,
    )
    document["drivers"][POOL_DRIVER] = driver
    return document


def pool_config() -> dict:
    """``sample_config`` plus three polled accounts of ``driver-p`` and a pool role."""
    data = sample_config()
    for name in ("seat-1", "seat-2", "seat-3"):
        data["accounts"][name] = {
            "provider": POOL_DRIVER,
            "config_dir": f"/operator/path/{name}",
        }
    data["usage_accounts"] = ["seat-1", "seat-2", "seat-3"]
    data["roles"]["pooled"] = {
        "accounts": ["seat-1", "seat-2"],
        "model": "pool-model",
        "access": "workspace-write",
    }
    data["sessions"]["chosen-session"]["lanes"][f"{POOL_DRIVER}:implementer"] = "pooled"
    return data


def write_pool_config(home: Path, data: dict) -> None:
    write_drivers(home, pool_driver_document())
    write_config(home, data, drivers=False)


def test_pool_role_parses_into_an_unselected_pool(tmp_path: Path) -> None:
    write_pool_config(tmp_path, pool_config())
    loaded = load_ai_accounts(tmp_path)
    pooled = loaded.roles["pooled"]
    assert pooled.is_pool
    assert isinstance(pooled.account, AIAccountPool)
    assert pooled.account == AIAccountPool(
        name="", provider=POOL_DRIVER, config_dir="", members=("seat-1", "seat-2")
    )
    assert pooled.model == "pool-model"
    assert pooled.access == "workspace-write"
    assert loaded.pool_members(pooled) == (
        loaded.accounts["seat-1"],
        loaded.accounts["seat-2"],
    )
    # Lane resolution checks the pool's shared provider.
    assert loaded.for_lane("chosen-session", "implementer", POOL_DRIVER) is pooled
    # Nothing may run on the unselected pool.
    with pytest.raises(AccountError, match="names an account pool"):
        pooled.env_prefix(loaded.drivers)


def test_single_account_role_is_unchanged_next_to_a_pool(tmp_path: Path) -> None:
    write_pool_config(tmp_path, pool_config())
    loaded = load_ai_accounts(tmp_path)
    builder = loaded.roles["builder"]
    assert builder == AIRole(
        "builder",
        AIAccount("driver-a-primary", "driver-a", "/operator/path/driver-a-primary"),
        "operator-selected-model",
        "workspace-write",
    )
    assert type(builder.account) is AIAccount
    assert not builder.is_pool
    assert loaded.pool_members(builder) == ()
    # The execution binding hashes this dictionary; it keeps today's shape.
    assert asdict(builder) == {
        "name": "builder",
        "account": {
            "name": "driver-a-primary",
            "provider": "driver-a",
            "config_dir": "/operator/path/driver-a-primary",
        },
        "model": "operator-selected-model",
        "access": "workspace-write",
    }


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {"account": "seat-1", "accounts": ["seat-1", "seat-2"]},
            "Role pooled must name exactly one of account and accounts",
        ),
        ({}, "Role pooled must name exactly one of account and accounts"),
        (
            {"accounts": ["seat-1", "driver-a-primary"]},
            "Role pooled accounts must all use one provider, found driver-a, driver-p",
        ),
        (
            {"accounts": ["seat-1", "seat-1"]},
            "Role pooled accounts lists account seat-1 twice",
        ),
        (
            {"accounts": ["seat-1", "missing"]},
            "Role pooled references an unknown account missing",
        ),
        (
            {"accounts": ["seat-1"]},
            "Role pooled accounts must be a list of at least two account names",
        ),
        (
            {"accounts": "seat-1"},
            "Role pooled accounts must be a list of at least two account names",
        ),
        (
            {"accounts": ["seat-1", 7]},
            "role pooled accounts entry must be a non-empty single-line string",
        ),
    ],
)
def test_pool_role_configuration_errors(
    tmp_path: Path, entry: dict, message: str
) -> None:
    data = pool_config()
    data["roles"]["pooled"] = {
        **entry,
        "model": "pool-model",
        "access": "workspace-write",
    }
    write_pool_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert str(excinfo.value) == message


def test_pool_member_must_be_polled(tmp_path: Path) -> None:
    data = pool_config()
    data["usage_accounts"] = ["seat-1", "seat-3"]
    write_pool_config(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert str(excinfo.value) == (
        "Role pooled pool account seat-2 is not listed in usage_accounts; "
        "a pool member's usage must be polled"
    )


def test_pool_provider_needs_a_windows_adapter(tmp_path: Path) -> None:
    data = pool_config()
    data["roles"]["pooled"]["accounts"] = ["driver-a-primary", "driver-a-secondary"]
    write_pool_config(tmp_path, data)
    with pytest.raises(AccountError, match="Role pooled account pool uses driver driver-a"):
        load_ai_accounts(tmp_path)


def test_pool_role_cannot_be_an_interactive_role(tmp_path: Path) -> None:
    data = pool_config()
    data["sessions"]["chosen-session"]["interactive"] = "pooled"
    write_pool_config(tmp_path, data)
    with pytest.raises(AccountError, match="interactive role pooled names an account pool"):
        load_ai_accounts(tmp_path)


def test_role_rejects_unknown_keys_next_to_a_pool(tmp_path: Path) -> None:
    data = pool_config()
    data["roles"]["pooled"]["strategy"] = "round-robin"
    write_pool_config(tmp_path, data)
    with pytest.raises(AccountError, match="Invalid role fields for pooled"):
        load_ai_accounts(tmp_path)


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
                    "provider": "driver-a",
                    "account": "seat-1@example.com",
                    "label": "Team seat 1",
                    "status": "active",
                    "note": "free text",
                },
                {"provider": "driver-b", "account": "spend-a"},
                {"provider": "other-plan-2", "account": "seat-1@example.com", "note": None},
            ]
        },
    )
    assert load_ai_subscriptions(tmp_path) == (
        AISubscription("driver-a", "seat-1@example.com", "Team seat 1", "active", "free text"),
        AISubscription("driver-b", "spend-a"),
        AISubscription("other-plan-2", "seat-1@example.com"),
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "accepts only subscriptions"),
        ({}, "accepts only subscriptions"),
        ({"subscriptions": [], "extra": 1}, "accepts only subscriptions"),
        ({"subscriptions": {}}, "subscriptions must be a list"),
        ({"subscriptions": ["driver-a"]}, r"subscriptions\[0\] must be an object"),
        (
            {"subscriptions": [{"provider": "driver-a", "account": "a@example.com", "plan": "max"}]},
            r"subscriptions\[0\] has unknown keys: plan",
        ),
        ({"subscriptions": [{"account": "a@example.com"}]}, r"subscriptions\[0\]\.provider"),
        (
            {"subscriptions": [{"provider": "Driver-A", "account": "a@example.com"}]},
            r"subscriptions\[0\]\.provider must be a lowercase token",
        ),
        (
            {"subscriptions": [{"provider": "driver a", "account": "a@example.com"}]},
            r"subscriptions\[0\]\.provider must be a lowercase token",
        ),
        ({"subscriptions": [{"provider": "driver-a"}]}, r"subscriptions\[0\]\.account"),
        (
            {"subscriptions": [{"provider": "driver-a", "account": " "}]},
            r"subscriptions\[0\]\.account must be a non-empty",
        ),
        (
            {"subscriptions": [{"provider": "driver-a", "account": "a@example.com", "label": 3}]},
            r"subscriptions\[0\]\.label",
        ),
        (
            {"subscriptions": [{"provider": "driver-a", "account": "a@example.com", "status": ""}]},
            r"subscriptions\[0\]\.status",
        ),
        (
            {"subscriptions": [{"provider": "driver-a", "account": "a@example.com", "note": "a\nb"}]},
            r"subscriptions\[0\]\.note",
        ),
        (
            {
                "subscriptions": [
                    {"provider": "driver-a", "account": "Seat@Example.com"},
                    {"provider": "driver-a", "account": "seat@example.com"},
                ]
            },
            r"subscriptions\[1\] repeats provider driver-a",
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
                {"provider": "driver-a", "account": "seat@example.com"},
                {"provider": "driver-b", "account": "seat@example.com"},
            ]
        },
    )
    assert len(load_ai_subscriptions(tmp_path) or ()) == 2


def test_subscription_inventory_rejects_unreadable_json(tmp_path: Path) -> None:
    (tmp_path / "ai-subscriptions.json").write_text("{", encoding="utf-8")
    with pytest.raises(AccountError, match="Cannot read ai-subscriptions.json"):
        load_ai_subscriptions(tmp_path)


# --- drivers.json next to ai-accounts.json -----------------------------------


def _names_driver_file(message: str) -> bool:
    return "drivers.json" in message and "docs/ai-accounts.md" in message


def test_placeholder_drivers_load_next_to_accounts(tmp_path: Path) -> None:
    write_drivers(tmp_path)
    write_accounts(tmp_path)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.lane_drivers() == LANE_DRIVERS
    assert loaded.declared_drivers() == DECLARED_DRIVERS
    assert loaded.models == ModelSlots("driver-a", "driver-b")
    assert loaded.implementer == "driver-a"
    assert loaded.recorded_slots == RecordedNames("driver-a", "driver-b")
    assert loaded.pane_driver == "driver-a"
    assert loaded.driver("driver-c").home_env == "TOOL_C_CONFIG"
    assert loaded.for_lane("any", "implementer", "driver-c").name == "driver-c-writer"
    with pytest.raises(AccountError) as excinfo:
        loaded.driver("driver-x")
    assert _names_driver_file(str(excinfo.value))
    assert load_model_configuration(tmp_path) == (
        ModelSlots("driver-a", "driver-b"),
        "driver-a",
    )


def test_a_missing_driver_file_configures_no_driver(tmp_path: Path) -> None:
    config = load_driver_configuration(tmp_path)
    assert config.present is False and config.drivers == {}
    write_config(tmp_path, {"accounts": {}, "roles": {}, "sessions": {}}, drivers=False)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert str(excinfo.value) == UNCONFIGURED_MODELS_MESSAGE
    deferred = load_ai_accounts(tmp_path, defer_model_configuration=True)
    assert deferred.drivers == {} and deferred.lane_drivers() == ()


@pytest.mark.parametrize(
    "data",
    [
        {"accounts": {"a": {"provider": "driver-a", "config_dir": "/operator/a"}}},
        {"models": {"first": "driver-a", "second": "driver-b"}},
        {"implementer": "driver-e"},
        {"lanes": {"driver-a:implementer": "missing-role"}},
        {"sessions": {"s": {"lanes": {"driver-a:reviewer": "missing-role"}}}},
    ],
    ids=["accounts", "models", "implementer", "lanes", "session-lanes"],
)
def test_a_missing_driver_file_fails_loudly_when_a_driver_is_named(
    tmp_path: Path, data: dict
) -> None:
    write_config(tmp_path, data, drivers=False)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert str(excinfo.value) == DRIVERS_NOT_CONFIGURED
    assert _names_driver_file(DRIVERS_NOT_CONFIGURED)


def test_a_missing_driver_file_defers_with_the_model_selection(tmp_path: Path) -> None:
    write_config(
        tmp_path, {"models": {"first": "driver-a", "second": "driver-b"}}, drivers=False
    )
    deferred = load_ai_accounts(tmp_path, defer_model_configuration=True)
    with pytest.raises(AccountError) as excinfo:
        deferred.require_model_configuration()
    assert str(excinfo.value) == DRIVERS_NOT_CONFIGURED
    with pytest.raises(AccountError) as model_exc:
        load_model_configuration(tmp_path)
    assert str(model_exc.value) == DRIVERS_NOT_CONFIGURED


def _set_session_usage(data: dict) -> None:
    data["sessions"]["session-a"]["interactive"] = "driver-b-writer"
    data["usage_session"] = "session-a"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["accounts"]["driver-a-account"].update(provider="driver-x"),
            "Unsupported provider for account driver-a-account: drivers.json configures no such driver",
        ),
        (
            lambda d: d["accounts"]["driver-a-account"].update(provider="driver-e"),
            "Unsupported provider for account driver-a-account: driver driver-e has no home_env",
        ),
        (
            lambda d: d["models"].update(first="driver-e"),
            "models.first must be one of driver-a|driver-b|driver-c|driver-d",
        ),
        (
            lambda d: d["models"].update(second="driver-x"),
            "models.second must be null or one of driver-a|driver-b|driver-c|driver-d",
        ),
        (
            lambda d: d.update(implementer="driver-x"),
            "implementer must be one of driver-a|driver-b|driver-c|driver-d|driver-e",
        ),
        (
            lambda d: d["lanes"].update({"driver-e:implementer": "driver-a-writer"}),
            "Lane slot driver-e:implementer must name a driver with a lane section",
        ),
        (
            lambda d: d["lanes"].update({"driver-a": "driver-a-writer"}),
            "Lane slot driver-a must name a driver with a lane section",
        ),
        (
            lambda d: d["sessions"]["session-a"].update(
                lanes={"driver-x:reviewer": "driver-a-reader"}
            ),
            "Lane slot driver-x:reviewer must name a driver with a lane section",
        ),
        (
            _set_session_usage,
            "usage_session uses driver driver-b, which has no period usage section",
        ),
        (
            lambda d: d.update(usage_accounts=["driver-a-account"]),
            "usage_accounts account driver-a-account uses driver driver-a, which has no spend usage section",
        ),
    ],
)
def test_names_must_match_configured_drivers(
    tmp_path: Path, mutate, message: str
) -> None:
    write_drivers(tmp_path)
    data = accounts_document()
    mutate(data)
    write_accounts(tmp_path, data)
    with pytest.raises(AccountError) as excinfo:
        load_ai_accounts(tmp_path)
    assert message in str(excinfo.value)
    assert _names_driver_file(str(excinfo.value))


def test_record_only_implementer_and_usage_selections(tmp_path: Path) -> None:
    write_drivers(tmp_path)
    data = accounts_document()
    data["implementer"] = "driver-e"
    data["usage_session"] = "session-a"
    data["usage_accounts"] = ["driver-d-account"]
    write_accounts(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.implementer == "driver-e"
    assert loaded.usage_session == "session-a"
    assert loaded.usage_accounts == ("driver-d-account",)


def test_driver_file_errors_name_the_file(tmp_path: Path) -> None:
    write_accounts(tmp_path)
    (tmp_path / "drivers.json").write_text("{", encoding="utf-8")
    with pytest.raises(AccountError) as unreadable:
        load_ai_accounts(tmp_path)
    assert str(unreadable.value) == "Cannot read drivers.json (docs/ai-accounts.md#drivers)"

    write_drivers(tmp_path, {"drivers": {}, "extra": 1})
    with pytest.raises(AccountError) as invalid:
        load_ai_accounts(tmp_path)
    assert "drivers.json accepts only drivers, recorded_slots, pane_driver" in str(
        invalid.value
    )
    assert _names_driver_file(str(invalid.value))

    # A scrubbed variable that is a home variable would run the wrong account.
    document = driver_document()
    document["drivers"]["driver-a"]["credential_env"] = ["TOOL_B_HOME"]
    write_drivers(tmp_path, document)
    with pytest.raises(AccountError) as overlap:
        load_ai_accounts(tmp_path)
    assert str(overlap.value) == (
        "drivers.driver-a.credential_env[0] names home_env TOOL_B_HOME of driver "
        "driver-b (see drivers.json, docs/ai-accounts.md#drivers)"
    )

    # ai-accounts.json keeps its own keys; drivers live only in drivers.json.
    write_drivers(tmp_path)
    data = accounts_document()
    data["drivers"] = {}
    write_accounts(tmp_path, data)
    with pytest.raises(AccountError, match="accepts only accounts"):
        load_ai_accounts(tmp_path)


@pytest.mark.parametrize("document", ["null", "[]", '"drivers"', "1", "true"])
@pytest.mark.parametrize(
    "load",
    [
        load_driver_configuration,
        load_recorded_names,
        load_ai_accounts,
        load_model_configuration,
    ],
)
def test_driver_file_that_is_not_an_object_fails(
    tmp_path: Path, load, document: str
) -> None:
    # Only an absent file means no drivers; a null document is not absent.
    write_accounts(tmp_path)
    (tmp_path / "drivers.json").write_text(document, encoding="utf-8")
    with pytest.raises(AccountError) as excinfo:
        load(tmp_path)
    assert str(excinfo.value) == (
        "drivers.json must be an object (see drivers.json, docs/ai-accounts.md#drivers)"
    )


def test_env_prefix_reads_home_variables_and_scrub_from_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    default_dir = str(tmp_path / ".tool-c")
    write_drivers(tmp_path)
    data = accounts_document()
    data["accounts"]["driver-c-account"]["config_dir"] = default_dir
    write_accounts(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    cleared = role_clear_env(loaded.drivers)
    assert cleared == (
        "TOOL_A_API_KEY",
        "TOOL_B_API_KEY",
        "TOOL_C_API_KEY",
        "TOOL_C_TOKEN",
        "TOOL_D_API_KEY",
        *NETWORK_OVERRIDE_ENV,
        "TOOL_A_HOME",
        "TOOL_B_HOME",
        "TOOL_C_CONFIG",
    )
    unset = [part for key in cleared for part in ("-u", key)]
    assert loaded.roles["driver-a-writer"].env_prefix(loaded.drivers) == [
        "env",
        *unset,
        "TOOL_A_HOME=/operator/path/tool-a",
    ]
    # The driver CLI's default directory is selected by leaving the variable unset.
    assert loaded.roles["driver-c-reader"].env_prefix(loaded.drivers) == ["env", *unset]
    # Another driver with the same variable and no default directory sets it.
    shared = AIRole("shared", AIAccount("x", "driver-d", default_dir), "m", "read-only")
    assert shared.env_prefix(loaded.drivers)[-1] == f"TOOL_C_CONFIG={default_dir}"
    record_only = AIRole("r", AIAccount("x", "driver-e", "/operator/e"), "m", "read-only")
    with pytest.raises(AccountError, match="Unsupported provider for role r"):
        record_only.env_prefix(loaded.drivers)
    with pytest.raises(AccountError, match="Unsupported provider for role r"):
        record_only.env_prefix({})


def test_pane_markers_follow_the_interactive_driver_then_pane_driver(
    tmp_path: Path,
) -> None:
    document = driver_document()
    interactive = json.loads(json.dumps(document["drivers"]["driver-a"]["interactive"]))
    interactive["pane"]["chrome"] = ["Footer C"]
    document["drivers"]["driver-c"]["interactive"] = interactive
    write_drivers(tmp_path, document)
    data = accounts_document()
    data["sessions"] = {
        "session-a": {"interactive": "driver-a-writer"},
        "session-c": {"interactive": "driver-c-writer"},
        "session-b": {"interactive": "driver-b-writer"},
        "session-raw": {"lanes": {}},
    }
    write_accounts(tmp_path, data)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.pane_markers("session-a").chrome == ("Status bar",)
    assert loaded.pane_markers("session-c").chrome == ("Footer C",)
    # No interactive driver behind the role, no role, or no session: pane_driver.
    for session in ("session-b", "session-raw", "unconfigured"):
        assert loaded.pane_markers(session).chrome == ("Status bar",)

    document.pop("pane_driver")
    write_drivers(tmp_path, document)
    loaded = load_ai_accounts(tmp_path)
    assert loaded.pane_markers("session-c").chrome == ("Footer C",)
    for session in ("session-b", "session-raw", "unconfigured"):
        with pytest.raises(AccountError) as excinfo:
            loaded.pane_markers(session)
        assert str(excinfo.value) == (
            f"session {session} has no interactive driver and no pane_driver is "
            "configured; cannot classify its pane "
            "(see drivers.json, docs/ai-accounts.md#drivers)"
        )
