from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.no_pg

STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_cli" / "static"
SECTION = (STATIC / "section.html").read_text(encoding="utf-8")
NEW_ORDER = (STATIC / "orders_new.html").read_text(encoding="utf-8")
WORK_JS = (STATIC / "design" / "work.js").read_text(encoding="utf-8")

STYLES = (
    "/design/tokens.css",
    "/design/brand.css",
    "/design/motion-base.css",
    "/design/work.css",
    "/design/motion.css",
)
SECTIONS = (
    "usage",
    "sessions",
    "tasks",
    "rounds",
    "agents",
    "checks",
    "gates",
    "work",
    "pings",
)
OS_IDS = (
    "os-repo",
    "os-title",
    "os-base",
    "os-worker",
    "os-notify",
    "os-max-rounds",
    "os-body",
    "os-skips",
    "os-add-skip",
    "os-send",
    "os-result",
)
EXPECTED_STATE_RING = {
    "local-check": ["work", 0.5],
    "pushing": ["work", 0.75],
    "pr-review": ["work", 0.75],
    "open": ["idle", 0],
    "gate-blocked": ["wait", 0.5],
    "finished": ["done", 1],
    "closed": ["cancel", 0],
    "superseded": ["cancel", 0],
    "error": ["bad", 1],
    "ja": ["ok", 1],
    "green": ["ok", 1],
    "nein": ["bad", 1],
    "n_a": ["cancel", 0],
    "unavailable": ["bad", 1],
    "no data": ["idle", 0],
    "not read": ["idle", 0],
    "unknown": ["idle", 0],
}
STATE_RING_LINES = (
    '"local-check": ["work", .5]',
    '"pushing": ["work", .75]',
    '"pr-review": ["work", .75]',
    '"open": ["idle", 0]',
    '"gate-blocked": ["wait", .5]',
    '"finished": ["done", 1]',
    '"closed": ["cancel", 0]',
    '"superseded": ["cancel", 0]',
    '"error": ["bad", 1]',
    '"ja": ["ok", 1]',
    '"green": ["ok", 1]',
    '"nein": ["bad", 1]',
    '"n_a": ["cancel", 0]',
    '"unavailable": ["bad", 1]',
    '"no data": ["idle", 0]',
    '"not read": ["idle", 0]',
    '"unknown": ["idle", 0]',
)


def _number(text: str) -> float:
    if text.startswith("."):
        text = "0" + text
    return float(text)


def ring_table(source: str) -> dict[str, list]:
    body = source.split("const RING = {", 1)[1].split("};", 1)[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    found: dict[str, list] = {}
    for match in re.finditer(
        r'(?:"([^"]+)"|([A-Za-z][\w-]*))\s*:\s*\[\s*"([a-z]+)"\s*,\s*(\.\d+|\d+(?:\.\d+)?)\s*\]',
        body,
    ):
        key = match.group(1) or match.group(2)
        found[key] = [match.group(3), _number(match.group(4))]
    return found


def state_ring_table(html: str) -> dict[str, list]:
    body = html.split("const STATE_RING = {", 1)[1].split("};", 1)[0]
    found: dict[str, list] = {}
    for match in re.finditer(
        r'"([^"]+)"\s*:\s*\[\s*"([a-z]+)"\s*,\s*(\.\d+|\d+(?:\.\d+)?)\s*\]',
        body,
    ):
        found[match.group(1)] = [match.group(2), _number(match.group(3))]
    return found


def test_both_pages_link_the_design_in_order() -> None:
    for html in (SECTION, NEW_ORDER):
        assert '<html lang="en" class="theme-light">' in html
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
        positions = [html.index(f'href="{href}"') for href in STYLES]
        assert positions == sorted(set(positions))
        script = html.index('<script src="/design/work.js"></script>')
        assert positions[-1] < script < html.index("<script>", script)


def test_section_keeps_every_section_and_table() -> None:
    for name in SECTIONS:
        assert f'data-section="{name}"' in SECTION
        assert f'<table id="{name}">' in SECTION
        assert f'href="/{name}"' in SECTION
    assert 'id="section-nav"' in SECTION
    assert 'href="/"' in SECTION
    assert 'href="/#events"' in SECTION


def test_state_ring_has_exactly_the_added_words() -> None:
    for html in (SECTION, NEW_ORDER):
        assert state_ring_table(html) == EXPECTED_STATE_RING
        for line in STATE_RING_LINES:
            assert html.count(line) == 1
        assert "Object.assign(RING, STATE_RING);" in html
    ring = ring_table(WORK_JS)
    assert set(EXPECTED_STATE_RING) & set(ring) == set()
    assert {item[0] for item in EXPECTED_STATE_RING.values()} <= {
        item[0] for item in ring.values()
    }


def test_new_order_page_keeps_every_field_id() -> None:
    for element_id in OS_IDS:
        assert NEW_ORDER.count(f'id="{element_id}"') == 1
