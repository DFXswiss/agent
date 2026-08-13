"""HTTP client for the hub. Every error is raised; nothing is guessed."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx


class HubError(SystemExit):
    pass


class Hub:
    def __init__(self, base_url: str, token: str | None = None, client: httpx.Client | None = None) -> None:
        if base_url == "":
            raise HubError("hub URL is empty")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns = client is None
        self.client = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns:
            self.client.close()

    def _headers(self) -> dict[str, str]:
        if self.token is None or self.token == "":
            raise HubError("device token is not set; run agent pair")
        return {"Authorization": f"Bearer {self.token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise HubError(f"hub request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = _detail(response)
            raise HubError(f"hub {method} {path} → HTTP {response.status_code}: {detail}")
        if response.content:
            return response.json()
        return None

    def prepare(self, device_id: str, challenge: str, device_name: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pair/prepare",
            json={"device_id": device_id, "challenge": challenge, "device_name": device_name},
        )

    def wait(self, device_id: str, challenge: str) -> dict[str, Any]:
        return self.request("GET", "/pair/wait", params={"device_id": device_id, "challenge": challenge})

    def push(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("POST", "/sync/push", headers=self._headers(), json={"events": events})

    def pull(self, after_hub_seq: int) -> dict[str, Any]:
        return self.request(
            "GET",
            "/sync/pull",
            headers=self._headers(),
            params={"after_hub_seq": str(after_hub_seq)},
        )

    def restore(self) -> dict[str, Any]:
        return self.request("GET", "/sync/restore", headers=self._headers())


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return response.text
