"""Small authenticated client for website-owned assignment creation."""

from __future__ import annotations

from typing import Any

from .coordinator_common import redact
from .hub import Hub
from .text_sanitize import redact_terminal_interleaved_urlencoded_secret

_ASSIGNMENT_TEXT_LIMIT = 2000


def _bearer_forms(token: str | None) -> tuple[str, ...]:
    if not token:
        return ()
    header = f"Bearer {token}"
    return header, token


def _without_bearer(text: str, token: str | None) -> str:
    """Remove raw, encoded, or mixed device bearer representations."""
    for form in _bearer_forms(token):
        text = redact_terminal_interleaved_urlencoded_secret(text, form)
    return text


def _without_interleaved_bearer(text: str, token: str | None) -> str:
    """Remove encoded bearer forms even when terminal controls split characters."""
    for form in _bearer_forms(token):
        text = redact_terminal_interleaved_urlencoded_secret(text, form)
    return text


def sanitized_assignment_text(text: str, token: str | None, *, limit: int) -> str:
    """Bounded, redacted, control-character-free hub text without the bearer."""
    cleaned = _without_interleaved_bearer(str(text or ""), token)
    cleaned = redact(cleaned, limit=None)
    cleaned = _without_bearer(cleaned, token)
    return redact(cleaned, limit=limit)


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
