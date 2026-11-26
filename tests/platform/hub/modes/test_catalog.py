"""U-HUB-modes: the catalogue of working modes equals `contracts/modes/` and offers the test mode only where configured
(ADR-098 section 1, KOR-75)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_hub.modes import catalog
from tests.platform.hub.conftest import contracts_registry


def test_catalog_equals_the_contract(repo_root: Path) -> None:
    index = json.loads((repo_root / "contracts/modes/INDEX.json").read_text(encoding="utf-8"))
    entries = [json.loads((repo_root / "contracts" / item["file"]).read_text(encoding="utf-8")) for item in index["modes"]]
    assert [item["name"] for item in index["modes"]] == [entry["name"] for entry in entries]
    assert catalog.offered(True) == entries
    for entry in entries:
        contracts_registry().validate(index["schema"], entry)


def test_offered_and_found_only_where_configured() -> None:
    assert [entry["name"] for entry in catalog.offered(True)] == ["test"]
    assert catalog.offered(False) == []
    found = catalog.find("test", True)
    assert found is not None
    assert (found.name, found.version, found.needs, found.passes_on) == ("test", 1, (), True)
    assert catalog.find("test", False) is None
    assert catalog.find("a38", True) is None
