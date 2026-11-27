"""Contract rules of KOR-74 (ADR-099): the mailbox body a `mailbox.check` request carries is the credential body."""

from __future__ import annotations

import json

from tests.platform.contracts.harness import CONTRACTS


def test_the_mailbox_of_a_check_request_is_the_credential_body() -> None:
    request = json.loads((CONTRACTS / "requests" / "mailbox.check.request.json").read_text(encoding="utf-8"))
    body = json.loads((CONTRACTS / "credentials" / "mailbox.json").read_text(encoding="utf-8"))
    copy = request["$defs"]["mailbox"]
    assert {key: copy[key] for key in ("type", "properties", "required", "additionalProperties")} == {
        key: body[key] for key in ("type", "properties", "required", "additionalProperties")
    }
