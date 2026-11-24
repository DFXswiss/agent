"""Probe of GitHub's handling of API commits (KOR-47 stage a, ADR-078 "Pre-study").

The package itself never uses a real account; the operator runs `scripts/probes/probe_github_identity.py` once against
a test repository after the merge. The probe answers, for a token of the test account:

1. `unsigned`: is a commit made through `POST /repos/{r}/git/commits` with this token signed by GitHub
   (`verification` of the answer)?
2. `attribution`: a commit with author != committer (both given as `<id>+<login>@users.noreply.github.com`) and two
   `Co-authored-by` trailers - which accounts does GitHub attribute it to (`GET /repos/{r}/commits/{sha}`)?
3. `ssh_signed`: with the scope `write:ssh_signing_key` (or `admin:…`), a fresh Ed25519 signing key is registered
   (`POST /user/ssh_signing_keys`), a commit is signed with the runner's own code (`agent_runner.github.signing`)
   and sent in the `signature` field; reported are GitHub's `verification`, whether GitHub's commit ID equals the one
   the runner computes for the signed object, and whether the key could be deleted again (`admin:ssh_signing_key`;
   otherwise its ID is reported for removal by hand).

The commits are created without a ref (nothing in the repository changes); `keep_ref` makes a branch for viewing.
The token is read from a file, used only in request headers and never printed. The result is one JSON object.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_runner.github import signing

Send = Callable[[str, str, dict[str, Any] | None], tuple[int, Any, dict[str, str]]]
"""`send(method, path, body)` -> `(status, json, headers)`."""

SCOPE_SIGN = "write:ssh_signing_key"
SCOPES_SIGN = frozenset({SCOPE_SIGN, "admin:ssh_signing_key"})
"""Scopes that allow `POST /user/ssh_signing_keys` (`admin:` includes `write:`)."""


@dataclass
class Account:
    login: str
    id: int

    def person(self, at: int) -> signing.Person:
        return signing.Person(self.login, signing.noreply(self.id, self.login), at)

    def trailer(self) -> str:
        return f"Co-authored-by: {self.login} <{signing.noreply(self.id, self.login)}>"


@dataclass
class Report:
    account: str = ""
    scopes: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return {"account": self.account, "scopes": self.scopes, "results": self.results}


def http_sender(api: str, token: str, opener: Callable[..., Any] = urllib.request.urlopen) -> Send:
    """A sender for the GitHub REST API at `api` with `token` (never part of any answer)."""

    def send(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(api.rstrip("/") + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with opener(request, timeout=30) as answer:
                raw, status, headers = answer.read(), answer.status, dict(answer.headers.items())
        except urllib.error.HTTPError as error:
            raw, status, headers = error.read(), error.code, dict(error.headers.items())
        return status, json.loads(raw) if raw else None, {key.lower(): value for key, value in headers.items()}

    return send


def _ok(status: int, payload: Any, what: str) -> dict[str, Any]:
    if status // 100 != 2 or not isinstance(payload, dict):
        raise RuntimeError(f"{what}: HTTP {status}")
    return payload


def _verification(payload: dict[str, Any]) -> dict[str, Any]:
    found = payload.get("verification")
    found = found if isinstance(found, dict) else {}
    return {"verified": found.get("verified"), "reason": found.get("reason")}


def _attribution(send: Send, repo: str, sha: str) -> dict[str, Any]:
    status, payload, _ = send("GET", f"/repos/{repo}/commits/{sha}", None)
    if status != 200 or not isinstance(payload, dict):
        return {"status": status}

    def login(key: str) -> Any:
        account = payload.get(key)
        return account.get("login") if isinstance(account, dict) else None

    return {"author": login("author"), "committer": login("committer")}


def run(
    send: Send, repo: str, *, co_author: Account, keep_ref: bool = False, now: Callable[[], float] = time.time
) -> Report:
    """The three questions of the module docstring for the token behind `send`."""
    report = Report()
    status, me, headers = send("GET", "/user", None)
    user = _ok(status, me, "GET /user")
    account = Account(str(user["login"]), int(user["id"]))
    report.account = account.login
    report.scopes = sorted(item.strip() for item in headers.get("x-oauth-scopes", "").split(",") if item.strip())
    meta = _ok(*send("GET", f"/repos/{repo}", None)[:2], f"GET /repos/{repo}")
    branch = str(meta["default_branch"])
    ref = _ok(*send("GET", f"/repos/{repo}/git/ref/heads/{branch}", None)[:2], "GET ref")
    head = str(ref["object"]["sha"])
    tree = str(_ok(*send("GET", f"/repos/{repo}/git/commits/{head}", None)[:2], "GET commit")["tree"]["sha"])
    stamp = int(now())

    plain = _ok(
        *send(
            "POST",
            f"/repos/{repo}/git/commits",
            {"message": f"Probe {stamp}: unsigned.", "tree": tree, "parents": [head]},
        )[:2],
        "POST commit (unsigned)",
    )
    report.results["unsigned"] = {"sha": plain["sha"], **_verification(plain)}

    message = f"Probe {stamp}: author and committer differ.\n\n{co_author.trailer()}\n{account.trailer()}\n"
    body = {
        "message": message,
        "tree": tree,
        "parents": [head],
        "author": co_author.person(stamp).api(),
        "committer": account.person(stamp).api(),
    }
    mixed = _ok(*send("POST", f"/repos/{repo}/git/commits", body)[:2], "POST commit (attribution)")
    report.results["attribution"] = {
        "sha": mixed["sha"],
        **_verification(mixed),
        **_attribution(send, repo, str(mixed["sha"])),
    }
    if keep_ref:
        name = f"refs/heads/probe-identity-{stamp}"
        status, _, _ = send("POST", f"/repos/{repo}/git/refs", {"ref": name, "sha": mixed["sha"]})
        report.results["attribution"]["ref"] = name if status == 201 else f"HTTP {status}"

    scoped = bool(SCOPES_SIGN & set(report.scopes))
    report.results["ssh_signed"] = _signed(send, repo, account, tree=tree, head=head, stamp=stamp, scoped=scoped)
    return report


def _signed(
    send: Send, repo: str, account: Account, *, tree: str, head: str, stamp: int, scoped: bool
) -> dict[str, Any]:
    if not scoped:
        return {"skipped": f"the token lacks {SCOPE_SIGN}"}
    seed = signing.new_seed()
    key = {"title": f"identity probe {stamp}", "key": signing.public_line(seed, "")}
    status, created, _ = send("POST", "/user/ssh_signing_keys", key)
    if status != 201 or not isinstance(created, dict):
        return {"skipped": f"POST /user/ssh_signing_keys: HTTP {status}"}
    person = account.person(stamp)
    message = f"Probe {stamp}: signed with an SSH key.\n"
    text = signing.commit_text(tree, [head], person, person, message)
    signature = signing.sign(seed, text)
    body = {
        "message": message,
        "tree": tree,
        "parents": [head],
        "author": person.api(),
        "committer": person.api(),
        "signature": signature,
    }
    status, commit, _ = send("POST", f"/repos/{repo}/git/commits", body)
    result: dict[str, Any] = {"status": status}
    if status == 201 and isinstance(commit, dict):
        result.update(
            sha=commit["sha"],
            expected_sha=signing.signed_commit_id(text, signature),
            **_verification(commit),
        )
    removed, _, _ = send("DELETE", f"/user/ssh_signing_keys/{created['id']}", None)
    result["key_removed"] = removed == 204
    if removed != 204:
        result["key_id"] = created["id"]
    return result
