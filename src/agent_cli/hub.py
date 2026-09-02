"""HTTP client for the hub. Every error is raised; nothing is guessed."""

from __future__ import annotations

import json
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
            try:
                return response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError) as exc:
                # RecursionError: CPython's C-accelerated json decoder still
                # bounds recursion by C stack depth (Py_EnterRecursiveCall),
                # not just sys.getrecursionlimit() - a pathologically nested
                # body (adversarial or buggy hub) hits it well before running
                # out of memory. Confirmed empirically: json.loads('[' * n +
                # ']' * n) raises RecursionError around n=1_000_000, not
                # JSONDecodeError, so it needs its own arm in this tuple.
                raise HubError(f"hub {method} {path} → invalid JSON response") from exc
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

    def pull(self, cursors: dict[str, int]) -> dict[str, Any]:
        params = [("cursor", f"{origin}:{seq}") for origin, seq in cursors.items()]
        return self.request("GET", "/sync/pull", headers=self._headers(), params=params)

    def restore(self) -> dict[str, Any]:
        return self.request("GET", "/sync/restore", headers=self._headers())

    def ack(self, ping_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/pings/{ping_id}/ack", headers=self._headers())

    def put_subscriptions(self, subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request(
            "PUT",
            "/sync/subscriptions",
            headers=self._headers(),
            json={"subscriptions": subscriptions},
        )

    def get_subscriptions(self) -> dict[str, Any]:
        return self.request("GET", "/sync/subscriptions", headers=self._headers())

    def query(self, match: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/sync/query", headers=self._headers(), json={"match": match})

    def sync_ws_url(self) -> str:
        if self.token is None or self.token == "":
            raise HubError("device token is not set; run agent pair")
        if self.base_url.startswith("https://"):
            ws_base = "wss://" + self.base_url.removeprefix("https://")
        elif self.base_url.startswith("http://"):
            ws_base = "ws://" + self.base_url.removeprefix("http://")
        else:
            raise HubError(f"unsupported hub URL scheme: {self.base_url}")
        return f"{ws_base}/sync/ws?token={self.token}"

    def connect_sync_ws(self) -> Any:
        """Open a synchronous WebSocket to GET /sync/ws. Caller must close it."""
        from websockets.sync.client import connect

        url = self.sync_ws_url()
        try:
            return connect(url)
        except Exception as exc:
            raise HubError(f"hub websocket failed: {exc}") from exc


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return response.text
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return response.text
