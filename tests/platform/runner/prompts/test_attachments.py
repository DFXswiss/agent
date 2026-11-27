"""U-RUN-prompts (attachments): the files a prompt names are fetched from the hub, checked against the verified
references, opened and imported into `attachments/<event id>/` of the workspace before the prompt is typed with their
paths; the first prompt of a start with files waits for the delivery; missing, damaged or unreadable files refuse the
prompt, an unreachable hub or guest is retried (ADR-102 section 5; 04-crypto.md section 4.14)."""

from __future__ import annotations

import io
import os
import tarfile
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.crypto import attachment
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.prompts import attachments
from agent_runner.prompts.attachments import AttachmentFetcher
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import ATTRIBUTE, SyncRuntime

from ..reconcile.rig import Env, Rig, scope_key, seal
from ..sessions.kit import ORG, TASK, WG
from .kit import PromptGuest, pull, run, setup

WK = scope_key("wg", WG, 1)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))
LOG = b"line 1\nline 2: error\n"


@dataclass
class Hub:
    """The device's hub client double: serves sealed chunks by object, raises the queued errors first."""

    chunks: dict[str, list[bytes]] = field(default_factory=dict)
    errors: list[BaseException] = field(default_factory=list)
    answers: list[Any] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)

    async def request(self, method: str, target: str) -> Any:
        assert method == "GET"
        self.targets.append(target)
        if self.errors:
            raise self.errors.pop(0)
        if self.answers:
            return self.answers.pop(0)
        _, _, _, _, object_id, _, index = target.split("/")
        found = self.chunks.get(object_id)
        if found is None:
            raise HubRefusal(404, "not_found")
        return {"ct": b64u_encode(found[int(index)])}


@dataclass
class AttachGuest(PromptGuest):
    """The prompt guest with the file import of the guest (`POST /v1/sessions/{id}/files/import`)."""

    async def import_files(
        self, sid: str, directory: str, data: AsyncIterable[bytes], *, replace: bool = False, size: int | None = None
    ) -> dict[str, Any]:
        raw = b"".join([part async for part in data])
        assert size == len(raw)
        self._call("import_files", (sid, directory, raw, replace))
        return {"files": 1, "bytes": len(raw)}

    def imports(self) -> list[tuple[str, str, dict[str, bytes], bool]]:
        found = []
        for name, value in self.calls:
            if name == "import_files":
                sid, directory, raw, replace = value
                with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                    files = {member.name: tar.extractfile(member).read() for member in tar.getmembers()}  # type: ignore[union-attr]
                found.append((sid, directory, files, replace))
        return found


def sealed(hub: Hub, data: bytes, name: str, media_type: str, *, epoch: int = 1) -> dict[str, object]:
    """A file sealed under the workgroup key, served by `hub`; its reference."""
    ref, chunks = attachment.seal_attachment(
        WK, object_id=uuid7(), name=name, media_type=media_type, epoch=epoch, data=data, salt=os.urandom(32)
    )
    hub.chunks[str(ref["object"])] = chunks
    return ref


def attach_prompt(env: Env, seq: int, label: str, refs: list[Any], text: str = "Look at this") -> str:
    """A prompt at `seq` that names `refs`; its event ID."""
    event = seal(
        env.world.orgs[ORG], "alice", "prompt", label=label, task=TASK, body={"text": text, "attachments": refs}
    )
    env.world.add_event(WG, seq, event)
    return str(event["header"]["id"])


@dataclass
class Scene:
    """The prompt kit with the attaching guest and one local main session."""

    pk: Any
    sid: str

    @property
    def guest(self) -> AttachGuest:
        found: AttachGuest = self.pk.guest
        return found


async def ready(env: Env, hub: Hub, *, state: str = "idle") -> Scene:
    pk = await setup(env, guest=AttachGuest())
    pk.deliverer.attachments = AttachmentFetcher(lambda: hub)
    return Scene(pk, await pk.add_session(state=state))


async def deliver(scene: Scene, env: Env, seq: int, event_id: str) -> None:
    await pull(env)
    await scene.pk.route(scene.sid, seq, event_id)
    await scene.pk.deliverer.advance(scene.sid)


# --- the delivery ----------------------------------------------------------------------------------------------------


def test_the_files_are_imported_before_the_prompt_is_typed_with_their_paths(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, guest = s.pk, s.sid, s.guest
        shot = sealed(hub, PNG, "Bildschirmfoto 2026-11-27 um 10.15.png", "image/png")
        log = sealed(hub, LOG, "server.log", "text/plain")
        event = attach_prompt(env, 1, "a", [shot, log])
        await deliver(s, env, 1, event)
        assert guest.sent() == [event]
        directory = f"attachments/{event}"
        names = ["1-Bildschirmfoto_2026-11-27_um_10.15.png", "2-server.log"]
        assert guest.imports() == [(sid, directory, {names[0]: PNG, names[1]: LOG}, True)]
        assert guest.texts() == [
            "Look at this\n\nAttached files (in your workspace):\n"
            f"- {directory}/{names[0]} (image/png, 208 B)\n"
            f"- {directory}/{names[1]} (text/plain, 21 B)"
        ]
        # the import comes first, then the registration with the sender line of the verified author
        assert [name for name, _ in guest.calls if name in ("import_files", "register_prompt")] == [
            "import_files",
            "register_prompt",
        ]
        assert guest.speakers()[0] is not None
        assert await pk.rows(sid) == {event: "typed"}
        assert any(f"attachments_imported session_id={sid} event_id={event} files=2" in line for line in pk.lines)
        # no name, type or content of a file in the log
        assert not any("server.log" in line or "image/png" in line for line in pk.lines)

    run(rig, body)


def test_a_prompt_of_text_only_imports_nothing(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        _pk, _sid, guest = s.pk, s.sid, s.guest
        event = attach_prompt(env, 1, "t", [], text="Only text")
        await deliver(s, env, 1, event)
        assert guest.texts() == ["Only text"]
        assert guest.imports() == []
        assert hub.targets == []

    run(rig, body)


def test_the_first_prompt_with_files_waits_for_the_delivery(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub, state="vm_assigning")
        pk, sid, guest = s.pk, s.sid, s.guest
        event = attach_prompt(env, 1, "f", [sealed(hub, LOG, "a.txt", "text/plain")])
        await pull(env)
        await pk.route(sid, 1, event)
        assert await pk.deliverer.first_prompt(await pk.session(sid)) is None
        assert await pk.rows(sid) == {event: "pending"}
        assert any(f"first_prompt_deferred session_id={sid} event_id={event}" in line for line in pk.lines)
        # the session starts without it and waits for the delivery, which imports the file and types the prompt
        await pk.set_state(sid, "prompt_delivering")
        await pk.deliverer.advance(sid)
        assert guest.sent() == [event]
        assert guest.imports()[0][2] == {"1-a.txt": LOG}

    run(rig, body)


@pytest.mark.parametrize(
    "error",
    [HubRefusal(503, "unavailable"), HubRefusal(429, "rate_limited"), HubTransportError("connect_failed")],
)
def test_an_unreachable_hub_is_retried(rig: Rig, error: BaseException) -> None:
    async def body(env: Env) -> None:
        hub = Hub(errors=[error])
        s = await ready(env, hub)
        pk, sid, guest = s.pk, s.sid, s.guest
        event = attach_prompt(env, 1, "r", [sealed(hub, LOG, "a.txt", "text/plain")])
        await deliver(s, env, 1, event)
        assert guest.sent() == []
        assert await pk.rows(sid) == {event: "registered"}
        assert any(
            f"send_retry session_id={sid} event_id={event} code=attachment_unavailable" in line for line in pk.lines
        )
        await pk.advance(sid, later=31)
        assert guest.sent() == [event]
        assert await pk.audit_codes() == []

    run(rig, body)


def test_a_device_without_a_hub_client_retries(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, guest = s.pk, s.sid, s.guest
        event = attach_prompt(env, 1, "n", [sealed(hub, LOG, "a.txt", "text/plain")])
        client: list[Hub] = []
        pk.deliverer.attachments = AttachmentFetcher(lambda: client[0] if client else None)
        await deliver(s, env, 1, event)
        assert await pk.rows(sid) == {event: "registered"}
        # a deliverer without a fetcher waits as well
        pk.deliverer.attachments = None
        await pk.advance(sid, later=31)
        assert await pk.rows(sid) == {event: "registered"}
        pk.deliverer.attachments = AttachmentFetcher(lambda: client[0] if client else None)
        client.append(hub)
        await pk.advance(sid, later=61)
        assert guest.sent() == [event]

    run(rig, body)


def _missing(hub: Hub, ref: dict[str, object]) -> None:
    del hub.chunks[str(ref["object"])]


def _damaged(hub: Hub, ref: dict[str, object]) -> None:
    chunk = hub.chunks[str(ref["object"])][0]
    hub.chunks[str(ref["object"])] = [chunk[:-1] + bytes([chunk[-1] ^ 1])]


def _forbidden(hub: Hub, ref: dict[str, object]) -> None:
    hub.errors.append(HubRefusal(403, "forbidden"))


def _not_b64u(hub: Hub, ref: dict[str, object]) -> None:
    hub.answers.append({"ct": "not b64u!"})


def _not_an_object(hub: Hub, ref: dict[str, object]) -> None:
    hub.answers.append(["ct"])


@pytest.mark.parametrize("spoil", [_missing, _damaged, _forbidden, _not_b64u, _not_an_object])
def test_a_file_that_is_missing_or_does_not_verify_refuses_the_prompt(rig: Rig, spoil: Any) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, guest = s.pk, s.sid, s.guest
        ref = sealed(hub, LOG, "a.txt", "text/plain")
        spoil(hub, ref)
        event = attach_prompt(env, 1, "x", [ref])
        await deliver(s, env, 1, event)
        assert guest.sent() == []
        assert guest.imports() == []
        assert await pk.rows(sid) == {event: "refused"}
        assert await pk.audit_codes() == ["prompt_unreadable"]

    run(rig, body)


def test_a_file_of_an_epoch_without_a_key_refuses_the_prompt(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, _guest = s.pk, s.sid, s.guest
        event = attach_prompt(env, 1, "k", [sealed(hub, LOG, "a.txt", "text/plain", epoch=7)])
        await deliver(s, env, 1, event)
        assert await pk.rows(sid) == {event: "refused"}
        assert await pk.audit_codes() == ["prompt_unreadable"]
        assert hub.targets == []

    run(rig, body)


def test_references_that_are_not_valid_refuse_the_prompt(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, _guest = s.pk, s.sid, s.guest
        ref = sealed(hub, LOG, "a.txt", "text/plain")
        event = attach_prompt(env, 1, "v", [ref, ref])
        await deliver(s, env, 1, event)
        assert await pk.rows(sid) == {event: "refused"}
        assert await pk.audit_codes() == ["prompt_unreadable"]

    run(rig, body)


def test_a_refused_import_refuses_the_prompt(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, guest = s.pk, s.sid, s.guest
        guest.fail["import_files"] = GuestRefused("files_import", 400, "path_invalid")
        event = attach_prompt(env, 1, "g", [sealed(hub, LOG, "a.txt", "text/plain")])
        await deliver(s, env, 1, event)
        assert guest.sent() == []
        assert await pk.rows(sid) == {event: "refused"}
        assert await pk.audit_codes() == ["prompt_guest_refused"]

    run(rig, body)


def test_a_retry_after_a_lost_registration_imports_again(rig: Rig) -> None:
    async def body(env: Env) -> None:
        hub = Hub()
        s = await ready(env, hub)
        pk, sid, guest = s.pk, s.sid, s.guest
        guest.prompt_errors.append(RunnerError("guest_unreachable"))
        event = attach_prompt(env, 1, "l", [sealed(hub, LOG, "a.txt", "text/plain")])
        await deliver(s, env, 1, event)
        assert await pk.rows(sid) == {event: "registered"}
        await pk.advance(sid, later=31)
        assert guest.sent() == [event, event]
        assert [item[1] for item in guest.imports()] == [f"attachments/{event}"] * 2
        assert await pk.rows(sid) == {event: "typed"}

    run(rig, body)


# --- the parts -------------------------------------------------------------------------------------------------------


def test_workspace_names_are_numbered_reduced_and_cut() -> None:
    assert attachments.workspace_name(1, "Bericht März 2026.pdf") == "1-Bericht_M_rz_2026.pdf"
    assert attachments.workspace_name(2, "CLAUDE.md") == "2-CLAUDE.md"
    assert attachments.workspace_name(3, ".env") == "3-env"
    assert attachments.workspace_name(4, "日本語") == "4-file"
    long = "x" * 150
    assert attachments.workspace_name(5, long + ".tar.gz") == "5-" + "x" * 97 + ".gz"
    assert attachments.workspace_name(6, long) == "6-" + "x" * 100
    assert attachments.directory("e1") == "attachments/e1"


def test_sizes_and_notes() -> None:
    assert attachments.size_text(1023) == "1023 B"
    assert attachments.size_text(1536) == "1.5 KB"
    assert attachments.size_text(3 * 1024 * 1024) == "3.0 MB"
    ref = {"name": "a.txt", "type": "text/plain", "bytes": 5}
    assert attachments.with_note("Text\n", "e1", []) == "Text\n"
    assert attachments.with_note("Text\n", "e1", [ref]) == (
        "Text\n\nAttached file (in your workspace):\n- attachments/e1/1-a.txt (text/plain, 5 B)"
    )


def test_the_hub_client_of_a_context() -> None:
    assert attachments.hub_of(SimpleNamespace(identity=None))() is None
    client: Any = object()
    none: Any = None
    built = SyncRuntime(client=client, pusher=none, ws=none, storage_key=b"", wall=lambda: 0.0)
    assert attachments.hub_of(SimpleNamespace(**{ATTRIBUTE: built}))() is client
