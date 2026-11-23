"""U-HUB-credentials (R-4.2, R-5.4): the CK rotation `POST /v1/me/credentials/rotate` (03-api.md section 4.8,
04-crypto.md section 5.10): new epoch, wraps and every record re-encrypted in one transaction - a refusal of any part
changes nothing."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.orgs.support import Actor

NO_CONTRACT = {"contract": False}


def _setup(desk: Desk) -> tuple[Actor, dict[str, Json]]:
    """The writer with a github (v1) and a mailbox record (v2)."""
    writer = desk.scene.writer
    github = desk.record(writer, "github")
    assert desk.put(writer, github, 0).status_code == 200
    mailbox = desk.record(writer, "mailbox")
    assert desk.put(writer, mailbox, 0).status_code == 200
    mailbox2 = desk.record(writer, "mailbox", version=2, record_id=mailbox["header"]["id"])
    assert desk.put(writer, mailbox2, 1).status_code == 200
    return writer, {"github": github, "mailbox": mailbox2}


def _rotated(desk: Desk, writer: Actor, stored: dict[str, Json], epoch: int = 2) -> list[Json]:
    return [
        desk.record(
            writer,
            kind,
            version=record["header"]["version"] + 1,
            epoch=epoch,
            record_id=record["header"]["id"],
            key=desk.key_only(writer, epoch),
        )
        for kind, record in sorted(stored.items())
    ]


def _wraps(desk: Desk, writer: Actor, epoch: int = 2) -> list[Json]:
    recipients = [writer, desk.scene.device, desk.scene.other_device]
    return [desk.wrap(writer, writer, epoch, recipient) for recipient in recipients]


def _rotate(writer: Actor, epoch: int, wraps: list[Json], records: list[Json], **options: Any) -> Any:
    return writer.browser.post(
        "/v1/me/credentials/rotate", json={"epoch": epoch, "wraps": wraps, "records": records}, **options
    )


def _snapshot(desk: Desk, writer: Actor) -> tuple[Any, ...]:
    return (
        desk.world.rows("SELECT epoch FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %s", writer.id),
        desk.world.rows("SELECT recipient_id, epoch FROM hub.key_wrap WHERE scope_id = %s ORDER BY 1, 2", writer.id),
        desk.world.rows(
            "SELECT kind, version, epoch, header_text, ct, sig FROM hub.credential WHERE owner_id = %s ORDER BY kind",
            writer.id,
        ),
        desk.audit("credential.rotate"),
    )


def test_rotation_replaces_epoch_wraps_and_records(desk: Desk, published: MemoryPublisher) -> None:
    writer, stored = _setup(desk)
    published.clear()
    records = _rotated(desk, writer, stored)
    response = _rotate(writer, 2, _wraps(desk, writer), records)
    assert response.status_code == 200, response.text
    assert response.json() == {}
    epochs = desk.world.rows("SELECT epoch FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %s", writer.id)
    assert sorted(epochs) == [(1,), (2,)]
    wraps = desk.world.rows(
        "SELECT recipient_id::text, wrapped_by::text FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s"
        " AND epoch = 2 ORDER BY recipient_id",
        writer.id,
    )
    recipients = sorted([writer.id, desk.scene.device.id, desk.scene.other_device.id])
    assert wraps == [(recipient, writer.id) for recipient in recipients]
    for record in records:
        kind = record["header"]["kind"]
        assert writer.browser.get(f"/v1/me/credentials/{kind}").json() == {"record": record}
    listed = writer.browser.get("/v1/me/credentials").json()["credentials"]
    assert sorted((item["kind"], item["version"], item["epoch"]) for item in listed) == [
        ("github", 2, 2),
        ("mailbox", 3, 2),
    ]
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = desk.audit("credential.rotate")
    assert (actor_kind, str(actor_id), target_kind, target_id) == ("user", writer.id, "credential_key", writer.id)
    assert detail == {"epoch": 2, "records": 2}
    frames = sorted((item.frame, item.audience.kind, str(item.audience.id)) for item in published.items())
    devices = sorted([desk.scene.device.id, desk.scene.other_device.id])
    assert frames == sorted(
        [("credentials", "device", device) for device in devices]
        + [("wraps", "device", device) for device in devices]
        + [("wraps", "user", writer.id)]
    )
    later = desk.record(writer, "github", version=2, record_id=stored["github"]["header"]["id"])
    assert desk.put(writer, later, 1).json() == {"error": "epoch_stale"}


def test_a_rotation_without_records(desk: Desk) -> None:
    reader = desk.scene.reader
    response = _rotate(reader, 2, [desk.wrap(reader, reader, 2, reader)], [])
    assert response.status_code == 200, response.text
    assert desk.audit("credential.rotate")[0][5] == {"epoch": 2, "records": 0}


@pytest.mark.parametrize(
    ("case", "status", "body"),
    [
        ("bad_signature", 401, {"error": "signature_invalid"}),
        ("missing_record", 409, {"error": "version_conflict"}),
        ("extra_record", 409, {"error": "version_conflict"}),
        ("stale_version", 409, {"error": "version_conflict"}),
        ("old_epoch_record", 400, {"error": "epoch_stale"}),
        ("changed_id", 400, {"error": "request_invalid", "detail": {"pointers": ["/records"]}}),
        ("foreign_author", 400, {"error": "header_invalid"}),
        ("unknown_kind", 400, {"error": "header_invalid"}),
        ("no_header", 400, {"error": "header_invalid"}),
        ("epoch_skip", 409, {"error": "version_conflict", "detail": {"expected": 2}}),
        ("foreign_wrap_scope", 400, {"error": "request_invalid", "detail": {"pointers": ["/wraps/1"]}}),
        ("no_self_wrap", 400, {"error": "request_invalid", "detail": {"pointers": ["/wraps"]}}),
        ("unauthorized_wrap", 400, {"error": "wrap_unauthorized"}),
    ],
)
def test_a_refused_part_changes_nothing(desk: Desk, case: str, status: int, body: Json) -> None:
    writer, stored = _setup(desk)
    before = _snapshot(desk, writer)
    plan = Plan(desk, writer, stored)
    getattr(plan, case)()
    response = _rotate(writer, plan.epoch, plan.wraps, plan.records, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (status, body)
    assert _snapshot(desk, writer) == before


class Plan:
    """A valid rotation of the writer's two records and one way to break it per case."""

    def __init__(self, desk: Desk, writer: Actor, stored: dict[str, Json]) -> None:
        self.desk, self.writer, self.stored = desk, writer, stored
        self.records = _rotated(desk, writer, stored)
        self.wraps = _wraps(desk, writer)
        self.epoch = 2

    def _record(self, kind: str, version: int, epoch: int = 2, **options: Any) -> Json:
        if "record_id" not in options:
            options["record_id"] = self.stored[kind]["header"]["id"]
        options.setdefault("key", self.desk.key_only(self.writer, epoch))
        return self.desk.record(self.writer, kind, version=version, epoch=epoch, **options)

    def bad_signature(self) -> None:
        self.records[1] = dict(self.records[1], sig=self.records[0]["sig"])

    def missing_record(self) -> None:
        self.records = self.records[:1]

    def extra_record(self) -> None:
        self.records.append(self._record("anthropic_api_key", 1, record_id=None))

    def stale_version(self) -> None:
        self.records[1] = self._record("mailbox", 2)

    def old_epoch_record(self) -> None:
        self.records[0] = self._record("github", 2, epoch=1)

    def changed_id(self) -> None:
        self.records[0] = self._record("github", 2, record_id=None)

    def foreign_author(self) -> None:
        self.records[0] = self._record("github", 2, header={"author": self.desk.scene.reader.id})

    def unknown_kind(self) -> None:
        self.records[0] = {**self.records[0], "header": {**self.records[0]["header"], "kind": "ssh"}}

    def no_header(self) -> None:
        self.records[0] = {"ct": self.records[0]["ct"], "sig": self.records[0]["sig"]}

    def epoch_skip(self) -> None:
        self.epoch = 3

    def foreign_wrap_scope(self) -> None:
        self.wraps[1] = self.desk.wrap(self.writer, self.writer, 3, self.desk.scene.device)

    def no_self_wrap(self) -> None:
        self.wraps = self.wraps[1:]

    def unauthorized_wrap(self) -> None:
        self.wraps.append(self.desk.wrap(self.writer, self.writer, 2, self.desk.scene.stranger))
