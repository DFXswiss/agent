"""``agent-guest-control`` (root): control socket, ``api.sock``, journal owner (05-runner.md §5.1, 03-api.md §7).

Started by the unit ``agent-guest-control.service`` as ``python3 -m agent_guest.control``:

* ``/run/agent-guest/control.sock`` (root:``agent-ctl`` 0660): HTTP/1.1 for the runner host through Lima's socket
  forward; every request needs ``Authorization: Bearer <control token>``. The token is read from
  ``/etc/agent-guest/control-token`` per request (written by ``agent-guest-harden.service`` from the Lima parameter
  at the first boot of a clone) and compared through SHA-256 digests with ``hmac.compare_digest``; a missing or
  empty file authorizes nobody.
* ``/run/agent-guest/api.sock`` (root:``platform`` 0660): JSON lines for the API process (``apisock.py``).
* the journal (``journal.py``), the session cache and assignment (``state.py``), pending calls; acknowledged
  records are pruned, and pending calls expired (``guest.error pending_expired``), at start and every
  ``PRUNE_SECONDS``.

Start-up hooks: ``control_routes/*:STARTUP`` (``hook(context)``, discovered in sorted module order) run once at the
end of ``ControlService.start``, after both sockets serve, so a restarted process (``Restart=always``) attaches
again to the sessions that kept running: agent watchers, terminal and transcript capture, TUI-ready watchers
(KOR-02). A failing hook is logged and never stops the process; a ``STARTUP`` that is not callable is
``registry_invalid``.

Routes come from ``control_routes/*:ROUTES``, api.sock ops from ``control_routes/*:API_OPS`` plus the core ops
``hello`` and ``session.resolve`` (registry.py). ``session.resolve`` binds a token to the UID of the connecting
process and counts, limits and journals failed checks (``authguard.py``, ATK-36). Startup failures print one line
``agent-guest-control: <code> <item>`` to stderr and exit 1 (systemd restarts the unit).
"""

from __future__ import annotations

import grp
import hashlib
import hmac
import json
import os
import re
import signal
import socketserver
import stat
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from types import FrameType
from typing import Any

from agent_guest import __version__, registry
from agent_guest.apisock import ApiSockServer
from agent_guest.authguard import AuthGuard
from agent_guest.clock import Clock, SystemClock
from agent_guest.config import ConfigError, GuestConfig, load_config
from agent_guest.journal import Journal, JournalError
from agent_guest.routing import Handler, Router
from agent_guest.schema import SchemaError, SchemaSet
from agent_guest.state import INACTIVE_STATES, Pending, State, StateError

PRUNE_SECONDS = 600.0
POLL_SECONDS = 0.05
SOCKET_MODE = 0o660
UNKNOWN_VERSION = "unknown"
_BOOT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


class StartupError(Exception):
    def __init__(self, code: str, item: str) -> None:
        super().__init__(f"{code} {item}")
        self.code = code
        self.item = item


@dataclass(frozen=True)
class GuestInfo:
    """Facts of this VM that do not change while the process runs (``GET /v1/health``)."""

    guest_version: str
    claude_version: str
    landlock_abi: int
    boot_id: str


@dataclass
class ControlContext:
    """Handler context of every control route and api.sock op."""

    config: GuestConfig
    schemas: SchemaSet
    journal: Journal
    state: State
    pending: Pending
    info: GuestInfo
    guard: AuthGuard = field(default_factory=lambda: AuthGuard(time.monotonic))
    closers: list[Callable[[], None]] = field(default_factory=list)
    closed: bool = False

    def on_close(self, closer: Callable[[], None]) -> None:
        """Register ``closer`` to run when the control process stops (``close``), before the journal closes; a part
        created while or after the process closes is closed at once."""
        if self.closed:
            _close(closer)
            return
        self.closers.append(closer)

    def close(self) -> None:
        """Run the closers once, in the order of registration: the session manager, which emits the session events,
        stops before the parts that handle them (capture, delivery). A failing closer is logged; the others run."""
        self.closed = True
        while self.closers:
            _close(self.closers.pop(0))


def _close(closer: Callable[[], None]) -> None:
    try:
        closer()
    except Exception:
        sys.stderr.write("agent-guest-control: close failed\n")
        traceback.print_exc(file=sys.stderr)


def _json_file(path: str, name: str) -> dict[str, Any] | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        raise StartupError("config_invalid", name) from None
    try:
        value = json.loads(text)
    except ValueError:
        raise StartupError("config_invalid", name) from None
    if not isinstance(value, dict):
        raise StartupError("config_invalid", name)
    return value


def load_info(config: GuestConfig) -> GuestInfo:
    """Versions from ``build.json``/``install.json`` (optional files) and the boot ID (required)."""
    build = _json_file(config.build_file, "BUILD_FILE") or {}
    abi = build.get("landlock_abi", 0)
    if type(abi) is not int or not 0 <= abi <= 255:
        raise StartupError("config_invalid", "BUILD_FILE")
    install = _json_file(config.claude_install_file, "CLAUDE_INSTALL_FILE") or {}
    claude = install.get("version", UNKNOWN_VERSION)
    if not isinstance(claude, str) or not _VERSION_RE.fullmatch(claude):
        raise StartupError("config_invalid", "CLAUDE_INSTALL_FILE")
    try:
        boot_id = Path(config.boot_id_file).read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        raise StartupError("config_invalid", "BOOT_ID_FILE") from None
    if not _BOOT_ID_RE.fullmatch(boot_id):
        raise StartupError("config_invalid", "BOOT_ID_FILE")
    return GuestInfo(guest_version=__version__, claude_version=claude, landlock_abi=abi, boot_id=boot_id)


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def token_authorizer(token_file: str) -> Any:
    """``authorize(headers) -> bool`` for ``Authorization: Bearer <token>`` against the token file."""

    def authorize(headers: Message) -> bool:
        values = headers.get_all("Authorization") or []
        scheme, _, given = (values[0] if len(values) == 1 else "").partition(" ")
        try:
            expected = Path(token_file).read_bytes().strip()
        except OSError:
            expected = b""
        match = hmac.compare_digest(_digest(given.encode("utf-8", "replace")), _digest(expected))
        return match and bool(expected) and scheme.lower() == "bearer"

    return authorize


def _hello(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"guest_version": context.info.guest_version}


def _owner(context: ControlContext, uid: int | None) -> str | None:
    """The active session of the connecting UID (the subject of a failed check), if any."""
    if uid is None:
        return None
    found = [s.id for s in context.state.sessions() if s.uid == uid and s.state not in INACTIVE_STATES]
    return found[0] if found else None


def _resolve(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    """``{token_sha256, peer_uid?}`` -> ``{session}``; a failed check is ``{session: null}`` (journaled while below
    the limit) or, over the limit, ``{session: null, retry_after}`` (``authguard.py``)."""
    token_hash, peer = args.get("token_sha256"), args.get("peer_uid")
    if not isinstance(token_hash, str) or not {"token_sha256"} <= set(args) <= {"token_sha256", "peer_uid"}:
        raise StateError("request_invalid")
    if peer is not None and type(peer) is not int:
        raise StateError("request_invalid")
    session = context.state.resolve(token_hash)
    if session is None or (peer is not None and session.uid != peer):
        failure = context.guard.fail(peer)
        if failure.record is not None:
            context.journal.append("guest.error", {"code": failure.record}, _owner(context, peer))
        answer: dict[str, Any] = {"session": None}
        if failure.retry_after is not None:
            answer["retry_after"] = failure.retry_after
        return answer
    return {
        "session": {
            "id": session.id,
            "uid": session.uid,
            "state": session.state,
            "workspace": session.workspace,
            "claude_session_id": session.claude_session_id,
        }
    }


CORE_API_OPS = {"hello": _hello, "session.resolve": _resolve}


StartupHook = Callable[["ControlContext"], None]


def startup_hooks(package: str = registry.CONTROL_ROUTES) -> list[tuple[str, StartupHook]]:
    """``control_routes/<name>.py:STARTUP`` in sorted module order."""
    found: list[tuple[str, StartupHook]] = []
    for module in registry.modules(package):
        hook = getattr(module, "STARTUP", None)
        if hook is None:
            continue
        if not callable(hook):
            raise registry.RegistryError("registry_invalid", f"{module.__name__}.STARTUP")
        found.append((module.__name__, hook))
    return found


def run_startup(context: ControlContext, hooks: Sequence[tuple[str, StartupHook]]) -> None:
    for name, hook in hooks:
        try:
            hook(context)
        except Exception:
            sys.stderr.write(f"agent-guest-control: startup hook {name} failed\n")
            traceback.print_exc(file=sys.stderr)


class ControlHandler(Handler):
    server_version = "agent-guest-control"
    timeout = 60


class ControlHttpServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 128
    handler_timeout = None

    def __init__(self, path: str, router: Router, context: ControlContext) -> None:
        self.router = router
        self.context = context
        self.schemas = context.schemas
        self.authorize = token_authorizer(context.config.control_token_file)
        super().__init__(path, ControlHandler)


def bind_unix(path: str, group: str, create: Any) -> Any:
    """Create the server socket at ``path`` (0660, group ``group``); a stale socket file is replaced."""
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        raise StartupError("group_unknown", group) from None
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(mode):
            raise StartupError("socket_path_invalid", path)
        os.unlink(path)
    previous = os.umask(0o177)
    try:
        server = create(path)
    finally:
        os.umask(previous)
    os.chown(path, -1, gid)
    os.chmod(path, SOCKET_MODE)
    return server


class ControlService:
    """Both sockets, the journal and the pruning loop of one control process."""

    def __init__(
        self,
        config: GuestConfig,
        clock: Clock | None = None,
        startup: Sequence[tuple[str, StartupHook]] | None = None,
    ) -> None:
        """``startup``: the start-up hooks to run (default: discovered, ``startup_hooks``)."""
        self.config = config
        self.clock = clock or SystemClock()
        self.startup = startup
        self.stopped = threading.Event()
        self._threads: list[threading.Thread] = []
        self._servers: list[socketserver.BaseServer] = []
        self.context: ControlContext | None = None

    def start(self) -> ControlContext:
        schemas = SchemaSet(self.config.contracts)
        try:
            schemas.load("common.json")
        except SchemaError:
            raise StartupError("contracts_missing", self.config.contracts) from None
        info = load_info(self.config)
        routes = registry.control_routes()
        hooks = startup_hooks() if self.startup is None else list(self.startup)
        ops: dict[str, Any] = dict(CORE_API_OPS)
        for name, op in registry.api_ops().items():
            if name in ops:
                raise registry.RegistryError("registry_duplicate", name)
            ops[name] = op
        journal = Journal.open(self.config.journal, self.clock, schemas)
        try:
            state = State(journal)
            context = ControlContext(self.config, schemas, journal, state, Pending(journal, state), info)
            journal.prune()
            journal.prune_pending()
            control = bind_unix(
                self.config.control_socket,
                self.config.control_group,
                lambda path: ControlHttpServer(path, Router(routes), context),
            )
            self._servers.append(control)
            api = bind_unix(
                self.config.api_socket, self.config.api_group, lambda path: ApiSockServer(path, ops, context)
            )
            self._servers.append(api)
        except BaseException:
            self._close_servers()
            journal.close()
            raise
        self.context = context
        for name, target in (
            ("control", lambda: control.serve_forever(POLL_SECONDS)),
            ("api", lambda: api.serve_forever(POLL_SECONDS)),
            ("prune", lambda: self._prune_loop(journal)),
        ):
            thread = threading.Thread(target=target, name=f"agent-guest-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        run_startup(context, hooks)
        return context

    def _prune_loop(self, journal: Journal) -> None:
        while not self.stopped.wait(PRUNE_SECONDS):
            try:
                journal.prune()
                journal.prune_pending()
            except JournalError:
                return

    def _close_servers(self) -> None:
        for server in self._servers:
            server.server_close()
        for path in (self.config.control_socket, self.config.api_socket):
            try:
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    os.unlink(path)
            except FileNotFoundError:
                pass

    def stop(self) -> None:
        self.stopped.set()
        for server in self._servers:
            server.shutdown()
        if self.context is not None:
            # the threads of the parts (session watchers, capture) end while the journal they write is still open
            self.context.close()
            self.context.pending.close()
            self.context.journal.close()
        for thread in self._threads:
            thread.join(5)
        self._close_servers()


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    if argv:
        sys.stderr.write("usage: python3 -m agent_guest.control (configuration through AGENT_GUEST_*)\n")
        return 2
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    # The handlers come first: a SIGTERM as soon as the sockets answer (systemd stop right after the start) must end
    # the service cleanly, not kill the process with the default action.
    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            service = ControlService(load_config(environ))
            service.start()
        except (ConfigError, StartupError, JournalError, registry.RegistryError, OSError) as error:
            sys.stderr.write(f"agent-guest-control: {_describe(error)}\n")
            return 1
        try:
            stop.wait()
        finally:
            service.stop()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def _describe(error: Exception) -> str:
    if isinstance(error, OSError):
        return f"os_error {error.errno}"
    return str(error)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
