"""U-HUB-config: the fail-closed configuration of 03-api.md section 2."""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_hub.config import (
    DEFAULT_STATIC_DIR,
    CatalogInvalid,
    ConfigError,
    load_config,
    parse_model_catalog,
    parse_public_url,
)
from tests.platform.contracts.harness import Contracts, load_json
from tests.platform.hub.conftest import TEST_MODEL_CATALOG, contracts_registry, hub_environ

SECRET_SEED = "4a" * 32


def refused(environ: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as error:
        load_config(environ)
    return str(error.value)


def test_complete_configuration() -> None:
    config = load_config(hub_environ())
    assert config.public.origin == "http://localhost:8788"
    assert config.public.rp_id == "localhost"
    assert config.public.host == "localhost:8788"
    assert config.database.role == "writer"
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 8788
    assert config.static_dir == DEFAULT_STATIC_DIR
    assert config.device_online_seconds == 30
    assert config.context_max_object_bytes == 8 * 1024**3
    assert config.context_keep == 3
    assert config.context_org_bytes == 200 * 1024**3
    assert config.log_keep == 500
    assert config.log_org_bytes == 100 * 1024**3
    assert config.platform_budget_usd_per_org_day is None
    assert config.model_catalog.platform_default == "deepseek/deepseek-v4.1-flash-1017"
    assert config.signer().key_id == "hub-test"
    assert SECRET_SEED not in repr(config)
    assert "0" * 64 not in repr(config)


def test_optional_values(tmp_path: Path) -> None:
    config = load_config(
        hub_environ(
            AGENT_HUB_PORT="20240",
            AGENT_HUB_STATIC_DIR=str(tmp_path),
            AGENT_HUB_DEVICE_ONLINE_SECONDS="45",
            AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES="1048576",
            AGENT_HUB_CONTEXT_KEEP="5",
            AGENT_HUB_CONTEXT_ORG_BYTES="9007199254740991",
            AGENT_HUB_LOG_KEEP="999999",
            AGENT_HUB_LOG_ORG_BYTES="1",
            AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY="12.5",
            AGENT_HUB_HOST="localhost",
        )
    )
    assert config.bind_port == 20240
    assert config.static_dir == tmp_path.resolve()
    assert config.device_online_seconds == 45
    assert config.context_max_object_bytes == 1048576
    assert config.context_keep == 5
    assert config.context_org_bytes == 2**53 - 1
    assert (config.log_keep, config.log_org_bytes) == (999_999, 1)
    assert config.platform_budget_usd_per_org_day == Decimal("12.5")
    assert config.bind_host == "localhost"


def test_scheduler_settings(tmp_path: Path) -> None:
    """ADR-075 section 1 and 7: the limits are settings with generous defaults; 0 means no limit or no hold."""
    config = load_config(hub_environ())
    assert (config.vms_per_wg, config.capacity_refusals, config.refusal_hold_seconds) == (100, 3, 60)
    config = load_config(
        hub_environ(AGENT_HUB_VMS_PER_WG="0", AGENT_HUB_CAPACITY_REFUSALS="5", AGENT_HUB_REFUSAL_HOLD_SECONDS="0")
    )
    assert (config.vms_per_wg, config.capacity_refusals, config.refusal_hold_seconds) == (0, 5, 0)
    assert tmp_path.exists()


def test_the_headroom_of_a_dfx_key() -> None:
    """ADR-081 section 7 (KOR-50): at most 5 USD beyond a per-device key's booked spend unless configured."""
    assert load_config(hub_environ()).dfx_key_headroom_usd == Decimal(5)
    assert load_config(hub_environ(AGENT_HUB_DFX_KEY_HEADROOM_USD="")).dfx_key_headroom_usd == Decimal(5)
    assert load_config(hub_environ(AGENT_HUB_DFX_KEY_HEADROOM_USD="0.5")).dfx_key_headroom_usd == Decimal("0.5")


def test_empty_optional_values_take_the_defaults() -> None:
    config = load_config(hub_environ(AGENT_HUB_PORT="", AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY=""))
    assert config.bind_port == 8788
    assert config.platform_budget_usd_per_org_day is None


@pytest.mark.parametrize(
    "name",
    [
        "AGENT_HUB_PUBLIC_URL",
        "AGENT_HUB_DATABASE",
        "AGENT_HUB_SIGNING_KEY",
        "AGENT_HUB_HUB_KEY_ID",
        "AGENT_HUB_PLATFORM_ROOT_KEY",
        "AGENT_HUB_HOST",
        "AGENT_HUB_MODEL_CATALOG",
    ],
)
@pytest.mark.parametrize("value", [None, ""])
def test_required_values(name: str, value: str | None) -> None:
    environ = hub_environ()
    if value is None:
        del environ[name]
    else:
        environ[name] = value
    assert refused(environ) == f"{name} is not set"


@pytest.mark.parametrize("value", ["", "postgresql://owner:" + "1" * 64 + "@127.0.0.1/hub?sslmode=disable"])
def test_owner_database_is_forbidden_in_the_hub_process(value: str) -> None:
    message = refused(hub_environ(AGENT_HUB_OWNER_DATABASE=value))
    assert message.startswith("AGENT_HUB_OWNER_DATABASE must not be set")
    assert "1" * 64 not in message


@pytest.mark.parametrize(
    ("name", "value", "rule"),
    [
        ("AGENT_HUB_DATABASE", "postgres://writer:secret-password@db/hub", "must have the form"),
        ("AGENT_HUB_SIGNING_KEY", "4A" * 32, "64 lower-case hex"),
        ("AGENT_HUB_SIGNING_KEY", "4a" * 31, "64 lower-case hex"),
        ("AGENT_HUB_HUB_KEY_ID", "Hub", "3 to 63"),
        ("AGENT_HUB_HUB_KEY_ID", "a", "3 to 63"),
        ("AGENT_HUB_HUB_KEY_ID", "-hub", "3 to 63"),
        ("AGENT_HUB_HUB_KEY_ID", "hub.key", "3 to 63"),
        ("AGENT_HUB_HUB_KEY_ID", "h" * 64, "3 to 63"),
        ("AGENT_HUB_PLATFORM_ROOT_KEY", "not-a-key", "valid Ed25519 public key"),
        ("AGENT_HUB_PLATFORM_ROOT_KEY", "A" * 43, "valid Ed25519 public key"),
        ("AGENT_HUB_PLATFORM_ROOT_KEY", "AQ", "valid Ed25519 public key"),
        ("AGENT_HUB_HOST", "Host Name", "IP address or a lower-case host name"),
        ("AGENT_HUB_PORT", "0", "from 1 to 65535"),
        ("AGENT_HUB_PORT", "65536", "from 1 to 65535"),
        ("AGENT_HUB_PORT", "80a", "from 1 to 65535"),
        ("AGENT_HUB_DEVICE_ONLINE_SECONDS", "0", "from 1 to 3600"),
        ("AGENT_HUB_DEVICE_ONLINE_SECONDS", "3601", "from 1 to 3600"),
        ("AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES", "0", "from 1 to 9007199254740991"),
        ("AGENT_HUB_CONTEXT_KEEP", "0", "from 1 to 1000"),
        ("AGENT_HUB_CONTEXT_KEEP", "1001", "from 1 to 1000"),
        ("AGENT_HUB_CONTEXT_ORG_BYTES", "8GiB", "from 1 to 9007199254740991"),
        ("AGENT_HUB_LOG_KEEP", "0", "from 1 to 999999"),
        ("AGENT_HUB_LOG_KEEP", "1000000", "from 1 to 999999"),
        ("AGENT_HUB_LOG_ORG_BYTES", "0", "from 1 to 9007199254740991"),
        ("AGENT_HUB_VMS_PER_WG", "1000001", "from 0 to 1000000"),
        ("AGENT_HUB_VMS_PER_WG", "-1", "from 0 to 1000000"),
        ("AGENT_HUB_CAPACITY_REFUSALS", "0", "from 1 to 1000"),
        ("AGENT_HUB_REFUSAL_HOLD_SECONDS", "86401", "from 0 to 86400"),
        ("AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY", "-1", "non-negative amount"),
        ("AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY", "1.1234567", "non-negative amount"),
        ("AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY", "01", "non-negative amount"),
        ("AGENT_HUB_DFX_KEY_HEADROOM_USD", "-5", "non-negative amount"),
        ("AGENT_HUB_STATIC_DIR", "/nonexistent/static", "is not a directory"),
        ("AGENT_HUB_MODEL_CATALOG", "/nonexistent/catalog.json", "is not a file"),
    ],
)
def test_malformed_values_name_the_rule_never_the_value(name: str, value: str, rule: str) -> None:
    message = refused({**hub_environ(), name: value})
    assert message.startswith(name)
    assert rule in message
    if len(value) > 3:
        assert value not in message


def test_the_writer_password_never_appears() -> None:
    message = refused(
        hub_environ(AGENT_HUB_DATABASE="postgresql://writer:" + "f" * 64 + "@db.example/hub?sslmode=disable")
    )
    assert "f" * 64 not in message


@pytest.mark.parametrize(
    ("url", "origin", "host"),
    [
        ("https://agent.example.org", "https://agent.example.org", "agent.example.org"),
        ("https://agent.example.org/", "https://agent.example.org", "agent.example.org"),
        ("https://agent.example.org:443", "https://agent.example.org", "agent.example.org"),
        ("http://localhost:80", "http://localhost", "localhost"),
        ("https://hub.example:8443", "https://hub.example:8443", "hub.example:8443"),
        ("HTTPS://Agent.Example.ORG", "https://agent.example.org", "agent.example.org"),
    ],
)
def test_public_url_forms(url: str, origin: str, host: str) -> None:
    public = parse_public_url(url)
    assert public.origin == origin
    assert public.host == host
    assert public.rp_id == host.split(":", maxsplit=1)[0]


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("https://agent.example.org/path", "must not have a path"),
        ("https://agent.example.org?x=1", "query or a fragment"),
        ("https://agent.example.org#x", "query or a fragment"),
        ("https://user@agent.example.org", "user information"),
        ("ftp://agent.example.org", "http or https"),
        ("https://agent.example.org:99999", "not a valid URL"),
        ("https://127.0.0.1", "DNS host name"),
        ("https://[::1]", "DNS host name"),
        ("https://example.123", "DNS host name"),
        ("https://example.0x10", "DNS host name"),
        ("https://agent.example.org.", "DNS host name"),
        ("https://agent_hub.example", "DNS host name"),
        ("https://xn--bcher-kva.example", None),
        ("https://", "DNS host name"),
    ],
)
def test_public_url_refusals(url: str, rule: str | None) -> None:
    if rule is None:
        assert parse_public_url(url).rp_id == "xn--bcher-kva.example"
        return
    with pytest.raises(ConfigError) as error:
        parse_public_url(url)
    assert rule in str(error.value)
    assert str(error.value).startswith("AGENT_HUB_PUBLIC_URL")


def test_unknown_hub_variables_are_refused() -> None:
    message = refused(hub_environ(AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAYS="5"))
    assert message == "AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAYS is not a hub setting"
    assert load_config(hub_environ(AGENT_HUB_TEST_POSTGRES="x")).bind_port == 8788


def test_ipv6_bind_host() -> None:
    assert load_config(hub_environ(AGENT_HUB_HOST="::1")).bind_host == "::1"


# The model catalog (03-api.md section 2 MODEL_CATALOG, contracts/model-catalog.json).


def example_catalog() -> dict[str, Any]:
    document: dict[str, Any] = load_json(TEST_MODEL_CATALOG)
    return document


def write(tmp_path: Path, content: bytes | str) -> str:
    path = tmp_path / "catalog.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return str(path)


def test_the_example_catalog_is_valid() -> None:
    catalog = parse_model_catalog(example_catalog())
    assert catalog.allowed["api_key"][0] == "claude-opus-5-5"
    assert catalog.document["version"] == 1


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        (b"\xff\xfe", "UTF-8 JSON"),
        ("{", "UTF-8 JSON"),
        ('{"version": 1, "version": 1}', "repeats a key"),
        ('{"version": NaN}', "NaN or Infinity"),
        ("[]", "exactly the keys"),
        (" " * (1024 * 1024 + 1), "larger than 1 MiB"),
    ],
)
def test_catalog_file_refusals(tmp_path: Path, content: bytes | str, rule: str) -> None:
    message = refused(hub_environ(AGENT_HUB_MODEL_CATALOG=write(tmp_path, content)))
    assert message.startswith("AGENT_HUB_MODEL_CATALOG")
    assert rule in message


def test_catalog_rules(tmp_path: Path) -> None:
    document = example_catalog()
    document["access"]["platform"]["allowed"] = ["deepseek/deepseek-v4-pro"]
    message = refused(hub_environ(AGENT_HUB_MODEL_CATALOG=write(tmp_path, json.dumps(document))))
    assert "the platform default must be an allowed platform model" in message
    document = example_catalog()
    document["access"]["api_key"]["allowed"].append("claude-unlisted")
    with pytest.raises(CatalogInvalid, match="listed under models"):
        parse_model_catalog(document)
    document = example_catalog()
    document["access"]["subscription"]["default"] = "claude-unlisted"
    with pytest.raises(CatalogInvalid, match="listed under models"):
        parse_model_catalog(document)


def test_catalog_refuses_non_finite_prices() -> None:
    # JSON has no Infinity (the file loader refuses it); a decoded document from elsewhere could carry one.
    document = example_catalog()
    document["models"]["claude-opus-5-5"]["price_per_mtok"]["input"] = float("inf")
    assert not implementation_accepts(document)


def test_catalog_with_a_subscription_default() -> None:
    document = example_catalog()
    document["access"]["subscription"]["default"] = "claude-opus-5-5"
    assert parse_model_catalog(document).platform_default == "deepseek/deepseek-v4.1-flash-1017"


def mutations(document: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    """Single changes of a catalog: every value replaced by values of other types and edge values, every object key
    removed, an extra key added to every object and every list."""
    replacements: list[Any] = [None, True, 0, -1, 1.5, 100_000_001, "", "x", "ab cd", [], {}, ["x y"]]
    replacements.append("m" * 129)
    for replacement in replacements:
        if replacement != document or type(replacement) is not type(document):
            yield path, replacement
    if isinstance(document, dict):
        for key, value in document.items():
            yield from mutations(value, (*path, key))
            reduced = {k: v for k, v in document.items() if k != key}
            yield path, reduced
        yield path, {**document, "extra": 1}
        yield path, {**document, "bad key!": document[next(iter(document))]} if document else {}
    if isinstance(document, list):
        for index, value in enumerate(document):
            yield from mutations(value, (*path, index))
        yield path, [*document, "not a model id!"]
        yield path, document * 40


def replaced(document: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return value
    copied = copy.deepcopy(document)
    target = copied
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return copied


def schema_and_rules_accept(contracts: Contracts, document: Any) -> bool:
    """The reference: the JSON Schema plus the two catalog rules a schema cannot state."""
    if contracts.errors("model-catalog.json", document):
        return False
    access = document["access"]
    listed = {access["platform"]["default"]} | {
        access[kind]["default"] for kind in ("api_key", "subscription") if access[kind]["default"] is not None
    }
    listed |= set(itertools.chain.from_iterable(access[kind]["allowed"] for kind in access))
    return listed <= set(document["models"]) and access["platform"]["default"] in access["platform"]["allowed"]


def implementation_accepts(document: Any) -> bool:
    try:
        parse_model_catalog(document)
    except CatalogInvalid:
        return False
    return True


def test_catalog_validation_equals_the_schema() -> None:
    """The hand-written validator (the contracts directory is not installed) decides exactly like the schema."""
    contracts = contracts_registry()
    base = example_catalog()
    checked = 0
    for path, value in mutations(base):
        document = replaced(base, path, value)
        assert implementation_accepts(document) == schema_and_rules_accept(contracts, document), (path, value)
        checked += 1
    assert checked > 500


def test_catalog_fixtures() -> None:
    fixtures = contracts_registry().fixtures("model-catalog.json")
    for path in fixtures["valid"]:
        assert implementation_accepts(load_json(path)), path.name
    for path in fixtures["invalid"]:
        assert not implementation_accepts(load_json(path)), path.name
