"""Process analysis of the agent tool (ADR-097 sections 1, 5, 6 and 7; twin of the web client's `core/analysis/`).

- `optimize (--org ORG | --wg WG) [--in WG] [--days N | --since T] [--until T] [--language en|de]` creates the
  analysis task with `POST /v1/tasks` as `task create` does (`tasks.task_create`), with the fixed title and prompt of
  `contracts/tasks/analysis.json` for the language (default `en`) and the plaintext field `analysis {scope, since,
  until}`. Scope `org` (`--org`) puts the task into the organization's default workgroup or into `--in WG` of that
  organization; scope `wg` (`--wg`) into WG itself. The period is `--since`/`--until` (ISO-8601 with a time zone) or
  the `--days` (1-92, default 7) before `--until` (default now); both times go to the hub and into the prompt as
  ISO-8601 UTC with milliseconds, so the session reads the same period the hub keeps. The period must be ordered and
  at most 92 days long (checked here before anything is sealed); that it ends at most one minute after now is the
  hub's rule against its own clock (`400 request_invalid`), not checked against this computer's clock.
  The answer is the one of `task create` plus `analysis`, and for scope `org` a `notice`: the report is visible to
  every member of the task's workgroup and may hold totals of every workgroup the user can read (section 7).
- `optimize adopt --task T [--level workgroup|org] [--notify]` writes the newest output of an analysis task (a task
  row with `analysis`; any other task is refused with `not_analysis`) as a whiteboard entry of the task's workgroup
  (default) or of the organization, sealed and signed as the user on the existing whiteboard path
  (`whiteboards.board_add`); the write right of the board is checked before the chat is read. No output yet is
  `no_output`. For the organization
  level the answer carries the visibility notice.

The hub's refusals pass through as everywhere in the tool (`409 peer_outdated` with the devices that must update).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_platform.account import Account
from agent_platform.commands import Context, Parser, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import uuid_argument
from agent_platform.orgs import find_org
from agent_platform.tasks import task_answer, task_create, task_show
from agent_platform.whiteboards import board_add, locate, require_writer

LANGUAGES = ("en", "de")
# Per language the title and the prompt (`contracts/tasks/analysis.json` `templates`) and the scope texts (`scopes`).
TEMPLATES = {
    "en": (
        "Optimize processes",
        "Optimize processes: analyse {scope} from {since} to {until} and propose improvements.",
    ),
    "de": (
        "Prozesse optimieren",
        "Prozesse optimieren: Analysiere {scope} von {since} bis {until} und schlage Verbesserungen vor.",
    ),
}
SCOPES = {
    "en": {"org": "the organization", "wg": "workgroup {name}"},
    "de": {"org": "die Organisation", "wg": "die Workgroup {name}"},
}
DAYS_DEFAULT = 7
DAYS_MAX = 92
LEVELS = ("workgroup", "org")
NOTICE_WG = (
    "The report is visible to all members of the workgroup {name} and may contain totals of all workgroups you can "
    "read."
)
NOTICE_ORG = (
    "The report is visible to all members of the organization and may contain totals of all workgroups you can read."
)


def now() -> datetime:
    return datetime.now(UTC)


def _ms(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def iso(moment: datetime) -> str:
    """ISO-8601 UTC with milliseconds (`2026-11-26T10:00:00.000Z`)."""
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_time(text: str) -> datetime:
    """An ISO-8601 time with a time zone, to the millisecond (`invalid_argument` otherwise)."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise PlatformError("invalid_argument", reason="time_invalid") from None
    if moment.tzinfo is None:
        raise PlatformError("invalid_argument", reason="time_invalid")
    return _ms(moment)


def period(days: int | None, since: str | None, until: str | None) -> tuple[str, str]:
    """`(since, until)` of the options (module docstring)."""
    if days is not None and (since is not None or not 1 <= days <= DAYS_MAX):
        raise PlatformError("invalid_argument", reason="period_invalid")
    current = _ms(now())
    end = current if until is None else parse_time(until)
    start = end - timedelta(days=days or DAYS_DEFAULT) if since is None else parse_time(since)
    if not start < end or end - start > timedelta(days=DAYS_MAX):
        raise PlatformError("invalid_argument", reason="period_invalid")
    return iso(start), iso(end)


def brief(language: str, scope: str, name: str | None, since: str, until: str) -> tuple[str, str]:
    """The fixed title and prompt of an analysis (`contracts/tasks/analysis.json`)."""
    title, prompt = TEMPLATES[language]
    where = SCOPES[language][scope].format(name=name)
    return title, prompt.format(scope=where, since=since, until=until)


def optimize(
    account: Account,
    *,
    org: str | None,
    wg: str | None,
    inside: str | None = None,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Creates the analysis task (module docstring)."""
    if (org is None) == (wg is None) or (wg is not None and inside is not None) or language not in LANGUAGES:
        raise PlatformError("invalid_argument", reason="org_or_wg")
    start, end = period(days, since, until)
    if org is not None:
        state = account.org_log(uuid_argument(org)).state
        target = state.default_wg if inside is None else uuid_argument(inside)
        if target not in state.workgroups:
            raise PlatformError("invalid_argument", reason="wg_not_in_org")
        scope, home = "org", org
    else:
        target = uuid_argument(cast(str, wg))
        scope, home = "wg", find_org(account, target)
    name = account.names(home).workgroups.get(target) or target
    title, prompt = brief(language, scope, name, start, end)
    analysis = {"scope": scope, "since": start, "until": end}
    created = {**task_create(account, target, prompt, title=title, analysis=analysis), "analysis": analysis}
    return {**created, "notice": NOTICE_WG.format(name=name)} if scope == "org" else created


def _newest_output(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The newest opened `session.output` with text (`no_output` without one)."""
    found = [event for event in events if event.get("kind") == "session.output" and _has_text(event.get("body"))]
    if not found:
        raise PlatformError("invalid_argument", reason="no_output")
    return found[-1]


def _has_text(body: object) -> bool:
    text = body.get("text") if isinstance(body, dict) else None
    return isinstance(text, str) and bool(text.strip())


def adopt(account: Account, task: str, level: str = "workgroup", notify: bool = False) -> dict[str, Any]:
    """Writes the newest output of the analysis task `task` onto the board of its workgroup or organization."""
    if level not in LEVELS:
        raise PlatformError("invalid_argument")
    row = task_answer(account, uuid_argument(task))["task"]
    if not isinstance(row.get("analysis"), dict):
        raise PlatformError("invalid_argument", reason="not_analysis")
    wg = str(row["wg_id"])
    location = locate(account, find_org(account, wg), wg if level == "workgroup" else None, None)
    require_writer(account, location)
    output = _newest_output(task_show(account, task)["events"])
    added = board_add(account, location, output["body"]["text"], notify)
    result = {**added, "analysis_task_id": task, "output_event_id": output["id"]}
    return {**result, "notice": NOTICE_ORG} if level == "org" else result


# --- command line ---


def _start(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return optimize(
        account,
        org=args.org,
        wg=args.wg,
        inside=args.inside,
        days=args.days,
        since=args.since,
        until=args.until,
        language=args.language,
    )


def _adopt(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return adopt(account, args.task, args.level, args.notify)


def register(tree: Tree) -> None:
    sub = tree.command(
        tree.commands,
        "optimize",
        _start,
        "start a process analysis: a read-only task that proposes improvements (ADR-097)",
        writes=True,
        example="agent-platform optimize --org <org_id> [--in <wg_id>] [--days 7] [--language en|de]",
        words="prozesse optimieren prozessanalyse auswertung verbesserungsvorschläge optimize processes improvements",
        then="agent-platform task wait --task <task_id>",
    )
    sub.add_argument("--org", help="scope org: analyse the organization (task in its default workgroup)")
    sub.add_argument("--wg", help="scope wg: analyse this workgroup (task in it)")
    sub.add_argument("--in", dest="inside", help="with --org: the workgroup of the task (default the default one)")
    sub.add_argument("--days", type=int, help=f"the days before --until (1-{DAYS_MAX}, default {DAYS_DEFAULT})")
    sub.add_argument("--since", help="the start, ISO-8601 with a time zone (instead of --days)")
    sub.add_argument("--until", help="the end, ISO-8601 with a time zone (default now)")
    sub.add_argument("--language", choices=LANGUAGES, default="en", help="the language of the prompt and report")
    adopting = sub.add_subparsers(dest="optimize_command", parser_class=Parser, metavar="adopt")
    sub = tree.command(
        adopting,
        "adopt",
        _adopt,
        "write the newest output of an analysis task onto the whiteboard of its workgroup or organization",
        writes=True,
        example="agent-platform optimize adopt --task <task_id> [--level workgroup|org] [--notify]",
        words="übernehmen analysebericht bericht-übernehmen adopt report analysis",
        then="agent-platform whiteboard list --org <org_id> --wg <wg_id>",
    )
    sub.add_argument("--task", required=True)
    sub.add_argument("--level", choices=LEVELS, default="workgroup")
    sub.add_argument("--notify", action="store_true", help="notify the board's readers")
