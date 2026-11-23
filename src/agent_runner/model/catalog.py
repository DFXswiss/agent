"""The runner's model catalog (docs/platform/spec/05-runner.md section 10.5, schema `contracts/model-catalog.json`;
ADR-010).

`model-catalog.json` next to this module is the packaged catalog (equal to `contracts/model-catalog.example.json`, kept
so by a test); `AGENT_RUNNER_MODEL_CATALOG` replaces it. Per access kind (`platform`, `api_key`, `subscription`) the
catalog names the default model (`null`: Claude Code's own default, no `--model`), the allowed models and, for the
platform's OpenRouter access, the provider allow-list (empty = any). Per model it names the context window, the
maximum output and the list prices in USD per million tokens.

- `parse_catalog(data)` checks what the runner relies on and raises `model_catalog_invalid` naming the first bad place
  (`version`, `root`, `models`, `access/<kind>`): every allowed model is listed under `models`, a default is allowed,
  the platform has a default. Prices are optional for the runner (a model without prices costs 0 at list price).
- `load_catalog(path)`: the file at `path`, else the packaged catalog.
- `list_cost_micro_usd(model, tokens)`: the list price of one request in micro-USD (05 section 10.4 `list_price` and
  `subscription_equivalent`): `input`, `output`, `cache_read` and `cache_write` tokens times their price per million
  tokens. Thinking tokens are part of Anthropic's `output_tokens` and are therefore not priced again.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_runner.errors import RunnerError

ACCESS_KINDS = ("subscription", "api_key", "platform")
PACKAGED = Path(__file__).with_name("model-catalog.json")
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
# usage record field -> price field
PRICED_TOKENS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_write_tokens": "cache_write",
}
MAX_PRICE = 1_000_000.0
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_LABEL_RE = re.compile(r"^[ -~]{1,64}$")


class CatalogInvalid(RunnerError):
    def __init__(self, where: str) -> None:
        super().__init__("model_catalog_invalid", where)


@dataclass(frozen=True)
class Price:
    """List prices in USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class ModelInfo:
    context_window: int
    max_output: int
    price: Price | None = None


@dataclass(frozen=True)
class AccessPolicy:
    """The catalog part of one access kind."""

    default: str | None
    allowed: tuple[str, ...]
    providers_allowed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalog:
    access: Mapping[str, AccessPolicy]
    models: Mapping[str, ModelInfo]

    def policy(self, kind: str) -> AccessPolicy:
        return self.access.get(kind) or AccessPolicy(default=None, allowed=())

    def allowed_models(self, kind: str) -> list[tuple[str, int]]:
        """`(model_id, context_window)` of every model the catalog allows for `kind`."""
        return [(model, self.models[model].context_window) for model in self.policy(kind).allowed]

    def list_cost_micro_usd(self, model: str, tokens: Mapping[str, int]) -> int:
        """List price of one request in micro-USD; 0 for a model without catalog prices."""
        info = self.models.get(model)
        if info is None or info.price is None:
            return 0
        total: float = sum(
            tokens.get(name, 0) * float(getattr(info.price, field)) for name, field in PRICED_TOKENS.items()
        )
        return round(total)


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000_000:
        raise CatalogInvalid(where)
    return value


def _model_id(value: object, where: str) -> str:
    if not isinstance(value, str) or not _MODEL_ID_RE.fullmatch(value):
        raise CatalogInvalid(where)
    return value


def _price(value: object, where: str) -> Price | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(PRICE_FIELDS):
        raise CatalogInvalid(where)
    amounts: list[float] = []
    for name in PRICE_FIELDS:
        amount = value[name]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise CatalogInvalid(where)
        if not math.isfinite(amount) or not 0 <= amount <= MAX_PRICE:
            raise CatalogInvalid(where)
        amounts.append(float(amount))
    return Price(*amounts)


def parse_catalog(data: object) -> Catalog:
    """A `Catalog` from the JSON of `model-catalog.json`; `model_catalog_invalid` names the first bad place."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise CatalogInvalid("version")
    models_raw, access_raw = data.get("models"), data.get("access")
    if not isinstance(models_raw, dict) or not isinstance(access_raw, dict):
        raise CatalogInvalid("root")
    models: dict[str, ModelInfo] = {}
    for model, info in models_raw.items():
        where = "models"
        if not isinstance(info, dict):
            raise CatalogInvalid(where)
        _model_id(model, where)
        models[model] = ModelInfo(
            _int(info.get("context_window"), where),
            _int(info.get("max_output"), where),
            _price(info.get("price_per_mtok"), where),
        )
    access: dict[str, AccessPolicy] = {}
    for kind in ACCESS_KINDS:
        raw = access_raw.get(kind)
        where = f"access/{kind}"
        if not isinstance(raw, dict) or not isinstance(raw.get("allowed"), list):
            raise CatalogInvalid(where)
        default = raw.get("default")
        allowed = tuple(_model_id(item, where) for item in raw["allowed"])
        if default is not None and _model_id(default, where) not in allowed:
            raise CatalogInvalid(where)
        if kind == "platform" and default is None:
            raise CatalogInvalid(where)
        if any(model not in models for model in allowed):
            raise CatalogInvalid(where)
        providers = raw.get("providers_allowed", [])
        if not isinstance(providers, list) or not all(isinstance(p, str) and _LABEL_RE.fullmatch(p) for p in providers):
            raise CatalogInvalid(where)
        access[kind] = AccessPolicy(default=default, allowed=allowed, providers_allowed=tuple(providers))
    return Catalog(access=access, models=models)


def load_catalog(path: Path | None) -> Catalog:
    """The configured catalog file, or the packaged catalog when none is configured."""
    try:
        data = json.loads((path or PACKAGED).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise CatalogInvalid("file") from None
    return parse_catalog(data)
