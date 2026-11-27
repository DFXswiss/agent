"""Device-request handlers of the login area (`<area>/requests.py:HANDLERS`, ADR-027 section 4): `claude.login`,
`credential.check`, `mailbox.check` (05-runner.md section 11, ADR-011, `contracts/requests/`).

The request service (RUN-09) verifies, decrypts and routes the requests, keeps at most one flow per user and kind
(a newer request replaces the running one, which is cancelled), ends a flow after the request's 15 minutes and seals
the responses. The handlers here never log or answer a secret: no token, code, URL, password or credential body.

`claude.login` (root body `{mode: "manual"|"automatic"}`, follow-up `{code}`; KOR-69, ADR-090 section 2: the root body
may add `subscription_id` - the further subscription the login fills, a new ID creates it -, `name` and `note` (taken
for the record; an existing one keeps its own unless given) and, for `automatic`, `email` - the address to sign in with
instead of the mailbox's own; a malformed one of them is `upstream_error`):

With `subscription_id` the login is the full login (`SUBSCRIPTION_LOGIN_ARGV`, section 11.6, ADR-092, KOR-71): the
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
With `{mailbox}` (ADR-099 section 9) the same check of that mailbox body, which the owner's client sealed into the
request (any mailbox of the store, also before it is saved); a malformed body is `upstream_error`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import imaplib
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from agent_cli.account_login import Mailbox
from agent_runner.credentials import subscriptions
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused, opened, put
from agent_runner.errors import RunnerError
from agent_runner.login import automation
from agent_runner.login.automation import CODE_RE, EMAIL_RE, MAX_EMAIL, AutomationFailed, Plan, mailbox_fields
from agent_runner.login.full_login import FullLogin
from agent_runner.login.pages import Page
from agent_runner.login.relay import LoginFailed, LoginOptions, LoginSession, options
from agent_runner.login.sandbox import (
    ProcessSandbox,
    Sandbox,
    SandboxError,
    load_playwright,
    use_browsers,
    vm_sandbox,
)
from agent_runner.model.selection import load_catalog
from agent_runner.requests.service import RESPOND_DELAYS, Flow, FollowUp
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.vnc.screens import Offer, screens_for
from agent_runner.workvm.service import workvms_for

MODES = ("manual", "automatic")
LOGIN_FIELDS = frozenset({"mode", "subscription_id", "name", "note", "email", "account", "extended", "vnc"})
ACCOUNT_FIELDS = frozenset({"name", "birth_date", "consents"})
CONSENTS = frozenset({"terms", "age", "marketing"})
BIRTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
BASIC_STAGES = frozenset({"browser_started", "email_entered", "magic_link_received", "authorized"})
"""The progress stages a client without `extended` knows (ADR-100 section 4)."""
VERIFY_FRAMES = 80
"""Pictures of a check for humans per login (the hub keeps at most 100 responses per request)."""
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


@dataclass(frozen=True)
class Stored:
    """What a further subscription's record already holds: its address and its own mailbox (ADR-100 section 1)."""

    email: str | None
    mailbox: tuple[Mailbox, str] | None


async def _legacy_mailbox(flow: Flow) -> tuple[Mailbox, str] | None:
    try:
        async with opened(flow.context, flow.user, "mailbox") as body:
            return mailbox_fields(body)
    except CredentialUnavailable:
        return None


async def _mailbox(flow: Flow, stored: Stored | None = None) -> tuple[Mailbox, str]:
    """The subscription's own mailbox, else the user's single one (`Failed("credential_missing")` without either)."""
    found = stored.mailbox if stored is not None and stored.mailbox is not None else await _legacy_mailbox(flow)
    if found is None:
        raise Failed("credential_missing")
    return found


def _label(value: object, most: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= most and all(ord(char) >= 0x20 for char in value)


def _account(value: object) -> bool:
    """`account` of the root body (`contracts/requests/claude.login.request.json`)."""
    if not isinstance(value, dict) or not set(value) <= ACCOUNT_FIELDS or not _label(value.get("name"), 100):
        return False
    birth, consents = value.get("birth_date", "2000-01-01"), value.get("consents", [])
    return (
        isinstance(birth, str)
        and BIRTH_RE.fullmatch(birth) is not None
        and isinstance(consents, list)
        and len(set(consents)) == len(consents)
        and all(item in CONSENTS for item in consents)
    )


def _target(body: Mapping[str, object]) -> dict[str, object]:
    """The subscription fields of a root body (`Failed("upstream_error")` when one is malformed): `subscription_id`,
    `name`, `note`, `email`, `account` and `extended` as given."""
    if not set(body) <= LOGIN_FIELDS or body.get("mode") not in MODES:
        raise Failed("upstream_error")
    found = {name: body[name] for name in sorted(LOGIN_FIELDS - {"mode"}) if name in body}
    record_id = found.get("subscription_id")
    if record_id is not None and not (isinstance(record_id, str) and UUID_RE.fullmatch(record_id)):
        raise Failed("upstream_error")
    if any(name in found and not _label(found[name], most) for name, most in LABEL_LIMITS.items()):
        raise Failed("upstream_error")
    email = found.get("email")
    automatic = body["mode"] == "automatic"
    if email is not None and not (
        automatic and isinstance(email, str) and len(email) <= MAX_EMAIL and EMAIL_RE.fullmatch(email)
    ):
        raise Failed("upstream_error")
    if "account" in found and not (automatic and _account(found["account"])):
        raise Failed("upstream_error")
    if any(name in found and found[name] is not True for name in ("extended", "vnc")):
        raise Failed("upstream_error")
    return found


async def _stored(flow: Flow, record_id: object) -> Stored | None:
    """The address and own mailbox of the further subscription (None when there is none or it cannot be read)."""
    if not isinstance(record_id, str):
        return None
    try:
        found = await subscriptions.fetch(flow.context, record_id)
        if found is None or found.primary or found.owner != flow.user:
            return None
        async with subscriptions.opened(flow.context, found) as body:
            email, box = body.get("email"), body.get("mailbox")
            mailbox = mailbox_fields(box) if isinstance(box, dict) else None
    except (CredentialUnavailable, RecordRefused, HubRefusal, HubTransportError):
        return None
    return Stored(email if isinstance(email, str) and EMAIL_RE.fullmatch(email) else None, mailbox)


class HumanCheck:
    """The check for humans of one login, done by its owner (ADR-100 section 4): the sandbox's picture goes out as
    `verify` responses (when it changed, every `verify_tick`, at most `VERIFY_FRAMES`), a `{click}` follow-up of the
    owner's own identity is clicked at that point of the viewport, `{relay: true}` or no click within `verify_wait`
    gives up (`human_check`). Without `extended` the check is given up at once.

    With `vnc` (the root asked for it, ADR-103) the check first answers `{step: "needs_human", vnc_available}` and,
    when the sandbox has a screen, offers it to the owner (`agent_runner.vnc.screens`, kind `login`, this request) until
    the check is gone or given up. While a VNC connection is open the owner works on the screen itself: no pictures are
    sent and the check keeps waiting (`verify_wait` starts again on every look)."""

    def __init__(
        self, flow: Flow, opts: LoginOptions, *, extended: bool, progress: automation.Progress, vnc: bool = False
    ) -> None:
        self.flow = flow
        self.opts = opts
        self.extended = extended
        self.progress = progress
        self.vnc = vnc
        self.frames = 0
        self.shown: str | None = None

    async def show(self, sandbox: Sandbox) -> None:
        picture = await sandbox.picture()
        if picture is None or picture.digest == self.shown:
            return
        if self.frames >= VERIFY_FRAMES:
            raise AutomationFailed("human_check")
        self.frames += 1
        self.shown = picture.digest
        image = {"width": picture.width, "height": picture.height, "data": base64.b64encode(picture.jpeg).decode()}
        await self.flow.respond({"step": "verify", "frame": self.frames, "image": image})

    async def click(self, sandbox: Sandbox, page: Page, follow: FollowUp) -> str:
        """Handle one follow-up: `relay`, `clicked` or `ignored` (malformed, or an agent's click)."""
        body, request = follow.request.body, follow.request
        if body == {"relay": True}:
            return "relay"
        point = body.get("click") if set(body) == {"click"} else None
        if not (isinstance(point, dict) and set(point) == {"x", "y"} and all(_fraction(point[k]) for k in point)):
            _line(self.flow, "verify_follow_up_invalid")
            return "ignored"
        if request.user != request.owner:
            _line(self.flow, "click_refused")  # a check for humans is never answered by an agent (ADR-100 section 4)
            return "ignored"
        await sandbox.click_at(float(point["x"]) * page.width, float(point["y"]) * page.height)
        await asyncio.sleep(self.opts.verify_settle)
        return "clicked"

    async def offer(self, sandbox: Sandbox) -> Offer | None:
        """Tell a client that asked for VNC that a human is needed, and offer the sandbox's screen when it has one."""
        if not self.vnc:
            return None
        screen = getattr(sandbox, "screen", None)
        await self.flow.respond({"step": "needs_human", "vnc_available": screen is not None})
        if screen is None:
            return None
        _line(self.flow, "vnc_offered")
        return screens_for(self.flow.context).offer("login", self.flow.id, self.flow.user, screen)

    async def __call__(self, sandbox: Sandbox, page: Page) -> None:
        if not self.extended:
            raise AutomationFailed("human_check")
        _line(self.flow, "verify_started")
        offer = await self.offer(sandbox)
        try:
            await self.wait(sandbox, page, offer)
        finally:
            if offer is not None:
                screens_for(self.flow.context).withdraw(offer)

    async def wait(self, sandbox: Sandbox, page: Page, offer: Offer | None) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.opts.verify_wait
        await self.show(sandbox)
        while True:
            follow = await self.flow.wait_follow_up(min(self.opts.verify_tick, max(deadline - loop.time(), 0.0)))
            outcome = "none" if follow is None else await self.click(sandbox, page, follow)
            if outcome == "relay":
                raise AutomationFailed("human_check")
            watched = offer is not None and offer.viewers > 0
            if outcome == "clicked" or watched:  # only the owner gives the check its time again
                deadline = loop.time() + self.opts.verify_wait
            if (await automation.classify(sandbox)).kind != "challenge":
                _line(self.flow, "verify_passed")
                await self.progress("verified")
                return
            if loop.time() >= deadline:
                raise AutomationFailed("human_check")
            if not watched:
                await self.show(sandbox)


def _fraction(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


async def _sandbox(flow: Flow, url: str, workdir: Path, opts: LoginOptions) -> Sandbox:
    """The login's own sandbox (`LOGIN_SANDBOX`, ADR-100 section 3)."""
    if opts.sandbox is not None:
        made: Sandbox = await opts.sandbox(flow, url, workdir)
        return made
    config = flow.context.config
    if config.login_sandbox == "process":
        starter = load_playwright(opts.playwright)
        use_browsers(config.home)
        return await ProcessSandbox.start(starter, workdir / "browser", url, opts.page_timeout)
    workvms = workvms_for(flow.context)
    if workvms is None:
        raise SandboxError("no_sandbox")
    return await vm_sandbox(workvms, flow.id, lambda text: _line(flow, text))


async def _automate(
    flow: Flow, url: str, plan: Plan, opts: LoginOptions, workdir: Path, *, extended: bool, vnc: bool = False
) -> str:
    """The code from the automation (`AutomationFailed` with the reason of a fall-back)."""

    async def progress(stage: str) -> None:
        if extended or stage in BASIC_STAGES:
            await flow.respond({"step": "progress", "stage": stage})

    try:
        sandbox = await _sandbox(flow, url, workdir, opts)
    except SandboxError as error:
        raise AutomationFailed(error.reason) from None
    try:
        return await automation.authorize(
            url,
            plan,
            sandbox=sandbox,
            opts=opts,
            progress=progress,
            human=HumanCheck(flow, opts, extended=extended, progress=progress, vnc=vnc),
            budget=flow.remaining() - opts.code_margin,
            clock=time.time,
        )
    finally:
        try:
            await sandbox.close()
        except Exception as error:  # a failed teardown is logged and swept (VMs), never the end of the login
            reason = error.reason if isinstance(error, SandboxError) else type(error).__name__
            _line(flow, f"sandbox_close_failed reason={reason}")


async def _store(flow: Flow, login: str | FullLogin, email: str | None, target: Mapping[str, object]) -> None:
    # a full login knows the address the program reports, unless the automation signed in with its own
    body = login.body() if isinstance(login, FullLogin) else {"token": login, "email": None, "kind": "setup_token"}
    if email is not None:
        body["email"] = email
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


async def _plan(flow: Flow, wanted: Mapping[str, object], stored: Stored | None) -> Plan | None:
    """The plan of an `automatic` login, None when the automation is switched off (`disabled`);
    `Failed("credential_missing")` without a mailbox."""
    if not flow.context.config.login_automation:
        return None
    mailbox, own = await _mailbox(flow, stored)
    email = wanted.get("email") or (stored.email if stored is not None else None) or own
    account = wanted.get("account")
    if isinstance(account, dict) and account.get("consents") and flow.request.user != flow.user:
        # consents are the account holder's, declared by the user, never by the user's agent (ADR-100 section 5)
        _line(flow, "consents_ignored")
        account = {name: value for name, value in account.items() if name != "consents"}
    return Plan(mailbox, str(email), account if isinstance(account, dict) else None)


async def _code(flow: Flow, opts: LoginOptions) -> FollowUp:
    """The follow-up with the code; late `{click}`/`{relay}` follow-ups of a check for humans are passed over."""
    while True:
        follow = await flow.wait_follow_up(flow.remaining() - opts.code_margin)
        if follow is None:
            raise Failed("timeout")
        if set(follow.request.body) not in ({"click"}, {"relay"}):
            return follow


async def _login(flow: Flow, session: LoginSession, opts: LoginOptions) -> None:
    wanted = _target(flow.body)
    mode, extended = flow.body["mode"], wanted.get("extended") is True
    stored = await _stored(flow, wanted.get("subscription_id"))
    plan = await _plan(flow, wanted, stored) if mode == "automatic" else None
    reason = "disabled" if mode == "automatic" and plan is None else None
    if reason is not None:
        _line(flow, f"automation_failed reason={reason}")
    if session.full:
        hint = plan.email if plan is not None else (stored.email if stored is not None else None)
        if hint is not None:
            session.argv = (*session.argv, "--email", hint)
    url = await session.start(opts)
    _line(flow, f"started mode={mode}")
    code: str | None = None
    if plan is not None:
        try:
            vnc = wanted.get("vnc") is True
            code = await _automate(flow, url, plan, opts, session.workdir, extended=extended, vnc=vnc)
        except AutomationFailed as failed:
            _line(flow, f"automation_failed reason={failed.reason}")
            reason = failed.answer
    target: FollowUp | None = None
    email: str | None = None
    if code is None:
        answer: dict[str, object] = {"step": "authorize", "url": url}
        if mode == "automatic":
            answer["automation"] = "failed"
            if extended and reason is not None:
                answer["reason"] = reason
        await flow.respond(answer)
        target = await _code(flow, opts)
        given = target.request.body.get("code")
        if set(target.request.body) != {"code"} or not isinstance(given, str) or not CODE_RE.fullmatch(given):
            await _fail(flow, "code_invalid", target)
            return
        code = given
    else:
        assert plan is not None
        email = plan.email
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
        record_id = flow.body.get("subscription_id")
        if (
            set(flow.body) - {"mailbox", "subscription_id"}
            or len(flow.body) > 1
            or (record_id is not None and not (isinstance(record_id, str) and UUID_RE.fullmatch(record_id)))
        ):
            raise Failed("upstream_error")
        if "mailbox" in flow.body:  # a mailbox of the store, sealed to this device with the request (ADR-099 §9)
            given = mailbox_fields(flow.body["mailbox"]) if isinstance(flow.body["mailbox"], dict) else None
            if given is None:
                raise Failed("upstream_error")
            mailbox, _email = given
        elif record_id is not None:  # the own mailbox of a further subscription (KOR-73, ADR-100 section 1)
            stored = await _stored(flow, record_id)
            if stored is None or stored.mailbox is None:
                raise Failed("credential_missing")
            mailbox = stored.mailbox[0]
        else:
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
