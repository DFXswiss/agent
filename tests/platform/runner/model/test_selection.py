"""U-RUN-model: selection of the access kind and the model (05-runner.md sections 10.1, 10.3, 10.5).

The matrix: preference (`auto`, `subscription`, `api_key`, `platform`) x credentials the device can open (none,
subscription, API key, both) x device kind (platform device with or without key, org device).
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Callable
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.model import selection
from agent_runner.model.catalog import PACKAGED
from agent_runner.model.selection import (
    Catalog,
    Choice,
    Request,
    load_catalog,
    order,
    parse_catalog,
    select,
    strategies,
)

FLASH = "deepseek/deepseek-v4.1-flash-1017"
PRO = "deepseek/deepseek-v4-pro"
OPUS = "claude-opus-5-5"
OWNER = "01930003-0000-7000-8000-000000000001"


def request(**overrides: Any) -> Request:
    fields: dict[str, Any] = {
        "preference": "auto",
        "model_pref": None,
        "device_kind": "platform",
        "owner_id": OWNER,
        "platform_key_available": True,
    }
    fields.update(overrides)
    return Request(**fields)


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(None)


PREFERENCES = ("auto", "subscription", "api_key", "platform")
CREDENTIALS: tuple[frozenset[str], ...] = (
    frozenset(),
    frozenset({"subscription"}),
    frozenset({"api_key"}),
    frozenset({"subscription", "api_key"}),
)
DEVICES = (("platform", True), ("platform", False), ("org", False), ("org", True))


def expected(preference: str, credentials: frozenset[str], device: str, key: bool) -> str | None:
    """The kind 05 section 10.1 asks for, None for `model_access_unavailable`. `platform` on any device with a key
    for it (the limited DFX key of an org device, ADR-080 section 7)."""
    available = set(credentials)
    if key:
        available.add("platform")
    kinds = ("subscription", "api_key", "platform") if preference == "auto" else (preference,)
    return next((kind for kind in kinds if kind in available), None)


@pytest.mark.parametrize(
    ("preference", "credentials", "device"), list(itertools.product(PREFERENCES, CREDENTIALS, DEVICES))
)
def test_selection_matrix(
    catalog: Catalog, preference: str, credentials: frozenset[str], device: tuple[str, bool]
) -> None:
    kind, key = device
    want = expected(preference, credentials, kind, key)
    req = request(preference=preference, credentials=credentials, device_kind=kind, platform_key_available=key)
    if want is None:
        with pytest.raises(RunnerError) as caught:
            select(req, catalog=catalog)
        assert caught.value.code == "model_access_unavailable"
        return
    choice = select(req, catalog=catalog)
    assert choice.access == want
    if want == "platform":
        assert choice.model_id == FLASH
        assert dict(choice.env) == dict.fromkeys(selection.MODEL_SLOTS, FLASH)
    else:
        # Claude Code's default model: no --model, no model environment
        assert (choice.model_id, dict(choice.env)) == (None, {})


def test_owner_model_used_when_allowed_for_the_kind_else_default(catalog: Catalog) -> None:
    assert select(request(model_pref=PRO), catalog=catalog).model_id == PRO
    assert select(request(model_pref=OPUS), catalog=catalog).model_id == FLASH
    assert select(request(preference="platform", model_pref=PRO), catalog=catalog).env["ANTHROPIC_MODEL"] == PRO
    sub = request(credentials=frozenset({"subscription"}), model_pref=OPUS)
    assert select(sub, catalog=catalog) == Choice(access="subscription", model_id=OPUS)
    assert select(request(credentials=frozenset({"api_key"}), model_pref=FLASH), catalog=catalog).model_id is None


def test_unknown_preference_is_unavailable(catalog: Catalog) -> None:
    with pytest.raises(RunnerError) as caught:
        select(request(preference="bogus"), catalog=catalog)
    assert caught.value.code == "model_access_unavailable"
    assert order("auto") == ("subscription", "api_key", "platform")
    assert order("api_key") == ("api_key",)


def test_auto_order_takes_the_first_available_strategy(catalog: Catalog) -> None:
    seen: list[str] = []

    def strategy(kind: str, available: bool) -> Callable[[Request, Catalog], Choice | None]:
        def run(_request: Request, _catalog: Catalog) -> Choice | None:
            seen.append(kind)
            return Choice(access=kind, model_id=None) if available else None

        return run

    found = {
        "subscription": strategy("subscription", False),
        "api_key": strategy("api_key", True),
        "platform": strategy("platform", True),
    }
    assert select(request(), catalog=catalog, strategies=found).access == "api_key"
    assert seen == ["subscription", "api_key"]
    # an explicit preference only tries its own kind
    seen.clear()
    with pytest.raises(RunnerError):
        select(request(preference="subscription"), catalog=catalog, strategies=found)
    assert seen == ["subscription"]
    # a kind without strategy is unavailable
    with pytest.raises(RunnerError):
        select(request(preference="api_key"), catalog=catalog, strategies={})


def test_small_models_get_context_and_output_limits() -> None:
    data = copy.deepcopy(json.loads(PACKAGED.read_text(encoding="utf-8")))
    data["models"]["tiny/model"] = {"context_window": 64000, "max_output": 8192}
    data["access"]["platform"]["allowed"].append("tiny/model")
    catalog = parse_catalog(data)
    choice = select(request(model_pref="tiny/model"), catalog=catalog)
    assert choice.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "64000"
    assert choice.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    assert catalog.allowed_models("platform")[-1] == ("tiny/model", 64000)
    assert catalog.models["tiny/model"].price is None
    assert catalog.allowed_models("nothing") == []


def test_strategies_of_the_runner_package() -> None:
    found = strategies()
    assert found == selection.BUILTIN
    assert found is not selection.BUILTIN
    assert found["platform"] is selection.platform_strategy
    assert found["subscription"] is selection.subscription_strategy
    assert found["api_key"] is selection.api_key_strategy
