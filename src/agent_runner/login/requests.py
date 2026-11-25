"""Device-request handlers of the login area (`<area>/requests.py:HANDLERS`, ADR-027 section 4): `claude.login`,
`credential.check`, `mailbox.check` (05-runner.md section 11, ADR-011, `contracts/requests/`).

The request service (RUN-09) verifies, decrypts and routes the requests, keeps at most one flow per user and kind
(a newer request replaces the running one, which is cancelled), ends a flow after the request's 15 minutes and seals
the responses. The handlers here never log or answer a secret: no token, code, URL, password or credential body.

`claude.login` (root body `{mode: "manual"|"automatic"}`, follow-up `{code}`; KOR-69, ADR-090 section 2: the root body
may add `subscription_id` - the further subscription the login fills, a new ID creates it -, `name` and `note` (taken
for the record; an existing one keeps its own unless given) and, for `automatic`, `email` - the address to sign in with
instead of the mailbox's own; a malformed one of them is `upstream_error`):

With `subscription_id` the login is the full login (`SUBSCRIPTION_LOGIN_ARGV`, section 11.6, ADR-091, KOR-71): the
command gets `--email <address>` when an address is known (the automatic login's, else the one the record holds), the
record stores `{token, refresh_token, expires_at, scopes, subscription_type?, rate_limit_tier?, email,
kind:"oauth_login"}`, and a login without `user:profile` and `user:inference` ends `scope_missing`.

- `manual` (setup-token relay, section 11.2): start the login command (`relay.LoginSession`), response 1
  `{step:"authorize", url}`, wait for the follow-up with the code until shortly before the request expires, type it,
  read the token, store the `claude_subscription` credential `{token, email, kind:"setup_token"}` under the owner's
  `CK` (`credentials.cache.put`, the primary record; with `subscription_id` `credentials.subscriptions.store_login`,
  which keeps the record's name, note and use; a read or write lost in transport is sent again after `RESPOND_DELAYS`,
  KOR-18, so a completed login is not discarded for one lost request), final `{step:"done"}` to the follow-up.
- `automatic` (section 11.3): needs `LOGIN_AUTOMATION=on` (config default per device kind) and the owner's `mailbox`
  credential (`credential_missing` without one). Progress responses, then the code is typed without a follow-up and
  the final `{step:"done", email}` names the mailbox address the subscription signed in with. When the automation is
  switched off, the extra is missing or a page is not as expected, the flow falls back to the relay with
  `{step:"authorize", url, automation:"failed"}` (the web shows `login_automation_failed`).

Failures end with `{step:"failed", code}`: `timeout`, `login_cli_failed`, `code_invalid`, `token_invalid`,
`credential_missing`, `upstream_error`, `upstream_unreachable` (`contracts/requests/objects.json`); a malformed root or
follow-up body is `upstream_error` resp. `code_invalid`. The temporary directory is removed on every path. A failure
of the login program logs the form of its screen before the code (`runner.login screen rows=… marker=yes|no
token_blocks=… prompt=yes|no error=yes|no`, KOR-21), never its text.

`credential.check` (`{kind: "anthropic_api_key"|"claude_subscription"}`): `POST <MODEL_UPSTREAM_ANTHROPIC>/v1/messages/
count_tokens` with the credential (`x-api-key`, resp. `authorization: Bearer` with `anthropic-beta: oauth-2025-04-20`)
-> `{step:"done", valid}` (2xx valid, 401/403 not valid); other answers `upstream_error`, network errors
`upstream_unreachable`, no credential `credential_missing`. `/api/hello` carries no credential check, so both kinds
use `count_tokens`.

`mailbox.check` (`{}`): IMAPS login with the owner's `mailbox` credential and `LIST` -> `{step:"done", valid}` (a
refused login is not valid); a connection or TLS failure `upstream_unreachable`, a failed `LIST` `upstream_error`.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import re
import ssl
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from agent_cli.account_login import Mailbox
from agent_runner.credentials import subscriptions
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused, opened, put
from agent_runner.errors import RunnerError
from agent_runner.login import automation
from agent_runner.login.automation import CODE_RE, EMAIL_RE, MAX_EMAIL, AutomationFailed, mailbox_fields
from agent_runner.login.full_login import FullLogin
from agent_runner.login.relay import LoginFailed, LoginOptions, LoginSession, options
from agent_runner.model.selection import load_catalog
from agent_runner.requests.service import RESPOND_DELAYS, Flow, FollowUp
from agent_runner.sync.http import HubRefusal, HubTransportError

MODES = ("manual", "automatic")
LOGIN_FIELDS = frozenset({"mode", "subscription_id", "name", "note", "email"})
LABEL_LIMITS = {"name": 64, "note": 500}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CHECK_KINDS = {"anthropic_api_key": ("api_key", "key"), "claude_subscription": ("subscription", "token")}
CHECK_MODEL = "claude-haiku-4-5"
"""Model of the `count_tokens` probe when the catalog names no default for the access kind."""
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
HTTP_TIMEOUT = 30.0
IMAP_TIMEOUT = 30.0


class Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _line(flow: Flow, text: str) -> None:
    flow.context.log.write(f"runner.login {text} request_id={flow.id}\n")
    flow.context.log.flush()


async def _fail(flow: Flow, code: str, to: FollowUp | None = None, *, shape: str | None = None) -> None:
    if shape is not None:
        _line(flow, f"screen {shape}")  # the form of the login program's screen only (KOR-21), never its text
    _line(flow, f"failed code={code}")
    await flow.respond({"step": "failed", "code": code}, final=True, to=to)


# --- claude.login --------------------------------------------------------------------------------------------------


async def _mailbox(flow: Flow) -> tuple[Mailbox, str]:
    try:
        async with opened(flow.context, flow.user, "mailbox") as body:
            found = mailbox_fields(body)
    except CredentialUnavailable:
        found = None
    if found is None:
        raise Failed("credential_missing")
    return found


async def _automate(flow: Flow, url: str, plan: tuple[Mailbox, str], opts: LoginOptions, workdir: Path) -> str | None:
    """The code from the automation, None after a fall-back (logged)."""
    mailbox, email = plan

    async def progress(stage: str) -> None:
        await flow.respond({"step": "progress", "stage": stage})

    try:
        return await automation.authorize(
            url,
            mailbox,
            email,
            opts=opts,
            progress=progress,
            budget=flow.remaining() - opts.code_margin,
            clock=time.time,
            profile_dir=workdir / "browser",
        )
    except AutomationFailed as failed:
        _line(flow, f"automation_failed reason={failed.reason}")
        return None


def _label(value: object, most: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= most and all(ord(char) >= 0x20 for char in value)


def _target(body: Mapping[str, object]) -> dict[str, object]:
    """The subscription fields of a root body (`Failed("upstream_error")` when one is malformed): `subscription_id`,
    `name`, `note` and `email` as given."""
    if not set(body) <= LOGIN_FIELDS or body.get("mode") not in MODES:
        raise Failed("upstream_error")
    found = {name: body[name] for name in ("subscription_id", "name", "note", "email") if name in body}
    record_id = found.get("subscription_id")
    if record_id is not None and not (isinstance(record_id, str) and UUID_RE.fullmatch(record_id)):
        raise Failed("upstream_error")
    if any(name in found and not _label(found[name], most) for name, most in LABEL_LIMITS.items()):
        raise Failed("upstream_error")
    email = found.get("email")
    if email is not None and not (
        body["mode"] == "automatic" and isinstance(email, str) and len(email) <= MAX_EMAIL and EMAIL_RE.fullmatch(email)
    ):
        raise Failed("upstream_error")
    return found


async def _plan(flow: Flow, opts: LoginOptions) -> tuple[Mailbox, str] | None:
    """Mailbox and address for an `automatic` login, None when the automation is off or the extra is missing (the
    relay is used then); `Failed("credential_missing")` without a usable `mailbox` credential."""
    try:
        if not flow.context.config.login_automation:
            raise AutomationFailed("disabled")
        automation.load_playwright(opts)
        automation.use_browsers(flow.context.config.home)
    except AutomationFailed as failed:
        _line(flow, f"automation_failed reason={failed.reason}")
        return None
    return await _mailbox(flow)


async def _known_email(flow: Flow, record_id: str) -> str | None:
    """The address the further subscription already holds (the `--email` of a new login), None when there is none
    or the record cannot be read."""
    try:
        found = await subscriptions.fetch(flow.context, record_id)
        if found is None or found.primary or found.owner != flow.user:
            return None
        async with subscriptions.opened(flow.context, found) as body:
            email = body.get("email")
    except (CredentialUnavailable, RecordRefused, HubRefusal, HubTransportError):
        return None
    return email if isinstance(email, str) and EMAIL_RE.fullmatch(email) else None


async def _store(flow: Flow, login: str | FullLogin, email: str | None, target: Mapping[str, object]) -> None:
    if isinstance(login, FullLogin):
        body = login.body()  # the address the program reports, unless the automation signed in with its own
        if email is not None:
            body["email"] = email
    else:
        body = {"token": login, "email": email, "kind": "setup_token"}
    record_id = target.get("subscription_id")
    try:
        if isinstance(record_id, str):
            fields = {**body, **{name: target[name] for name in ("name", "note") if name in target}}
            try:
                await subscriptions.store_login(
                    flow.context, flow.user, record_id, fields, flow.id, delays=RESPOND_DELAYS, sleep=flow.sleep
                )
            finally:
                fields.clear()
        else:
            await put(
                flow.context, flow.user, "claude_subscription", body, flow.id, delays=RESPOND_DELAYS, sleep=flow.sleep
            )
    except CredentialUnavailable as unavailable:
        _line(flow, f"store_failed reason={unavailable.reason} code={unavailable.code}")
        raise Failed("credential_missing" if unavailable.reason == "key_missing" else "upstream_error") from None
    except HubRefusal as refusal:
        _line(flow, f"store_failed reason=refused code={refusal.code}")
        raise Failed("upstream_error") from None
    except HubTransportError as error:
        _line(flow, f"store_failed {error.describe()}")
        raise Failed("upstream_unreachable") from None
    finally:
        body.clear()


async def _login(flow: Flow, session: LoginSession, opts: LoginOptions) -> None:
    wanted = _target(flow.body)
    mode = flow.body["mode"]
    plan = await _plan(flow, opts) if mode == "automatic" else None
    if plan is not None and "email" in wanted:
        plan = (plan[0], str(wanted["email"]))
    if session.full:
        record_id = str(wanted["subscription_id"])
        hint = plan[1] if plan is not None else await _known_email(flow, record_id)
        if hint is not None:
            session.argv = (*session.argv, "--email", hint)
    url = await session.start(opts)
    _line(flow, f"started mode={mode}")
    code = None if plan is None else await _automate(flow, url, plan, opts, session.workdir)
    target: FollowUp | None = None
    email: str | None = None
    if code is None:
        answer: dict[str, object] = {"step": "authorize", "url": url}
        if mode == "automatic":
            answer["automation"] = "failed"
        await flow.respond(answer)
        target = await flow.wait_follow_up(flow.remaining() - opts.code_margin)
        if target is None:
            raise Failed("timeout")
        given = target.request.body.get("code")
        if set(target.request.body) != {"code"} or not isinstance(given, str) or not CODE_RE.fullmatch(given):
            await _fail(flow, "code_invalid", target)
            return
        code = given
    else:
        assert plan is not None
        email = plan[1]
    try:
        login: str | FullLogin = await (session.finish_full(code, opts) if session.full else session.finish(code, opts))
        await _store(flow, login, email, wanted)
    except LoginFailed as failed:
        await _fail(flow, failed.code, target, shape=failed.shape)
        return
    except Failed as failed:
        await _fail(flow, failed.code, target)
        return
    _line(flow, "done")
    done: dict[str, object] = {"step": "done"} if email is None else {"step": "done", "email": email}
    await flow.respond(done, final=True, to=target)


async def claude_login(flow: Flow) -> None:
    config = flow.context.config
    full = isinstance(flow.body.get("subscription_id"), str)
    argv = config.subscription_login_argv if full else config.login_argv
    session = LoginSession(config.tmp_dir, flow.id, argv, flow.context.host.env.get("PATH", ""), full=full)
    try:
        await _login(flow, session, options(flow.context))
    except LoginFailed as failed:
        await _fail(flow, failed.code, shape=failed.shape)
    except Failed as failed:
        await _fail(flow, failed.code)
    finally:
        session.close()
        if session.left:
            _line(flow, "cleanup_incomplete")


# --- credential.check ----------------------------------------------------------------------------------------------


def _check_request(kind: str, secret: str, model: str) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if kind == "anthropic_api_key":
        headers["x-api-key"] = secret
    else:
        headers["authorization"] = f"Bearer {secret}"
        headers["anthropic-beta"] = OAUTH_BETA
    return headers, {"model": model, "messages": [{"role": "user", "content": "ping"}]}


def _model(config: Any, access: str) -> str:
    try:
        default = load_catalog(config.model_catalog).policy(access).default
    except RunnerError:
        default = None
    return default or CHECK_MODEL


async def _probe(flow: Flow, kind: str) -> bool:
    access, field = CHECK_KINDS[kind]
    config = flow.context.config
    try:
        async with opened(flow.context, flow.user, kind) as body:
            secret = body.get(field)
            if not isinstance(secret, str) or not secret:
                raise Failed("credential_missing")
            headers, payload = _check_request(kind, secret, _model(config, access))
    except CredentialUnavailable as unavailable:
        _line(flow, f"credential_unavailable reason={unavailable.reason}")
        raise Failed("credential_missing") from None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
            answer = await client.post(
                f"{config.model_upstream_anthropic}/v1/messages/count_tokens", headers=headers, json=payload
            )
    except httpx.HTTPError:
        raise Failed("upstream_unreachable") from None
    finally:
        headers.clear()
    if 200 <= answer.status_code < 300:
        return True
    if answer.status_code in (401, 403):
        return False
    _line(flow, f"check_refused status={answer.status_code}")
    raise Failed("upstream_error")


async def credential_check(flow: Flow) -> None:
    kind = flow.body.get("kind")
    try:
        if set(flow.body) != {"kind"} or not isinstance(kind, str) or kind not in CHECK_KINDS:
            raise Failed("upstream_error")
        valid = await _probe(flow, kind)
    except Failed as failed:
        await _fail(flow, failed.code)
        return
    _line(flow, f"credential_checked kind={kind} valid={str(valid).lower()}")
    await flow.respond({"step": "done", "valid": valid}, final=True)


# --- mailbox.check -------------------------------------------------------------------------------------------------


def imap_login(mailbox: Mailbox, factory: Callable[..., Any]) -> bool:
    """IMAPS login and `LIST`; False when the server refuses the login. `Failed` for a failed `LIST`; connection and TLS
    errors propagate as `OSError`."""
    conn = factory(mailbox.host, mailbox.port, ssl_context=ssl.create_default_context(), timeout=IMAP_TIMEOUT)
    try:
        try:
            conn.login(mailbox.user, mailbox.password)
        except imaplib.IMAP4.error:
            return False
        status, _ = conn.list()
        if status != "OK":
            raise Failed("upstream_error")
        return True
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            conn.logout()


async def mailbox_check(flow: Flow) -> None:
    opts = options(flow.context)
    try:
        if flow.body:
            raise Failed("upstream_error")
        mailbox, _email = await _mailbox(flow)
        try:
            valid = await asyncio.to_thread(imap_login, mailbox, opts.imap_factory)
        except (OSError, imaplib.IMAP4.error):
            raise Failed("upstream_unreachable") from None
    except Failed as failed:
        await _fail(flow, failed.code)
        return
    _line(flow, f"mailbox_checked valid={str(valid).lower()}")
    await flow.respond({"step": "done", "valid": valid}, final=True)


HANDLERS: Mapping[str, Callable[..., Any]] = {
    "claude.login": claude_login,
    "credential.check": credential_check,
    "mailbox.check": mailbox_check,
}
