"""Pull requests linked to tasks (KOR-48, ADR-084 sections 1-3 and 6, 11-agent-access.md section 8.4; twin of
`web/src/core/pulls/`): `pr list`, `pr show`, `pr link`, `pr unlink` and `pr fix-conflicts`.

A pull request is named by its address (`https://github.com/<owner>/<name>/pull/<n>`, also with `www.`, a sub-page,
a query or a fragment) or as `<owner>/<name>#<n>`, with the limits of `web/src/core/pulls/address.ts` (owner, name,
a number from 1 to 2^31-1; `invalid_argument` with `reason: pull_request` otherwise).

The hub cannot read the index (04-crypto.md section 4.11a): a link is the opaque tag `pr_link_tag(WK_e, repo, number)`
of a workgroup epoch, the task, the session that created the pull request or the person who linked it, and `sealed`,
the pull request's repository and number sealed under the same workgroup key and bound to all of these
(`crypto.context.open_pr_link`, which also recomputes the tag). The tool opens every row with the user's own key of
the row's epoch: a row whose key the user does not hold is `locked`, one without `sealed` (registered by an older
runner) has its details `unavailable`, and one that does not open, names another pull request than its tag or does not
fit the row shape is left out and counted (`unverified`).

- `pr list --wg|--task|--org` pages through `GET /v1/workgroups/{wg}/pr-links` (newest first, `cursor`), for `--org`
  in every workgroup of the org the user can read, and adds each task's verified title.
- `pr show`, `pr unlink` and `pr fix-conflicts` find a pull request by looking its tag up under every epoch the user
  holds of every readable workgroup (`--wg`: of that one), `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`.
- `pr link --task` looks the tag up in the task's workgroup first: a pull request linked already is reported with its
  task (`created: false`) and never moved; otherwise `POST /v1/workgroups/{wg}/pr-links` under the workgroup's current
  epoch (refused while its rotation is owed, `rotation_pending`) with `origin = user:<the user>`.
- `pr unlink` deletes every link a person made (`DELETE .../{epoch}/{tag}`); a link the device registered is a fact of
  the task and stays (`kept`).
- The fresh state comes from a device (`github.pull_status`, `pullstate`): one request at a time to a device that
  serves the workgroup (the task's device first, then online ones), at most `PULLS_MAX` pull requests per request.
  A pull request whose `mergeable` GitHub has not computed yet (`checking`) is asked once more after `RECHECK_S`
  seconds. `pr list` and `pr show` report a failed read as `status_error` (a failure code of the device request, or
  `no_device`) instead of failing; `--no-status` skips the read.
- `pr fix-conflicts` sends the fixed platform instruction (`instruction`, version `INSTRUCTION_VERSION`; the same
  text as `web/src/core/pulls/instruction.ts`) as an ordinary prompt to the linked task (`tasks.task_prompt`), so the
  task's own session resolves the conflicts with a merge commit. It needs a conflict (`pullstate.fixable`;
  `pr_no_conflict` with the `reason` otherwise) unless `--anyway`, and a linked task (`pr_not_linked`) unless
  `--task` names one; a pull request linked to several tasks needs `--task`.
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from agent_platform import tasks
from agent_platform.account import Account, KeyRing, unverified
from agent_platform.commands import Context, Tree
from agent_platform.devicereq import Keys, device_keys, submit, wait
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_platform.orgs import find_org, seal_epoch
from agent_platform.pullstate import PULLS_MAX, fixable, is_fork, merge_state
from agent_runner.crypto import context as crypto
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import WG

KIND = "github.pull_status"
INSTRUCTION_VERSION = 1
INSTRUCTION = "\n".join(
    (
        'Platform instruction "fix merge conflicts" (v1) for pull request {repo}#{number}:',
        '1. Call github.merge_base with {"repo": "{repo}", "number": {number}}.',
        "2. If the result says up_to_date, say so and stop.",
        "3. Resolve every listed conflict in the workspace directory of the repository, keeping the intent of both"
        " sides; remove every conflict marker; record a decision for every conflict without markers.",
        "4. If the repository has tests, run them with test.run on the resolved workspace and fix what the merge broke"
        " before you commit.",
        "5. Call github.commit with merge: true on the pull request's branch, then github.push of that commit to the"
        " same branch.",
        "6. Never merge, close or force-push the pull request.",
        "7. If a resolution needs a decision of a person, ask with human.question and wait for the answer.",
    )
)
"""The platform instruction v1; `{repo}` and `{number}` are replaced (`instruction`)."""
PAGE = 200
RECHECK_S = 10.0
WAIT_DEFAULT = 30
DONE = frozenset({"done", "failed"})
STATUS_ERRORS = ("no_device", "device_request_failed")
"""Error codes of a status read that `pr list` and `pr show` report as `status_error`."""
OWNER, NAME, NUMBER = r"[A-Za-z0-9-]{1,39}", r"[A-Za-z0-9._-]{1,100}", r"([1-9][0-9]{0,9})"
URL = re.compile(rf"https://(?:www\.)?github\.com/({OWNER})/({NAME})/pull/{NUMBER}(?:[/?#]\S*)?", re.IGNORECASE)
SHORT = re.compile(rf"({OWNER})/({NAME})#{NUMBER}")
"""The two forms of `web/src/core/pulls/address.ts`."""
TAG = re.compile(r"[A-Za-z0-9_-]{43}")
NUMBER_MAX = 2**31 - 1

Pull = tuple[str, int]
"""`(repo, number)` with the repository as given or as GitHub spells it."""


def instruction(repo: str, number: int) -> str:
    """The platform instruction "fix merge conflicts" v1 for `<owner>/<name>#<number>`."""
    return INSTRUCTION.replace("{repo}", repo).replace("{number}", str(number))


def parse_ref(text: str) -> Pull:
    """`(repo, number)` of a pull-request address or `owner/name#n` (module docstring)."""
    value = text.strip()
    found = URL.fullmatch(value) or SHORT.fullmatch(value)
    if found is None:
        raise PlatformError("invalid_argument", reason="pull_request")
    repo, number = f"{found.group(1)}/{found.group(2)}", int(found.group(3))
    if number > NUMBER_MAX or found.group(2) in (".", ".."):
        raise PlatformError("invalid_argument", reason="pull_request")
    return repo, number


def key_of(repo: str, number: int) -> Pull:
    """The comparison key of a pull request (GitHub names are case-insensitive)."""
    return repo.lower(), number


# --- links ---


def _origin(row: dict[str, Any]) -> str | None:
    """`device:<session>` or `user:<linked_by>` of a row with exactly one of them, else None."""
    session, person = row.get("session_id"), row.get("linked_by")
    if isinstance(session, str) and is_uuid7(session) and person is None:
        return f"device:{session}"
    if isinstance(person, str) and is_uuid7(person) and session is None:
        return f"user:{person}"
    return None


def open_row(
    org: str, wg: str, epoch: object, tag: object, row: dict[str, Any], *, ring: KeyRing
) -> dict[str, Any] | None:
    """The link item of a hub row of workgroup `wg` (module docstring), None when it does not verify."""
    task, origin, sealed = row.get("task_id"), _origin(row), row.get("sealed")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 1
        or not isinstance(tag, str)
        or TAG.fullmatch(tag) is None
        or not isinstance(task, str)
        or not is_uuid7(task)
        or origin is None
        or not (sealed is None or isinstance(sealed, str))
    ):
        return None
    item: dict[str, Any] = {
        "org_id": org,
        "wg_id": wg,
        "epoch": epoch,
        "tag": tag,
        "task_id": task,
        "session_id": row.get("session_id"),
        "linked_by": row.get("linked_by"),
        "origin": "device" if origin.startswith("device:") else "person",
        "created_at": row.get("created_at"),
        "repo": None,
        "number": None,
    }
    key = ring.get((WG, wg, epoch))
    if key is None:
        return {**item, "details": "locked"}
    if sealed is None:
        return {**item, "details": "unavailable"}
    try:
        repo, number = crypto.open_pr_link(
            crypto.pr_link_body_key(key),
            crypto.pr_link_key(key),
            wg_id=wg,
            epoch=epoch,
            tag=tag,
            task_id=task,
            origin=origin,
            sealed=sealed,
        )
    except CryptoError:
        return None
    return {**item, "repo": repo, "number": number, "details": "available"}


def _pages(account: Account, wg: str, task: str | None) -> Iterator[dict[str, Any]]:
    cursor: str | None = None
    while True:
        query = {"limit": PAGE, **({"task": task} if task else {}), **({"cursor": cursor} if cursor else {})}
        answer = account.call("GET", f"/v1/workgroups/{segment(wg)}/pr-links?{urlencode(query)}")
        yield from rows_of(answer, "items")
        following = answer.get("next_cursor")
        if not isinstance(following, str) or not following or following == cursor:
            return
        cursor = following


def _titled(account: Account, org: str, wg: str, items: list[dict[str, Any]]) -> None:
    if items:
        found = tasks.titles(account, org, wg)
        for item in items:
            item["title"] = found.get(item["task_id"])


def wg_links(account: Account, org: str, wg: str, task: str | None) -> tuple[list[dict[str, Any]], int]:
    """The opened links of a workgroup (of one task) and the number of rows left out."""
    ring = account.keys(org)
    items: list[dict[str, Any]] = []
    skipped = 0
    for row in _pages(account, wg, task):
        opened = open_row(org, wg, row.get("epoch"), row.get("tag"), row, ring=ring)
        if opened is None or (task is not None and opened["task_id"] != task):
            skipped += 1
            continue
        items.append(opened)
    _titled(account, org, wg, items)
    return items, skipped


def _readable(account: Account, org: str) -> list[str]:
    state = account.org_log(org).state
    return [wg for wg in sorted(state.workgroups) if state.can_read(account.user_id, WG, wg)]


def _places(account: Account, wg: str | None) -> list[tuple[str, str]]:
    """`(org, wg)` of every readable workgroup of the user, or of `wg` alone."""
    if wg is not None:
        return [(find_org(account, uuid_argument(wg)), wg)]
    orgs = [str(item.get("org_id")) for item in account.me().get("orgs", []) if isinstance(item, dict)]
    return [(org, found) for org in orgs for found in _readable(account, org)]


def lookup(account: Account, pull: Pull, wg: str | None = None) -> list[dict[str, Any]]:
    """Every link of the pull request under every epoch the user holds (of `wg`, else of every readable workgroup)."""
    repo, number = pull
    found: list[dict[str, Any]] = []
    for org, place in _places(account, wg):
        ring = account.keys(org)
        here: list[dict[str, Any]] = []
        for epoch in sorted(slot[2] for slot in ring if slot[:2] == (WG, place)):
            tag = crypto.pr_link_tag(crypto.pr_link_key(ring[(WG, place, epoch)]), repo, number)
            try:
                row = account.call("GET", f"/v1/workgroups/{segment(place)}/pr-links/{epoch}/{tag}")
            except PlatformError as error:
                if error.fields.get("hub_error") == "not_found":
                    continue
                raise
            opened = open_row(org, place, epoch, tag, row, ring=ring)
            if opened is not None:
                here.append({**opened, "repo": opened["repo"] or repo, "number": number})
        _titled(account, org, place, here)
        found.extend(here)
    return found


# --- the fresh state ---


def status_device(account: Account, wg: str, first: str | None) -> tuple[str, Keys]:
    """A device that serves the workgroup (`first`, the task's device, before the others; online before offline),
    with its keys from the verified logs; `no_device` without one."""
    rows = [
        row
        for row in rows_of(account.call("GET", "/v1/me/devices"), "devices")
        if isinstance(row.get("wg_ids"), list) and wg in row["wg_ids"] and isinstance(row.get("device_id"), str)
    ]
    rows.sort(key=lambda row: (row["device_id"] != first, row.get("online") is not True, row["device_id"]))
    for row in rows:
        keys = device_keys(account, row["device_id"])
        if keys is not None:
            return row["device_id"], keys
    raise PlatformError("no_device")


def _ask(
    account: Account, target: tuple[str, Keys], place: tuple[str, str, str | None], batch: list[Pull], seconds: int
) -> dict[Pull, dict[str, Any]]:
    org, wg, task = place
    body: dict[str, Any] = {"org_id": org, "wg_id": wg, "pulls": [{"repo": r, "number": n} for r, n in batch]}
    if task is not None:
        body["task_id"] = task
    prepared = submit(account, target, KIND, body)
    found = wait(account, prepared, seconds, DONE)
    if found["step"] != "done":
        failure = found.get("code", "timeout")
        reason = {"reason": failure} if failure == "github_not_connected" else {}
        raise PlatformError("device_request_failed", failure=failure, request_id=prepared["id"], **reason)
    answered = {key_of(item["repo"], item["number"]): item for item in found["pulls"]}
    if set(answered) != {key_of(*pull) for pull in batch}:
        raise PlatformError("device_request_failed", failure="response_invalid", request_id=prepared["id"])
    return answered


def read_states(
    account: Account, place: tuple[str, str, str | None], pulls: list[Pull], seconds: int, first: str | None = None
) -> dict[Pull, dict[str, Any]]:
    """The fresh state of every pull request by its `key_of`, read through one device of the workgroup
    `place = (org, wg, task or None)`; a `checking` one is asked once more after `RECHECK_S`."""
    wanted = list({key_of(*pull): pull for pull in pulls}.values())
    target = status_device(account, place[1], first)
    states: dict[Pull, dict[str, Any]] = {}
    for start in range(0, len(wanted), PULLS_MAX):
        states.update(_ask(account, target, place, wanted[start : start + PULLS_MAX], seconds))
    checking = [pull for pull in wanted if merge_state(states[key_of(*pull)]) == "checking"]
    if checking:
        time.sleep(RECHECK_S)
        for start in range(0, len(checking), PULLS_MAX):
            states.update(_ask(account, target, place, checking[start : start + PULLS_MAX], seconds))
    return states


def _status_error(error: PlatformError) -> str:
    if error.code not in STATUS_ERRORS:
        raise error
    return str(error.fields.get("failure", error.code))


def _with_states(
    account: Account, place: tuple[str, str, str | None], items: list[dict[str, Any]], seconds: int, first: str | None
) -> None:
    """Add `pull`, `merge_state` and `status_error` to the items that name a pull request."""
    named = [item for item in items if item["repo"] is not None]
    for item in items:
        item.update(pull=None, merge_state=None, status_error=None)
    if not named:
        return
    try:
        states = read_states(account, place, [(item["repo"], item["number"]) for item in named], seconds, first)
    except PlatformError as error:
        code = _status_error(error)
        for item in named:
            item["status_error"] = code
        return
    for item in named:
        pull = states[key_of(item["repo"], item["number"])]
        item.update(pull=pull, merge_state=merge_state(pull))


def _task_device(account: Account, task: str) -> tuple[dict[str, Any], str | None]:
    """The task row and the device of its main session (None without one)."""
    answer = tasks.task_answer(account, uuid_argument(task))
    session = tasks.main_session(answer)
    device = session.get("device_id") if session is not None else None
    return answer["task"], device if isinstance(device, str) else None


# --- commands ---


def pr_list(
    account: Account, where: tuple[str | None, str | None, str | None], status: bool, seconds: int
) -> dict[str, Any]:
    """The links of a workgroup, a task or an org (`where = (wg, task, org)`, exactly one set) with their state."""
    wg, task, org = where
    first: str | None = None
    if task is not None:
        row, first = _task_device(account, task)
        groups = [(find_org(account, str(row["wg_id"])), str(row["wg_id"]))]
    elif wg is not None:
        groups = [(find_org(account, uuid_argument(wg)), wg)]
    else:
        org = uuid_argument(str(org))
        if org not in {str(item.get("org_id")) for item in account.me().get("orgs", []) if isinstance(item, dict)}:
            raise PlatformError("no_org")
        groups = [(org, found) for found in _readable(account, org)]
    links: list[dict[str, Any]] = []
    skipped = 0
    for place_org, place_wg in groups:
        items, left_out = wg_links(account, place_org, place_wg, task)
        if status:
            _with_states(account, (place_org, place_wg, task), items, seconds, first)
        links.extend(items)
        skipped += left_out
    return {"links": links, "unverified": skipped}


def pr_show(account: Account, ref: str, wg: str | None, status: bool, seconds: int) -> dict[str, Any]:
    """A pull request: its links (task, session, who linked it) and its fresh state."""
    repo, number = parse_ref(ref)
    links = lookup(account, (repo, number), wg)
    result: dict[str, Any] = {"repo": repo, "number": number, "links": links}
    shown: dict[str, Any] = {"repo": links[0]["repo"] if links else repo, "number": number}
    if not status:
        return result
    if links:
        task = str(links[0]["task_id"])
        _row, first = _task_device(account, task)
        place: tuple[str, str, str | None] = (str(links[0]["org_id"]), str(links[0]["wg_id"]), task)
    elif wg is not None:
        place, first = (find_org(account, wg), wg, None), None
    else:
        return {**result, "pull": None, "merge_state": None, "fixable": False, "status_error": "no_workgroup"}
    _with_states(account, place, [shown], seconds, first)
    pull = shown["pull"]
    return {
        **result,
        "pull": pull,
        "merge_state": shown["merge_state"],
        "fixable": pull is not None and fixable(pull),
        "status_error": shown["status_error"],
    }


def pr_link(account: Account, ref: str, task: str) -> dict[str, Any]:
    """Link the pull request to the task under the workgroup's current epoch, or report the link it has."""
    repo, number = parse_ref(ref)
    row, _first = _task_device(account, task)
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    existing = lookup(account, (repo, number), wg)
    if existing:
        return {"created": False, "link": existing[0]}
    epoch = seal_epoch(account.org_log(org).state, WG, wg)
    ring = account.keys(org)
    key = ring.get((WG, wg, epoch))
    if key is None:
        raise PlatformError("hub_refused", reason="epoch_stale")
    tag = crypto.pr_link_tag(crypto.pr_link_key(key), repo, number)
    sealed = crypto.seal_pr_link(
        crypto.pr_link_body_key(key),
        wg_id=wg,
        epoch=epoch,
        tag=tag,
        task_id=task,
        origin=f"user:{account.user_id}",
        repo=repo,
        number=number,
    )
    body = {"epoch": epoch, "tag": tag, "sealed": sealed, "task_id": task}
    answer = account.call("POST", f"/v1/workgroups/{segment(wg)}/pr-links", body)
    link = answer.get("link")
    opened = open_row(org, wg, epoch, tag, link, ring=ring) if isinstance(link, dict) else None
    if opened is None or key_of(str(opened["repo"]), number) != key_of(repo, number):
        raise unverified("response_invalid")
    _titled(account, org, wg, [opened])
    return {"created": answer.get("created") is True, "link": opened}


def pr_unlink(account: Account, ref: str, wg: str | None) -> dict[str, Any]:
    """Remove every link of the pull request a person made; a device's link stays."""
    pull = parse_ref(ref)
    links = lookup(account, pull, wg)
    if not links:
        raise PlatformError("pr_not_linked")
    removed: list[dict[str, Any]] = []
    for link in links:
        if link["origin"] == "person":
            target = f"/v1/workgroups/{segment(link['wg_id'])}/pr-links/{link['epoch']}/{segment(link['tag'])}"
            account.call("DELETE", target)
            removed.append(link)
    kept = [link for link in links if link["origin"] == "device"]
    return {"repo": pull[0], "number": pull[1], "unlinked": removed, "kept": kept}


def _chosen_task(links: list[dict[str, Any]], task: str | None) -> str:
    linked = sorted({str(link["task_id"]) for link in links})
    if task is not None:
        if linked and task not in linked:
            raise PlatformError("invalid_argument", reason="linked_elsewhere", tasks=linked)
        return task
    if not linked:
        raise PlatformError("pr_not_linked")
    if len(linked) > 1:
        raise PlatformError("invalid_argument", reason="ambiguous", tasks=linked)
    return linked[0]


def fix_conflicts(
    account: Account, ref: str, task: str | None, wg: str | None, *, anyway: bool, seconds: int
) -> dict[str, Any]:
    """Send the platform instruction to the task the pull request is linked to (module docstring)."""
    repo, number = parse_ref(ref)
    links = lookup(account, (repo, number), wg)
    chosen = _chosen_task(links, None if task is None else uuid_argument(task))
    mine = [link for link in links if link["task_id"] == chosen]
    shown = str(mine[0]["repo"]) if mine else repo
    row, first = _task_device(account, chosen)
    task_wg = str(row["wg_id"])
    pull: dict[str, Any] | None = None
    if not anyway:
        place = (find_org(account, task_wg), task_wg, chosen)
        pull = read_states(account, place, [(shown, number)], seconds, first)[key_of(shown, number)]
        if not fixable(pull):
            found = merge_state(pull) or pull["code"]
            raise PlatformError("pr_no_conflict", reason="fork" if found == "conflict" and is_fork(pull) else found)
        shown = str(pull["repo"])
    text = instruction(shown, number)
    sent = tasks.task_prompt(account, chosen, text)
    return {
        **sent,
        "repo": shown,
        "number": number,
        "instruction_version": INSTRUCTION_VERSION,
        "instruction": text,
        "pull": pull,
        "merge_state": None if pull is None else merge_state(pull),
    }


def _seconds(args: argparse.Namespace) -> int:
    seconds = int(args.wait)
    if not 0 <= seconds <= 600:
        raise PlatformError("invalid_argument")
    return seconds


def _list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return pr_list(account, (args.wg, args.task, args.org), not args.no_status, _seconds(args))


def _show(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return pr_show(account, args.pull, args.wg, not args.no_status, _seconds(args))


def _link(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return pr_link(account, args.pull, args.task)


def _unlink(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return pr_unlink(account, args.pull, args.wg)


def _fix(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return fix_conflicts(account, args.pull, args.task, args.wg, anyway=args.anyway, seconds=_seconds(args))


PULL_HELP = "the pull request: its address (https://github.com/<owner>/<name>/pull/<n>) or <owner>/<name>#<n>"
WAIT_HELP = "seconds to wait for the device's answer (0-600, default 30)"


def register(tree: Tree) -> None:
    group = tree.group("pr", "pull requests linked to tasks (the index is sealed; the state is read by a device)")
    sub = tree.command(
        group,
        "list",
        _list,
        "the pull requests linked to the tasks of a workgroup, a task or an org, with their fresh state",
        example="agent-platform pr list --wg <wg_id>",
        words="pullrequests pull requests liste konflikte zusammenführen status prs conflicts mergeable overview",
        then="agent-platform pr fix-conflicts <owner>/<name>#<n>",
        trim="first",
    )
    where = sub.add_mutually_exclusive_group(required=True)
    where.add_argument("--wg", help="the links of a workgroup")
    where.add_argument("--task", help="the links of one task")
    where.add_argument("--org", help="the links of every readable workgroup of an org")
    sub.add_argument("--no-status", action="store_true", help="without reading the state through a device")
    sub.add_argument("--wait", type=int, default=WAIT_DEFAULT, help=WAIT_HELP)
    sub = tree.command(
        group,
        "show",
        _show,
        "a pull request: the task and session it belongs to and its fresh state (conflict, checking, clean)",
        example="agent-platform pr show https://github.com/<owner>/<name>/pull/<n>",
        words="pullrequest pull request ansehen aufgabe sitzung konflikt prüfen pr view task session conflict check",
        then="agent-platform pr fix-conflicts <owner>/<name>#<n>",
    )
    sub.add_argument("pull", help=PULL_HELP)
    sub.add_argument("--wg", help="look only in this workgroup (and read the state there when it is not linked)")
    sub.add_argument("--no-status", action="store_true", help="without reading the state through a device")
    sub.add_argument("--wait", type=int, default=WAIT_DEFAULT, help=WAIT_HELP)
    sub = tree.command(
        group,
        "link",
        _link,
        "link a pull request to a task by its address (an existing link is reported, never moved)",
        writes=True,
        example="agent-platform pr link https://github.com/<owner>/<name>/pull/<n> --task <task_id>",
        words="verknüpfen zuordnen pullrequest adresse einfügen aufgabe pr link attach paste address task",
        then="agent-platform pr show <owner>/<name>#<n>",
    )
    sub.add_argument("pull", help=PULL_HELP)
    sub.add_argument("--task", required=True, help="the task the pull request belongs to")
    sub = tree.command(
        group,
        "unlink",
        _unlink,
        "remove the links a person made to a pull request (a device's link stays)",
        writes=True,
        example="agent-platform pr unlink https://github.com/<owner>/<name>/pull/<n>",
        words="verknüpfung entfernen lösen pullrequest pr unlink detach remove link",
    )
    sub.add_argument("pull", help=PULL_HELP)
    sub.add_argument("--wg", help="only the links in this workgroup")
    sub = tree.command(
        group,
        "fix-conflicts",
        _fix,
        "let the task's session resolve the pull request's merge conflicts with a merge commit (never a merge)",
        writes=True,
        example="agent-platform pr fix-conflicts https://github.com/<owner>/<name>/pull/<n>",
        words="konflikte lösen beheben zusammenführen basis mergen pullrequest fix conflicts resolve merge base update",
        then="agent-platform task follow --task <task_id>",
    )
    sub.add_argument("pull", help=PULL_HELP)
    sub.add_argument("--task", help="the task to send it to (needed when the pull request is not linked)")
    sub.add_argument("--wg", help="look only in this workgroup")
    sub.add_argument("--anyway", action="store_true", help="send it without reading the state (no conflict check)")
    sub.add_argument("--wait", type=int, default=WAIT_DEFAULT, help=WAIT_HELP)
