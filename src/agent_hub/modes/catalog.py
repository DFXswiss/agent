"""The catalogue of working modes (ADR-098 section 1), a copy of `contracts/modes/`.

The contracts directory is not part of the installed package, so the entries are kept here as code; the test
`test_catalog_equals_the_contract` fails when the two differ. `offered(test)` is what `GET /v1/modes` lists: every
entry in catalogue order, an entry with `test: true` only on a hub configured with `AGENT_HUB_MODES_TEST=1`.
`find(name, test)` resolves a name among the offered entries (None: unknown here). Listing or resolving a mode never
switches it on (ADR-098 section 2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

CATALOG: tuple[Mapping[str, Any], ...] = (
    MappingProxyType(
        {
            "name": "test",
            "version": 1,
            "title": {"de": "Testmodus", "en": "Test mode"},
            "summary": {
                "de": (
                    "Prüft die Grundlage der Arbeitsweisen: Wahl, Anzeige, Ausschalten und einen Treiber, der die"
                    " Sitzung einmal weckt. Nur in Testumgebungen."
                ),
                "en": (
                    "Checks the foundation of working modes: choice, display, switching off and a driver that wakes"
                    " the session once. Test environments only."
                ),
            },
            "brief": "Working mode: test",
            "operations": [],
            "drivers": ["test"],
            "settings": {"type": "object", "properties": {}, "additionalProperties": False},
            "needs": {"modes": [], "prerequisites": []},
            "accesses": [],
            "passes_on": True,
            "test": True,
        }
    ),
)
"""The entries of `contracts/modes/INDEX.json`, in its order (`contracts/modes/<name>.json` each)."""


@dataclass(frozen=True)
class Mode:
    """A catalogue entry as the hub uses it."""

    name: str
    version: int
    needs: tuple[str, ...]
    passes_on: bool
    entry: Mapping[str, Any]


def _mode(entry: Mapping[str, Any]) -> Mode:
    return Mode(
        name=str(entry["name"]),
        version=int(entry["version"]),
        needs=tuple(entry["needs"]["modes"]),
        passes_on=bool(entry["passes_on"]),
        entry=entry,
    )


def offered(test: bool) -> list[dict[str, Any]]:
    """The entries `GET /v1/modes` lists (module docstring), as plain JSON."""
    return [_plain(entry) for entry in CATALOG if test or not entry["test"]]


def find(name: str, test: bool) -> Mode | None:
    """The offered entry of that name, None when this hub offers none."""
    for entry in CATALOG:
        if entry["name"] == name and (test or not entry["test"]):
            return _mode(entry)
    return None


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_plain(item) for item in value]
    return value
