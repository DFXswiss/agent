"""U-DBL-model: the credential check per access kind (05-runner.md section 10.2)."""

from __future__ import annotations

import pytest

from agent_doubles.model.access import Access, AccessError, parse_access

TOKEN = "double-test-token"  # not a secret: the double only compares it


@pytest.mark.parametrize(
    ("kind", "headers", "reason"),
    [
        ("api_key", {"x-api-key": TOKEN}, None),
        ("api_key", {"x-api-key": "other"}, "invalid x-api-key"),
        ("api_key", {}, "invalid x-api-key"),
        ("api_key", {"x-api-key": TOKEN, "authorization": f"Bearer {TOKEN}"}, "authorization header not expected"),
        ("openrouter", {"authorization": f"Bearer {TOKEN}"}, None),
        ("openrouter", {"authorization": TOKEN}, "invalid bearer token"),
        ("openrouter", {"authorization": f"Bearer {TOKEN}", "x-api-key": TOKEN}, "x-api-key header not expected"),
        ("subscription", {"authorization": f"Bearer {TOKEN}", "anthropic-beta": "a, oauth-2025-04-20"}, None),
        ("subscription", {"authorization": f"Bearer {TOKEN}"}, "OAuth authentication is currently not supported."),
        ("subscription", {"authorization": "Bearer other", "anthropic-beta": "oauth-2025-04-20"}, "invalid bearer"),
    ],
)
def test_refusal_per_kind(kind: str, headers: dict[str, str], reason: str | None) -> None:
    refusal = Access(kind, TOKEN).refusal(headers)
    if reason is None:
        assert refusal is None
    else:
        assert refusal is not None
        assert refusal.startswith(reason)
        assert TOKEN not in refusal


def test_access_validation_and_repr() -> None:
    assert TOKEN not in repr(Access("api_key", TOKEN))
    with pytest.raises(AccessError, match="kind"):
        Access("cookie", TOKEN)
    with pytest.raises(AccessError, match="empty"):
        Access("api_key", "")
    assert parse_access(None) is None
    assert parse_access({"kind": "openrouter", "token": "t"}) == Access("openrouter", "t")
    invalid: list[object] = [[], {"kind": "api_key"}, {"kind": "api_key", "token": "t", "x": 1}]
    for raw in invalid:
        with pytest.raises(AccessError, match="null or an object"):
            parse_access(raw)
    with pytest.raises(AccessError, match="strings"):
        parse_access({"kind": 1, "token": "t"})
    with pytest.raises(AccessError, match="strings"):
        parse_access({"kind": "api_key", "token": 1})
