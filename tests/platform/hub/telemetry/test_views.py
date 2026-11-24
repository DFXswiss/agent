"""The hub's copy of the closed view list equals `contracts/enums/ui-views.json` (ADR-086 section 5)."""

from __future__ import annotations

import json
import re

from agent_hub.telemetry import views
from tests.platform.contracts.harness import CONTRACTS


def test_views_equal_the_contract() -> None:
    document = json.loads((CONTRACTS / "enums" / "ui-views.json").read_text(encoding="utf-8"))
    assert list(views.VIEWS) == document["views"]
    assert document["fallback"] == views.FALLBACK
    assert list(views.BUCKETS_MS) == document["buckets_ms"]
    assert document["limits"] == views.LIMITS
    request = json.loads((CONTRACTS / "api" / "telemetry-ui.request.json").read_text(encoding="utf-8"))
    common = json.loads((CONTRACTS / "common.json").read_text(encoding="utf-8"))
    assert common["$defs"]["version_text"]["pattern"] == views.VERSION_PATTERN
    assert all(re.fullmatch(views.VIEW_PATTERN[1:-1], name) for name in views.VIEWS)
    assert re.fullmatch(views.VERSION_PATTERN[1:-1], views.OTHER_VERSION)
    assert request["properties"]["version"]["$ref"].endswith("/version_text")
