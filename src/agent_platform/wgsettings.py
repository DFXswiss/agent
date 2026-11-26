"""Workgroup settings: the repository allow-list and the other settings of a workgroup (06-web.md section 8.1, 03-api.md
sections 9 and 10, ADR-054; twin of `web/src/core/wg-settings/settings.ts`).

The settings are a `settings.workgroup` event of the workgroup scope, sealed with the workgroup's current key and
signed by the user (`POST /v1/events`); the newest verified one (by `scope_seq`) wins, a body that does not fit the
contract counts as `invalid` and the defaults apply. Writing needs `wg:write` (checked here against the verified log,
and again by the hub). Repositories are `owner/name` (stored lower case, duplicates dropped, at most 500); an empty list
allows every repository the user's GitHub connection reaches. Protected branch patterns: at most 100.

- `repos list|allow|disallow --wg`: the list, and a change of it;
- `wg settings --wg`: shows the settings, or with `--protected-branch` / `--pr-draft-default` sets them; the merge
  criteria and the separation of duties (KOR-51, ADR-093 sections 5 and 6, all optional and off by default) with
  `--merge-verdicts N`, `--merge-verdict-profile NAME`, `--merge-checks-green true|false`, `--merge-approvals N` and
  `--separate-duties true|false`;
- `wg rename --wg --name`: the workgroup's name, a `wg.profile` event of the org scope sealed with the org key (admins).
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_all, post_event, pull, seal, uuid_argument
from agent_platform.orgs import display_name, find_org
from agent_runner.crypto.log.state import ORG, WG

KIND = "settings.workgroup"
DEFAULT_PROTECTED: tuple[str, ...] = ()
"""No default (ADR-079 section 3): GitHub's own branch protection applies."""
MAX_REPOSITORIES = 500
MAX_PATTERNS = 100
REPO_RE = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
LOWER_REPO_RE = re.compile(r"[a-z0-9-]{1,39}/[a-z0-9._-]{1,100}")
PATTERN_RE = re.compile(r"[A-Za-z0-9._/*+-]{1,200}")
DEFAULTS: dict[str, Any] = {"repositories": [], "protected_branches": list(DEFAULT_PROTECTED), "pr_draft_default": True}


KEYS = frozenset({"pr_draft_default", "protected_branches", "repositories"})
OPTIONAL_KEYS = frozenset({"merge_criteria", "separate_duties"})
CRITERIA_KEYS = frozenset({"passed_verdicts", "verdict_profile", "checks_green", "github_approvals"})
PROFILE_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _criteria_valid(value: object) -> bool:
    if not isinstance(value, dict) or not set(value) <= CRITERIA_KEYS:
        return False
    profile = value.get("verdict_profile", "review")
    return (
        all(_count(value.get(name, 0)) for name in ("passed_verdicts", "github_approvals"))
        and isinstance(value.get("checks_green", False), bool)
        and isinstance(profile, str)
        and PROFILE_RE.fullmatch(profile) is not None
    )


def check_body(body: object) -> dict[str, Any] | None:
    """A body exactly as `contracts/events/settings.workgroup.json` allows it, else None."""
    if not isinstance(body, dict) or not KEYS <= set(body) <= KEYS | OPTIONAL_KEYS:
        return None
    if "merge_criteria" in body and not _criteria_valid(body["merge_criteria"]):
        return None
    if not isinstance(body.get("separate_duties", False), bool):
        return None

    def listed(value: object, pattern: re.Pattern[str], most: int) -> bool:
        return (
            isinstance(value, list)
            and len(value) <= most
            and all(isinstance(item, str) and pattern.fullmatch(item) for item in value)
        )

    repositories, patterns = body["repositories"], body["protected_branches"]
    if not listed(repositories, LOWER_REPO_RE, MAX_REPOSITORIES) or not listed(patterns, PATTERN_RE, MAX_PATTERNS):
        return None
    if not isinstance(body["pr_draft_default"], bool):
        return None
    return dict(body)


def _place(account: Account, wg: str) -> str:
    return find_org(account, uuid_argument(wg))


def current(account: Account, wg: str) -> dict[str, Any]:
    """`{status: none|set|invalid, seq, settings, author}` of the newest verified settings event."""
    org = _place(account, wg)
    items, _last = pull(account, wg, {"kinds": KIND})
    events, skipped = open_all(account, org, (WG, wg), items)
    found: dict[str, Any] = {"status": "none", "seq": 0, "settings": DEFAULTS, "author": None}
    for event in sorted(events, key=lambda item: int(item["seq"])):
        if event.get("locked"):
            continue
        settings = check_body(event.get("body"))
        status = "invalid" if settings is None else "set"
        found = {"status": status, "seq": event["seq"], "settings": settings or DEFAULTS, "author": event["author"]}
    writable = account.org_log(org).state.can_write_wg(account.user_id, wg)
    return {"org_id": org, "wg_id": wg, **found, "writable": writable, "unverified": skipped}


def save(account: Account, wg: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Seal and send new settings (`no_write_access` without `wg:write`)."""
    org = _place(account, wg)
    if not account.org_log(org).state.can_write_wg(account.user_id, wg):
        raise PlatformError("no_write_access")
    seq = post_event(account, seal(account, org, (WG, wg), KIND, settings, wg=wg))
    return {"org_id": org, "wg_id": wg, "status": "set", "seq": seq, "settings": settings, "author": account.user_id}


def repository(text: str) -> str:
    """`owner/name` lower case (`invalid_argument` otherwise)."""
    value = text.strip()
    if not REPO_RE.fullmatch(value):
        raise PlatformError("invalid_argument", reason="repo_invalid")
    return value.lower()


def _repos_result(result: dict[str, Any]) -> dict[str, Any]:
    repositories = result["settings"]["repositories"]
    return {
        "org_id": result["org_id"],
        "wg_id": result["wg_id"],
        "repositories": repositories,
        "all_repositories_allowed": not repositories,
        "seq": result["seq"],
    }


def repos_list(account: Account, wg: str) -> dict[str, Any]:
    return _repos_result(current(account, wg))


def repos_change(account: Account, wg: str, names: list[str], allow: bool) -> dict[str, Any]:
    wanted = [repository(name) for name in names]
    if not wanted:
        raise PlatformError("invalid_argument")
    shown = current(account, wg)
    settings = dict(shown["settings"])
    repositories = list(settings["repositories"])
    for name in wanted:
        if allow and name not in repositories:
            repositories.append(name)
        if not allow and name in repositories:
            repositories.remove(name)
    if repositories == settings["repositories"]:
        return {**_repos_result(shown), "changed": False}
    if len(repositories) > MAX_REPOSITORIES:
        raise PlatformError("invalid_argument", reason="too_many")
    return {**_repos_result(save(account, wg, {**settings, "repositories": repositories})), "changed": True}


def merge_settings(settings: dict[str, Any], merge: dict[str, Any]) -> dict[str, Any]:
    """`settings` with the given merge criteria and separation (`None` values are left as they are)."""
    criteria = dict(settings.get("merge_criteria", {}))
    for name in ("passed_verdicts", "github_approvals"):
        if merge.get(name) is not None:
            if merge[name] < 0:
                raise PlatformError("invalid_argument", reason="count_invalid")
            criteria[name] = merge[name]
    if merge.get("verdict_profile") is not None:
        if PROFILE_RE.fullmatch(merge["verdict_profile"]) is None:
            raise PlatformError("invalid_argument", reason="profile_invalid")
        criteria["verdict_profile"] = merge["verdict_profile"]
    if merge.get("checks_green") is not None:
        criteria["checks_green"] = merge["checks_green"] == "true"
    out = {**settings, "merge_criteria": criteria} if criteria else dict(settings)
    if merge.get("separate_duties") is not None:
        out["separate_duties"] = merge["separate_duties"] == "true"
    return out


def wg_settings(
    account: Account, wg: str, patterns: list[str] | None, draft: str | None, merge: dict[str, Any] | None = None
) -> dict[str, Any]:
    shown = current(account, wg)
    wanted = {name: value for name, value in (merge or {}).items() if value is not None}
    if patterns is None and draft is None and not wanted:
        return shown
    settings = merge_settings(dict(shown["settings"]), wanted)
    if patterns is not None:
        values: list[str] = []
        for pattern in (item.strip() for item in patterns):
            if not PATTERN_RE.fullmatch(pattern):
                raise PlatformError("invalid_argument", reason="pattern_invalid")
            if pattern not in values:
                values.append(pattern)
        if len(values) > MAX_PATTERNS:
            raise PlatformError("invalid_argument", reason="too_many")
        settings["protected_branches"] = values
    if draft is not None:
        settings["pr_draft_default"] = draft == "true"
    return save(account, wg, settings)


def wg_rename(account: Account, wg: str, name: str) -> dict[str, Any]:
    """A new `wg.profile` event of the org scope (admins; twin of `renameWorkgroup`)."""
    org = _place(account, wg)
    text = display_name(name)
    log = account.org_log(org)
    if not log.state.is_admin(account.user_id):
        raise PlatformError("not_admin")
    event = seal(account, org, (ORG, org), "wg.profile", {"name": text}, wg=wg)
    return {"org_id": org, "wg_id": wg, "name": text, "seq": post_event(account, event)}


# --- commands ---


def _list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return repos_list(account, args.wg)


def _change(allow: bool) -> Any:
    def work(account: Account, args: Any, context: Context) -> dict[str, Any]:
        return repos_change(account, args.wg, args.repo, allow)

    return work


def _settings(account: Account, args: Any, context: Context) -> dict[str, Any]:
    merge = {
        "passed_verdicts": args.merge_verdicts,
        "verdict_profile": args.merge_verdict_profile,
        "checks_green": args.merge_checks_green,
        "github_approvals": args.merge_approvals,
        "separate_duties": args.separate_duties,
    }
    return wg_settings(account, args.wg, args.protected_branch, args.pr_draft_default, merge)


def _rename(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return wg_rename(account, args.wg, args.name)


# `repos allow` and `repos disallow`: example and search words.
REPO_DOCS = {
    "allow": (
        "agent-platform repos allow --wg <wg_id> --repo owner/name",
        "erlauben freigeben hinzufügen repository permit add",
    ),
    "disallow": (
        "agent-platform repos disallow --wg <wg_id> --repo owner/name",
        "verbieten sperren entfernen repository block deny remove",
    ),
}


def register(tree: Tree) -> None:
    group = tree.group("repos", "the repository allow-list of a workgroup (empty: every repository)")
    sub = tree.command(
        group,
        "list",
        _list,
        "the allowed repositories",
        example="agent-platform repos list --wg <wg_id>",
        words="erlaubte repositories freigabeliste allowed allowlist",
        then="agent-platform repos allow --wg <wg_id> --repo owner/name",
        trim="first",
    )
    sub.add_argument("--wg", required=True)
    for name, allow, text in (("allow", True, "add repositories"), ("disallow", False, "remove repositories")):
        example, words = REPO_DOCS[name]
        sub = tree.command(group, name, _change(allow), text, writes=True, example=example, words=words)
        sub.add_argument("--wg", required=True)
        sub.add_argument("--repo", action="append", default=[], help="owner/name, repeatable")
    group = tree.group("wg", "workgroups")
    sub = tree.command(
        group,
        "settings",
        _settings,
        "show or set the workgroup settings",
        writes=True,
        example="agent-platform wg settings --wg <wg_id> [--protected-branch main] [--pr-draft-default true]",
        words="geschützt zweig entwurf einstellungen arbeitsgruppe protected branch draft configure",
    )
    sub.add_argument("--wg", required=True)
    sub.add_argument("--protected-branch", action="append", help="branch or pattern, repeatable (replaces the list)")
    sub.add_argument("--pr-draft-default", choices=["true", "false"])
    sub.add_argument("--merge-verdicts", type=int, help="passed verdicts of review sessions a merge needs (0: none)")
    sub.add_argument("--merge-verdict-profile", help="the profile those sessions must have (default review)")
    sub.add_argument("--merge-checks-green", choices=["true", "false"])
    sub.add_argument("--merge-approvals", type=int, help="approving GitHub reviews a merge needs (0: none)")
    sub.add_argument("--separate-duties", choices=["true", "false"])
    sub = tree.command(
        group,
        "rename",
        _rename,
        "rename a workgroup (admins)",
        writes=True,
        example='agent-platform wg rename --wg <wg_id> --name "Research"',
        words="umbenennen arbeitsgruppe name",
    )
    sub.add_argument("--wg", required=True)
    sub.add_argument("--name", required=True)
