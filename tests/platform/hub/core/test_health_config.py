"""`GET /v1/health` and `GET /v1/config` against contracts/api/ (03-api.md section 4.1)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.signing import b64url_encode
from agent_hub import __version__
from agent_hub.config import LIMITS
from tests.platform.hub.conftest import TEST_HUB_KEY_ID, HubHarness, raw_public, root_private_key


def test_health(hub: HubHarness) -> None:
    response = hub.anon.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": __version__}
    assert "agent-hub-signature" not in response.headers


def test_health_signed_is_signed(hub: HubHarness) -> None:
    response = hub.root.get("/v1/health")
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True


def test_health_unavailable_without_database(hub: HubHarness) -> None:
    hub.pool.close()
    response = hub.anon.get("/v1/health")
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}


def test_config(hub: HubHarness) -> None:
    response = hub.anon.get("/v1/config")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    signer = hub.config.signer()
    assert body["rp_id"] == "localhost"
    assert body["origin"] == "http://localhost:8788"
    assert body["platform_root_key"] == b64url_encode(raw_public(root_private_key()))
    assert body["hub_key"] == {"id": TEST_HUB_KEY_ID, "public_key": signer.public_key_text}
    assert body["version"] == __version__
    # the fixed limits and the subagent settings with their defaults (ADR-088 section 2; 0 = no limit)
    assert body["limits"] == {
        **LIMITS,
        "subagent_max_depth": 5,
        "subagent_max_running": 50,
        "subagent_max_total": 0,
        "subagent_max_runtime_seconds": 0,
    }
    assert body["defaults"] == {"platform": "deepseek/deepseek-v4.1-flash-1017"}
    assert body["models"]["platform"] == ["deepseek/deepseek-v4.1-flash-1017", "deepseek/deepseek-v4-pro"]
    assert "claude-opus-5-5" in body["models"]["api_key"]
    assert body["models"]["subscription"] == body["models"]["api_key"]


@pytest.mark.parametrize("path", ["/v1/health", "/v1/config"])
def test_unknown_query_parameters_are_refused(hub: HubHarness, path: str) -> None:
    with pytest.raises(Exception, match="Additional properties"):
        hub.anon.get(path, params={"x": "1"})
    response = hub.anon.get(path, params={"x": "1"}, extensions={"contract": False})
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid"}
