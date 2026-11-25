"""The platform browser of a session (ADR-085 section 7, 13-work-vm.md sections 13.5 and 13.6; twin of the chat card
of `browser.state`, the session's Browser tab with its action record and its live picture).

- `parse_state` and `parse_action`: decrypted `browser.state` and `browser.action` bodies checked against
  13-work-vm.md section 13.5 (every field present, no other field); anything else is None and counted as `invalid`;
- `browser list --session|--task`: the browser sessions, the latest `browser.state` per `browser_id`, newest first.
  The events are the task's events of the workgroup scope, opened and verified like the chat; an event of another
  task, session or kind the hub slips in is left out (`unverified`);
- `browser show --session [--browser]`: the action record, read with `kinds=browser.action` (a watched kind, never in
  a default read) and `session=`, in record order; each entry with its picture's size, never the picture itself;
- `browser frames --session [--browser] --output DIR`: the recorded pictures as `<browser_id>-<seq>.jpg`;
- `browser frames --session --follow --output DIR [--kbps] [--seconds]`: watches live over `GET /v1/stream` with
  `browser=<session>` (an unsigned streaming answer; a refusal is a signed answer and verified). Each `browser_live`
  frame is opened with the workgroup key of its epoch (04-crypto.md section 4.13, `browserframe.open_frame`) and
  written as `live-<seq>.jpg`; a frame of another browser session than the session's newest open one (or the one
  named), or whose `seq` is not above the last written, is dropped. It stops after `--seconds`, on Ctrl-C or when
  the hub ends the stream.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_platform import workvm
from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_all, pull
from agent_platform.hub import key_id, refused
from agent_platform.orgs import find_org
from agent_platform.sessions import session_row, task_row
from agent_runner.crypto import browserframe
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import WG
from agent_runner.sync.codes import HUB_ERROR_CODES
from agent_runner.sync.http import HubTransportError, Signer, hub_origin, verify_answer

STATE_KIND = "browser.state"
ACTION_KIND = "browser.action"
STATES = ("starting", "open", "restarted", "closed")
REASONS = (
    "browser_closed",
    "browser_idle",
    "vm_lost",
    "session_ended",
    "task_closed",
    "credit_exhausted",
    "restart",
    "failed",
)
OMITTED = ("too_large", "unavailable")
IMAGE_BYTES = 160 * 1024
IMAGE_WIDTH = 960
KBPS_DEFAULT, KBPS_MIN, KBPS_MAX = 1000, 100, 10000
SECONDS_DEFAULT, SECONDS_MAX = 60, 3600
CONNECT_TIMEOUT_S = 30.0
MAX_ERROR_BYTES = 64 * 1024
MAX_LINE = 1024 * 1024
"""A stream line longer than this (a live frame is at most 512 KiB of picture) is dropped."""
LIVE_FRAME = "browser_live"

_BROWSER_ID = workvm._pattern(browserframe.BROWSER_RE.pattern)
_COUNT = workvm._COUNT
_BOOL = workvm._one_of(True, False)


def _image(value: object) -> bool:
    """`{type: "image/jpeg", width, height, data}` with at most `IMAGE_WIDTH` px and `IMAGE_BYTES` decoded, or null."""
    if value is None:
        return True
    shape: Mapping[str, workvm.Check] = {
        "type": workvm._one_of("image/jpeg"),
        "width": workvm._integer(1, IMAGE_WIDTH),
        "height": workvm._integer(1, 100_000),
        "data": workvm._text(IMAGE_BYTES * 2),
    }
    if not workvm._matches(value, shape):
        return False
    try:
        return len(b64u_decode(cast(dict[str, Any], value)["data"])) <= IMAGE_BYTES
    except CryptoError:
        return False


STATE_SHAPE: Mapping[str, workvm.Check] = {
    "browser_id": _BROWSER_ID,
    "operation_id": workvm._nullable(workvm._UUID),
    "implicit": _BOOL,
    "state": workvm._one_of(*STATES),
    "workvm_id": workvm._nullable(workvm._WORKVM_ID),
    "reason": workvm._nullable(workvm._one_of(*REASONS)),
    "url": workvm._nullable(workvm._text(8192)),
    "actions": _COUNT,
    "ready_ms": workvm._nullable(_COUNT),
    "idle_seconds": workvm._integer(60, 3600),
    "opened_at": workvm._nullable(workvm._time),
    "closed_at": workvm._nullable(workvm._time),
}
ACTION_SHAPE: Mapping[str, workvm.Check] = {
    "browser_id": _BROWSER_ID,
    "seq": _COUNT,
    "tool": workvm._pattern(r"browser_[a-z]{1,32}"),
    "action": workvm._text(64),
    "target": workvm._nullable(workvm._text(1024)),
    "text": workvm._nullable(workvm._text(1024)),
    "url": workvm._nullable(workvm._text(8192)),
    "title": workvm._nullable(workvm._text(1024)),
    "tab": workvm._nullable(workvm._integer(0, 63)),
    "ok": _BOOL,
    "error": workvm._nullable(workvm._text(2048)),
    "notice": workvm._nullable(workvm._text(1024)),
    "at": workvm._time,
    "duration_ms": _COUNT,
    "image": _image,
    "image_omitted": workvm._nullable(workvm._one_of(*OMITTED)),
}
LIST_FIELDS = tuple(STATE_SHAPE)
SHOW_FIELDS = tuple(key for key in ACTION_SHAPE if key != "image")


def parse_state(body: object) -> dict[str, Any] | None:
    """A `browser.state` body, or None when it does not follow 13-work-vm.md section 13.5."""
    return cast(dict[str, Any], body) if workvm._matches(body, STATE_SHAPE) else None


def parse_action(body: object) -> dict[str, Any] | None:
    """A `browser.action` body, or None when it does not follow 13-work-vm.md section 13.5."""
    return cast(dict[str, Any], body) if workvm._matches(body, ACTION_SHAPE) else None


# --- reading the events ---


@dataclass
class Found:
    """The verified events of one kind of a task (of one session when named), in event order."""

    org: str
    task: str
    wg: str
    session: str | None
    items: list[tuple[dict[str, Any], dict[str, Any]]]
    """(verified event without its body, checked body)"""
    locked: int
    invalid: int
    unverified: int

    def counts(self) -> dict[str, int]:
        return {"locked": self.locked, "invalid": self.invalid, "unverified": self.unverified}


def _place(account: Account, session: str | None, task: str | None) -> tuple[str, str]:
    """`(task, wg)` of the session or of the task (exactly one of them)."""
    if (session is None) == (task is None):
        raise PlatformError("invalid_argument", reason="session_or_task")
    if session is not None:
        row = session_row(account, session)["session"]
        if not isinstance(row.get("task_id"), str):
            raise unverified("response_invalid")
        return str(row["task_id"]), str(row["wg_id"])
    row = task_row(account, cast(str, task))
    return str(row["task_id"]), str(row["wg_id"])


def _read(account: Account, kind: str, session: str | None, task: str | None, browser: str | None = None) -> Found:
    """The checked `kind` events of the task (or session), of the browser session `browser` only if named."""
    if browser is not None and not _BROWSER_ID(browser):
        raise PlatformError("invalid_argument", reason="browser_id")
    task_id, wg = _place(account, session, task)
    query = {"kinds": kind, "task": task_id, **({} if session is None else {"session": session})}
    items, _last = pull(account, wg, query)
    org = find_org(account, wg)
    events, skipped = open_all(account, org, (WG, wg), items)
    found = Found(org, task_id, wg, session, [], 0, 0, skipped)
    parse = parse_action if kind == ACTION_KIND else parse_state
    for event in sorted(events, key=lambda opened: int(opened["seq"])):
        if event.get("locked"):
            found.locked += 1
            continue
        if event["task"] != task_id or event["kind"] != kind or (session is not None and event["session"] != session):
            found.unverified += 1  # slipped in by the hub
            continue
        body = parse(event.get("body"))
        if body is None:
            found.invalid += 1
            continue
        if browser is None or body["browser_id"] == browser:
            found.items.append(({name: value for name, value in event.items() if name != "body"}, body))
    return found


def browsers(account: Account, session: str | None, task: str | None) -> dict[str, Any]:
    """The browser sessions of a session or task: the latest `browser.state` per browser, newest opened first."""
    found = _read(account, STATE_KIND, session, task)
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for event, body in found.items:
        latest[body["browser_id"]] = (event, body)  # keeps the place of the first event, the latest values
    rows = [
        {"session_id": event["session"], **{name: body[name] for name in LIST_FIELDS}}
        for event, body in reversed(latest.values())
    ]
    return {"task_id": found.task, "session_id": session, "browsers": rows, **found.counts()}


def _picture(body: Mapping[str, Any]) -> dict[str, Any] | None:
    image = body["image"]
    if image is None:
        return None
    return {"width": image["width"], "height": image["height"], "bytes": len(b64u_decode(image["data"]))}


def record(account: Account, session: str, browser: str | None) -> dict[str, Any]:
    """The action record of the session's browser sessions (or of `browser`), in record order."""
    found = _read(account, ACTION_KIND, session, None, browser)
    actions = [{**{name: body[name] for name in SHOW_FIELDS}, "image": _picture(body)} for _event, body in found.items]
    return {"task_id": found.task, "session_id": session, "browser_id": browser, "actions": actions, **found.counts()}


# --- writing pictures ---


def _directory(output: str) -> None:
    try:
        os.makedirs(output, mode=0o700, exist_ok=True)
    except OSError:
        raise PlatformError("invalid_argument", reason="output_unwritable") from None


def _write(output: str, name: str, data: bytes) -> dict[str, Any]:
    """`data` into `output/name` (created or replaced, mode 0600, never through a link)."""
    path = os.path.join(output, name)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except OSError:
        raise PlatformError("invalid_argument", reason="output_unwritable", file=path) from None
    return {"file": path, "bytes": len(data)}


def recorded_frames(account: Account, session: str, browser: str | None, output: str) -> dict[str, Any]:
    """The pictures of the action record as `<browser_id>-<seq>.jpg` in `output`."""
    found = _read(account, ACTION_KIND, session, None, browser)
    _directory(output)
    written: list[dict[str, Any]] = []
    without = 0
    for _event, body in found.items:
        if body["image"] is None:
            without += 1
            continue
        name = f"{body['browser_id']}-{body['seq']}.jpg"
        data = b64u_decode(body["image"]["data"])
        written.append({"browser_id": body["browser_id"], "seq": body["seq"], **_write(output, name, data)})
    return {
        "task_id": found.task,
        "session_id": session,
        "output": output,
        "frames": written,
        "without_picture": without,
        **found.counts(),
    }


# --- live ---


def _target(account: Account, session: str, browser: str | None) -> tuple[Found, str]:
    """The session's place and the browser session whose live frames count: the named one, else the newest open."""
    found = _read(account, STATE_KIND, session, None, browser)
    if browser is not None:
        return found, browser
    latest: dict[str, str] = {}
    for _event, body in found.items:
        latest.pop(body["browser_id"], None)
        latest[body["browser_id"]] = body["state"]
    open_ones = [ident for ident, state in latest.items() if state != "closed"]
    if not open_ones:
        raise PlatformError("not_found", reason="no_open_browser", session_id=session)
    return found, open_ones[-1]


def _lines(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """The `(event, data)` frames of a server-sent event stream; comments (heartbeats) and oversized lines dropped."""
    name, data = "", ""
    for line in lines:
        if line == "":
            if name:
                yield name, data
            name, data = "", ""
        elif len(line) > MAX_LINE:
            name = ""
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = line[6:]


def _reader(response: httpx.Response, inbox: queue.Queue[tuple[str, str] | None]) -> None:
    """Puts the stream's frames into `inbox`, None when the stream ended (or broke)."""
    try:
        for frame in _lines(response.iter_lines()):
            inbox.put(frame)
    except (httpx.HTTPError, OSError):
        pass
    finally:
        inbox.put(None)


def _take(inbox: queue.Queue[tuple[str, str] | None], timeout: float) -> tuple[str, str] | None:
    return inbox.get(timeout=timeout)


def _refusal(account: Account, signer: Signer, signed: Any, response: httpx.Response) -> PlatformError:
    """The error of a refused stream: a signed answer, verified like every other answer."""
    body = response.read()[:MAX_ERROR_BYTES]
    hub_key = Ed25519PublicKey.from_public_bytes(b64u_decode(account.state["hub_key"]["public_key"], 32))
    try:
        verify_answer(
            hub_key,
            signed,
            signer.key_id,
            status=response.status_code,
            headers=response.headers,
            body=body,
            now=signer.wall(),
        )
    except HubTransportError as error:
        return unverified(error.reason)
    try:
        code = json.loads(body).get("error")
    except (ValueError, AttributeError):
        code = None
    if isinstance(code, str) and code in HUB_ERROR_CODES:
        return refused(code, response.status_code)
    return unverified("response_invalid")


def _connect(account: Account, target: str) -> tuple[httpx.Client, httpx.Response]:
    """`GET target` as a signed streaming request; the open response of a `200`."""
    origin = hub_origin(account.origin)
    signer = Signer(key_id=key_id(account.agent_id), signing_key=account.agent.sig, host=origin.host, wall=time.time)
    signed = signer.sign("GET", target, b"")
    headers = {**signed.headers, "Accept": "text/event-stream", "Accept-Encoding": "identity"}
    timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=None)
    client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
    try:
        response = client.send(client.build_request("GET", origin.base_url + target, headers=headers), stream=True)
        if response.status_code != 200:
            raise _refusal(account, signer, signed, response)
    except httpx.HTTPError:
        client.close()
        raise PlatformError("hub_unreachable") from None
    except PlatformError:
        client.close()
        raise
    return client, response


@dataclass
class Live:
    """What the follower wrote and left out."""

    output: str
    session: str
    wg: str
    browser: str
    ring: dict[tuple[str, str, int], bytes]
    frames: list[dict[str, Any]]
    last: int = -1
    dropped: int = 0
    locked: int = 0
    invalid: int = 0

    def take(self, data: str) -> None:
        """One `browser_live` frame: opened, checked against the watched browser session and written."""
        try:
            found = json.loads(data)
            frame = found["frame"] if isinstance(found, dict) and found.get("session_id") == self.session else None
        except (ValueError, KeyError):
            frame = None
        if frame is None:
            self.invalid += 1
            return
        try:
            checked = browserframe.check_frame(frame, session=self.session, wg=self.wg)
            key = self.ring.get((WG, self.wg, cast(int, checked["epoch"])))
            if key is None:
                self.locked += 1
                return
            picture = browserframe.open_frame(key, frame, session=self.session, wg=self.wg)
        except CryptoError:
            self.invalid += 1
            return
        seq = cast(int, checked["seq"])
        if checked["browser"] != self.browser or seq <= self.last:
            self.dropped += 1
            return
        self.last = seq
        self.frames.append({"seq": seq, "ts": checked["ts"], **_write(self.output, f"live-{seq}.jpg", picture)})


def follow(
    account: Account,
    session: str,
    browser: str | None,
    output: str,
    *,
    kbps: int | None,
    seconds: int | None,
) -> dict[str, Any]:
    """Watches the session's browser live for `seconds` and writes its frames (module docstring)."""
    rate = KBPS_DEFAULT if kbps is None else kbps
    span = SECONDS_DEFAULT if seconds is None else seconds
    if not KBPS_MIN <= rate <= KBPS_MAX or not 1 <= span <= SECONDS_MAX:
        raise PlatformError("invalid_argument", reason="kbps_or_seconds")
    found, watched = _target(account, session, browser)
    _directory(output)
    live = Live(output, session, found.wg, watched, account.keys(found.org), [])
    query = urlencode({"org": found.org, "browser": session, "browser_kbps": rate})
    client, response = _connect(account, f"/v1/stream?{query}")
    inbox: queue.Queue[tuple[str, str] | None] = queue.Queue()
    threading.Thread(target=_reader, args=(response, inbox), daemon=True).start()
    deadline = time.monotonic() + span
    ended = "deadline"
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                item = _take(inbox, remaining)
            except queue.Empty:
                continue
            if item is None:
                ended = "stream_closed"
                break
            if item[0] == LIVE_FRAME:
                live.take(item[1])
    except KeyboardInterrupt:
        ended = "interrupted"
    finally:
        response.close()
        client.close()
    return {
        "task_id": found.task,
        "session_id": session,
        "browser_id": watched,
        "output": output,
        "kbps": rate,
        "ended": ended,
        "frames": live.frames,
        "dropped": live.dropped,
        "locked": live.locked,
        "invalid": live.invalid,
    }


# --- commands ---


def _list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return browsers(account, args.session, args.task)


def _show(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return record(account, args.session, args.browser)


def _frames(account: Account, args: Any, context: Context) -> dict[str, Any]:
    if args.follow:
        return follow(account, args.session, args.browser, args.output, kbps=args.kbps, seconds=args.seconds)
    if args.kbps is not None or args.seconds is not None:
        raise PlatformError("invalid_argument", reason="follow_only")
    return recorded_frames(account, args.session, args.browser, args.output)


def register(tree: Tree) -> None:
    group = tree.group("browser", "the platform browser of sessions: browser sessions, action record, pictures, live")
    listed = tree.command(
        group,
        "list",
        _list,
        "the browser sessions of a session or task, newest first: state, URL, actions, opened and closed, reason, VM",
        example="agent-platform browser list --session <session_id>",
        words="browser browsersitzung webseite surfen geöffnet geschlossen browser sessions web page opened closed",
        then="agent-platform browser show --session <session_id>",
        trim="first",
    )
    listed.add_argument("--session", help="the browser sessions of this session")
    listed.add_argument("--task", help="the browser sessions of every session of this task")
    shown = tree.command(
        group,
        "show",
        _show,
        "the action record of a session's browser: each click, input and page with result and picture size",
        example="agent-platform browser show --session <session_id> [--browser <browser_id>] [--limit 20]",
        words="browserprotokoll aktionen klicks eingaben nachvollziehen verlauf browser record actions clicks history",
        then="agent-platform browser frames --session <session_id> --output pictures",
        trim="last",
    )
    shown.add_argument("--session", required=True)
    shown.add_argument("--browser", help="only this browser session (br-…)")
    pictures = tree.command(
        group,
        "frames",
        _frames,
        "the pictures of a session's browser as JPEG files: the recorded ones, or with --follow the live picture",
        example="agent-platform browser frames --session <session_id> --output pictures",
        words="bilder screenshots livebild zuschauen mitschauen browser pictures screenshots live watch frames",
    )
    pictures.add_argument("--session", required=True)
    pictures.add_argument("--browser", help="only this browser session (br-…)")
    pictures.add_argument("--output", required=True, help="the directory for the JPEG files (created when missing)")
    pictures.add_argument("--follow", action="store_true", help="watch live: write each new frame as live-<seq>.jpg")
    pictures.add_argument("--kbps", type=int, help=f"live bandwidth in kbit/s ({KBPS_MIN}-{KBPS_MAX}, {KBPS_DEFAULT})")
    pictures.add_argument("--seconds", type=int, help=f"watch this long (1-{SECONDS_MAX}, default {SECONDS_DEFAULT})")
