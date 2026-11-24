"""An account without a passkey is a valid state everywhere (rule E-27, KOR-62, ADR-070; rule E-19: the class, not
the single case).

An account of an agent alone has an identity, a recovery slot, `CK_1` and an active agent, but no `hub.passkey` row
and never a `hub.browser_session`. The places that may depend on those two tables are few and named here with the
reason why an account without them is fine there; a new hub module that reads them (a list, a count, a join that would
drop or refuse such an account) fails this test until it is reviewed and added. On the web client's side the views of
other members and of organizations never read passkeys, so an account of an agent appears like any member.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB = REPO_ROOT / "src" / "agent_hub"
WEB = REPO_ROOT / "web" / "src"
TABLES = re.compile(r"\bhub\.(?:passkey|browser_session)\b")
REVIEWED = {
    "agents/store.py": "has_passkey: the one test 'account of a person or of an agent alone'; the gesture session",
    "auth/jobs.py": "deletes the rows of a released registration (zero rows of an agent account are fine)",
    "auth/keys.py": "resolves browser session keys only; agents resolve through hub.agent",
    "auth/login.py": "the passkey sign-in itself (an agent account never signs in this way)",
    "auth/passkeys_manage.py": "adding and removing passkeys (an agent account has none to remove)",
    "auth/recovery.py": "excludes the active passkeys from new options (an empty list for an agent account)",
    "auth/register.py": "inserts the passkey of a person's registration",
    "auth/sessions.py": "ends a browser session (an agent ends its access with agents revoke)",
    "me/recovery_code.py": "the browser's proof for a code replacement; an agent's signed request is its own proof",
    "me/routes.py": "lists the passkeys in GET /v1/me (an empty list for an agent account)",
}
MEMBER_VIEWS = ("ui/screens/members", "ui/screens/workgroups", "core/org")


def hub_modules() -> set[str]:
    return {
        path.relative_to(HUB).as_posix()
        for path in HUB.rglob("*.py")
        if TABLES.search(path.read_text(encoding="utf-8"))
    }


def test_every_hub_module_that_reads_passkeys_or_sessions_is_reviewed() -> None:
    assert sorted(hub_modules() - set(REVIEWED)) == []


def test_the_review_list_names_no_module_that_no_longer_reads_them() -> None:
    assert sorted(set(REVIEWED) - hub_modules()) == []


def test_the_member_views_of_the_web_client_never_read_passkeys() -> None:
    readers = [
        path.relative_to(WEB).as_posix()
        for view in MEMBER_VIEWS
        for path in (WEB / view).rglob("*.ts*")
        if "passkey" in path.read_text(encoding="utf-8").lower()
    ]
    assert readers == []
    assert all((WEB / view).is_dir() for view in MEMBER_VIEWS)
