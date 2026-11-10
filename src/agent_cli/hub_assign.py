"""Small authenticated client for website-owned assignment creation."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .coordinator_common import redact
from .hub import Hub

_ASSIGNMENT_TEXT_LIMIT = 2000


def _without_bearer(text: str, token: str | None) -> str:
    """Remove the device bearer (raw, header form and URL-encoded) from hub text."""
    if token:
        forms = {token, f"Bearer {token}", quote(token, safe="")}
        for form in sorted(forms, key=len, reverse=True):
            if form:
                text = text.replace(form, "[redacted]")
    return text


def sanitized_assignment_text(text: str, token: str | None, *, limit: int) -> str:
    """Bounded, redacted, control-character-free hub text without the bearer."""
    from .main import _sanitize_lane_output

    cleaned = _without_bearer(str(text or ""), token)
    return redact(_sanitize_lane_output(cleaned), limit=limit)


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
        json={
            "agent": agent,
            "repo": repo,
            "title": title,
            "body": body,
            "deviation": {"declared": False},
        },
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 201:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        url = payload.get("url") if isinstance(payload, dict) else None
        raw_detail = str(
            detail or response.text or f"hub returned HTTP {response.status_code}"
        )
        safe_url: str | None = None
        if isinstance(url, str) and url:
            safe_url = sanitized_assignment_text(
                url, hub.token, limit=_ASSIGNMENT_TEXT_LIMIT
            ) or None
        raise AssignmentFailure(
            sanitized_assignment_text(
                raw_detail, hub.token, limit=_ASSIGNMENT_TEXT_LIMIT
            ),
            safe_url,
        )
    if not isinstance(payload, dict):
        raise AssignmentFailure("hub returned an invalid assignment response")
    return payload
