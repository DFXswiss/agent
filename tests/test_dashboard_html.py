from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.no_pg


def test_dashboard_html_contains_usage_table() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_cli"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="usage"' in html
    assert "usage.snapshot" in html
    assert 'typeof p.used_percent === "number"' in html
    assert 'if (ev.type !== "usage.snapshot") return;' in html
    assert "if (usageSeen[key]) return;" in html
    assert ".reverse(" not in html
