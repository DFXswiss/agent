"""Mail into the mail double for the mailbox scenario (E2E-74; KOR-74, ADR-099).

- `POST /mail/deliver` (`{"to", "from", "subject", "text"?, "html"?, "spam"?}`): delivers one mail through the control
  route of the mail double that `POST /login/doubles` started (`409` before), like a service sending a sign-in mail.
  Answers `{delivered: [{user, folder, uid}]}`.
"""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes.login import _control

REQUIRED = ("to", "from", "subject")
OPTIONAL = ("text", "html")


def post_deliver(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if (
        not isinstance(body, dict)
        or set(body) - {*REQUIRED, *OPTIONAL, "spam"}
        or not all(isinstance(body.get(key), str) and body[key] for key in REQUIRED)
        or not all(isinstance(body[key], str) for key in OPTIONAL if key in body)
        or not isinstance(body.get("spam", False), bool)
    ):
        raise RouteError(400, "invalid", 'body must be {"to", "from", "subject", "text"?, "html"?, "spam"?}')
    double = ctx.doubles.get("mail")
    if double is None:
        raise RouteError(409, "invalid", "the mail double is not running (POST /login/doubles first)")
    return 200, _control(double, "/_double/deliver", body)


ROUTES = [Route("POST", "/mail/deliver", post_deliver)]
