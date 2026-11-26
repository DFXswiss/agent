"""Test kit of the context suites (KOR-33): archives as the guest exports them, a guest that exports and stages them,
the hub's context endpoints on the rig's hub (contract-checked like every rig answer), a hub client wrapper that
injects transport failures, and `ckit(env)` = the RUN-05 prompt kit plus an installed `ContextEnv`.

Everything below the runner code is real: the store, the verified logs and keys of the rig's world, the signed hub
client and the hub's signature gate.
"""

from __future__ import annotations

import atexit
import io
import json
import shutil
import tarfile
import tempfile
import uuid
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_runner.contexts.cache import Cache
from agent_runner.contexts.env import ContextEnv, install
from agent_runner.sync.http import HubTransportError

from ..prompts.kit import CLAUDE_ID, PKit, PromptGuest, setup
from ..reconcile.rig import Env, device_keys
from ..sessions.kit import TASK, Kit, session_id
from ..sync.fakehub import utc_ms

CLAUDE_VERSION = "2.1.330"
CWD = "/srv/sessions/01930005-0000-7000-8000-000000000001/work"
SLUG = "-srv-sessions-01930005-0000-7000-8000-000000000001-work"


def context_json(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "v": 1,
        "agent_kind": "claude-code",
        "agent_version": CLAUDE_VERSION,
        "claude_session_id": CLAUDE_ID,
        "session_id": session_id(1),
        "task_id": TASK,
        "cwd": CWD,
        "created_at": "2026-11-21T12:00:00.000Z",
        "transcript_files": [f"projects/{SLUG}/{CLAUDE_ID}.jsonl"],
        "workspace": "included",
        "workspace_files": 1,
        "workspace_bytes": 5,
    }
    value.update(overrides)
    return value


TURN = (
    b'{"type":"user","cwd":"' + CWD.encode() + b'","message":{"role":"user","content":"hello"}}\n'
    b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}\n'
)
"""A complete turn: the conversation file of every checkpoint holds at least one (`archive.inspect`)."""
CONVERSATION = f"transcript/projects/{SLUG}/{CLAUDE_ID}.jsonl"


def tar_bytes(members: list[tuple[str, bytes | None]]) -> bytes:
    """A PAX tar of `(path, content)`; content None is a directory."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, content in members:
            info = tarfile.TarInfo(path)
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
    return out.getvalue()


def archive(
    *,
    context: dict[str, Any] | None = None,
    work: dict[str, bytes] | None = None,
    transcript: bytes = TURN,
    extra: list[tuple[str, bytes | None]] | None = None,
    **overrides: Any,
) -> bytes:
    """A context archive as the guest exports it (ADR-072 section 2)."""
    files = {"notes.txt": b"hello"} if work is None else work
    members: list[tuple[str, bytes | None]] = [
        ("context.json", json.dumps(context if context is not None else context_json(**overrides)).encode()),
        ("transcript", None),
        (CONVERSATION, transcript),
        ("work", None),
    ]
    members += [(f"work/{path}", content) for path, content in files.items()]
    return tar_bytes(members + (extra or []))


@dataclass
class ContextGuest(PromptGuest):
    archives: dict[str, bytes] = field(default_factory=dict)
    staged: dict[str, bytes] = field(default_factory=dict)

    async def export_context(
        self, sid: str, write: Callable[[bytes], object], *, workspace_max_bytes: int, transcript_max_bytes: int
    ) -> None:
        self._call("export_context", (sid, workspace_max_bytes, transcript_max_bytes))
        data = self.archives[sid]
        for start in range(0, len(data), 65536):
            write(data[start : start + 65536])

    async def stage_context(self, sid: str, data: AsyncIterable[bytes], size: int) -> dict[str, Any]:
        self._call("stage_context", (sid, size))
        body = b"".join([piece async for piece in data])
        assert len(body) == size
        self.staged[sid] = body
        return {"files": 1, "bytes": size}


@dataclass
class HubContexts:
    """`/v1/device/contexts/...` and `/v1/device/pr-links` of the hub (ADR-072 sections 5 and 8), in memory."""

    hub: Any
    chunks: dict[tuple[str, int], str] = field(default_factory=dict)
    committed: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    refuse: dict[str, tuple[int, str]] = field(default_factory=dict)
    """Operation name (`put`, `commit`, `get`, `chunk`, `link`) -> refusal `(status, code)`."""
    link_pointers: list[str] | None = None
    """A hub that refuses a registration with `request_invalid` at these pointers when it carries `sealed` (an older
    hub names `/sealed`, ADR-084 section 1)."""

    def install(self) -> HubContexts:
        original = self.hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            answer = self.route(method, urlsplit(target).path, body)
            return answer if answer is not None else original(method, target, body)

        self.hub.route = route
        return self

    def _refused(self, name: str) -> tuple[int, dict[str, Any]] | None:
        found = self.refuse.get(name)
        return None if found is None else (found[0], {"error": found[1]})

    def _link(self, body: Any) -> tuple[int, dict[str, Any]]:
        self.hub.check("api/device-pr-links-post.request.json", body)
        refused = self._refused("link")
        if refused is not None:
            return refused
        if self.link_pointers is not None and "sealed" in body:
            return 400, {"error": "request_invalid", "detail": {"pointers": self.link_pointers}}
        self.links.append(body)
        return 200, {"replayed": False}

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict[str, Any]] | None:  # noqa: PLR0911
        parts = path.split("/")
        if path == "/v1/device/pr-links" and method == "POST":
            return self._link(body)
        if parts[:4] != ["", "v1", "device", "contexts"] or len(parts) < 5:
            return None
        object_id = parts[4]
        if len(parts) == 7 and parts[5] == "chunks" and method == "PUT":
            self.hub.check("api/device-context-chunk-put.request.json", body)
            refused = self._refused("put")
            if refused is not None:
                return refused
            self.chunks[(object_id, int(parts[6]))] = body["ct"]
            return 200, {"replayed": False}
        if len(parts) == 6 and parts[5] == "commit" and method == "POST":
            self.hub.check("api/device-context-commit.request.json", body)
            refused = self._refused("commit")
            if refused is not None:
                return refused
            now = utc_ms(self.hub.wall())
            self.committed[object_id] = {**body, "created_at": now, "completed_at": now}
            answer = {"completed_at": now, "replayed": False}
            self.hub.check("api/device-context-commit.response.json", answer)
            return 200, answer
        if len(parts) == 5 and method == "GET":
            refused = self._refused("get")
            if refused is not None:
                return refused
            if object_id not in self.committed:
                return 404, {"error": "not_found"}
            self.hub.check("api/device-context-get.response.json", self.committed[object_id])
            return 200, dict(self.committed[object_id])
        if len(parts) == 7 and parts[5] == "chunks" and method == "GET":
            refused = self._refused("chunk")
            if refused is not None:
                return refused
            found = self.chunks.get((object_id, int(parts[6])))
            return (404, {"error": "not_found"}) if found is None else (200, {"ct": found})
        return None


@dataclass
class FlakyHub:
    """The signed hub client; `errors` are raised by the next requests (one each) before they reach the hub, and
    `answers` (target -> list of answers, one per request) stand in for the hub's answer to a target."""

    inner: Any
    errors: list[BaseException] = field(default_factory=list)
    targets: list[tuple[str, str]] = field(default_factory=list)
    answers: dict[str, list[dict[str, object]]] = field(default_factory=dict)

    async def request(self, method: str, target: str, body: object | None = None) -> dict[str, object]:
        self.targets.append((method, target))
        if self.errors:
            raise self.errors.pop(0)
        queued = self.answers.get(target)
        if queued:
            return queued.pop(0)
        answer: dict[str, object] = await self.inner.request(method, target, body)
        return answer


def transport_error() -> HubTransportError:
    return HubTransportError("hub_unreachable")


def install_contexts(env: Env, found: Kit, **settings: Any) -> ContextEnv:
    """A `ContextEnv` over the kit's session environment and the rig's hub, installed on the daemon context."""
    root = Path(tempfile.mkdtemp(prefix="ctx-test-"))
    atexit.register(shutil.rmtree, root, True)
    (root / "tmp").mkdir()
    built = ContextEnv(
        sessions=found.session_env,
        hub=FlakyHub(env.sync.client),
        signing_key=device_keys().signing_key(),
        cache=Cache(root / "contexts"),
        scratch=root / "tmp",
        sleep=env.clock.sleep,
        clock=env.clock,
        **settings,
    )
    return install(env.context, built)


@dataclass
class CKit:
    pk: PKit
    contexts: ContextEnv
    store: HubContexts

    @property
    def guest(self) -> ContextGuest:
        guest = self.pk.guest
        assert isinstance(guest, ContextGuest)
        return guest

    @property
    def hub(self) -> FlakyHub:
        hub = self.contexts.hub
        assert isinstance(hub, FlakyHub)
        return hub

    @property
    def lines(self) -> list[str]:
        return self.pk.lines

    async def audits(self, action: str) -> list[dict[str, Any]]:
        return [item for item in await self.pk.env.audits() if item["action"] == action]


async def ckit(env: Env, **settings: Any) -> CKit:
    pk = await setup(env, guest=ContextGuest())
    existing = getattr(env.context, "context_env", None)
    contexts = existing if existing is not None and not settings else install_contexts(env, pk.kit, **settings)
    return CKit(pk=pk, contexts=contexts, store=HubContexts(env.hub).install())


async def accept(env: Env, sid: str, count: int = 1) -> None:
    """`count` prompts accepted for the session on this device: it worked (HOT-24)."""
    async with env.pool.connection() as conn:
        for _ in range(count):
            await conn.execute(
                "INSERT INTO runner.prompt_delivery (event_id, session_id, state) VALUES (%s, %s, 'accepted')",
                (str(uuid.uuid4()), sid),
            )
