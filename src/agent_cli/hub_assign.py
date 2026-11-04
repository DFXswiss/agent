"""Small authenticated client for website-owned assignment creation."""

from __future__ import annotations

from typing import Any

from .hub import Hub


class AssignmentFailure(Exception):
    def __init__(self, detail: str, url: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.url = url


def create_assignment(
    hub: Hub,
    *,
    agent: str,
    repo: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    response = hub.request_raw(
        "POST",
        "/api/assignments",
        headers=hub.authenticated_headers(),
        json={"agent": agent, "repo": repo, "title": title, "body": body},
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 201:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        url = payload.get("url") if isinstance(payload, dict) else None
        raise AssignmentFailure(
            str(detail or response.text or f"hub returned HTTP {response.status_code}"),
            str(url) if isinstance(url, str) and url else None,
        )
    if not isinstance(payload, dict):
        raise AssignmentFailure("hub returned an invalid assignment response")
    return payload
