"""The Community: one group chat of all users (14-community.md sections 3 and 4, ADR-069; twin of the web client's
`core/community/engine.ts`).

The community principal is the actor (ADR-082 section 1): an agent with its own identity joins, asks for the key,
wraps and posts under its agent ID with its own key ("Agent of <user>"); an account that holds the user's identity
acts as the user.

Every command joins first (`POST /v1/community/join`, idempotent) and reads `GET /v1/community`. The commands that
need the community key run the key flow of section 4.2 (`ensure_key`), as the browser does when it starts:

- no epoch: draw `CMK_1`, its key check and the self-wrap, `POST /v1/community/epochs` (`community_epoch_exists`:
  read again once);
- a self-wrap: check its signature under the actor's key, open it, compare the key check;
- the tool's own open request (its one-off X25519 key is kept in the state file, `state["community"]["request"]`, so
  a later run can open the wrap): wrapped: check the wrapper's signature, open, compare the key check, store the
  self-wrap (`POST /v1/community/wraps`), else reject the wrap (`POST /v1/community/request/reject`); not wrapped yet:
  wait;
- a wrapped request of another client of the user gets `FOREIGN_S` to store its self-wrap before it is replaced;
- otherwise a new request (`PUT /v1/community/request`).

Reading verifies every message under its author's identity key (served with the page and pinned on first sight in
the state file: a changed key makes the message unverifiable), checks that header and row agree, opens it with the key
and checks the body rules; a message that fails any check is `unverifiable` and carries no content. Posting cleans name
and text like the web client, seals and signs; a post that failed in a way that may succeed later (`hub_unreachable`,
`rate_limited`) is kept and sent byte for byte again when the same name and text are posted (the hub answers
`replayed` if the first attempt was stored). `community serve` seals the key to open requests of others.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_platform import communityfmt as fmt
from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree, text_argument
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.keys import x25519_private, x25519_private_raw, x25519_public_raw

PAGE = 50
MAX_PAGE = 100
REQUESTS = 20
SERVE_ROUNDS = 5
"""At most this many request pages are served in one run (as the web client)."""
POLL_S = 5.0
"""`community key --wait`: the pause between two reads of the state."""
FOREIGN_S = 60.0
"""A wrapped request of another client of the user gets this long to store its self-wrap before it is replaced."""
RETRY_FALLBACK_S = 6
"""The wait after a `429` without a usable `Retry-After` (one refill of the hub's bucket)."""
STATE_KEY = "community"
MARK_CHARS = 4
TEXT_CONTROLS = ("\n", "\t")

HELD, CREATED, OPENED, WAITING, FAILED = "held", "created", "opened", "waiting", "failed"


@dataclass(frozen=True)
class Key:
    """The outcome of the key flow: `secret` is set for `held`, `created` and `opened`."""

    status: str
    epoch: int | None = None
    secret: bytes | None = None
    request: str | None = None
    code: str | None = None

    def summary(self) -> dict[str, Any]:
        found: dict[str, Any] = {"key": self.status, "epoch": self.epoch}
        if self.request is not None:
            found["request_id"] = self.request
        if self.code is not None:
            found["key_code"] = self.code
        return found


# --- helpers ---


def mark(user: str) -> str:
    """`#` and the first four base32 characters of SHA-256 of the user ID (the sender mark of the web client)."""
    digest = hashlib.sha256(user.encode("utf-8")).digest()
    return "#" + base64.b32encode(digest).decode("ascii")[:MARK_CHARS]


def clean_name(value: str) -> str:
    """The web client's `cleanName`: characters of the refused categories removed, NFC, surrounding white space cut."""
    kept = "".join(char for char in value if unicodedata.category(char) not in fmt.NAME_CATEGORIES)
    return unicodedata.normalize("NFC", kept).strip()


def clean_text(value: str) -> str:
    """The web client's `cleanText`: CR LF and CR to LF, refused characters removed, surrounding white space cut."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    kept = (
        char
        for char in text
        if char in TEXT_CONTROLS or (ord(char) not in fmt.TEXT_FORBIDDEN and unicodedata.category(char) != "Cc")
    )
    return "".join(kept).strip()


def _id(value: str) -> str:
    if not is_uuid7(value):
        raise PlatformError("invalid_argument")
    return value


def _local(account: Account) -> dict[str, Any]:
    found = account.state.get(STATE_KEY)
    return dict(found) if isinstance(found, dict) else {}


def _save(account: Account, local: dict[str, Any]) -> None:
    account.state[STATE_KEY] = local
    account.home.save(account.state)


def _text(value: object) -> bool:
    return isinstance(value, str)


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _overview(answer: dict[str, Any]) -> dict[str, Any]:
    """The checked body of `GET /v1/community` (`response_invalid` otherwise)."""
    epoch, wrap, request, member = answer.get("epoch"), answer.get("wrap"), answer.get("request"), answer.get("member")
    valid = (
        (epoch is None or (isinstance(epoch, dict) and _count(epoch.get("epoch")) and _text(epoch.get("key_check"))))
        and (wrap is None or (isinstance(wrap, dict) and _text(wrap.get("wrap")) and _text(wrap.get("sig"))))
        and (
            request is None
            or (
                isinstance(request, dict)
                and _text(request.get("id"))
                and (request.get("wrapped") is None or isinstance(request.get("wrapped"), dict))
            )
        )
        and isinstance(member, dict)
        and _count(member.get("last_read_seq"))
        and _count(answer.get("unread"))
        and _count(answer.get("head_seq"))
        and isinstance(answer.get("limits"), dict)
    )
    if not valid:
        raise unverified("response_invalid")
    return answer


def _join_and_read(account: Account) -> dict[str, Any]:
    account.call("POST", "/v1/community/join", {})
    return _read(account)


def _read(account: Account) -> dict[str, Any]:
    return _overview(account.call("GET", "/v1/community"))


def _hub_error(error: PlatformError) -> object:
    return error.fields.get("hub_error")


# --- the key flow ---


def _matches(secret: bytes, epoch: int, check: str) -> None:
    if not hmac.compare_digest(fmt.key_check(secret, epoch), b64u_decode(check, 32)):
        raise CryptoError("decrypt_failed")


def _self_wrap(account: Account, epoch: int, secret: bytes) -> dict[str, Any]:
    user = account.actor
    wrap = fmt.wrap_community_key(account.signer.kem_public(), epoch=epoch, recipient=user, key=secret)
    sig = fmt.sign_community_wrap(account.signer.sig, epoch=epoch, recipient=user, wrap_text=wrap)
    return {"epoch": epoch, "wrap": wrap, "sig": sig}


def _bootstrap(account: Account, retry: bool) -> Key:
    secret = os.urandom(32)
    body = {**_self_wrap(account, 1, secret), "key_check": b64u_encode(fmt.key_check(secret, 1))}
    try:
        account.call("POST", "/v1/community/epochs", body)
    except PlatformError as error:
        if _hub_error(error) != "community_epoch_exists" or not retry:
            raise
        return _flow(account, _read(account), retry=False)
    return Key(CREATED, 1, secret)


def _own(account: Account, epoch: int, check: str, wrap: dict[str, Any]) -> Key:
    user = account.actor
    try:
        text = str(wrap["wrap"])
        fmt.verify_community_wrap(
            account.signer.sig_public(), epoch=epoch, recipient=user, wrap_text=text, signature=str(wrap["sig"])
        )
        secret = fmt.unwrap_community_key(account.signer.kem, epoch=epoch, recipient=user, wrap_text=text)
        _matches(secret, epoch, check)
    except CryptoError as error:
        # a broken self-wrap cannot be repaired by a client (the hub refuses requests of holders)
        return Key(FAILED, epoch, code=error.code)
    return Key(HELD, epoch, secret)


def _mine(local: dict[str, Any]) -> tuple[str, X25519PrivateKey] | None:
    """The tool's own open request (ID and one-off key) from the state file, None without a sound one."""
    found = local.get("request")
    try:
        request = cast(dict[str, Any], found)
        return str(request["id"]), x25519_private(b64u_decode(request["kem_sk"], 32))
    except (CryptoError, KeyError, TypeError):
        return None


def _reject(account: Account) -> None:
    try:
        account.call("POST", "/v1/community/request/reject", {})
    except PlatformError as error:
        # `not_found`: the wrap is gone already (replaced or rejected by another client of the user)
        if _hub_error(error) != "not_found":
            raise


def _open(  # noqa: PLR0917
    account: Account, epoch: int, check: str, request_id: str, wrapped: dict[str, Any], kem: X25519PrivateKey
) -> Key:
    try:
        if wrapped.get("epoch") != epoch:
            raise CryptoError("epoch_stale")
        text = str(wrapped.get("wrap"))
        wrapper = b64u_decode(wrapped.get("wrapper_sig"), 32)
        fmt.verify_community_wrap(
            wrapper, epoch=epoch, recipient=request_id, wrap_text=text, signature=str(wrapped.get("sig"))
        )
        secret = fmt.unwrap_community_key(kem, epoch=epoch, recipient=request_id, wrap_text=text)
        _matches(secret, epoch, check)
    except CryptoError as error:
        # a wrong key or a forged wrap: the hub records the wrapper, the request stays open for another holder
        _reject(account)
        return Key(WAITING, epoch, request=request_id, code=error.code)
    account.call("POST", "/v1/community/wraps", _self_wrap(account, epoch, secret))
    local = _local(account)
    local.pop("request", None)
    _save(account, local)
    return Key(OPENED, epoch, secret)


def _new_request(account: Account, epoch: int) -> Key:
    kem = X25519PrivateKey.generate()
    request_id, kem_pub = uuid7(), b64u_encode(x25519_public_raw(kem))
    sig = fmt.sign_request(account.signer.sig, id=request_id, kem_pub=kem_pub, user=account.actor)
    local = _local(account)
    local.pop("foreign", None)
    local["request"] = {"id": request_id, "kem_sk": b64u_encode(x25519_private_raw(kem))}
    _save(account, local)
    account.call("PUT", "/v1/community/request", {"id": request_id, "kem_pub": kem_pub, "sig": sig})
    return Key(WAITING, epoch, request=request_id)


def _foreign_waits(account: Account, request_id: str) -> bool:
    """A wrapped request of another client of the user: wait `FOREIGN_S` from the first sight, then replace it."""
    local = _local(account)
    seen = local.get("foreign")
    if isinstance(seen, dict) and seen.get("id") == request_id and isinstance(seen.get("since"), float):
        return bool(time.time() - seen["since"] < FOREIGN_S)
    local["foreign"] = {"id": request_id, "since": time.time()}
    _save(account, local)
    return True


def _flow(account: Account, view: dict[str, Any], retry: bool = True) -> Key:
    epoch = view["epoch"]
    if epoch is None:
        return _bootstrap(account, retry)
    number, check = int(epoch["epoch"]), str(epoch["key_check"])
    if view["wrap"] is not None:
        return _own(account, number, check, view["wrap"])
    request, mine = view["request"], _mine(_local(account))
    if request is not None and mine is not None and request["id"] == mine[0]:
        if request["wrapped"] is not None:
            return _open(account, number, check, mine[0], request["wrapped"], mine[1])
        return Key(WAITING, number, request=mine[0])
    if request is not None and request["wrapped"] is not None and _foreign_waits(account, str(request["id"])):
        return Key(WAITING, number, request=str(request["id"]), code="other_client")
    return _new_request(account, number)


def ensure_key(account: Account) -> tuple[Key, dict[str, Any]]:
    """Join, read the state and run the key flow once (module docstring)."""
    view = _join_and_read(account)
    return _flow(account, view), view


def require_key(key: Key) -> bytes:
    if key.secret is None:
        raise PlatformError("community_key_missing", **key.summary())
    return key.secret


# --- reading ---


def _row(value: object) -> dict[str, Any]:
    if not (
        isinstance(value, dict)
        and isinstance(value.get("seq"), int)
        and _text(value.get("id"))
        and _text(value.get("author"))
        and _text(value.get("created_at"))
        and (value.get("deleted") is None or _text(value.get("deleted")))
    ):
        raise unverified("response_invalid")
    return value


def _page(answer: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    rows, authors, following = answer.get("messages"), answer.get("authors"), answer.get("next_before")
    if not isinstance(rows, list) or not isinstance(authors, dict) or not (following is None or _text(following)):
        raise unverified("response_invalid")
    known = {
        str(user): {"sig_pub": entry["sig_pub"], "moderator": entry.get("moderator") is True}
        for user, entry in authors.items()
        if isinstance(entry, dict) and _text(entry.get("sig_pub"))
    }
    return [_row(row) for row in rows], known, following


class Reader:
    """Verifies and opens the rows of pages for one viewer, with the author keys pinned in the state file."""

    def __init__(self, account: Account, key: Key) -> None:
        self.account = account
        self.key = key
        local = _local(account)
        pins = local.get("pins")
        self.pins: dict[str, str] = dict(pins) if isinstance(pins, dict) else {}
        self.changed = False

    def _pinned(self, author: str, sig_pub: str) -> bool:
        known = self.pins.get(author)
        if known is not None:
            return known == sig_pub
        self.pins[author] = sig_pub
        self.changed = True
        return True

    def item(self, row: dict[str, Any], author: dict[str, Any] | None) -> dict[str, Any]:
        user = str(row["author"])
        base: dict[str, Any] = {
            "seq": row["seq"],
            "id": row["id"],
            "author": user,
            "mark": mark(user),
            "own": user == self.account.actor,
            "moderator": author is not None and author["moderator"],
            "created_at": row["created_at"],
            "deleted": row["deleted"],
            "name": None,
            "text": None,
        }
        if row["deleted"] is not None or row.get("message") is None:
            return {**base, "status": "removed"}
        body = self._open(row, author)
        if isinstance(body, str):
            return {**base, "status": body}
        return {**base, "status": "verified", "name": body["name"], "text": body["text"]}

    def _open(self, row: dict[str, Any], author: dict[str, Any] | None) -> dict[str, Any] | str:
        """The checked body, or the status of a row that does not open (`unverifiable`, `locked` without a key)."""
        if author is None:
            return "unverifiable"
        try:
            sig_public = b64u_decode(author["sig_pub"], 32)
            header = fmt.verify_message(row["message"], sig_public)
            same = header["id"] == row["id"] and header["author"] == row["author"]
            if not same or not self._pinned(str(row["author"]), str(author["sig_pub"])):
                raise CryptoError("signature_invalid")
            if self.key.secret is None:
                return "locked"
            if header["epoch"] != self.key.epoch:
                raise CryptoError("epoch_stale")
            return fmt.open_message(self.key.secret, row["message"], sig_public)
        except CryptoError:
            return "unverifiable"

    def page(self, query: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """One page (`GET /v1/community/messages`), oldest first."""
        rows, authors, following = _page(self.account.call("GET", f"/v1/community/messages?{urlencode(query)}"))
        items = sorted((self.item(row, authors.get(str(row["author"]))) for row in rows), key=lambda item: item["seq"])
        return items, following

    def keep_pins(self) -> None:
        if self.changed:
            _save(self.account, {**_local(self.account), "pins": self.pins})
            self.changed = False


def community_messages(
    account: Account, before: int | None, after: int | None, limit: int, mark_read: bool
) -> dict[str, Any]:
    if not 1 <= limit <= MAX_PAGE or (before is not None and before < 0) or (after is not None and after < 0):
        raise PlatformError("invalid_argument")
    key, _view = ensure_key(account)
    reader = Reader(account, key)
    query: dict[str, Any] = {"limit": limit}
    query |= {"before": before} if before is not None else {}
    query |= {"after": after} if after is not None else {}
    items, following = reader.page(query)
    reader.keep_pins()
    result: dict[str, Any] = {**key.summary(), "messages": items, "next_before": following}
    if mark_read and items:
        answer = account.call("PUT", "/v1/community/read", {"seq": items[-1]["seq"]})
        result["last_read_seq"] = answer.get("last_read_seq")
    return result


def community_status(account: Account) -> dict[str, Any]:
    """Join and the state (`GET /v1/community`) without the key flow."""
    view = _join_and_read(account)
    epoch, request, member = view["epoch"], view["request"], view["member"]
    mine = _mine(_local(account))
    return {
        "user_id": account.actor,
        "mark": mark(account.actor),
        "epoch": None if epoch is None else epoch["epoch"],
        "key_held": view["wrap"] is not None,
        "request": None
        if request is None
        else {
            "id": request["id"],
            "wrapped": request["wrapped"] is not None,
            "this_agent": mine is not None and mine[0] == request["id"],
        },
        "joined": member.get("joined") is True,
        "muted": member.get("muted") is True,
        "posted": member.get("posted") is True,
        "moderator": view.get("moderator") is True,
        "unread": view["unread"],
        "last_read_seq": member["last_read_seq"],
        "head_seq": view["head_seq"],
        "limits": view["limits"],
    }


# --- the key commands ---


def community_key(account: Account, wait: float) -> dict[str, Any]:
    """The key flow; with `wait` > 0 repeated every `POLL_S` while the key is awaited from another user."""
    if wait < 0:
        raise PlatformError("invalid_argument")
    deadline = time.monotonic() + wait
    key = _flow(account, _join_and_read(account))
    while key.status == WAITING and time.monotonic() < deadline:
        time.sleep(POLL_S)
        key = _flow(account, _read(account))
    return key.summary()


def community_serve(account: Account) -> dict[str, Any]:
    """Seal the key to open requests of others (holders only; at most `SERVE_ROUNDS` pages)."""
    key, _view = ensure_key(account)
    result: dict[str, Any] = {**key.summary(), "offered": 0, "stored": 0, "skipped": 0}
    if key.secret is None:
        return result
    epoch = cast(int, key.epoch)
    for _round in range(SERVE_ROUNDS):
        found = account.call("GET", f"/v1/community/requests?limit={REQUESTS}").get("requests")
        if not isinstance(found, list):
            raise unverified("response_invalid")
        wraps = []
        for request in found:
            request_id = request.get("id") if isinstance(request, dict) else None
            try:
                if not is_uuid7(request_id):
                    raise CryptoError("header_invalid")
                kem_pub = b64u_decode(cast(dict[str, Any], request).get("kem_pub"), 32)
                wrap = fmt.wrap_community_key(kem_pub, epoch=epoch, recipient=str(request_id), key=key.secret)
            except (CryptoError, ValueError):
                # an entry that cannot be sealed is skipped (14-community.md section 4.2)
                result["skipped"] += 1
                continue
            sig = fmt.sign_community_wrap(account.signer.sig, epoch=epoch, recipient=str(request_id), wrap_text=wrap)
            wraps.append({"request": request_id, "epoch": epoch, "wrap": wrap, "sig": sig})
        result["offered"] += len(found)
        if wraps:
            answer = account.call("POST", "/v1/community/requests/wraps", {"wraps": wraps})
            result["stored"] += int(answer.get("stored", 0))
            result["skipped"] += int(answer.get("skipped", 0))
        if len(found) < REQUESTS:
            break
    return result


# --- writing ---


def default_name(account: Account, reader: Reader) -> str:
    """The name of the last post of this tool, else of the user's newest own message on the newest page, else the
    user's display name in the first organization whose member profile verifies (cut to the limit)."""
    remembered = _local(account).get("name")
    if isinstance(remembered, str) and remembered:
        return remembered
    items, _following = reader.page({"limit": PAGE})
    own = [item["name"] for item in items if item["own"] and item["name"] is not None]
    if own:
        return str(own[-1])
    profile = account.display_name() or ""
    name = clean_name(clean_name(profile)[: fmt.MAX_NAME_CHARS])
    if not name:
        raise PlatformError("invalid_argument", reason="name_missing")
    return name


def _sealed(account: Account, key: Key, name: str, text: str, kept: object) -> dict[str, Any]:
    if isinstance(kept, dict) and (kept.get("name"), kept.get("text"), kept.get("epoch")) == (name, text, key.epoch):
        return cast(dict[str, Any], kept["message"])
    try:
        body = fmt.body_bytes(name, text)
    except CryptoError:
        raise PlatformError("invalid_argument", reason="community_body") from None
    header = fmt.new_header(
        id=uuid7(), author=account.actor, epoch=cast(int, key.epoch), ts=format_time(datetime.now(UTC))
    )
    return fmt.seal_message(require_key(key), header, body, account.signer.sig)


def community_post(account: Account, text: str, name: str | None) -> dict[str, Any]:
    key, _view = ensure_key(account)
    require_key(key)
    reader = Reader(account, key)
    chosen = clean_name(name if name is not None else default_name(account, reader))
    reader.keep_pins()
    cleaned = clean_text(text)
    local = _local(account)
    message = _sealed(account, key, chosen, cleaned, local.get("kept"))
    try:
        answer = account.call("POST", "/v1/community/messages", {"message": message})
    except PlatformError as error:
        limited = _hub_error(error) == "rate_limited"
        if error.code == "hub_unreachable" or limited:
            local["kept"] = {"name": chosen, "text": cleaned, "epoch": key.epoch, "message": message}
        else:
            local.pop("kept", None)
        _save(account, local)
        if limited and error.fields.get("retry_after") is None:
            raise PlatformError("hub_refused", **{**error.fields, "retry_after": RETRY_FALLBACK_S}) from None
        raise
    local.pop("kept", None)
    local["name"] = chosen
    _save(account, local)
    header = cast(dict[str, Any], message["header"])
    return {"id": header["id"], "seq": answer.get("seq"), "replayed": answer.get("replayed") is True, "name": chosen}


def community_delete(account: Account, message: str) -> dict[str, Any]:
    answer = account.call("DELETE", f"/v1/community/messages/{segment(_id(message))}")
    return {"id": message, "deleted": answer.get("deleted")}


# --- moderation ---


def community_mutes(account: Account) -> dict[str, Any]:
    found = account.call("GET", "/v1/community/mutes").get("mutes")
    if not isinstance(found, list):
        raise unverified("response_invalid")
    rows = [row for row in found if isinstance(row, dict) and _text(row.get("user"))]
    mutes = [{"user": row["user"], "mark": mark(row["user"]), "muted_at": row.get("muted_at")} for row in rows]
    return {"mutes": mutes}


def community_mute(account: Account, user: str, muted: bool) -> dict[str, Any]:
    path = f"/v1/community/mutes/{segment(_id(user))}"
    answer = account.call("PUT", path, {}) if muted else account.call("DELETE", path)
    return {"user": user, "mark": mark(user), "muted": answer.get("muted") is True}


# --- command line ---


def _status(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_status(account)


def _key(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_key(account, args.wait)


def _serve(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_serve(account)


def _messages(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_messages(account, args.before, args.after, args.limit, args.mark_read)


def _post(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_post(account, text_argument(args, context), args.name)


def _delete(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_delete(account, args.id)


def _mutes(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_mutes(account)


def _mute(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_mute(account, args.user, True)


def _unmute(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return community_mute(account, args.user, False)


def register(tree: Tree) -> None:
    group = tree.group("community", "the Community: one group chat of all users (end-to-end encrypted)")
    tree.command(
        group,
        "status",
        _status,
        "join; epoch, key, request, mute, moderator, unread count",
        example="agent-platform community status",
        words="gemeinschaft gruppenchat community group chat forum join",
        then="agent-platform community key --wait 120",
    )
    key = tree.command(
        group,
        "key",
        _key,
        "obtain the community key (create, open, request)",
        example="agent-platform community key [--wait 120]",
        words="gemeinschaftsschlüssel community-schlüssel community key obtain",
    )
    key.add_argument("--wait", type=float, default=0.0, help="seconds to wait for the key from another user")
    tree.command(
        group,
        "serve",
        _serve,
        "hand the community key to users who wait for it",
        example="agent-platform community serve",
        words="schlüssel weitergeben community hand out key waiting users",
    )
    messages = tree.command(
        group,
        "messages",
        _messages,
        "verified and decrypted messages, oldest first",
        example="agent-platform community messages [--before <next_before> | --after <seq>] [--limit 50] [--mark-read]",
        words="gruppenchat lesen community nachrichten messages read group chat",
    )
    where = messages.add_mutually_exclusive_group()
    where.add_argument("--before", type=int, help="older messages: the page before this seq (see next_before)")
    where.add_argument("--after", type=int, help="newer messages: every message after this seq")
    messages.add_argument("--limit", type=int, default=PAGE, help=f"1-{MAX_PAGE} (default {PAGE})")
    messages.add_argument("--mark-read", action="store_true", help="set the read position to the newest one shown")
    post = tree.command(
        group,
        "post",
        _post,
        "write a message (readable by every user of the platform)",
        example='agent-platform community post --text - [--name "Ada"] < message.txt',
        words="gruppenchat schreiben community posten write post message group chat",
    )
    post.add_argument("--text", required=True, help="the text, or - to read it from stdin")
    post.add_argument("--name", help="the display name (default: the one used before, else the profile name)")
    delete = tree.command(
        group,
        "delete",
        _delete,
        "delete an own message (moderators: any message)",
        example="agent-platform community delete --id <message_id>",
        words="beitrag löschen community nachricht delete message post remove",
    )
    delete.add_argument("--id", required=True, help="the message ID")
    tree.command(
        group,
        "mutes",
        _mutes,
        "the muted users (moderators)",
        example="agent-platform community mutes",
        words="stummgeschaltete nutzer community moderation muted users list",
    )
    for name, work, text, words in (
        ("mute", _mute, "mute a user (moderators)", "stummschalten sperren community moderation mute silence"),
        ("unmute", _unmute, "unmute a user", "stummschaltung aufheben community moderation unmute"),
    ):
        sub = tree.command(
            group, name, work, text, example=f"agent-platform community {name} --user <author>", words=words
        )
        sub.add_argument("--user", required=True, help="the user ID (the author of a message)")
