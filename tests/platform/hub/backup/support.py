"""Test support of the backup suites (DR-01): a hub with a row in every table, export files and a second database.

`Estate.build` fills the running test hub through the real services where an area offers them - the events scene
(`tests.platform.hub.events.support.Scene`: a public org with an admin, a writer, a reader, two assigned org devices
and a stranger with an org of its own), whiteboard entries (`Board`), the platform log (`Platform`: a root, a role, an
org platform access, a certified platform device) and sealed events that carry canary plaintext - and writes the rows
of the remaining tables (tasks, sessions, VMs, operations, pings, notifications, credentials, device requests,
metrics, usage, quotas, passkeys, bundles, outbox receipts, ...) as their areas store them. Last it writes an audit
checkpoint. Every table of the export rules then holds at least one row of the scene's org (a test proves it).
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from agent_hub.audit.checkpoint import write_checkpoint
from agent_hub.audit.eventbatch import summarise
from agent_hub.backup.export import line, signed_text, write_export
from agent_hub.crypto import aead
from agent_hub.crypto import event as event_format
from agent_hub.crypto.encoding import b64u_encode, body_json, format_time
from tests.platform.hub.billing.support import PRICES
from tests.platform.hub.conftest import (
    MIN_SERVER_VERSION,
    HubDatabase,
    HubHarness,
    admin_params,
    create_role,
    drop_database,
)
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, World, new_id
from tests.platform.hub.platform.conftest import Platform
from tests.platform.hub.whiteboards.support import Board

Json = dict[str, Any]
CANARIES = ("canary-prompt-7f3a", "canary-whiteboard-91bc", "canary-credential-0d2e", "canary-community-5c1d")


@dataclass
class Estate:
    """The filled hub (module docstring)."""

    hub: HubHarness
    scene: Scene
    board: Board
    platform: Platform
    platform_device: str
    task: str
    session: str
    charge: str = "01930028-0000-7000-8000-0000000000c1"
    """The ID of the estate's charge (KOR-31)."""
    subscription: str = "01930028-0000-7000-8000-0000000000c2"
    """The ID of the writer's further subscription (KOR-69)."""
    mailbox: str = "01930028-0000-7000-8000-0000000000c3"
    """The ID of the writer's further mailbox, released to a workgroup (KOR-74)."""

    @property
    def org(self) -> str:
        return self.scene.org

    @property
    def other_org(self) -> str:
        """The stranger's org (a second org of the hub)."""
        rows = self.rows("SELECT id::text FROM hub.org WHERE id <> %s", self.org)
        return str(rows[0][0])

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        with self.hub.pool.connection() as connection:
            return connection.execute(query, params).fetchall()

    def export(self, org: str | None = None) -> bytes:
        """The export file of `org` (default: the scene's org) at the hub clock."""
        out = io.BytesIO()
        with self.hub.pool.connection() as connection:
            connection.autocommit = True
            write_export(connection, UUID(org or self.org), self.hub.config.signer(), out, self.hub.clock())
        return out.getvalue()

    @classmethod
    def build(cls, hub: HubHarness) -> Estate:
        world = World(hub)
        scene = Scene.build(world)
        board = Board.build(scene)
        platform = Platform(hub)
        platform.append("role_grant", subject=scene.writer.id, role="inviter")
        platform.append("org_platform_access", org=scene.org, granted=True)
        # ADR-079: the scene's devices run this release (they announced `log.policy`), so the switch is appended
        with hub.pool.connection() as connection, connection.transaction():
            connection.execute("UPDATE hub.device SET features = array_append(features, 'log.policy')")
        platform.append("platform_policy", name="open_platform_invites", on=True)
        platform.invite_user()
        enrolled = platform.enroll()
        platform.certify(enrolled)
        # a device of the org given to the whole organization (ADR-075): rows in `org_device`
        scene.draft.add("device_assign_org", scene.admin, device=scene.other_device.id)
        scene.draft.append(scene.admin)
        # a support person and a release of the workgroup (ADR-096): rows in every support table
        person = scene.stranger  # exported with its own org, so every table restores completely
        with hub.pool.connection() as connection, connection.transaction():
            connection.execute("UPDATE hub.device SET features = features || ARRAY['log.admit', 'log.support']")
        platform.append("user_certify", user=person.id, pub=person.pub)
        platform.append("role_grant", subject=person.id, role="support")
        scene.draft.add("support_grant", scene.admin, wg=scene.wg, access="read")
        scene.draft.append(scene.admin, scene.draft.wraps(scene.admin, "wg", scene.wg, 1, [person]))
        assert person.browser.get(f"/v1/workgroups/{scene.wg}/tasks").status_code == 200
        task, session = scene.start_session()
        estate = cls(hub, scene, board, platform, enrolled.device_id, task, session)
        estate._events()
        estate._rows()
        with hub.pool.connection() as connection:
            summarise(connection, hub.clock())  # the audit job's summary of the session's events (ADR-063)
            write_checkpoint(connection, hub.config.signer(), hub.clock())
        return estate

    def _sealed(self, author: Actor, kind: str, plaintext: str, **extra: Any) -> Json:
        draft, wg = self.scene.draft, self.scene.wg
        header = event_format.new_header(
            id=new_id(),
            org=draft.id,
            scope="wg",
            scope_id=wg,
            epoch=1,
            author=author.id,
            kind=kind,
            ts=format_time(self.hub.clock()),
            lh=len(draft.entries) - 1,
            wg=wg,
            **extra,
        )
        return event_format.seal_event(
            draft.key("wg", wg, 1), header, body_json({"text": plaintext}), author.identity.sig
        )

    def _events(self) -> None:
        scene, board = self.scene, self.board
        prompt = self._sealed(scene.writer, "prompt", CANARIES[0], task=self.task)
        entry = self._sealed(scene.writer, "whiteboard.entry", CANARIES[1], ref=new_id())
        board.posted(scene.writer, prompt, entry)
        board.posted(scene.admin, board.entry(scene.admin, level="org"))
        written = board.posted(scene.writer, board.entry(scene.writer))
        board.posted(scene.writer, board.event(scene.writer, "whiteboard.delete", written[0]["header"]["ref"]))
        scene.append(scene.device, scene.session_event(scene.device, self.session), origin_seqs=[1])

    def _rows(self) -> None:
        scene, now = self.scene, self.hub.clock()
        writer, device, wg, org = scene.writer, scene.device, scene.wg, scene.org
        operation, request, context_object, company = new_id(), new_id(), new_id(), new_id()
        credential_key = secrets.token_bytes(32)
        statements: list[tuple[str, tuple[object, ...]]] = [
            (
                "INSERT INTO hub.user_settings (user_id, model_access, locale, updated_at)"
                " VALUES (%s, 'auto', 'de', %s)",
                (writer.id, now),
            ),
            (
                "INSERT INTO hub.identity_bundle (user_id, slot, ct, updated_at) VALUES (%s, 'recovery', %s, %s)",
                (writer.id, secrets.token_bytes(48), now),
            ),
            (
                "INSERT INTO hub.passkey (credential_id, user_id, public_key, sign_count, transports, prf_seen,"
                " created_at, last_used_at) VALUES (%s, %s, %s, 3, %s, true, %s, %s)",
                (
                    secrets.token_bytes(16),
                    writer.id,
                    b"\x04" + secrets.token_bytes(64),
                    ["internal", "hybrid"],
                    now,
                    now,
                ),
            ),
            (
                "INSERT INTO hub.ceremony (id, kind, challenge, created_at, expires_at)"
                " VALUES (%s, 'login', %s, %s, %s)",
                (new_id(), secrets.token_bytes(32), now, now + timedelta(seconds=300)),
            ),
            (
                "INSERT INTO hub.device_presence (device_id, hub_instance, connected_at, last_heartbeat_at)"
                " VALUES (%s, 'hub-1', %s, %s)",
                (device.id, now, now),
            ),
            (
                "INSERT INTO hub.task_transition (task_id, seq, state, reason, at, by_id)"
                " VALUES (%s, 1, 'working', NULL, %s, %s)",
                (self.task, now, writer.id),
            ),
            (
                "INSERT INTO hub.task_attention (task_id, org_id, wg_id, owner_id, category, reason, since, seq,"
                " subagents_total, subagents_active, updated_at)"
                " VALUES (%s, %s, %s, %s, 'working', 'working', %s, 1, 0, 0, %s)",
                (self.task, org, wg, writer.id, now, now),
            ),
            (
                "INSERT INTO hub.session_transition (session_id, seq, state, at, received_at, reported_by)"
                " VALUES (%s, 1, 'working', %s, %s, %s)",
                (self.session, now, now, device.id),
            ),
            (
                "INSERT INTO hub.vm (id, device_id, wg_id, state, golden_version, cpus, memory_mib, disk_gib,"
                " created_at, state_at) VALUES ('vm-0000000a', %s, %s, 'running', 'g1', 2, 4096, 20, %s, %s),"
                " ('vm-0000000b', %s, NULL, 'pool', 'g1', 2, 4096, 20, %s, %s)",
                (device.id, wg, now, now, device.id, now, now),
            ),
            (
                "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
                " dedupe_epoch, dedupe_tag, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'github.comment',"
                " 'accepted', 1, 'tag-1', %s)",
                (operation, org, wg, self.task, self.session, device.id, writer.id, now),
            ),
            (
                "INSERT INTO hub.operation_transition (operation_id, seq, state, at, by_id)"
                " VALUES (%s, 1, 'accepted', %s, NULL)",
                (operation, now),
            ),
            (
                "INSERT INTO hub.dedupe_slot (wg_id, epoch, tag, operation_id, expires_at)"
                " VALUES (%s, 1, 'tag-1', %s, %s)",
                (wg, operation, now + timedelta(days=1)),
            ),
            (
                "INSERT INTO hub.session_ping (id, session_id, kind, level, ref, created_at)"
                " VALUES (%s, %s, 'whiteboard.entry', 'workgroup', %s, %s)",
                (new_id(), self.session, new_id(), now),
            ),
            (
                "INSERT INTO hub.notification (id, recipient_id, kind, org_id, wg_id, task_id, created_at)"
                " VALUES (%s, %s, 'task.idle', %s, %s, %s, %s)",
                (new_id(), writer.id, org, wg, self.task, now),
            ),
            (
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " created_at, is_primary) VALUES (%s, %s, 'github', 1, 1, '{}', %s, %s, %s, %s, true)",
                (
                    new_id(),
                    writer.id,
                    aead.seal_blob(credential_key, CANARIES[2].encode(), b"aad"),
                    secrets.token_bytes(64),
                    now,
                    now,
                ),
            ),
            (
                # a company identity of the org and its org default (ADR-078)
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " created_at, is_primary, owner_kind)"
                " VALUES (%s, %s, 'github', 1, 1, '{}', %s, %s, %s, %s, false, 'org')",
                (company, org, secrets.token_bytes(40), secrets.token_bytes(64), now, now),
            ),
            (
                "INSERT INTO hub.github_default (scope, scope_id, identity_id, set_by, set_at)"
                " VALUES ('org', %s, %s, %s, %s)",
                (org, company, writer.id, now),
            ),
            (
                # an own rights profile of the org (ADR-094)
                "INSERT INTO hub.agent_profile (scope, scope_id, org_id, name, operations, reads, updated_by,"
                " updated_at) VALUES ('org', %s, %s, 'triage', %s, 'task', %s, %s)",
                (org, org, ["github.get_issue"], writer.id, now),
            ),
            (
                "INSERT INTO hub.agent (id, user_id, kem_pub, sig_pub, state, bundle_ct, label_ct, claim_hash,"
                " created_at, expires_at, confirmed_at, claimed_at, last_used_at)"
                " VALUES (%s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    writer.id,
                    secrets.token_bytes(32),
                    secrets.token_bytes(32),
                    secrets.token_bytes(140),
                    secrets.token_bytes(80),
                    secrets.token_bytes(32),
                    now,
                    now + timedelta(minutes=10),
                    now,
                    now,
                    now,
                ),
            ),
            (
                "INSERT INTO hub.credential_notice (device_id, owner_id, kind, changed_at)"
                " VALUES (%s, %s, 'github', %s)",
                (device.id, writer.id, now),
            ),
            (
                "INSERT INTO hub.device_request (id, user_id, device_id, kind, state, envelope, created_at, expires_at)"
                " VALUES (%s, %s, %s, 'github.connect', 'answered', 'envelope', %s, %s)",
                (request, writer.id, device.id, now, now + timedelta(minutes=15)),
            ),
            (
                "INSERT INTO hub.device_response (request_id, seq, envelope, created_at) VALUES (%s, 1, 'reply', %s)",
                (request, now),
            ),
            (
                "INSERT INTO hub.metric_sample (entity_kind, entity_id, ts, device_id, cpu_ratio, mem_used_bytes,"
                " reachable, state, received_at) VALUES ('device', %s, %s, %s, 0.30000001192092896, 1024, true,"
                " 'active', %s)",
                (device.id, now, device.id, now),
            ),
            (
                "INSERT INTO hub.metric_rollup (entity_kind, entity_id, ts, device_id, samples, cpu_ratio_avg,"
                " free_vm_slots_avg, reachable, state) VALUES ('device', %s, '2026-11-21T12:00:00Z', %s, 2, 0.25,"
                " 1.5, true, 'active')",
                (device.id, device.id),
            ),
            # the community (ADR-069): hub-wide, one row in every table
            (
                "INSERT INTO hub.community_epoch (epoch, key_check, created_by, created_at) VALUES (1, %s, %s, %s)",
                (secrets.token_bytes(32), writer.id, now),
            ),
            (
                "INSERT INTO hub.community_wrap (epoch, user_id, wrap, sig, created_at) VALUES (1, %s, %s, %s, %s)",
                (writer.id, secrets.token_bytes(80), secrets.token_bytes(64), now),
            ),
            (
                "INSERT INTO hub.community_request (user_id, id, kem_pub, sig, created_at, offered_at, epoch, wrap,"
                " wrapped_by, wrap_sig, wrapped_at) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s)",
                (
                    scene.admin.id,
                    new_id(),
                    secrets.token_bytes(32),
                    secrets.token_bytes(64),
                    now,
                    now,
                    secrets.token_bytes(80),
                    writer.id,
                    secrets.token_bytes(64),
                    now,
                ),
            ),
            (
                "INSERT INTO hub.community_reject (wrapper_id, user_id, rejected_at) VALUES (%s, %s, %s)",
                (scene.admin.id, writer.id, now),
            ),
            (
                "INSERT INTO hub.community_member (user_id, joined_at, last_read_seq, muted_at, muted_by)"
                " VALUES (%s, %s, 1, NULL, NULL), (%s, %s, 0, %s, %s)",
                (writer.id, now, scene.admin.id, now, now, writer.id),
            ),
            ("INSERT INTO hub.community_moderator (user_id, granted_at) VALUES (%s, %s)", (writer.id, now)),
            (
                "INSERT INTO hub.community_message (seq, id, author_id, epoch, created_at, ct_bytes, header, ct, sig)"
                " VALUES (1, %s, %s, 1, %s, 48, %s, %s, %s)",
                (
                    new_id(),
                    writer.id,
                    now,
                    Jsonb({"v": 1, "id": "x"}),
                    aead.seal_blob(secrets.token_bytes(32), CANARIES[3].encode(), b"aad"),
                    secrets.token_bytes(64),
                ),
            ),
            (
                "INSERT INTO hub.community_message (seq, id, author_id, epoch, created_at, ct_bytes, deleted_at,"
                " deleted_as, deleted_by) VALUES (2, %s, %s, 1, %s, 48, %s, 'moderator', %s)",
                (new_id(), scene.admin.id, now, now, writer.id),
            ),
            ("UPDATE hub.metric_rollup_cursor SET received_through = %s", (now,)),
            (
                "INSERT INTO hub.usage_counter (session_id, requests, input_tokens, cost_micro_usd, cost_basis,"
                " updated_at) VALUES (%s, 2, 100, 5000, 'provider', %s)",
                (self.session, now),
            ),
            (
                'INSERT INTO hub.quota_snapshot (owner_id, "window", used_percent, resets_at, fetched_at, device_id)'
                " VALUES (%s, 'five_hour', 42.5, %s, %s, %s)",
                (writer.id, now + timedelta(hours=5), now, device.id),
            ),
            (
                "INSERT INTO hub.quota_unreadable (owner_id, since, device_id) VALUES (%s, %s, %s)",
                (writer.id, now, device.id),
            ),
            # a further subscription of the writer and its reading (KOR-69, ADR-090)
            (
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " created_at, is_primary) VALUES (%s, %s, 'claude_subscription', 1, 1, '{}', %s, %s, %s, %s, false)",
                (self.subscription, writer.id, secrets.token_bytes(40), secrets.token_bytes(64), now, now),
            ),
            (
                "INSERT INTO hub.subscription_quota (credential_id, owner_id, windows, unreadable, fetched_at,"
                " device_id) VALUES (%s, %s, %s, false, %s, %s)",
                (self.subscription, writer.id, Jsonb([{"window": "seven_day", "used_percent": 12.0}]), now, device.id),
            ),
            (
                "INSERT INTO hub.subscription_use (credential_id, owner_id, pool, orgs, set_at)"
                " VALUES (%s, %s, false, %s, %s)",
                (self.subscription, writer.id, Jsonb([org]), now),
            ),
            # the tokens of a person on it (KOR-75, ADR-103)
            (
                "INSERT INTO hub.subscription_usage (credential_id, owner_id, user_id, org_id, session_id, slot,"
                " requests, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)"
                " VALUES (%s, %s, %s, %s, %s, %s, 2, 100, 20, 1000, 30)",
                (self.subscription, writer.id, writer.id, org, self.session, now),
            ),
            # a further mailbox of the writer and its release (KOR-74, ADR-099)
            (
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " created_at, is_primary) VALUES (%s, %s, 'mailbox', 1, 1, '{}', %s, %s, %s, %s, false)",
                (self.mailbox, writer.id, secrets.token_bytes(40), secrets.token_bytes(64), now, now),
            ),
            (
                "INSERT INTO hub.mailbox_use (credential_id, owner_id, wgs, set_at) VALUES (%s, %s, %s, %s)",
                (self.mailbox, writer.id, Jsonb([org]), now),
            ),
            # model access with fallback (KOR-50): a limit, a snapshot, an issuer, a key and its waiting cost
            (
                "INSERT INTO hub.quota_limit (owner_id, at, until, readable, device_id, fetched_at)"
                " VALUES (%s, %s, %s, true, %s, %s)",
                (writer.id, now, now + timedelta(hours=2), device.id, now),
            ),
            (
                "INSERT INTO hub.operator_quota (device_id, at, snapshot, received_at) VALUES (%s, %s, %s, %s)",
                (device.id, now, Jsonb([]), now),
            ),
            ("INSERT INTO hub.dfx_issuer (device_id, announced_at) VALUES (%s, %s)", (device.id, now)),
            (
                "INSERT INTO hub.dfx_key (request_id, org_id, device_id, requested_at, issuer_device_id, key_ref,"
                " envelope, issued_at, booked_micro_usd) VALUES (%s, %s, %s, %s, %s, 'dfx-backup1', 'AAAA', %s, 10)",
                (new_id(), org, device.id, now, device.id, now),
            ),
            (
                "INSERT INTO hub.dfx_key_cost (device_id, session_id, org_id, cost_micro_usd) VALUES (%s, %s, %s, 7)",
                (device.id, self.session, org),
            ),
            (
                "INSERT INTO hub.usage_daily (org_id, day, device_id, cost_micro_usd) VALUES (%s, %s, %s, 5000)",
                (org, date(2026, 11, 21), device.id),
            ),
            # billing (KOR-31): a price list, the org's account, its history, a charge, its pending part and its day
            (
                # valid from next year: a restored hub's settle job finds nothing to do, so the copy stays identical
                "INSERT INTO hub.price_list (version, valid_from, document, created_at) VALUES (1, %s, %s, %s)",
                (now + timedelta(days=365), Jsonb(PRICES), now),
            ),
            (
                "INSERT INTO hub.fx_rate (n, day, chf_per_usd_e8, source, set_at, actor, reason)"
                " VALUES (1, %s, 84860000, 'BAZG', %s, 'operator', NULL)",
                (date(2026, 11, 21), now),
            ),
            (
                "INSERT INTO hub.credit_account (org_id, balance_micro_usd, booked_micro_usd, updated_at)"
                " VALUES (%s, 5000000, 5000000, %s)",
                (org, now),
            ),
            (
                "INSERT INTO hub.credit_entry (org_id, n, at, kind, amount_micro_usd, balance_micro_usd, reason, actor)"
                " VALUES (%s, 1, %s, 'start', 5000000, 5000000, NULL, 'hub')",
                (org, now),
            ),
            (
                "INSERT INTO hub.charge (id, org_id, wg_id, task_id, session_id, device_id, origin_seq, kind, at,"
                " amount_micro_usd, price_version, quantity) VALUES (%s, %s, %s, %s, %s, %s, 9, 'runtime', %s, 3000, 1,"
                " %s)",
                (self.charge, org, wg, self.task, self.session, device.id, now, Jsonb({"runtime_ms": 30000})),
            ),
            (
                "INSERT INTO hub.charge_pending (charge_id, org_id, amount_micro_usd) VALUES (%s, %s, 3000)",
                (self.charge, org),
            ),
            (
                "INSERT INTO hub.charge_daily (org_id, day, wg_id, device_id, kind, amount_micro_usd, quantity)"
                " VALUES (%s, %s, %s, %s, 'runtime', 3000, 30000)",
                (org, date(2026, 11, 21), wg, device.id),
            ),
            # the consumption per person and a withdrawn prompt (KOR-46)
            (
                "INSERT INTO hub.charge_person_daily (org_id, day, user_id, kind, amount_micro_usd, quantity)"
                " VALUES (%s, %s, %s, 'runtime', 3000, 30000)",
                (org, date(2026, 11, 21), writer.id),
            ),
            (
                "INSERT INTO hub.prompt_withdrawal (event_id, task_id, requested_by, requested_at, withdrawn_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (new_id(), self.task, writer.id, now, now),
            ),
            (
                "INSERT INTO hub.outbox_receipt (device_id, origin_seq, item_kind, item_sha256, status, received_at)"
                " VALUES (%s, 1, 'event', %s, 'ok', %s)",
                (device.id, hashlib.sha256(b"item").digest(), now),
            ),
            (
                "INSERT INTO hub.context_object (id, org_id, wg_id, task_id, session_id, author_id, state, chunks,"
                " size, epoch, digest, manifest_text, sig, created_at, updated_at, completed_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, 'complete', 1, 40, 1, 'digest', '{}', %s, %s, %s, %s)",
                (context_object, org, wg, self.task, self.session, device.id, bytes(64), now, now, now),
            ),
            (
                "INSERT INTO hub.context_chunk (object_id, chunk_index, ct, sha256) VALUES (%s, 0, %s, %s)",
                (context_object, bytes(40), hashlib.sha256(bytes(40)).digest()),
            ),
            (
                "INSERT INTO hub.pr_link (wg_id, epoch, tag, task_id, session_id, created_at)"
                " VALUES (%s, 1, 'tag', %s, %s, %s)",
                (wg, self.task, self.session, now),
            ),
            (
                "INSERT INTO hub.wg_placement (wg_id, device_id, vm_id, set_by, set_at)"
                " VALUES (%s, %s, 'vm-0000000a', %s, %s)",
                (wg, device.id, writer.id, now),
            ),
            (
                "UPDATE hub.task SET placement_device_id = %s, placement_vm_id = 'vm-0000000a' WHERE id = %s",
                (device.id, self.task),
            ),
            (
                "INSERT INTO hub.device_stream (device_id, last_origin_seq) VALUES (%s, 1)"
                " ON CONFLICT (device_id) DO UPDATE SET last_origin_seq = 1",
                (device.id,),
            ),
        ]
        with self.hub.pool.connection() as connection, connection.transaction():
            for statement, params in statements:
                connection.execute(statement, params)


# --- files -----------------------------------------------------------------------------------------------------------


def lines(data: bytes) -> list[Json]:
    """The lines of an export file as JSON objects."""
    return [json.loads(raw) for raw in data.decode("ascii").splitlines()]


def rows_of(data: bytes, table: str) -> list[Json]:
    """The `row` objects of one table."""
    return [item["row"] for item in lines(data)[1:-1] if item["table"] == table]


def resign(data: bytes, hub: HubHarness, change: Callable[[list[Json]], None]) -> bytes:
    """The file after `change` edited its lines (header, rows; not the trailer), with a fresh digest and signature
    of the test hub key: what only the holder of the hub key could produce."""
    items = lines(data)
    body = items[:-1]
    change(body)
    raw = b"".join(line(item) for item in body)
    digest = hashlib.sha256(raw).hexdigest()
    count = len(body) - 1
    signer = hub.config.signer()
    signature = signer.sign(signed_text(str(body[0]["hub_key_id"]), str(body[0]["org_id"]), count, digest))
    return raw + line({"trailer": "agent-hub-export-v1", "rows": count, "sha256": digest, "signature": signature})


def find_row(body: list[Json], table: str, predicate: Callable[[Json], bool] | None = None) -> Json:
    """The `row` of the first line of `table` that matches."""
    for item in body[1:]:
        if item["table"] == table and (predicate is None or predicate(item["row"])):
            return item["row"]  # type: ignore[no-any-return]
    raise LookupError(table)


def b64(raw: bytes) -> str:
    return b64u_encode(raw)


# --- a second database -----------------------------------------------------------------------------------------------


@contextmanager
def fresh_database() -> Iterator[HubDatabase]:
    """Another empty database with its own owner and writer roles (the target of a restore)."""
    params = admin_params()
    suffix = secrets.token_hex(6)
    database = HubDatabase(
        admin_params=params,
        name=f"t_hub_{suffix}",
        owner_role=f"t_hub_owner_{suffix}",
        writer_role=f"t_hub_writer_{suffix}",
        owner_password=secrets.token_hex(32),
        writer_password=secrets.token_hex(32),
        host=str(params["host"]),
        port=int(params.get("port") or 5432),
    )
    try:
        with psycopg.connect(**params, autocommit=True) as admin:
            assert admin.info.server_version >= MIN_SERVER_VERSION
            create_role(admin, database.owner_role, database.owner_password)
            create_role(admin, database.writer_role, database.writer_password)
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database.name), sql.Identifier(database.owner_role)
                )
            )
        yield database
    finally:
        with psycopg.connect(**params, autocommit=True) as admin:
            drop_database(admin, database.name, (database.writer_role, database.owner_role))


def table_rows(database: HubDatabase, table: str) -> list[tuple[Any, ...]]:
    """Every row of a table, sorted by its text form (superuser connection)."""
    with database.admin() as connection:
        query = sql.SQL("SELECT t::text FROM hub.{} t ORDER BY 1").format(sql.Identifier(table))
        return connection.execute(query).fetchall()


__all__ = ["CANARIES", "Estate", "Jsonb", "b64", "find_row", "fresh_database", "lines", "resign", "rows_of"]
