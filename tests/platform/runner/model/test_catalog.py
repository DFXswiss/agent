"""U-RUN-model: the model catalog (05-runner.md sections 10.4, 10.5; schema `contracts/model-catalog.json`)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.errors import RunnerError
from agent_runner.model.catalog import PACKAGED, Price, load_catalog, parse_catalog

REPO = Path(__file__).resolve().parents[4]
FLASH = "deepseek/deepseek-v4.1-flash-1017"
PRO = "deepseek/deepseek-v4-pro"
OPUS = "claude-opus-5-5"


def packaged() -> dict[str, Any]:
    data = json.loads(PACKAGED.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_the_packaged_catalog_is_the_contract_example_and_matches_the_schema() -> None:
    example = json.loads((REPO / "contracts" / "model-catalog.example.json").read_text(encoding="utf-8"))
    assert packaged() == example
    assert SchemaSet(REPO / "contracts").errors("model-catalog.json", packaged()) == []
    catalog = load_catalog(None)
    assert catalog.policy("platform").default == FLASH
    assert catalog.policy("api_key").default is None
    assert catalog.policy("subscription").default is None
    assert OPUS in catalog.policy("subscription").allowed
    assert catalog.models[FLASH].price == Price(input=0.15, output=0.6, cache_read=0.003, cache_write=0.15)


def test_list_cost_is_tokens_times_the_price_per_million_tokens() -> None:
    catalog = load_catalog(None)
    tokens = {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 5000, "cache_write_tokens": 100}
    # opus: 4 / 20 / 0.2 / 5 USD per Mtok -> micro-USD = tokens x price
    assert catalog.list_cost_micro_usd(OPUS, tokens) == 1000 * 4 + 200 * 20 + 5000 * 0.2 + 100 * 5
    # thinking tokens are part of the output tokens and are not priced again
    assert catalog.list_cost_micro_usd(OPUS, {**tokens, "thinking_tokens": 10**6}) == 9500
    assert catalog.list_cost_micro_usd("unknown/model", tokens) == 0
    data = packaged()
    del data["models"][OPUS]["price_per_mtok"]
    assert parse_catalog(data).list_cost_micro_usd(OPUS, tokens) == 0


def test_catalog_file_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    data = packaged()
    data["access"]["platform"]["default"] = PRO
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_catalog(path).policy("platform").default == PRO
    path.write_text("{", encoding="utf-8")
    with pytest.raises(RunnerError) as caught:
        load_catalog(path)
    assert (caught.value.code, caught.value.name) == ("model_catalog_invalid", "file")
    with pytest.raises(RunnerError):
        load_catalog(tmp_path / "missing.json")


def _broken(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    data = copy.deepcopy(packaged())
    mutate(data)
    return data


def _price(**values: object) -> Callable[[dict[str, Any]], None]:
    def mutate(data: dict[str, Any]) -> None:
        data["models"][FLASH]["price_per_mtok"].update(values)

    return mutate


@pytest.mark.parametrize(
    ("data", "where"),
    [
        ([], "version"),
        ({"version": 2}, "version"),
        (_broken(lambda d: d.pop("models")), "root"),
        (_broken(lambda d: d["models"].__setitem__("x/y", [])), "models"),
        (_broken(lambda d: d["models"].__setitem__("bad id", {"context_window": 1, "max_output": 1})), "models"),
        (_broken(lambda d: d["models"][FLASH].__setitem__("context_window", True)), "models"),
        (_broken(lambda d: d["models"][FLASH].__setitem__("max_output", 0)), "models"),
        (_broken(lambda d: d["models"][FLASH].__setitem__("price_per_mtok", [])), "models"),
        (_broken(lambda d: d["models"][FLASH]["price_per_mtok"].pop("output")), "models"),
        (_broken(_price(input="1")), "models"),
        (_broken(_price(output=True)), "models"),
        (_broken(_price(cache_read=-1)), "models"),
        (_broken(_price(cache_write=float("inf"))), "models"),
        (_broken(_price(input=10**7)), "models"),
        (_broken(lambda d: d["access"].pop("api_key")), "access/api_key"),
        (_broken(lambda d: d["access"]["platform"].__setitem__("allowed", "x")), "access/platform"),
        (_broken(lambda d: d["access"]["platform"].__setitem__("default", PRO + "x")), "access/platform"),
        (_broken(lambda d: d["access"]["platform"].__setitem__("default", None)), "access/platform"),
        (_broken(lambda d: d["access"]["api_key"]["allowed"].append("claude-x")), "access/api_key"),
        (_broken(lambda d: d["access"]["platform"].__setitem__("providers_allowed", [""])), "access/platform"),
        (_broken(lambda d: d["access"]["platform"].__setitem__("providers_allowed", "x")), "access/platform"),
    ],
)
def test_invalid_catalogs_name_the_first_bad_place(data: object, where: str) -> None:
    with pytest.raises(RunnerError) as caught:
        parse_catalog(data)
    assert (caught.value.code, caught.value.name) == ("model_catalog_invalid", where)
