"""`pair` (11-agent-access.md sections 5.1 and 8.2): fresh keys, the pairing at the hub, the link in the user's
browser and the loopback handover.

The listener binds `127.0.0.1` on a port the OS draws. It accepts exactly one kind of request: `POST /agent-pairing`
with `Host: 127.0.0.1:<port>` and a form body (`application/x-www-form-urlencoded`, the fields `p`, `r`, `k` once
each, plus `u`, the user ID, see below) whose `p` is this pairing (constant-time comparison), `r` a well-formed
32-byte secret and `k` a well-formed 32-byte key. Everything else (another method, path, host, content type, a
foreign pairing, a malformed or wrong `r`) is answered `404` with an empty body and no CORS headers and does not end
the wait; nothing of such a request is logged. The browser hands `r` over with a top-level form POST, never in a URL
(no history entry).

The claim is bound to the user (`HKDF(r, P || "agent-claim\\0" || canonical({"agent", "user"}))`, section 3), but
neither the link nor the hub tells a confirmed agent its user before the claim. The handover therefore carries the
user ID as the form field `u` (decisions/KOR-24.md); a handover without it ends the pairing as `pairing_failed`
(`user_id_missing`).

With a candidate `r` the tool claims (`POST /v1/agent/claim` with `HKDF(r, ...)`; `r` itself is never sent). A wrong
`r` is refused by the hub (`claim_invalid`) and treated like any bad request. After a successful claim the tool opens
the identity with its key and `r`, checks it against `GET /v1/me`, stores the state and answers `303` to
`<origin>/settings?agent=connected` (`?agent=failed` when the claimed identity does not open or does not match;
`pairing_failed`). The origin comes from the tool's own state, never from the request. The wait ends after
`PAIRING_TIMEOUT_S` (`pairing_timeout`).

`register` (section 5.2) is the same pairing behind an invitation: the tool reads the invitation link (stdin or
environment, never the command line), checks it at the hub, and opens `<origin>/invite#<code>&<pairing>&u=<display
name>` in the user's browser. The user creates the passkey; the registration's sign-in is the pairing-tagged
ceremony, and the page confirms and hands over like `/connect`. The link carries the invitation code, so it is never
printed and never a program argument (which other local users could read in the process list): the browser opens a
redirect file (`0600`, in the state directory, removed when the wait ends) whose only content forwards to the link.
The hub address comes from the invitation link; it must be `https` (plain `http` only for a loopback hub, as in
tests), and the result names it.

`Pairing` also serves the pairing from another computer (`remote`, ADR-067; the commands are in
`agent_platform.remote`): it registers with `remote: true`, requires the answer to echo `remote: true` and a nonce
(else `hub_too_old`, so it never proceeds with a loopback pairing it did not ask for), builds the link without a port
(`m=remote`) and the agent code, and completes with a handover the tool fetched from the hub (`complete`).
"""

from __future__ import annotations

import hmac
import html
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from agent_platform.errors import PlatformError
from agent_platform.hub import AgentClient, anon_request, refused
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.keys import Identity
from agent_runner.sync.http import hub_origin

PAIRING_TIMEOUT_S = 600.0
POLL_S = 0.5
MAX_FORM_BYTES = 4096
FORM_TYPE = "application/x-www-form-urlencoded"
PATH = "/agent-pairing"
DISPLAY_NAME_MAX = 80
# what JavaScript's `String.prototype.trim` removes (the web's rule for the display name, `account/genesis.ts`)
JS_TRIM = (
    "\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
OPEN_YOURSELF = "agent-platform: the browser could not be started; open the link yourself"


@dataclass(frozen=True)
class Handover:
    """A well-formed handover of this pairing."""

    secret: bytes
    root_key: str
    user_id: str | None


def parse_form(body: bytes, pairing_id: str) -> Handover | None:
    """The handover of a form body, None for anything else (foreign pairing, malformed `r` or `k`)."""
    try:
        fields = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError):
        return None
    if set(fields) - {"u"} != {"p", "r", "k"} or any(len(values) != 1 for values in fields.values()):
        return None
    user_id = fields["u"][0] if "u" in fields else None
    if user_id is not None and not is_uuid7(user_id):
        return None
    if not hmac.compare_digest(fields["p"][0].encode(), pairing_id.encode()):
        return None
    try:
        secret = b64u_decode(fields["r"][0], 32)
        root_key = b64u_encode(b64u_decode(fields["k"][0], 32))
    except (CryptoError, ValueError):
        return None
    return Handover(secret, root_key, user_id)


def default_browser(platform: str = sys.platform) -> str:
    return "open" if platform == "darwin" else "xdg-open"


def open_browser(environ: Mapping[str, str], url: str, fallback: str = OPEN_YOURSELF) -> None:
    """Start `AGENT_PLATFORM_BROWSER` (else the system's opener) with the URL as its only argument."""
    program = environ.get("AGENT_PLATFORM_BROWSER", "") or default_browser()
    try:
        subprocess.Popen(
            [program, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        print(fallback, file=sys.stderr)


class _WrongSecret(Exception):
    """The hub refused the claim (`claim_invalid`): a wrong `r`, treated like any bad request."""


class Pairing:
    """One pairing: keys, the hub's pinned key and the claim."""

    def __init__(
        self, home: Home, hub: str, name: str, host: str, *, remote: bool = False, message: str | None = None
    ) -> None:
        try:
            self.origin = hub_origin(hub).base_url
        except ValueError:
            raise PlatformError("invalid_argument") from None
        try:
            agent_crypto.check_label({"name": name, "host": host})
            if message is not None:
                agent_crypto.check_message(message)
        except CryptoError:
            raise PlatformError("invalid_argument") from None
        self.home, self.name, self.host, self.remote = home, name, host, remote
        self.message = message
        self.agent: Identity = agent_crypto.new_agent()
        self.pairing_id = uuid7()
        self.nonce = b""
        self.expires_at = ""
        self.result: dict[str, Any] | None = None
        self.failure: PlatformError | None = None

    def register(self) -> None:
        config = anon_request(self.origin, "GET", "/v1/config")
        hub_key = config.get("hub_key")
        try:
            if not isinstance(hub_key, dict):
                raise CryptoError("header_invalid")
            b64u_decode(hub_key.get("public_key"), 32)
            self.hub_key = {"id": str(hub_key.get("id")), "public_key": str(hub_key["public_key"])}
        except CryptoError:
            raise PlatformError("hub_refused", reason="response_invalid") from None
        self.config_root = config.get("platform_root_key")
        body: dict[str, Any] = {
            "pairing_id": self.pairing_id,
            "pub": {"kem": self.agent.public()["kem"], "sig": self.agent.public()["sig"]},
            "pop": agent_crypto.sign_pop(self.agent, self.pairing_id),
        }
        if self.remote:
            body["remote"] = True
        try:
            answer = anon_request(self.origin, "POST", "/v1/agent-pairings", body)
        except PlatformError as error:
            if self.remote and error.fields.get("hub_error") == "request_invalid":
                raise PlatformError("hub_too_old") from None
            raise
        if answer.get("pairing_id") != self.pairing_id:
            raise PlatformError("hub_refused", reason="response_invalid")
        self.expires_at = str(answer.get("expires_at"))
        if self.remote:
            try:
                if answer.get("remote") is not True:
                    raise CryptoError("header_invalid")
                self.nonce = b64u_decode(answer.get("nonce"), agent_crypto.NONCE_BYTES)
            except CryptoError:
                raise PlatformError("hub_too_old") from None

    @classmethod
    def restore(cls, home: Home, pending: Mapping[str, Any], agent: Identity) -> Pairing:
        """The pending remote pairing of `pending.json` with its private keys (`state_damaged` when unsound)."""
        try:
            label = pending["label"]
            pairing = cls(home, str(pending["hub"]), str(label["name"]), str(label["host"]), remote=True)
            pairing.nonce = b64u_decode(pending["nonce"], agent_crypto.NONCE_BYTES)
            pairing.hub_key = {"id": str(pending["hub_key"]["id"]), "public_key": str(pending["hub_key"]["public_key"])}
            b64u_decode(pairing.hub_key["public_key"], 32)
            if not is_uuid7(pending["pairing_id"]):
                raise CryptoError("header_invalid")
        except (KeyError, TypeError, CryptoError, PlatformError):
            raise PlatformError("state_damaged") from None
        pairing.pairing_id = str(pending["pairing_id"])
        pairing.agent = agent
        pairing.config_root = pending.get("platform_root_key")
        pairing.expires_at = str(pending.get("expires_at"))
        return pairing

    def fingerprint(self) -> str:
        return agent_crypto.fingerprint(self.agent.kem_public(), self.agent.sig_public())

    def _message_parameter(self) -> str:
        """`&t=<message>`, the agent's personal message to the user (rule E-28), or nothing."""
        return "" if self.message is None else f"&t={quote(self.message, safe='')}"

    def remote_fragment(self) -> str:
        """`p=<id>&f=<fp>&m=remote&a=<name>&h=<host>[&t=<message>]`: the parameters of a remote pairing link."""
        name, host = quote(self.name, safe=""), quote(self.host, safe="")
        return f"p={self.pairing_id}&f={self.fingerprint()}&m=remote&a={name}&h={host}{self._message_parameter()}"

    def agent_code(self) -> str:
        """The agent code the user types on the page, shown as `XXXXX-XXXXX`."""
        code = agent_crypto.agent_code(self.pairing_id, self.fingerprint(), self.nonce, self.origin)
        return agent_crypto.format_code(code)

    def client(self) -> AgentClient:
        return AgentClient(self.origin, self.pairing_id, self.agent, b64u_decode(self.hub_key["public_key"], 32))

    def complete(self, client: AgentClient, handover: Handover) -> dict[str, Any]:
        """Claim with a verified handover of a remote pairing and store the state (`pairing_failed` when the claim is
        refused or the identity does not match)."""
        try:
            user_id, first = self._verified_claim(client, handover)
        except _WrongSecret:
            raise PlatformError("pairing_failed", reason="claim_invalid") from None
        self._store(user_id, first, handover)
        assert self.result is not None
        return self.result

    def fragment(self, port: int) -> str:
        fp = self.fingerprint()
        name, host = quote(self.name, safe=""), quote(self.host, safe="")
        return f"p={self.pairing_id}&f={fp}&c={port}&a={name}&h={host}{self._message_parameter()}"

    def link(self, port: int) -> str:
        return f"{self.origin}/connect#{self.fragment(port)}"

    def claim(self, handover: Handover) -> bool:
        """True when the claim succeeded (state stored or `failure` set); False for a wrong `r`."""
        client = self.client()
        try:
            return self._claim(client, handover)
        finally:
            client.close()

    def _claim(self, client: AgentClient, handover: Handover) -> bool:
        try:
            self._store(*self._verified_claim(client, handover), handover)
        except _WrongSecret:
            return False
        except PlatformError as error:
            self.failure = error
        return True

    def _verified_claim(self, client: AgentClient, handover: Handover) -> tuple[str, dict[str, Any]]:
        """`(user ID, claim answer)` after the identity opened and matched; `PlatformError` otherwise."""
        user_id = handover.user_id
        if user_id is None:
            raise PlatformError("pairing_failed", reason="user_id_missing")
        claim = agent_crypto.claim(handover.secret, self.pairing_id, user_id)
        try:
            first = client.call("POST", "/v1/agent/claim", {"claim": b64u_encode(claim)})
        except PlatformError as error:
            if error.fields.get("hub_error") == "claim_invalid":
                raise _WrongSecret from None
            raise
        try:
            identity = agent_crypto.open_identity(
                self.agent.kem, handover.secret, self.pairing_id, user_id, str(first.get("bundle_ct"))
            )
            me = client.call("GET", "/v1/me")
            expected = {"kem_pub": identity.public()["kem"], "sig_pub": identity.public()["sig"]}
            same_user = me.get("user_id") == user_id and first.get("agent_id") == self.pairing_id
            if me.get("identity") != expected or not same_user:
                raise CryptoError("decrypt_failed")
        except CryptoError:
            raise PlatformError("pairing_failed", reason="identity_mismatch") from None
        if isinstance(self.config_root, str) and self.config_root != handover.root_key:
            raise PlatformError("pairing_failed", reason="platform_root_mismatch")
        return user_id, first

    def _store(self, user_id: str, first: Mapping[str, Any], handover: Handover) -> None:
        try:
            previous = self.home.load()
            self.home.delete(str(previous.get("agent_id")), previous.get("key_store"))
        except PlatformError:
            pass
        where = self.home.store_keys(self.pairing_id, self.agent)
        self.home.save(
            {
                "hub": self.origin,
                "hub_key": self.hub_key,
                "platform_root_key": handover.root_key,
                "agent_id": self.pairing_id,
                "user_id": user_id,
                "label": {"name": self.name, "host": self.host},
                "bundle_ct": str(first["bundle_ct"]),
                "secret": b64u_encode(handover.secret),
                "key_store": where,
            }
        )
        self.result = {"agent_id": self.pairing_id, "user_id": user_id}


def make_handler(pairing: Pairing, port: int, done: Callable[[], None]) -> type[BaseHTTPRequestHandler]:
    expected_host = f"127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-platform"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _not_found(self) -> None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._not_found()

        do_PUT = do_DELETE = do_OPTIONS = do_HEAD = do_PATCH = do_GET

        def do_POST(self) -> None:
            handover = self._handover()
            if handover is None or not pairing.claim(handover):
                self._not_found()
                return
            outcome = "connected" if pairing.failure is None else "failed"
            self.send_response(303)
            self.send_header("Location", f"{pairing.origin}/settings?{urlencode({'agent': outcome})}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            done()

        def _handover(self) -> Handover | None:
            if self.path != PATH or self.headers.get_all("Host") != [expected_host]:
                return None
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != FORM_TYPE:
                return None
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return None
            if not 0 < length <= MAX_FORM_BYTES:
                return None
            return parse_form(self.rfile.read(length), pairing.pairing_id)

    return Handler


def listen(pairing: Pairing, on_ready: Callable[[int], None], timeout: float | None = None) -> None:
    """Serve the loopback listener until one claim ends the pairing or the timeout passes (`pairing_timeout`)."""
    finished: list[bool] = []
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(pairing, port, lambda: finished.append(True))
    server.timeout = POLL_S
    deadline = time.monotonic() + (PAIRING_TIMEOUT_S if timeout is None else timeout)
    try:
        on_ready(port)
        while not finished:
            if time.monotonic() >= deadline:
                raise PlatformError("pairing_timeout")
            server.handle_request()
    finally:
        server.server_close()


def pair(
    home: Home,
    environ: Mapping[str, str],
    *,
    hub: str,
    name: str,
    host: str,
    browser: bool,
    message: str | None = None,
) -> dict[str, Any]:
    pairing = Pairing(home, hub, name, host, message=message)
    pairing.register()
    opened: list[str] = []

    def ready(port: int) -> None:
        url = pairing.link(port)
        opened.append(url)
        print("agent-platform: waiting for the confirmation in the browser (10 minutes)", file=sys.stderr)
        if browser:
            open_browser(environ, url)
        else:
            print(f"agent-platform: open {url}", file=sys.stderr)

    listen(pairing, ready)
    if pairing.failure is not None:
        raise pairing.failure
    assert pairing.result is not None
    return {**pairing.result, "hub": pairing.origin, "link": opened[0]}


def parse_invite(text: str) -> tuple[str, bytes]:
    """The hub origin and the code of an invitation link `<origin>/invite#<code>` (`invalid_argument` otherwise)."""
    parts = urlsplit(text.strip())
    if parts.path != "/invite" or parts.query or not parts.fragment:
        raise PlatformError("invalid_argument")
    if parts.scheme != "https" and (parts.scheme != "http" or parts.netloc.rsplit(":", 1)[0] not in LOOPBACK_HOSTS):
        raise PlatformError("invalid_argument")
    try:
        origin = hub_origin(f"{parts.scheme}://{parts.netloc}").base_url
        code = invite_format.parse_code(parts.fragment.split("&", 1)[0])
    except (ValueError, CryptoError):
        raise PlatformError("invalid_argument") from None
    return origin, code


def register(
    home: Home,
    environ: Mapping[str, str],
    *,
    invite: str,
    display_name: str,
    name: str,
    host: str,
    message: str | None = None,
) -> dict[str, Any]:
    """A new account through an invitation, with this agent paired (module comment, 11-agent-access.md section 5.2)."""
    origin, code = parse_invite(invite)
    member = display_name.strip(JS_TRIM)
    if not 1 <= len(member) <= DISPLAY_NAME_MAX:
        raise PlatformError("invalid_argument")
    invite_id = invite_format.invite_secrets(code).invite_id
    lookup = anon_request(origin, "POST", "/v1/invites/lookup", {"invite_id": invite_id})
    if lookup.get("registered") is True:
        raise refused("invite_registered", 409)
    pairing = Pairing(home, origin, name, host, message=message)
    pairing.register()
    code_text = invite_format.format_code(code)

    redirect = home.path / f"invite-{pairing.pairing_id}.html"

    def ready(port: int) -> None:
        url = f"{origin}/invite#{code_text}&{pairing.fragment(port)}&u={quote(member, safe='')}"
        write_redirect(home, redirect, url)
        print("agent-platform: waiting for the user to create a passkey in the browser (10 minutes)", file=sys.stderr)
        open_browser(
            environ,
            redirect.as_uri(),
            "agent-platform: the browser could not be started; run the command where the user's browser runs",
        )

    try:
        listen(pairing, ready)
    finally:
        redirect.unlink(missing_ok=True)
    if pairing.failure is not None:
        raise pairing.failure
    assert pairing.result is not None
    return {**pairing.result, "hub": origin, "scope": lookup.get("scope")}


def write_redirect(home: Home, path: Path, url: str) -> None:
    """A page that only forwards to `url`, readable by the user alone (module comment)."""
    home.ensure_dir()
    target = html.escape(url, quote=True)
    page = '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
    page += f'<meta http-equiv="refresh" content="0;url={target}"><title>agent.dfx.swiss</title>'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(page)
