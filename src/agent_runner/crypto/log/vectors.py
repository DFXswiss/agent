"""Copied from src/agent_hub/log/vectors.py.

Log vectors of 04-crypto.md section 10 (`contracts/vectors/log*.json`, `platform-log.json`), discovered by the
generator of `agent_hub.crypto.vectors` through `SECTIONS`.

Every file is self-contained: it carries the signed logs it uses under `logs` (name -> entries). Operation arguments
refer to a log prefix as `{"name": <log>, "count": n}` (the first n entries); `entries`/`append` add entries after
that prefix. All keys, codes, wraps and events are derived from fixed labels, so the files are byte-identical on every
run. Negative vectors are executed while generating and must fail with exactly the stated code (as in
`agent_hub.crypto.vectors.negative`). `LogBuilder` and `PlatformBuilder` are also used by the tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_runner.crypto import agent as agent_format
from agent_runner.crypto import enroll, event, invite, keys, signed, wrap
from agent_runner.crypto import identity as identity_proof
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, body_json, format_time, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import catchup, events, platform, rotation, verify, wraps
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.entries import Head, check_extends, entry_hash, sign_log_entry, sign_platform_entry
from agent_runner.crypto.log.state import OrgState
from agent_runner.crypto.vectors import Section, fixed, fixed_id

Json = dict[str, Any]
START = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
EXPIRES = "2026-11-24T12:00:00.000Z"
CAPACITY = {
    "vm_memory_budget_mib": 65536,
    "vm_memory_used_mib": 0,
    "vm_slots_total": 8,
    "vm_slots_free": 8,
    "pool_ready": 2,
}
NAMES = ("alice", "bob", "carol", "dave", "erin", "device", "dev2", "pdev", "pdev-new", "qdev", "hub")
AGENTS = ("bot", "bot2", "bot3", "bot4", "bot5", "bot6", "bot7")
"""Labels of agents (ADR-082); their identities are listed with the actors."""


def identity(label: str) -> keys.Identity:
    """The fixed identity of an actor of the log vectors."""
    return keys.Identity.from_raw(fixed("log/" + label + "/kem"), fixed("log/" + label + "/sig"))


def pid(label: str) -> str:
    """The fixed principal (or object) ID of a label."""
    return fixed_id("log/" + label)


def code(label: str) -> bytes:
    """The fixed 16-byte invitation code of a label."""
    return fixed("log/code/" + label, 16)


def secrets(label: str) -> invite.InviteSecrets:
    """Invitation secrets of the code of a label."""
    return invite.invite_secrets(code(label))


def pub(label: str) -> dict[str, str]:
    """`{"kem", "sig"}` of a label."""
    return identity(label).public()


def sig_pk(label: str) -> str:
    """The b64u Ed25519 public key of a label."""
    return pub(label)["sig"]


def ts(minutes: int) -> str:
    """`START + minutes` as a header time."""
    return format_time(START + timedelta(minutes=minutes))


ORG = pid("org")
OTHER_ORG = pid("org/other")
"""An organization without an `org_platform_access` of its own (ADR-079 switch vectors)."""
W0 = pid("wg/default")
W1 = pid("wg/one")
W2 = pid("wg/two")
ROOT = "platform-root"


class LogBuilder:
    """Builds a signed membership log entry by entry (labels name the actors)."""

    def __init__(self, org: str = ORG, entries: Sequence[Json] = ()) -> None:
        self.org = org
        self.entries: list[Json] = list(entries)

    def body(self, entry_type: str, by: str, /, **fields: object) -> Json:
        """The next body (seq, prev, ts) without signing it."""
        seq = len(self.entries)
        prev = entry_hash(self.entries[-1]) if self.entries else ""
        return {
            "org": self.org,
            "seq": seq,
            "prev": prev,
            "type": entry_type,
            "by": pid(by),
            "ts": ts(seq),
            "v": 1,
            **fields,
        }

    def add(self, entry_type: str, by: str, /, *, signer: str | None = None, **fields: object) -> Json:
        """Append an entry signed by `signer` (default: `by`)."""
        entry = sign_log_entry(self.body(entry_type, by, **fields), identity(signer or by).sig)
        self.entries.append(entry)
        return entry

    def genesis(
        self, founder: str = "alice", default_wg: str = W0, private: bool = False, agent: str | None = None
    ) -> Json:
        """`genesis` by the founder; with `agent` (generation 1) the entry adds the founder's agent and is signed by
        the agent's key (ADR-082)."""
        extra = {} if agent is None else {"agent": agent_member(agent, founder, 1)}
        return self.add(
            "genesis",
            founder,
            signer=agent,
            subject=pid(founder),
            pub=pub(founder),
            default_wg=default_wg,
            private=private,
            **extra,
        )

    def agent_add(
        self,
        by: str,
        agent: str,
        user: str,
        gen: int,
        *,
        signer: str | None = None,
        cert: Json | None = None,
    ) -> Json:
        """`agent_add` of `agent` for `user` by `by` (the user or the agent itself); `cert` changes the arguments of
        the certificate (`signer`, `agent`, `pub`, `user`, `gen` as labels or values)."""
        return self.add(
            "agent_add",
            by,
            signer=signer,
            subject=pid(agent),
            user=uid(user),
            pub=pub(agent),
            gen=gen,
            cert=certificate(agent, user, gen, **(cert or {})),
        )

    def invite_user(self, by: str, label: str, role: str = "member", wgs: Mapping[str, str] | None = None) -> Json:
        """`invite` of kind user with the code of `label`."""
        found = secrets(label)
        return self.add(
            "invite",
            by,
            invite_id=found.invite_id,
            pk_inv=found.pk_inv,
            kind="user",
            role=role,
            wgs=dict(wgs or {}),
            expires=EXPIRES,
        )

    def invite_device(self, by: str, label: str, wgs: Sequence[str] = ()) -> Json:
        """`invite` of kind device with the code of `label`."""
        found = secrets(label)
        return self.add(
            "invite",
            by,
            invite_id=found.invite_id,
            pk_inv=found.pk_inv,
            kind="device",
            wgs=dict.fromkeys(wgs, "device"),
            expires=EXPIRES,
        )

    def accept(
        self,
        label: str,
        kind: str = "user",
        code_label: str | None = None,
        key_label: str | None = None,
        agent: Json | None = None,
    ) -> Json:
        """`accept` by `label` with its own code (or `code_label`'s) and its own keys (or `key_label`'s), signed by
        the subject key and by `sk_inv`; with an `agent` member (`agent_member`) signed by the agent's key."""
        found = secrets(code_label or label)
        keys_of = key_label or label
        extra = {} if agent is None else {"agent": agent}
        body = self.body(
            "accept", label, invite_id=found.invite_id, subject=pid(label), kind=kind, pub=pub(keys_of), **extra
        )
        signer = identity(keys_of) if agent is None else identity(agent_label(agent["id"]))
        entry = sign_log_entry(body, signer.sig, found.signing)
        self.entries.append(entry)
        return entry

    def admit(
        self,
        by: str,
        label: str,
        platform_seq: int,
        by_platform_seq: int,
        *,
        role: str = "member",
        wgs: Mapping[str, str] | None = None,
        key_label: str | None = None,
        proof_code: str = "",
        by_proof_code: str = "",
        signer: str | None = None,
    ) -> Json:
        """`admit` of `label` (keys of `key_label`, default its own) anchored at `platform_seq`, by `by` anchored at
        `by_platform_seq`; a `proof_code`/`by_proof_code` makes the identity proof with that platform invitation code
        (ADR-060). The IDs of `INVITED` labels are the IDs derived from their invitation."""
        keys_of = pub(key_label or label)
        subject, author = uid(label), uid(by)
        body = self.body(
            "admit",
            by,
            subject=subject,
            pub=keys_of,
            platform_seq=platform_seq,
            id_sig=identity_proof.sign_identity(secrets(proof_code).signing, subject, keys_of) if proof_code else "",
            by_platform_seq=by_platform_seq,
            by_id_sig=(
                identity_proof.sign_identity(secrets(by_proof_code).signing, author, pub(by)) if by_proof_code else ""
            ),
            role=role,
            wgs=dict(wgs or {}),
        )
        body["by"] = author
        entry = sign_log_entry(body, identity(signer or by).sig)
        self.entries.append(entry)
        return entry

    def copy(self) -> LogBuilder:
        """An independent builder with the same entries."""
        return LogBuilder(self.org, self.entries)


def certificate(
    agent: str,
    user: str,
    gen: int,
    *,
    signer: str | None = None,
    pub_of: str | None = None,
    user_of: str | None = None,
    gen_of: int | None = None,
) -> str:
    """The delegation certificate of `agent` for `user` (ADR-082), signed by `signer` (default: the user); `pub_of`,
    `user_of` and `gen_of` certify other keys, another user or another generation."""
    return agent_format.sign_cert(
        identity(signer or user).sig,
        pid(agent),
        pub(pub_of or agent),
        uid(user_of or user),
        gen if gen_of is None else gen_of,
    )


def agent_member(agent: str, user: str, gen: int, **cert: Any) -> Json:
    """The `agent` member of `genesis`/`accept`: `{id, pub, gen, cert}`."""
    return {"id": pid(agent), "pub": pub(agent), "gen": gen, "cert": certificate(agent, user, gen, **cert)}


def agent_label(agent_id: str) -> str:
    """The label of an agent ID of `AGENTS`."""
    return next(label for label in AGENTS if pid(label) == agent_id)


class PlatformBuilder:
    """Builds a platform log signed by the fixed platform root (label `root`)."""

    def __init__(self, entries: Sequence[Json] = ()) -> None:
        self.entries: list[Json] = list(entries)

    def add(self, entry_type: str, /, *, signer: str = "root", **fields: object) -> Json:
        """Append an entry signed by `signer`."""
        seq = len(self.entries)
        prev = entry_hash(self.entries[-1]) if self.entries else ""
        body = {"seq": seq, "prev": prev, "type": entry_type, "by": ROOT, "ts": ts(seq), "v": 1, **fields}
        entry = sign_platform_entry(body, identity(signer).sig)
        self.entries.append(entry)
        return entry

    def certify(self, label: str, code_label: str | None = None, key_label: str | None = None) -> Json:
        """`device_certify` of `label` enrolled with the code of `code_label` and the keys of `key_label` (default:
        its own)."""
        found = secrets(code_label or label)
        keys_of = key_label or label
        body = {
            "device_id": pid(label),
            "kind": "platform",
            "invite_id": found.invite_id,
            "pub": pub(keys_of),
            "agent_version": "0.1.0",
            "os": "macos",
            "arch": "arm64",
            "capacity": CAPACITY,
        }
        signed = enroll.sign_enrollment(body, identity(keys_of).sig, found.signing)
        return self.add(
            "device_certify",
            device=pid(label),
            pub=pub(keys_of),
            invite_id=found.invite_id,
            enroll_sig=signed["enroll_sig"],
            inv_sig=signed["inv_sig"],
            agent_version="0.1.0",
            os="macos",
            arch="arm64",
            capacity=CAPACITY,
        )


def platform_builder() -> PlatformBuilder:
    """The platform log of the vectors: every entry type, one certified and one decertified device."""
    log = PlatformBuilder()
    log.add("root", pub=sig_pk("root"))
    log.add("device_invite", invite_id=secrets("pdev").invite_id, pk_inv=secrets("pdev").pk_inv, expires=EXPIRES)
    log.certify("pdev")  # seq 2
    log.add(
        "user_invite",
        invite_id=secrets("platform-user").invite_id,
        pk_inv=secrets("platform-user").pk_inv,
        expires=EXPIRES,
    )
    log.add("role_grant", subject=pid("alice"), role="inviter")
    log.add("org_platform_access", org=ORG, granted=True)
    log.add("device_invite", invite_id=secrets("qdev").invite_id, pk_inv=secrets("qdev").pk_inv, expires=EXPIRES)
    log.certify("qdev")  # seq 7
    log.add("device_decertify", device=pid("qdev"))
    log.add("role_revoke", subject=pid("alice"), role="inviter")
    log.add("org_platform_access", org=ORG, granted=False)
    # identity anchors of users (ADR-060)
    found = secrets("erin-platform")
    log.add("user_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=EXPIRES)  # 11
    log.add("user_certify", user=pid("alice"), pub=pub("alice"))  # 12
    log.add("user_certify", user=pid("dave"), pub=pub("dave"))  # 13
    log.add("user_certify", user=pid("bob"), pub=pub("bob"))  # 14
    log.add("user_decertify", user=pid("bob"))  # 15
    log.add("user_certify", user=pid("bob"), pub=pub("hub"))  # 16 (bob's identity renewed)
    # switches, the operator-subscription list and the role `operator` (ADR-079)
    log.add("platform_policy", name="open_platform_access", on=True)  # 17
    log.add("org_operator_subscriptions", org=ORG, granted=True)  # 18
    log.add("role_grant", subject=pid("alice"), role="operator")  # 19
    log.add("platform_policy", name="open_platform_invites", on=True)  # 20
    log.add("org_operator_subscriptions", org=ORG, granted=False)  # 21
    log.add("platform_policy", name="open_platform_access", on=False)  # 22
    return log


PDEV_SEQ = 2
QDEV_SEQ = 7
PDEV_RECERT_SEQ = 13
BASE_PLATFORM_COUNT = 11
"""The platform entries before the identity anchors (the membership vectors of the devices refer to them)."""
ERIN_INVITE_SEQ = 11
ALICE_CERT_SEQ = 12
DAVE_CERT_SEQ = 13
BOB_CERT_SEQ = 14
BOB_RENEWED_SEQ = 16
PLATFORM_COUNT = 23
INVITED = {"erin": ("erin-platform", ERIN_INVITE_SEQ)}
"""Labels registered through a platform invitation: their ID is derived from it (`identity.invited_user_id`)."""


def uid(label: str) -> str:
    """The user ID of a label: derived from its platform invitation for `INVITED` labels, else `pid`."""
    if label in INVITED:
        code_label, seq = INVITED[label]
        return identity_proof.invited_user_id(secrets(code_label).invite_id, ts(seq))
    return pid(label)


def recertified_builder() -> PlatformBuilder:
    """The platform log, then `pdev` decertified (its key is compromised) and certified again with new keys."""
    log = PlatformBuilder(platform_builder().entries[:BASE_PLATFORM_COUNT])
    log.add("device_decertify", device=pid("pdev"))  # 11
    found = secrets("pdev-again")
    log.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=EXPIRES)  # 12
    log.certify("pdev", code_label="pdev-again", key_label="pdev-new")  # 13
    return log


def admit_builder() -> LogBuilder:
    """The main log, then direct admissions (ADR-060): dave by a root certification with read on `W1`, erin (ID
    derived from its platform invitation) by the identity proof of that invitation as admin, then erin admits bob (a
    former member) again with its earlier keys, anchored by bob's certification that was ended later; dave leaves."""
    log = LogBuilder(ORG, main_builder().entries)
    log.admit("alice", "dave", DAVE_CERT_SEQ, ALICE_CERT_SEQ, wgs={W1: "read"})  # 27
    log.admit("alice", "erin", ERIN_INVITE_SEQ, ALICE_CERT_SEQ, role="admin", proof_code="erin-platform")  # 28
    log.admit("erin", "bob", BOB_CERT_SEQ, ERIN_INVITE_SEQ, wgs={W1: "write"}, by_proof_code="erin-platform")  # 29
    log.add("remove", "dave", subject=pid("dave"))  # 30 (an admitted member leaves)
    return log


ADMIT_COUNT = 31


def main_builder() -> LogBuilder:
    """The main membership log: a valid chain with every entry type (the comments give the `seq`)."""
    log = LogBuilder()
    log.genesis()  # 0
    log.add("wg_create", "alice", wg=W1, epoch=1)  # 1
    log.invite_user("alice", "bob", wgs={W1: "read"})  # 2
    log.accept("bob")  # 3
    log.invite_device("alice", "device", [W1])  # 4
    log.accept("device", "device")  # 5
    log.invite_user("alice", "carol", role="admin")  # 6
    log.accept("carol")  # 7
    log.invite_user("alice", "dave")  # 8
    log.add("invite_revoke", "alice", invite_id=secrets("dave").invite_id)  # 9
    log.add("role", "alice", subject=pid("bob"), role="admin")  # 10
    log.add("role", "carol", subject=pid("bob"), role="member")  # 11
    log.add("grant", "alice", subject=pid("bob"), wg=W1, access="write")  # 12
    log.add("device_assign", "alice", device=pid("pdev"), wg=W1, platform_seq=PDEV_SEQ)  # 13
    log.add("device_assign", "alice", device=pid("device"), wg=W0)  # 14
    log.add("device_unassign", "alice", device=pid("device"), wg=W0)  # 15
    log.add("rotate", "alice", scope="wg", scope_id=W0, epoch=2)  # 16
    log.add("ungrant", "alice", subject=pid("bob"), wg=W1)  # 17
    log.add("remove", "alice", subject=pid("carol"))  # 18 (an admin: it read W1 by inheritance, ADR-058)
    log.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2)  # 19 (R of 17 and 18)
    log.add("rotate", "alice", scope="org", scope_id=ORG, epoch=2)  # 20
    log.add("rotate", "alice", scope="wg", scope_id=W0, epoch=3)  # 21
    log.add("wg_create", "alice", wg=W2, epoch=1)  # 22
    log.add("wg_archive", "alice", wg=W2)  # 23
    log.add("remove", "bob", subject=pid("bob"))  # 24 (leave: rotation pending)
    log.add("rotate", "alice", scope="org", scope_id=ORG, epoch=3)  # 25
    log.add("rotate", "alice", scope="wg", scope_id=W0, epoch=4)  # 26
    return log


def orgwide_builder() -> LogBuilder:
    """A log with assignments to the whole organization (ADR-075): an org device assigned to the org next to its
    explicit assignment, a workgroup created afterwards, the org assignment ended (the explicit one stays), a platform
    device assigned to the org and then removed."""
    log = LogBuilder()
    log.genesis()  # 0
    log.add("wg_create", "alice", wg=W1, epoch=1)  # 1
    log.invite_device("alice", "device", [W1])  # 2
    log.accept("device", "device")  # 3 (explicitly assigned to W1)
    log.add("device_assign_org", "alice", device=pid("device"))  # 4 (now every workgroup)
    log.add("wg_create", "alice", wg=W2, epoch=1)  # 5 (the org device reads it as well)
    log.add("device_unassign_org", "alice", device=pid("device"))  # 6 (W1 stays)
    log.add("rotate", "alice", scope="wg", scope_id=W0, epoch=2)  # 7
    log.add("rotate", "alice", scope="wg", scope_id=W2, epoch=2)  # 8
    log.add("device_assign_org", "alice", device=pid("pdev"), platform_seq=PDEV_SEQ)  # 9
    log.add("remove", "alice", subject=pid("pdev"))  # 10
    log.add("rotate", "alice", scope="org", scope_id=ORG, epoch=2)  # 11
    log.add("rotate", "alice", scope="wg", scope_id=W0, epoch=3)  # 12
    log.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2)  # 13
    log.add("rotate", "alice", scope="wg", scope_id=W2, epoch=3)  # 14
    return log


ORGWIDE_COUNT = 15


def agents_builder() -> LogBuilder:
    """A log with agents (ADR-082): alice creates the org through her agent `bot`, which then works with her admin
    rights; bob accepts through his agent `bot2`; carol adds `bot3` herself, removes it, and `bot4` adds itself with
    the next generation; bob's `bot5` replaces `bot2`; bob is removed (with `bot5`) and rejoins through `bot5`; `bot4`
    leaves and the pending rotations are done."""
    log = LogBuilder()
    log.genesis(agent="bot")  # 0 (signed by bot, adds bot gen 1)
    log.add("wg_create", "bot", wg=W1, epoch=1)  # 1 (alice gets write on W1)
    log.invite_user("bot", "bob", wgs={W1: "read"})  # 2
    log.accept("bob", agent=agent_member("bot2", "bob", 1))  # 3
    log.invite_user("alice", "carol")  # 4
    log.accept("carol")  # 5
    log.agent_add("carol", "bot3", "carol", 1)  # 6 (by the user)
    log.add("grant", "bot", subject=pid("bob"), wg=W1, access="write")  # 7
    log.add("rotate", "bot", scope="wg", scope_id=W1, epoch=2)  # 8
    log.invite_device("alice", "device", [W1])  # 9
    log.accept("device", "device")  # 10
    log.add("remove", "carol", subject=pid("bot3"))  # 11 (the user removes its agent)
    log.agent_add("bot4", "bot4", "carol", 2)  # 12 (the agent adds itself)
    log.agent_add("bob", "bot5", "bob", 2)  # 13 (replaces bot2)
    log.add("remove", "alice", subject=pid("bob"))  # 14 (bot5 is removed with bob)
    log.invite_user("alice", "bob-again")  # 15
    log.accept("bob", code_label="bob-again", agent=agent_member("bot5", "bob", 2))  # 16 (bot5 comes back)
    log.add("remove", "bot4", subject=pid("bot4"))  # 17 (the agent leaves: rotation pending)
    log.add("rotate", "alice", scope="org", scope_id=ORG, epoch=2)  # 18
    log.add("rotate", "alice", scope="wg", scope_id=W0, epoch=2)  # 19
    return log


AGENTS_COUNT = 20
ORG_B = pid("org/b")
WB = pid("wg/b")


def other_org_builder() -> LogBuilder:
    """A second org of alice in which she adds `bot6` (generation 2): it supersedes `bot` in every log."""
    log = LogBuilder(ORG_B)
    log.genesis(default_wg=WB)  # 0
    log.agent_add("alice", "bot6", "alice", 2)  # 1
    return log


def scope_key(scope: str, scope_id: str, epoch: int) -> bytes:
    """The fixed key of a scope epoch."""
    return fixed(f"log/key/{scope}/{scope_id}/{epoch}")


def make_wrap(wrapper: str, scope: str, scope_id: str, epoch: int, recipient: str) -> Json:
    """A signed wrap `{scope, scope_id, epoch, recipient_id, wrap, sig}` with a fixed ephemeral key."""
    sealed = wrap.wrap_key(
        identity(recipient).kem_public(),
        scope=scope,
        scope_id=scope_id,
        epoch=epoch,
        recipient=pid(recipient),
        key=scope_key(scope, scope_id, epoch),
        ephemeral=keys.x25519_private(fixed(f"log/eph/{wrapper}/{scope}/{scope_id}/{epoch}/{recipient}")),
    )
    signature = wrap.sign_wrap(
        identity(wrapper).sig, scope=scope, scope_id=scope_id, epoch=epoch, recipient=pid(recipient), wrap=sealed
    )
    return {
        "scope": scope,
        "scope_id": scope_id,
        "epoch": epoch,
        "recipient_id": pid(recipient),
        "wrap": sealed,
        "sig": signature,
    }


def make_event(
    author: str, kind: str, *, scope: str, scope_id: str, epoch: int, lh: int, label: str, **extra: str
) -> Json:
    """A sealed event with fixed salt and nonce, encrypted under `scope_key(scope, scope_id, epoch)`."""
    header = event.new_header(
        id=pid("event/" + label),
        org=ORG,
        scope=scope,
        scope_id=scope_id,
        epoch=epoch,
        author=pid(author),
        kind=kind,
        ts=ts(100),
        lh=lh,
        **extra,
    )
    return event.seal_event(
        scope_key(scope, scope_id, epoch),
        header,
        body_json({"label": label, "text": "Grüße"}),
        identity(author).sig,
        fixed_salt_nonce=(fixed("log/salt/" + label), fixed("log/nonce/" + label, 12)),
    )


# --- Operations named by the vectors (the same names exist in every implementation). ---


class Context:
    """The logs of one vector file and the operations that resolve references into them."""

    def __init__(self, logs: Mapping[str, Sequence[Json]]) -> None:
        self.logs = {name: list(entries) for name, entries in logs.items()}
        self.ops: dict[str, Callable[[Json], object]] = {
            "verify_log": lambda a: verify.verify_log(self.entries(a), a["founder"], platform=self.platform(a)),
            "append_log": self._append_log,
            "check_extends": lambda a: check_extends(Head(a["head"]["seq"], a["head"]["hash"]), self.entries(a)),
            "require_lh": lambda a: verify.require_lh(self.log(a), a["lh"]),
            "verify_platform_log": lambda a: platform.verify_platform_log(self.entries(a), a["root"]),
            "append_platform_log": lambda a: platform.append_platform(
                platform.verify_platform_log(self.entries(a), a["root"]), a["append"], a["root"]
            ),
            "check_batch": self._check_batch,
            "check_wrap": lambda a: wraps.check_wrap(
                a["wrap"],
                a["wrapped_by"],
                self.log(a),
                a.get("excluded", []),
                require_active=a.get("require_active", False),
            ),
            "check_cred_wrap": lambda a: wraps.check_cred_wrap(
                a["wrap"], a["wrapped_by"], self.logs_of(a), a["current_epoch"], a.get("excluded", [])
            ),
            "cred_readers": lambda a: sorted(wraps.cred_readers([log.state for log in self.logs_of(a)], a["owner"])),
            "revoked_agents": lambda a: sorted(wraps.revoked_agents([log.state for log in self.logs_of(a)])),
            "check_event": lambda a: events.check_event(
                a["event"], self.log(a), session_device=a.get("session_device"), excluded=a.get("excluded", [])
            ),
            "check_event_hub": lambda a: events.check_event_hub(
                a["event"],
                self.log(a),
                now=parse_time(a["now"]),
                rotated_at=parse_time(a["rotated_at"]) if a.get("rotated_at") else None,
                session_device=a.get("session_device"),
                excluded=a.get("excluded", []),
            ),
            "check_replay": lambda a: events.check_replay(a["stored"], a["incoming"]),
        }

    def entries(self, args: Json) -> list[Json]:
        """The prefix `args["log"]` followed by `args["entries"]`."""
        ref = args["log"]
        return self.logs[ref["name"]][: ref["count"]] + list(args.get("entries", []))

    def platform(self, args: Json) -> platform.PlatformState | None:
        """The verified platform state named by `args["platform"]`, if any."""
        ref = args.get("platform")
        if ref is None:
            return None
        return platform.verify_platform_log(self.logs[ref["name"]][: ref["count"]], ref["root"]).state

    def log(self, args: Json) -> verify.VerifiedLog:
        """The verified membership log of `args`."""
        return verify.verify_log(self.entries(args), args["founder"], platform=self.platform(args))

    def logs_of(self, args: Json) -> list[verify.VerifiedLog]:
        """The verified logs of `args["logs"]` (each `{log, founder}`), else the one log of `args`."""
        return [self.log(ref) for ref in args["logs"]] if "logs" in args else [self.log(args)]

    def _append_log(self, args: Json) -> object:
        now = parse_time(args["now"]) if "now" in args else None
        return verify.append_entries(
            self.log(args),
            args["append"],
            founder=args["founder"],
            platform=self.platform(args),
            now=now,
            platform_access=args.get("platform_access", False),
        )

    def _check_batch(self, args: Json) -> object:
        return check_batch(
            self.log(args),
            args["append"],
            args["wraps"],
            wrapped_by=args["wrapped_by"],
            platform=self.platform(args),
            now=parse_time(args["now"]),
            platform_access=args.get("platform_access", False),
            excluded=args.get("excluded", []),
        )

    def run(self, op: str, args: Json) -> object:
        """Run one named operation."""
        return self.ops[op](args)

    def negative(self, name: str, op: str, args: Json, error: str) -> Json:
        """A negative vector; `op(args)` must fail with exactly `error`."""
        try:
            self.run(op, args)
        except CryptoError as refused:
            if refused.code == error:
                return {"name": name, "op": op, "args": args, "error": error}
            raise RuntimeError(f"vector {name}: {refused.code} instead of {error}") from None
        raise RuntimeError(f"vector {name}: no error instead of {error}")


def _identities() -> Json:
    return {
        name: {
            "id": pid(name),
            "kem_sk": b64u_encode(fixed("log/" + name + "/kem")),
            "sig_sk": b64u_encode(fixed("log/" + name + "/sig")),
            **pub(name),
        }
        for name in (*NAMES, *AGENTS, "root")
    }


def _ids() -> Json:
    return {"org": ORG, "default_wg": W0, "wg_one": W1, "wg_two": W2, "pdev_platform_seq": PDEV_SEQ}


def _ref(name: str, count: int) -> Json:
    return {"name": name, "count": count}


def _platform_ref(count: int = BASE_PLATFORM_COUNT, name: str = "platform") -> Json:
    return {"name": name, "count": count, "root": sig_pk("root")}


def _main_args(count: int, **extra: object) -> Json:
    return {"log": _ref("main", count), "founder": sig_pk("alice"), "platform": _platform_ref(), **extra}


def _resign(entry: Json, signer: str, **changes: object) -> Json:
    """The entry with a changed body, signed again by `signer` (an `inv_sig` is kept as it was)."""
    body = {**entry["body"], **changes}
    signed = sign_log_entry(body, identity(signer).sig)
    return {**entry, **signed}


# --- log.json ---


def build_log() -> Json:
    """Valid chain with every entry type and one broken variant per rule L1-L8, plus L9/L10 and the other codes."""
    main = main_builder()
    plat = platform_builder()
    recert = recertified_builder()
    admit = admit_builder()
    orgwide = orgwide_builder()
    agents = agents_builder()
    ctx = Context(
        {
            "main": main.entries,
            "platform": plat.entries,
            "platform_recertified": recert.entries,
            "admit": admit.entries,
            "orgwide": orgwide.entries,
            "agents": agents.entries,
        }
    )
    founder = sig_pk("alice")
    admitted = verify.verify_log(
        admit.entries, founder, platform=platform.verify_platform_log(plat.entries, sig_pk("root")).state
    )
    verified = verify.verify_log(
        main.entries, founder, platform=platform.verify_platform_log(plat.entries, sig_pk("root")).state
    )
    neg = ctx.negative

    def at(seq: int) -> LogBuilder:
        return LogBuilder(ORG, main.entries[:seq])

    def broken(seq: int, build: Callable[[LogBuilder], object]) -> Json:
        prefix = at(seq)
        build(prefix)
        return _main_args(seq, entries=prefix.entries[seq:])

    wg1 = main.entries[1]
    forged_accept = at(3)
    hub_body = forged_accept.body(
        "accept", "bob", invite_id=secrets("bob").invite_id, subject=pid("bob"), kind="user", pub=pub("hub")
    )
    swapped = sign_log_entry(hub_body, identity("hub").sig, identity("hub").sig)
    fork = LogBuilder(ORG, main.entries[:20])
    fork.add("rotate", "alice", scope="wg", scope_id=W1, epoch=3)
    invalid = [
        neg("log_empty", "verify_log", {**_main_args(0)}, "log_empty"),
        # L1
        neg("L1_seq_gap", "verify_log", _main_args(1, entries=[_resign(wg1, "alice", seq=2)]), "log_chain"),
        neg(
            "L1_other_org",
            "verify_log",
            _main_args(1, entries=[_resign(wg1, "alice", org=pid("org/other"))]),
            "log_chain",
        ),
        neg("L1_duplicate_entry", "verify_log", _main_args(2, entries=[wg1]), "log_chain"),
        # L2
        neg("L2_prev", "verify_log", _main_args(1, entries=[_resign(wg1, "alice", prev=entry_hash(wg1))]), "log_chain"),
        # L3
        neg("L3_founder_not_pinned", "verify_log", {**_main_args(1), "founder": sig_pk("bob")}, "log_genesis"),
        neg(
            "L3_genesis_by_other",
            "verify_log",
            {**_main_args(0), "entries": [_resign(main.entries[0], "hub", pub=pub("hub"))]},
            "log_genesis",
        ),
        neg(
            "L3_subject_not_by",
            "verify_log",
            {**_main_args(0), "entries": [_resign(main.entries[0], "alice", subject=pid("bob"))]},
            "log_genesis",
        ),
        neg(
            "L3_first_not_genesis",
            "verify_log",
            {**_main_args(0), "entries": [_resign(wg1, "alice", seq=0, prev="")]},
            "log_genesis",
        ),
        neg("L3_second_genesis", "verify_log", broken(1, lambda b: b.genesis()), "log_genesis"),
        # L4
        neg("L4_wrong_key", "verify_log", _main_args(1, entries=[_resign(wg1, "hub")]), "log_signature"),
        neg(
            "L4_unknown_author",
            "verify_log",
            broken(1, lambda b: b.add("wg_create", "hub", wg=W1, epoch=1)),
            "log_signature",
        ),
        neg(
            "L4_hub_injects_member",
            "verify_log",
            broken(3, lambda b: b.add("grant", "alice", signer="hub", subject=pid("bob"), wg=W1, access="write")),
            "log_signature",
        ),
        neg("L4_signature_missing", "verify_log", _main_args(1, entries=[{**wg1, "sig": ""}]), "log_signature"),
        # L5
        neg(
            "L5_member_creates_wg",
            "verify_log",
            broken(4, lambda b: b.add("wg_create", "bob", wg=W2, epoch=1)),
            "log_rights",
        ),
        neg("L5_member_invites", "verify_log", broken(4, lambda b: b.invite_user("bob", "erin")), "log_rights"),
        neg(
            "L5_grant_default_wg",
            "verify_log",
            broken(4, lambda b: b.add("grant", "alice", subject=pid("bob"), wg=W0, access="read")),
            "log_rights",
        ),
        neg(
            "L5_ungrant_default_wg",
            "verify_log",
            broken(4, lambda b: b.add("ungrant", "alice", subject=pid("bob"), wg=W0)),
            "log_rights",
        ),
        neg(
            "L5_archive_default_wg",
            "verify_log",
            broken(4, lambda b: b.add("wg_archive", "alice", wg=W0)),
            "log_rights",
        ),
        neg(
            "L5_invite_default_wg",
            "verify_log",
            broken(4, lambda b: b.invite_user("alice", "erin", wgs={W0: "read"})),
            "log_rights",
        ),
        neg(
            "L5_invite_unknown_wg",
            "verify_log",
            broken(4, lambda b: b.invite_user("alice", "erin", wgs={W2: "read"})),
            "log_rights",
        ),
        neg(
            "L5_grant_archived_wg",
            "verify_log",
            broken(24, lambda b: b.add("grant", "alice", subject=pid("bob"), wg=W2, access="read")),
            "log_rights",
        ),
        neg(
            "L5_assign_unknown_device",
            "verify_log",
            broken(4, lambda b: b.add("device_assign", "alice", device=pid("device"), wg=W1)),
            "log_rights",
        ),
        neg(
            "L5_assign_platform_seq_not_certify",
            "verify_log",
            broken(13, lambda b: b.add("device_assign", "alice", device=pid("pdev"), wg=W1, platform_seq=1)),
            "log_rights",
        ),
        neg(
            "L5_assign_platform_device_other_id",
            "verify_log",
            broken(13, lambda b: b.add("device_assign", "alice", device=pid("qdev"), wg=W1, platform_seq=PDEV_SEQ)),
            "log_rights",
        ),
        neg(
            "L5_unassign_not_assigned",
            "verify_log",
            broken(6, lambda b: b.add("device_unassign", "alice", device=pid("device"), wg=W0)),
            "log_rights",
        ),
        neg(
            "L5_assign_org_twice",
            "verify_log",
            broken(
                6,
                lambda b: (
                    b.add("device_assign_org", "alice", device=pid("device")),
                    b.add("device_assign_org", "alice", device=pid("device")),
                ),
            ),
            "log_rights",
        ),
        neg(
            "L5_assign_org_unknown_device",
            "verify_log",
            broken(4, lambda b: b.add("device_assign_org", "alice", device=pid("device"))),
            "log_rights",
        ),
        neg(
            "L5_assign_org_by_member",
            "verify_log",
            broken(6, lambda b: b.add("device_assign_org", "bob", device=pid("device"))),
            "log_rights",
        ),
        neg(
            "L5_unassign_org_not_assigned",
            "verify_log",
            broken(6, lambda b: b.add("device_unassign_org", "alice", device=pid("device"))),
            "log_rights",
        ),
        neg(
            "L5_assign_wg_while_org_assigned",
            "verify_log",
            broken(
                6,
                lambda b: (
                    b.add("device_assign_org", "alice", device=pid("device")),
                    b.add("device_assign", "alice", device=pid("device"), wg=W0),
                ),
            ),
            "log_rights",
        ),
        neg(
            "L5_unassign_wg_of_org_device",
            "verify_log",
            broken(
                6,
                lambda b: (
                    b.add("device_assign_org", "alice", device=pid("device")),
                    b.add("device_unassign", "alice", device=pid("device"), wg=W0),
                ),
            ),
            "log_rights",
        ),
        neg(
            "L5_assign_org_platform_seq_not_certify",
            "verify_log",
            broken(13, lambda b: b.add("device_assign_org", "alice", device=pid("pdev"), platform_seq=1)),
            "log_rights",
        ),
        neg(
            "L5_member_removes_other",
            "verify_log",
            broken(4, lambda b: b.add("remove", "bob", subject=pid("alice"))),
            "log_rights",
        ),
        neg(
            "L5_member_rotates_org",
            "verify_log",
            broken(4, lambda b: b.add("rotate", "bob", scope="org", scope_id=ORG, epoch=2)),
            "log_rights",
        ),
        neg(
            "L5_reader_rotates_wg",
            "verify_log",
            broken(4, lambda b: b.add("rotate", "bob", scope="wg", scope_id=W1, epoch=2)),
            "log_rights",
        ),
        neg(
            "L5_role_unchanged",
            "verify_log",
            broken(4, lambda b: b.add("role", "alice", subject=pid("bob"), role="member")),
            "log_rights",
        ),
        neg(
            "last_admin_demoted",
            "verify_log",
            broken(1, lambda b: b.add("role", "alice", subject=pid("alice"), role="member")),
            "last_admin",
        ),
        neg(
            "last_admin_leaves",
            "verify_log",
            broken(4, lambda b: b.add("remove", "alice", subject=pid("alice"))),
            "last_admin",
        ),
        # L6
        neg(
            "L6_rotate_skips_epoch",
            "verify_log",
            broken(4, lambda b: b.add("rotate", "alice", scope="wg", scope_id=W1, epoch=3)),
            "log_epoch",
        ),
        neg(
            "L6_rotate_same_epoch",
            "verify_log",
            broken(4, lambda b: b.add("rotate", "alice", scope="org", scope_id=ORG, epoch=1)),
            "log_epoch",
        ),
        neg(
            "L6_wg_create_epoch",
            "verify_log",
            broken(4, lambda b: b.add("wg_create", "alice", wg=W2, epoch=2)),
            "log_epoch",
        ),
        # L7
        neg(
            "L7_accept_twice", "verify_log", broken(4, lambda b: b.accept("erin", code_label="bob")), "invite_consumed"
        ),
        neg("invite_revoked", "verify_log", broken(10, lambda b: b.accept("dave")), "invite_revoked"),
        neg("invite_not_in_log", "verify_log", broken(4, lambda b: b.accept("erin")), "invite_not_in_log"),
        neg(
            "revoke_unknown_invite",
            "verify_log",
            broken(4, lambda b: b.add("invite_revoke", "alice", invite_id=secrets("erin").invite_id)),
            "invite_not_in_log",
        ),
        neg(
            "revoke_accepted_invite",
            "verify_log",
            broken(4, lambda b: b.add("invite_revoke", "alice", invite_id=secrets("bob").invite_id)),
            "invite_consumed",
        ),
        neg(
            "revoke_twice",
            "verify_log",
            broken(10, lambda b: b.add("invite_revoke", "alice", invite_id=secrets("dave").invite_id)),
            "invite_revoked",
        ),
        neg("accept_swapped_keys", "verify_log", _main_args(3, entries=[swapped]), "log_accept"),
        neg(
            "accept_other_code",
            "verify_log",
            broken(3, lambda b: b.accept("bob", code_label="carol")),
            "invite_not_in_log",
        ),
        neg(
            "accept_inv_sig_wrong",
            "verify_log",
            _main_args(3, entries=[{**main.entries[3], "inv_sig": main.entries[7]["inv_sig"]}]),
            "log_accept",
        ),
        neg("accept_kind_mismatch", "verify_log", broken(3, lambda b: b.accept("bob", kind="device")), "log_accept"),
        neg("accept_member_again", "verify_log", broken(9, lambda b: b.accept("bob", code_label="dave")), "log_accept"),
        # L8
        neg(
            "L8_removed_author",
            "verify_log",
            broken(20, lambda b: b.add("wg_create", "carol", wg=pid("wg/three"), epoch=1)),
            "log_rights",
        ),
        neg(
            "L8_removed_leaves_again",
            "verify_log",
            broken(25, lambda b: b.add("remove", "bob", subject=pid("bob"))),
            "log_rights",
        ),
        # formats
        neg("unknown_type", "verify_log", _main_args(1, entries=[_resign(wg1, "alice", type="wg_delete")]), "log_type"),
        neg(
            "missing_field",
            "verify_log",
            _main_args(1, entries=[{**wg1, "body": {k: v for k, v in wg1["body"].items() if k != "epoch"}}]),
            "header_invalid",
        ),
        neg("extra_field", "verify_log", _main_args(1, entries=[_resign(wg1, "alice", name="x")]), "header_invalid"),
        neg(
            "non_ascii",
            "verify_log",
            _main_args(1, entries=[{**wg1, "body": {**wg1["body"], "ts": "2026-11-21T12:01:00.000Ž"}}]),
            "non_ascii",
        ),
        neg(
            "inv_sig_on_other_type",
            "verify_log",
            _main_args(1, entries=[{**wg1, "inv_sig": wg1["sig"]}]),
            "header_invalid",
        ),
        neg("version", "verify_log", _main_args(1, entries=[_resign(wg1, "alice", v=2)]), "header_invalid"),
        # hub append mode
        neg("seq_conflict", "append_log", {**_main_args(3), "append": [main.entries[4]]}, "seq_conflict"),
        neg(
            "invite_expired",
            "append_log",
            {**_main_args(3), "append": [main.entries[3]], "now": EXPIRES},
            "invite_expired",
        ),
        neg(
            "assign_decertified_device",
            "append_log",
            {
                **_main_args(13),
                "append": [at(13).add("device_assign", "alice", device=pid("qdev"), wg=W1, platform_seq=QDEV_SEQ)],
                "now": ts(30),
                "platform_access": True,
            },
            "log_rights",
        ),
        neg(
            "assign_without_platform_access",
            "append_log",
            {**_main_args(13), "append": [main.entries[13]], "now": ts(30)},
            "log_rights",
        ),
        neg(
            "assign_org_without_platform_access",
            "append_log",
            {
                **_main_args(13),
                "append": [at(13).add("device_assign_org", "alice", device=pid("pdev"), platform_seq=PDEV_SEQ)],
                "now": ts(30),
            },
            "log_rights",
        ),
        neg(
            "assign_revoked_certification_of_recertified_device",
            "append_log",
            {
                **_main_args(13),
                "platform": _platform_ref(len(recert.entries), "platform_recertified"),
                "append": [main.entries[13]],
                "now": ts(30),
                "platform_access": True,
            },
            "log_rights",
        ),
        neg(
            "rejoin_user_with_other_keys",
            "verify_log",
            broken(
                25,
                lambda b: (b.invite_user("alice", "erin"), b.accept("bob", code_label="erin", key_label="hub")),
            ),
            "log_accept",
        ),
        # L9, L10
        neg(
            "L9_shorter",
            "check_extends",
            {"head": {"seq": 26, "hash": entry_hash(main.entries[26])}, "log": _ref("main", 26)},
            "log_rollback",
        ),
        neg(
            "L9_fork",
            "check_extends",
            {"head": {"seq": 20, "hash": entry_hash(fork.entries[20])}, "log": _ref("main", 27)},
            "log_rollback",
        ),
        neg("L10_lh_beyond_head", "require_lh", _main_args(27, lh=27), "log_fork"),
        *_admit_negatives(ctx, admit),
    ]
    agents_valid, agents_invalid = _agent_log_part(ctx, agents)
    invalid += agents_invalid
    return {
        "about": "Membership log of 04-crypto.md section 4.7: a valid chain with every entry type, its states, and "
        "one broken variant per rule L1-L10 (06-1) with the expected code. See README.md, 'Log vectors'.",
        "identities": _identities(),
        "ids": _ids(),
        "founder": founder,
        "platform_root": sig_pk("root"),
        "logs": {
            "main": main.entries,
            "platform": plat.entries,
            "platform_recertified": recert.entries,
            "admit": admit.entries,
            "orgwide": orgwide.entries,
            "agents": agents.entries,
        },
        "excluded": [
            {"platform": _platform_ref(), "log": _ref("main", 27), "excluded": []},
            {
                "platform": _platform_ref(len(recert.entries), "platform_recertified"),
                "log": _ref("main", 27),
                "excluded": sorted(
                    platform.verify_platform_log(recert.entries, sig_pk("root")).state.excluded(verified.state)
                ),
            },
        ],
        "valid": {
            "log": _ref("main", 27),
            "platform": _platform_ref(),
            "heads": [{"seq": s.seq, "hash": s.head} for s in verified.states],
            "projections": [s.projection() for s in verified.states],
        },
        "admit_valid": {
            "log": _ref("admit", ADMIT_COUNT),
            "platform": _platform_ref(PLATFORM_COUNT),
            "heads": [{"seq": s.seq, "hash": s.head} for s in admitted.states[27:]],
            "projections": [s.projection() for s in admitted.states[27:]],
        },
        "extends": [
            {"head": {"seq": 20, "hash": entry_hash(main.entries[20])}, "log": _ref("main", 27)},
            {"head": {"seq": 26, "hash": entry_hash(main.entries[26])}, "log": _ref("main", 27)},
        ],
        "lh": {"log": _ref("main", 27), "needs_refresh": [{"lh": 26, "refresh": False}, {"lh": 27, "refresh": True}]},
        "orgwide": _orgwide_valid(orgwide, plat),
        "agents_valid": agents_valid,
        "invalid": invalid,
    }


def _agents_args(count: int, name: str = "agents", **extra: object) -> Json:
    return {"log": _ref(name, count), "founder": sig_pk("alice"), **extra}


def _agent_log_part(ctx: Context, agents: LogBuilder) -> tuple[Json, list[Json]]:
    """log.json, ADR-082: the valid agents log (heads, projections) and the negatives of the agent rules."""
    verified = verify.verify_log(agents.entries, sig_pk("alice"))
    neg = ctx.negative

    def broken(count: int, build: Callable[[LogBuilder], object]) -> Json:
        prefix = LogBuilder(ORG, agents.entries[:count])
        build(prefix)
        return _agents_args(count, entries=prefix.entries[count:])

    def first(build: Callable[[LogBuilder], Json]) -> Json:
        return build(LogBuilder())

    def genesis_with(member: Json, signer: str = "bot") -> Json:
        body = LogBuilder().body(
            "genesis", "alice", subject=pid("alice"), pub=pub("alice"), default_wg=W0, private=False, agent=member
        )
        return sign_log_entry(body, identity(signer).sig)

    agent_genesis = first(lambda b: b.genesis(agent="bot"))
    bot2 = agent_member("bot2", "bob", 1)
    added = agents.entries[6]  # agent_add of bot3 by carol
    without_gen = {**added, "body": {key: value for key, value in added["body"].items() if key != "gen"}}
    invalid = [
        # agent_add by the hub, by another agent or for keys, users or generations the user did not certify
        neg(
            "agent_add_cert_by_other_key",
            "verify_log",
            broken(6, lambda b: b.agent_add("bot3", "bot3", "carol", 1, cert={"signer": "hub"})),
            "log_agent",
        ),
        neg(
            "agent_add_cert_for_other_pub",
            "verify_log",
            broken(6, lambda b: b.agent_add("carol", "bot3", "carol", 1, cert={"pub_of": "hub"})),
            "log_agent",
        ),
        neg(
            "agent_add_cert_for_other_user",
            "verify_log",
            broken(6, lambda b: b.agent_add("carol", "bot3", "carol", 1, cert={"user_of": "alice"})),
            "log_agent",
        ),
        neg(
            "agent_add_cert_for_other_gen",
            "verify_log",
            broken(6, lambda b: b.agent_add("carol", "bot3", "carol", 1, cert={"gen_of": 2})),
            "log_agent",
        ),
        neg(
            "agent_adds_another_agent",
            "verify_log",
            broken(6, lambda b: b.agent_add("bot2", "bot3", "bob", 2)),
            "log_agent",
        ),
        neg(
            "agent_add_by_member_for_other_user",
            "verify_log",
            broken(6, lambda b: b.agent_add("bob", "bot3", "carol", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_user_not_member",
            "verify_log",
            broken(6, lambda b: b.agent_add("bot3", "bot3", "dave", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_subject_is_a_user",
            "verify_log",
            broken(6, lambda b: b.agent_add("carol", "bob", "carol", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_subject_is_a_device",
            "verify_log",
            broken(11, lambda b: b.agent_add("carol", "device", "carol", 2)),
            "log_agent",
        ),
        neg(
            "agent_add_second_agent_same_gen",
            "verify_log",
            broken(7, lambda b: b.agent_add("carol", "bot4", "carol", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_gen_of_removed_agent",
            "verify_log",
            broken(12, lambda b: b.agent_add("carol", "bot4", "carol", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_lower_gen",
            "verify_log",
            broken(13, lambda b: b.agent_add("carol", "bot6", "carol", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_removed_agent_again",
            "verify_log",
            broken(12, lambda b: b.agent_add("carol", "bot3", "carol", 1)),
            "log_agent",
        ),
        neg(
            "agent_add_returning_agent_other_gen",
            "verify_log",
            broken(
                16,
                lambda b: (b.accept("bob", code_label="bob-again"), b.agent_add("bob", "bot5", "bob", 3)),
            ),
            "log_agent",
        ),
        neg(
            "accept_with_agent_removed_otherwise",
            "verify_log",
            broken(16, lambda b: b.accept("bob", code_label="bob-again", agent=bot2)),
            "log_agent",
        ),
        neg(
            "agent_add_signed_by_other_key",
            "verify_log",
            broken(6, lambda b: b.agent_add("bot3", "bot3", "carol", 1, signer="hub")),
            "log_signature",
        ),
        neg(
            "agent_add_unknown_author",
            "verify_log",
            broken(6, lambda b: b.agent_add("dave", "bot3", "carol", 1)),
            "log_signature",
        ),
        neg(
            "agent_add_by_removed_user",
            "verify_log",
            broken(15, lambda b: b.agent_add("bob", "bot6", "bob", 3)),
            "log_rights",
        ),
        # genesis and accept with an agent member
        neg(
            "genesis_with_agent_signed_by_user",
            "verify_log",
            {**_agents_args(0), "entries": [_resign(agent_genesis, "alice")]},
            "log_genesis",
        ),
        neg(
            "genesis_with_agent_cert_by_agent",
            "verify_log",
            {**_agents_args(0), "entries": [genesis_with(agent_member("bot", "alice", 1, signer="bot"))]},
            "log_agent",
        ),
        neg(
            "genesis_agent_is_the_founder",
            "verify_log",
            {**_agents_args(0), "entries": [genesis_with({**agent_member("bot", "alice", 1), "id": pid("alice")})]},
            "log_agent",
        ),
        neg(
            "accept_with_agent_signed_by_user",
            "verify_log",
            _agents_args(3, entries=[_resign(agents.entries[3], "bob")]),
            "log_accept",
        ),
        neg(
            "accept_with_agent_cert_by_hub",
            "verify_log",
            broken(3, lambda b: b.accept("bob", agent=agent_member("bot2", "bob", 1, signer="hub"))),
            "log_agent",
        ),
        neg(
            "accept_device_with_agent",
            "verify_log",
            broken(10, lambda b: b.accept("device", "device", agent=agent_member("bot6", "device", 1))),
            "log_agent",
        ),
        # an agent has its user's rights and no rights of its own
        neg(
            "agent_of_member_invites",
            "verify_log",
            broken(4, lambda b: b.invite_user("bot2", "carol")),
            "log_rights",
        ),
        neg(
            "agent_of_member_rotates_org",
            "verify_log",
            broken(4, lambda b: b.add("rotate", "bot2", scope="org", scope_id=ORG, epoch=2)),
            "log_rights",
        ),
        neg(
            "grant_to_agent",
            "verify_log",
            broken(4, lambda b: b.add("grant", "alice", subject=pid("bot2"), wg=W1, access="write")),
            "log_rights",
        ),
        neg(
            "role_of_agent",
            "verify_log",
            broken(4, lambda b: b.add("role", "alice", subject=pid("bot2"), role="admin")),
            "log_rights",
        ),
        neg(
            "member_removes_agent_of_other_user",
            "verify_log",
            broken(4, lambda b: b.add("remove", "bob", subject=pid("bot"))),
            "log_rights",
        ),
        neg(
            "agent_removes_agent_of_other_user",
            "verify_log",
            broken(4, lambda b: b.add("remove", "bot2", subject=pid("bot"))),
            "log_rights",
        ),
        neg(
            "last_admin_leaves_through_agent",
            "verify_log",
            broken(4, lambda b: b.add("remove", "bot", subject=pid("alice"))),
            "last_admin",
        ),
        neg(
            "removed_agent_authors",
            "verify_log",
            broken(14, lambda b: b.add("rotate", "bot2", scope="wg", scope_id=W1, epoch=3)),
            "log_rights",
        ),
        # formats
        neg(
            "agent_member_extra_key",
            "verify_log",
            {**_agents_args(0), "entries": [genesis_with({**agent_member("bot", "alice", 1), "name": "x"})]},
            "header_invalid",
        ),
        neg(
            "agent_member_gen_zero",
            "verify_log",
            {**_agents_args(0), "entries": [genesis_with(agent_member("bot", "alice", 1) | {"gen": 0})]},
            "header_invalid",
        ),
        neg(
            "agent_member_on_wg_create",
            "verify_log",
            _agents_args(1, entries=[_resign(agents.entries[1], "bot", agent=bot2)]),
            "header_invalid",
        ),
        neg("agent_add_without_gen", "verify_log", _agents_args(6, entries=[without_gen]), "header_invalid"),
    ]
    section = {
        "log": _ref("agents", AGENTS_COUNT),
        "heads": [{"seq": s.seq, "hash": s.head} for s in verified.states],
        "projections": [s.projection() for s in verified.states],
    }
    return section, invalid


def _admit_negatives(ctx: Context, admit: LogBuilder) -> list[Json]:
    """One broken variant per rule of `admit` (ADR-060), against the platform log with the identity anchors."""
    neg = ctx.negative
    full = _platform_ref(PLATFORM_COUNT)

    def broken(count: int, build: Callable[[LogBuilder], object], platform_ref: Json = full) -> Json:
        prefix = LogBuilder(ORG, admit.entries[:count])
        build(prefix)
        return {
            "log": _ref("admit", count),
            "founder": sig_pk("alice"),
            "platform": platform_ref,
            "entries": prefix.entries[count:],
        }

    def dave(**options: Any) -> Callable[[LogBuilder], object]:
        fields: dict[str, Any] = {"label": "dave", "platform_seq": DAVE_CERT_SEQ, "by_platform_seq": ALICE_CERT_SEQ}
        fields.update(options)
        return lambda b: b.admit(fields.pop("by", "alice"), **fields)

    return [
        neg(
            "admit_by_member",
            "verify_log",
            broken(28, lambda b: b.admit("dave", "bob", BOB_CERT_SEQ, DAVE_CERT_SEQ)),
            "log_rights",
        ),
        neg("admit_default_wg", "verify_log", broken(27, dave(wgs={W0: "write"})), "log_rights"),
        neg("admit_archived_wg", "verify_log", broken(27, dave(wgs={W2: "read"})), "log_rights"),
        neg("admit_active_member", "verify_log", broken(28, dave()), "log_admit"),
        neg("admit_device_subject", "verify_log", broken(27, dave(label="device", key_label="dave")), "log_admit"),
        neg(
            "admit_former_member_renewed_keys",
            "verify_log",
            broken(
                29,
                lambda b: b.admit(
                    "erin", "bob", BOB_RENEWED_SEQ, ERIN_INVITE_SEQ, key_label="hub", by_proof_code="erin-platform"
                ),
            ),
            "log_admit",
        ),
        neg("admit_anchor_not_identity", "verify_log", broken(27, dave(platform_seq=PDEV_SEQ)), "log_admit"),
        neg(
            "admit_certification_of_other_user",
            "verify_log",
            broken(27, dave(label="carol", key_label="dave")),
            "log_admit",
        ),
        neg("admit_hub_swaps_keys", "verify_log", broken(27, dave(key_label="hub")), "log_admit"),
        neg(
            "admit_certification_with_proof",
            "verify_log",
            broken(27, dave(proof_code="erin-platform")),
            "log_admit",
        ),
        neg(
            "admit_invitation_without_proof",
            "verify_log",
            broken(27, dave(label="erin", platform_seq=ERIN_INVITE_SEQ)),
            "log_admit",
        ),
        neg(
            "admit_proof_of_other_code",
            "verify_log",
            broken(27, dave(label="erin", platform_seq=ERIN_INVITE_SEQ, proof_code="platform-user")),
            "log_admit",
        ),
        neg(
            "admit_hub_swaps_proven_keys",
            "verify_log",
            {**broken(28, lambda _b: None), "entries": [_resign(admit.entries[28], "alice", pub=pub("hub"))]},
            "log_admit",
        ),
        neg(
            "admit_proof_for_other_account",
            "verify_log",
            broken(27, dave(platform_seq=ERIN_INVITE_SEQ, proof_code="erin-platform")),
            "log_admit",
        ),
        neg("admit_admitter_unproven", "verify_log", broken(27, dave(by_platform_seq=DAVE_CERT_SEQ)), "log_admit"),
        neg(
            "admit_admitter_proof_on_certification",
            "verify_log",
            broken(27, dave(by_proof_code="erin-platform")),
            "log_admit",
        ),
        neg(
            "admit_anchor_beyond_platform_log",
            "verify_log",
            broken(27, dave(), _platform_ref(DAVE_CERT_SEQ)),
            "log_admit",
        ),
        neg("admit_forged_by_hub", "verify_log", broken(27, dave(signer="hub")), "log_signature"),
        neg(
            "admit_decertified_anchor_at_hub",
            "append_log",
            {
                "log": _ref("admit", 29),
                "founder": sig_pk("alice"),
                "platform": full,
                "append": [admit.entries[29]],
                "now": ts(40),
            },
            "log_admit",
        ),
        neg(
            "admit_batch_without_wraps",
            "check_batch",
            {
                "log": _ref("admit", 27),
                "founder": sig_pk("alice"),
                "platform": full,
                "append": [admit.entries[27]],
                "wraps": [make_wrap("alice", "org", ORG, 3, "dave")],
                "wrapped_by": pid("alice"),
                "now": ts(40),
            },
            "log_admit",
        ),
        # Key model KM-15: bob was admitted again without consent (29), so pdev, assigned to W1 where bob writes,
        # is no recipient of bob's credential key (ADR-060).
        neg(
            "cred_device_of_org_without_consent",
            "check_cred_wrap",
            {
                "log": _ref("admit", ADMIT_COUNT - 1),
                "founder": sig_pk("alice"),
                "platform": full,
                "wrap": make_wrap("bob", "cred", pid("bob"), 1, "pdev"),
                "wrapped_by": pid("bob"),
                "current_epoch": 1,
            },
            "wrap_unauthorized",
        ),
    ]


def _orgwide_valid(orgwide: LogBuilder, plat: PlatformBuilder) -> Json:
    """States of the log with assignments to the whole organization (ADR-075): projections and, per state, the
    workgroups each device is assigned to."""
    verified = verify.verify_log(
        orgwide.entries, sig_pk("alice"), platform=platform.verify_platform_log(plat.entries, sig_pk("root")).state
    )
    devices = [pid("device"), pid("pdev")]
    return {
        "log": _ref("orgwide", ORGWIDE_COUNT),
        "platform": _platform_ref(),
        "heads": [{"seq": s.seq, "hash": s.head} for s in verified.states],
        "projections": [s.projection() for s in verified.states],
        "device_wgs": [{device: s.device_wgs(device) for device in devices} for s in verified.states],
    }


# --- platform-log.json ---


def _presign(entry: Json, signer: str = "root", **changes: object) -> Json:
    return sign_platform_entry({**entry["body"], **changes}, identity(signer).sig)


def build_platform_log() -> Json:
    """Platform log with every entry type including a decertification, and one broken variant per rule P1-P4 and
    per type rule."""
    plat = platform_builder()
    ctx = Context({"platform": plat.entries})
    root = sig_pk("root")
    verified = platform.verify_platform_log(plat.entries, root)
    neg = ctx.negative

    def args(count: int, *extra: Json) -> Json:
        return {"log": _ref("platform", count), "entries": list(extra), "root": root}

    def at(count: int) -> PlatformBuilder:
        return PlatformBuilder(plat.entries[:count])

    def broken(count: int, build: Callable[[PlatformBuilder], object]) -> Json:
        prefix = at(count)
        build(prefix)
        return args(count, *prefix.entries[count:])

    first = plat.entries[1]
    invalid = [
        neg("log_empty", "verify_platform_log", args(0), "log_empty"),
        neg("P1_seq_gap", "verify_platform_log", args(1, _presign(first, seq=2)), "log_chain"),
        neg("P2_prev", "verify_platform_log", args(1, _presign(first, prev=entry_hash(first))), "log_chain"),
        neg("P3_root_not_first", "verify_platform_log", args(0, _presign(first, seq=0, prev="")), "log_genesis"),
        neg(
            "P3_second_root",
            "verify_platform_log",
            broken(1, lambda b: b.add("root", pub=sig_pk("root"))),
            "log_genesis",
        ),
        neg("P3_root_not_pinned", "verify_platform_log", {**args(1), "root": sig_pk("hub")}, "platform_root_mismatch"),
        neg("P4_wrong_key", "verify_platform_log", args(1, _presign(first, "hub")), "log_signature"),
        neg(
            "P4_hub_certifies_device",
            "verify_platform_log",
            broken(
                2,
                lambda b: b.add(
                    "device_certify",
                    signer="hub",
                    **{
                        k: v
                        for k, v in plat.entries[2]["body"].items()
                        if k not in ("seq", "prev", "type", "by", "ts", "v")
                    },
                ),
            ),
            "log_signature",
        ),
        # ADR-079: a switch or list entry the root did not sign stops the log like every other entry (P4)
        neg(
            "P4_policy_signed_by_hub",
            "verify_platform_log",
            args(17, _presign(plat.entries[17], "hub")),
            "log_signature",
        ),
        neg(
            "P4_operator_subscriptions_signed_by_hub",
            "verify_platform_log",
            args(18, _presign(plat.entries[18], "hub")),
            "log_signature",
        ),
        neg(
            "operator_role_grant_twice",
            "verify_platform_log",
            broken(20, lambda b: b.add("role_grant", subject=pid("alice"), role="operator")),
            "log_rights",
        ),
        neg(
            "certify_unknown_invite", "verify_platform_log", broken(3, lambda b: b.certify("erin")), "invite_not_in_log"
        ),
        neg(
            "certify_with_user_invite",
            "verify_platform_log",
            broken(4, lambda b: b.certify("erin", code_label="platform-user")),
            "invite_not_in_log",
        ),
        neg(
            "certify_twice",
            "verify_platform_log",
            broken(3, lambda b: b.certify("erin", code_label="pdev")),
            "invite_consumed",
        ),
        neg(
            "certify_swapped_key",
            "verify_platform_log",
            args(2, _presign(plat.entries[2], pub=pub("hub"))),
            "log_accept",
        ),
        neg(
            "certify_certified_device",
            "verify_platform_log",
            broken(7, lambda b: b.certify("pdev", code_label="qdev")),
            "log_rights",
        ),
        neg(
            "decertify_unknown",
            "verify_platform_log",
            broken(3, lambda b: b.add("device_decertify", device=pid("erin"))),
            "log_rights",
        ),
        neg(
            "decertify_twice",
            "verify_platform_log",
            broken(9, lambda b: b.add("device_decertify", device=pid("qdev"))),
            "log_rights",
        ),
        neg(
            "role_grant_twice",
            "verify_platform_log",
            broken(5, lambda b: b.add("role_grant", subject=pid("alice"), role="inviter")),
            "log_rights",
        ),
        neg(
            "role_revoke_not_held",
            "verify_platform_log",
            broken(4, lambda b: b.add("role_revoke", subject=pid("alice"), role="inviter")),
            "log_rights",
        ),
        neg(
            "invite_id_reused",
            "verify_platform_log",
            broken(
                3,
                lambda b: b.add(
                    "user_invite", invite_id=secrets("pdev").invite_id, pk_inv=secrets("pdev").pk_inv, expires=EXPIRES
                ),
            ),
            "log_rights",
        ),
        neg(
            "user_certify_while_certified",
            "verify_platform_log",
            broken(14, lambda b: b.add("user_certify", user=pid("dave"), pub=pub("hub"))),
            "log_rights",
        ),
        neg(
            "user_decertify_not_certified",
            "verify_platform_log",
            broken(14, lambda b: b.add("user_decertify", user=pid("bob"))),
            "log_rights",
        ),
        neg(
            "user_certify_device_id",
            "verify_platform_log",
            broken(12, lambda b: b.add("user_certify", user=pid("pdev"), pub=pub("alice"))),
            "log_rights",
        ),
        neg(
            "device_certify_user_id",
            "verify_platform_log",
            broken(
                13,
                lambda b: (
                    b.add(
                        "device_invite",
                        invite_id=secrets("alice-dev").invite_id,
                        pk_inv=secrets("alice-dev").pk_inv,
                        expires=EXPIRES,
                    ),
                    b.certify("alice", code_label="alice-dev"),
                ),
            ),
            "log_rights",
        ),
        neg("unknown_type", "verify_platform_log", args(1, _presign(first, type="device_delete")), "log_type"),
        neg("by_not_root", "verify_platform_log", args(1, _presign(first, by=pid("alice"))), "header_invalid"),
        neg("seq_conflict", "append_platform_log", {**args(3), "append": [plat.entries[4]]}, "seq_conflict"),
    ]
    return {
        "about": "Platform log of 04-crypto.md section 4.8: every entry type, a decertification, the states, and one "
        "broken variant per rule P1-P4 and per type rule (section 6.2). See README.md, 'Log vectors'.",
        "identities": {
            name: value
            for name, value in _identities().items()
            if name in ("root", "pdev", "qdev", "hub", "alice", "bob", "dave", "erin")
        },
        "root": root,
        "logs": {"platform": plat.entries},
        "invite_codes": {
            label: b64u_encode(code(label)) for label in ("pdev", "qdev", "platform-user", "erin-platform")
        },
        "valid": {
            "log": _ref("platform", len(plat.entries)),
            "heads": [{"seq": state.seq, "hash": state.head} for state in verified.states],
            "projections": [state.projection() for state in verified.states],
            "platform_access": [
                {"count": 6, "org": ORG, "private": False, "access": True},
                {"count": 11, "org": ORG, "private": False, "access": False},
                {"count": 11, "org": ORG, "private": True, "access": True},
                # ADR-079: without an `org_platform_access` of its own an org follows the switch; an explicit entry wins
                {"count": 17, "org": OTHER_ORG, "private": False, "access": False},
                {"count": 18, "org": OTHER_ORG, "private": False, "access": True},
                {"count": 18, "org": ORG, "private": False, "access": False},
                {"count": 23, "org": OTHER_ORG, "private": False, "access": False},
            ],
        },
        "invalid": invalid,
    }


# --- log-rotation.json ---


def edge_builder() -> LogBuilder:
    """A log for the edge cases of R: a device without workgroup, a device with one workgroup, an archived workgroup,
    an admin without explicit workgroup access (it reads every workgroup by inheritance, ADR-058)."""
    log = LogBuilder()
    log.genesis()  # 0
    log.add("wg_create", "alice", wg=W1, epoch=1)  # 1
    log.invite_device("alice", "device")  # 2
    log.accept("device", "device")  # 3 (no workgroup)
    log.invite_user("alice", "bob", wgs={W1: "read"})  # 4
    log.accept("bob")  # 5
    log.invite_device("alice", "dev2", [W1])  # 6
    log.accept("dev2", "device")  # 7
    log.add("wg_create", "alice", wg=W2, epoch=1)  # 8
    log.add("grant", "alice", subject=pid("bob"), wg=W2, access="read")  # 9
    log.add("wg_archive", "alice", wg=W2)  # 10
    log.invite_user("alice", "carol", role="admin")  # 11
    log.accept("carol")  # 12
    return log


EDGE_COUNT = 13


def _rejoin_builder() -> LogBuilder:
    log = edge_builder()
    log.add("remove", "bob", subject=pid("bob"))  # 13 (leave)
    log.invite_user("alice", "erin")  # 14 (the second invitation of bob uses the code labelled erin)
    log.accept("bob", code_label="erin")  # 15 (rejoin without workgroups)
    return log


def _r_case(name: str, log_name: str, prefix: LogBuilder, count: int) -> Json:
    entry = prefix.entries[count]
    before = verify.verify_log(prefix.entries[:count], sig_pk("alice"))
    after = verify.apply(before.state, entry, founder=sig_pk("alice"))
    subject = rotation.entry_subject(entry["body"])
    return {
        "name": name,
        "log": _ref(log_name, count),
        "entry": entry,
        "subject": subject,
        "required": rotation.as_json(rotation.required_set(before.state, after, subject)),
        **_lost(before.state, after, entry["body"]),
    }


def _lost(before: OrgState, after: OrgState, body: Json) -> Json:
    """`losers` and the union R `lost` of an entry (ADR-082)."""
    return {
        "losers": sorted(rotation.losers(before, body)),
        "lost": rotation.as_json(rotation.lost(before, after, body)),
        "self_owed": rotation.self_owed(before, body),
    }


def build_rotation() -> Json:
    """The rotate set R for every entry type and the edge cases, and batches with and without the exact R."""
    main, plat, edge, rejoin = main_builder(), platform_builder(), edge_builder(), _rejoin_builder()
    agents = agents_builder()
    logs = {
        "main": main.entries,
        "platform": plat.entries,
        "edge": edge.entries,
        "rejoin": rejoin.entries,
        "orgwide": orgwide_builder().entries,
        "agents": agents.entries,
        "member_agent": member_agent_builder().entries,
    }
    ctx = Context(logs)
    plat_state = platform.verify_platform_log(plat.entries, sig_pk("root")).state
    cases = []
    main_log = verify.verify_log(main.entries, sig_pk("alice"), platform=plat_state)
    for seq in range(1, len(main.entries)):
        body = main.entries[seq]["body"]
        subject = rotation.entry_subject(body)
        before, after = main_log.states[seq - 1], main_log.states[seq]
        cases.append(
            {
                "name": f"main_{seq}_{body['type']}",
                "log": _ref("main", seq),
                "entry": main.entries[seq],
                "subject": subject,
                "required": rotation.as_json(rotation.required_set(before, after, subject)),
                **_lost(before, after, body),
            }
        )

    def edge_case(name: str, build: Callable[[LogBuilder], object], base: str = "edge") -> Json:
        prefix = (edge if base == "edge" else rejoin).copy()
        count = len(prefix.entries)
        build(prefix)
        return _r_case(name, base, prefix, count)

    cases += [
        edge_case("device_without_workgroup_removed", lambda b: b.add("remove", "alice", subject=pid("device"))),
        edge_case(
            "device_last_workgroup_unassigned", lambda b: b.add("device_unassign", "alice", device=pid("dev2"), wg=W1)
        ),
        edge_case("device_with_one_workgroup_removed", lambda b: b.add("remove", "alice", subject=pid("dev2"))),
        edge_case("member_removed_incl_archived_wg", lambda b: b.add("remove", "alice", subject=pid("bob"))),
        edge_case("member_leaves", lambda b: b.add("remove", "bob", subject=pid("bob"))),
        edge_case("reader_ungranted", lambda b: b.add("ungrant", "alice", subject=pid("bob"), wg=W1)),
        edge_case("admin_without_wg_access_removed", lambda b: b.add("remove", "alice", subject=pid("carol"))),
        edge_case("admin_demoted", lambda b: b.add("role", "alice", subject=pid("carol"), role="member")),
        edge_case("admin_demotes_itself", lambda b: b.add("role", "carol", subject=pid("carol"), role="member")),
        edge_case("admin_leaves", lambda b: b.add("remove", "carol", subject=pid("carol"))),
        edge_case("member_promoted", lambda b: b.add("role", "alice", subject=pid("bob"), role="admin")),
        edge_case(
            "admin_explicit_access_ungranted",
            lambda b: (
                b.add("grant", "alice", subject=pid("carol"), wg=W1, access="write"),
                b.add("ungrant", "alice", subject=pid("carol"), wg=W1),
            ),
        ),
        edge_case("device_assigned", lambda b: b.add("device_assign", "alice", device=pid("device"), wg=W0)),
        edge_case("device_assigned_to_org", lambda b: b.add("device_assign_org", "alice", device=pid("device"))),
        edge_case("rejoined_member_removed", lambda b: b.add("remove", "alice", subject=pid("bob")), "rejoin"),
    ]
    rejoin_accept = _r_case("rejoin_accept", "rejoin", rejoin, EDGE_COUNT + 2)
    cases.append(rejoin_accept)
    orgwide_prefix = LogBuilder(ORG, orgwide_builder().entries[:7])
    cases.append(_r_case("org_device_unassigned_keeps_explicit", "orgwide", orgwide_prefix, 6))
    for name, count in (
        ("agent_removed_by_its_user", 11),
        ("agent_added_by_itself", 12),
        ("agent_replaced_by_higher_gen", 13),
        ("user_removed_with_its_agent", 14),
        ("agent_returns_with_its_user", 16),
        ("agent_leaves", 17),
    ):
        cases.append(_r_case(name, "agents", agents, count))

    now = ts(60)
    base = {"log": _ref("edge", EDGE_COUNT), "founder": sig_pk("alice"), "wrapped_by": pid("alice"), "now": now}

    def batch(*build: Callable[[LogBuilder], object]) -> list[Json]:
        prefix = edge.copy()
        for step in build:
            step(prefix)
        return prefix.entries[EDGE_COUNT:]

    def remove_bob(b: LogBuilder) -> object:
        return b.add("remove", "alice", subject=pid("bob"))

    def rot(scope: str, scope_id: str, epoch: int) -> Callable[[LogBuilder], object]:
        return lambda b: b.add("rotate", "alice", scope=scope, scope_id=scope_id, epoch=epoch)

    def ungrant_bob(b: LogBuilder) -> object:
        return b.add("ungrant", "alice", subject=pid("bob"), wg=W1)

    def assign_device_to_org(b: LogBuilder) -> object:
        return b.add("device_assign_org", "alice", device=pid("device"))

    full_wraps = (
        [make_wrap("alice", "org", ORG, 2, r) for r in ("alice", "carol", "dev2")]
        + [make_wrap("alice", "wg", W0, 2, r) for r in ("alice", "carol")]
        + [make_wrap("alice", "wg", W1, 2, r) for r in ("alice", "carol", "dev2")]
        + [make_wrap("alice", "wg", W2, 2, r) for r in ("alice", "carol")]
    )
    remove_batch = batch(remove_bob, rot("org", ORG, 2), rot("wg", W0, 2), rot("wg", W1, 2), rot("wg", W2, 2))
    w1_wraps = [make_wrap("alice", "wg", W1, 2, r) for r in ("alice", "carol", "dev2")]

    def demote_carol(b: LogBuilder) -> object:
        return b.add("role", "alice", subject=pid("carol"), role="member")

    demotion_wraps = [make_wrap("alice", "wg", W1, 2, r) for r in ("alice", "bob", "dev2")] + [
        make_wrap("alice", "wg", W2, 2, r) for r in ("alice", "bob")
    ]
    w3 = pid("wg/three")
    valid_batches = []
    for name, append, batch_wraps in (
        ("remove_with_exact_r", remove_batch, full_wraps),
        ("leave_without_rotation", batch(lambda b: b.add("remove", "bob", subject=pid("bob"))), []),
        ("ungrant_with_rotation", batch(ungrant_bob, rot("wg", W1, 2)), w1_wraps),
        (
            "wg_create_with_grant",
            batch(
                lambda b: b.add("wg_create", "alice", wg=w3, epoch=1),
                lambda b: b.add("grant", "alice", subject=pid("bob"), wg=w3, access="write"),
            ),
            [make_wrap("alice", "wg", w3, 1, r) for r in ("alice", "bob", "carol")],
        ),
        (
            "manual_rotation",
            batch(rot("wg", W1, 2)),
            [make_wrap("alice", "wg", W1, 2, r) for r in ("alice", "bob", "carol", "dev2")],
        ),
        ("admin_demoted_with_rotation", batch(demote_carol, rot("wg", W1, 2), rot("wg", W2, 2)), demotion_wraps),
        ("admin_demotes_itself", batch(lambda b: b.add("role", "carol", subject=pid("carol"), role="member")), []),
        (
            "org_device_in_new_workgroup",
            batch(assign_device_to_org, lambda b: b.add("wg_create", "alice", wg=w3, epoch=1)),
            [make_wrap("alice", "wg", w3, 1, r) for r in ("alice", "carol", "device")],
        ),
        (
            "org_unassign_with_exact_r",
            batch(
                assign_device_to_org,
                lambda b: b.add("device_unassign_org", "alice", device=pid("device")),
                rot("org", ORG, 2),
                rot("wg", W0, 2),
                rot("wg", W1, 2),
                rot("wg", W2, 2),
            ),
            [make_wrap("alice", "org", ORG, 2, r) for r in ("alice", "bob", "carol", "dev2")]
            + [make_wrap("alice", "wg", W0, 2, r) for r in ("alice", "bob", "carol")]
            + [make_wrap("alice", "wg", W1, 2, r) for r in ("alice", "bob", "carol", "dev2")]
            + [make_wrap("alice", "wg", W2, 2, r) for r in ("alice", "bob", "carol")],
        ),
    ):
        args = {**base, "append": append, "wraps": batch_wraps}
        if name == "admin_demotes_itself":
            args["wrapped_by"] = pid("carol")
        result = cast(Any, ctx.run("check_batch", args))
        valid_batches.append(
            {
                "name": name,
                "args": args,
                "required": rotation.as_json(result.required),
                "pending": rotation.as_json(result.log.state.pending),
                "head": result.log.head.to_json(),
            }
        )
    neg = ctx.negative
    invalid = [
        neg(
            "rotate_missing",
            "check_batch",
            {**base, "append": remove_batch[:-1], "wraps": full_wraps[:-1]},
            "rotation_required",
        ),
        neg("rotate_none", "check_batch", {**base, "append": batch(ungrant_bob), "wraps": []}, "rotation_required"),
        neg(
            "rotate_extra",
            "check_batch",
            {**base, "append": batch(ungrant_bob, rot("wg", W1, 2), rot("org", ORG, 2)), "wraps": w1_wraps},
            "rotation_required",
        ),
        neg(
            "rotate_before_trigger",
            "check_batch",
            {**base, "append": batch(rot("wg", W1, 2), ungrant_bob), "wraps": w1_wraps},
            "rotation_required",
        ),
        neg(
            "wrap_missing_for_remaining_recipient",
            "check_batch",
            {**base, "append": batch(ungrant_bob, rot("wg", W1, 2)), "wraps": w1_wraps[:1]},
            "rotation_required",
        ),
        neg(
            "wrap_for_removed_member",
            "check_batch",
            {
                **base,
                "append": batch(ungrant_bob, rot("wg", W1, 2)),
                "wraps": [*w1_wraps, make_wrap("alice", "wg", W1, 2, "bob")],
            },
            "wrap_unauthorized",
        ),
        neg(
            "wg_create_without_wraps",
            "check_batch",
            {**base, "append": batch(lambda b: b.add("wg_create", "alice", wg=w3, epoch=1)), "wraps": []},
            "rotation_required",
        ),
        neg(
            "wrap_signed_by_other",
            "check_batch",
            {**base, "append": batch(ungrant_bob, rot("wg", W1, 2)), "wraps": w1_wraps, "wrapped_by": pid("carol")},
            "signature_invalid",
        ),
        neg("seq_conflict", "check_batch", {**base, "append": remove_batch[1:], "wraps": []}, "seq_conflict"),
        neg(
            "wg_create_without_admin_wrap",
            "check_batch",
            {
                **base,
                "append": batch(lambda b: b.add("wg_create", "alice", wg=w3, epoch=1)),
                "wraps": [make_wrap("alice", "wg", w3, 1, "alice")],
            },
            "rotation_required",
        ),
        neg(
            "wg_create_without_wrap_for_org_device",
            "check_batch",
            {
                **base,
                "append": batch(assign_device_to_org, lambda b: b.add("wg_create", "alice", wg=w3, epoch=1)),
                "wraps": [make_wrap("alice", "wg", w3, 1, r) for r in ("alice", "carol")],
            },
            "rotation_required",
        ),
        neg(
            "admin_demoted_without_rotation",
            "check_batch",
            {**base, "append": batch(demote_carol), "wraps": []},
            "rotation_required",
        ),
        neg(
            "admin_demoted_wrap_for_demoted",
            "check_batch",
            {
                **base,
                "append": batch(demote_carol, rot("wg", W1, 2), rot("wg", W2, 2)),
                "wraps": [*demotion_wraps, make_wrap("alice", "wg", W1, 2, "carol")],
            },
            "wrap_unauthorized",
        ),
        neg(
            "org_unassign_without_rotation",
            "check_batch",
            {
                **base,
                "append": batch(
                    assign_device_to_org, lambda b: b.add("device_unassign_org", "alice", device=pid("device"))
                ),
                "wraps": [],
            },
            "rotation_required",
        ),
    ]
    agent_batches, agent_invalid = _agent_rotation_part(ctx, agents, now)
    return {
        "about": "Rotation obligation of 04-crypto.md section 4.7: R(before, after, subject) for every entry type and "
        "the edge cases, and append batches that must contain exactly R as rotate entries (section 5.10, "
        "03-api.md section 4.3). See README.md, 'Log vectors'.",
        "identities": _identities(),
        "ids": {**_ids(), "wg_three": w3},
        "founder": sig_pk("alice"),
        "logs": logs,
        "platform": _platform_ref(),
        "cases": cases,
        "batches": valid_batches + agent_batches,
        "invalid": invalid + agent_invalid,
    }


def _agent_rotation_part(ctx: Context, agents: LogBuilder, now: str) -> tuple[list[Json], list[Json]]:
    """log-rotation.json, ADR-082: R of an agent's removal, of its replacement and of its user's removal."""
    base = {"log": _ref("agents", AGENTS_COUNT), "founder": sig_pk("alice"), "wrapped_by": pid("alice"), "now": now}

    def batch(*build: Callable[[LogBuilder], object]) -> list[Json]:
        prefix = agents.copy()
        for step in build:
            step(prefix)
        return prefix.entries[AGENTS_COUNT:]

    def rotates(by: str, scopes: Sequence[tuple[str, str]]) -> Callable[[LogBuilder], object]:
        return lambda b: [b.add("rotate", by, scope=scope, scope_id=scope_id, epoch=3) for scope, scope_id in scopes]

    def wraps_for(by: str, readers: Mapping[tuple[str, str], Sequence[str]]) -> list[Json]:
        return [make_wrap(by, scope, scope_id, 3, r) for (scope, scope_id), names in readers.items() for r in names]

    every = (("org", ORG), ("wg", W0), ("wg", W1))
    without_bot = {
        ("org", ORG): ("alice", "bob", "bot5", "carol", "device"),
        ("wg", W0): ("alice", "bob", "bot5", "carol"),
        ("wg", W1): ("alice", "device"),
    }
    with_bot6 = {
        ("org", ORG): ("alice", "bob", "bot5", "bot6", "carol", "device"),
        ("wg", W0): ("alice", "bob", "bot5", "bot6", "carol"),
        ("wg", W1): ("alice", "bot6", "device"),
    }
    without_bob = {("org", ORG): ("alice", "bot", "carol", "device"), ("wg", W0): ("alice", "bot", "carol")}

    def remove(by: str, subject: str) -> Callable[[LogBuilder], object]:
        return lambda b: b.add("remove", by, subject=pid(subject))

    def add_bot6(by: str) -> Callable[[LogBuilder], object]:
        return lambda b: b.agent_add(by, "bot6", "alice", 2)

    member_batches, member_invalid = _member_agent_batches(ctx, now)
    replaced = batch(add_bot6("alice"), rotates("alice", every))
    bob_removed = batch(remove("alice", "bob"), rotates("alice", every[:2]))
    valid = []
    for name, append, batch_wraps, wrapper in (
        ("user_removes_its_agent", batch(remove("alice", "bot"), rotates("alice", every)), without_bot, "alice"),
        ("agent_leaves_without_rotation", batch(remove("bot", "bot")), {}, "bot"),
        ("user_leaves_through_its_agent", batch(remove("bot5", "bob")), {}, "bot5"),
        ("user_removed_with_its_agent", bob_removed, without_bob, "alice"),
        (
            "replacement_remove_then_add",
            batch(remove("alice", "bot"), rotates("alice", every), add_bot6("alice")),
            with_bot6,
            "alice",
        ),
        ("replacement_by_higher_gen", replaced, with_bot6, "alice"),
        ("replacement_by_the_new_agent", batch(add_bot6("bot6"), rotates("bot6", every)), with_bot6, "bot6"),
    ):
        args = {**base, "append": append, "wraps": wraps_for(wrapper, batch_wraps), "wrapped_by": pid(wrapper)}
        valid.append(_batch_case(ctx, name, args))
    neg = ctx.negative
    invalid = [
        neg(
            "agent_removed_by_user_without_rotation",
            "check_batch",
            {**base, "append": batch(remove("alice", "bot")), "wraps": []},
            "rotation_required",
        ),
        neg(
            "replacement_without_rotation",
            "check_batch",
            {**base, "append": batch(add_bot6("alice")), "wraps": []},
            "rotation_required",
        ),
        neg(
            "replacement_without_wrap_for_new_agent",
            "check_batch",
            {**base, "append": replaced, "wraps": wraps_for("alice", without_bot)},
            "rotation_required",
        ),
        neg(
            "wrap_for_replaced_agent",
            "check_batch",
            {
                **base,
                "append": replaced,
                "wraps": [*wraps_for("alice", with_bot6), make_wrap("alice", "wg", W1, 3, "bot")],
            },
            "wrap_unauthorized",
        ),
        neg(
            "wrap_for_agent_of_removed_user",
            "check_batch",
            {
                **base,
                "append": bob_removed,
                "wraps": [*wraps_for("alice", without_bob), make_wrap("alice", "org", ORG, 3, "bot5")],
            },
            "wrap_unauthorized",
        ),
    ]
    return valid + member_batches, invalid + member_invalid


def _batch_case(ctx: Context, name: str, args: Json) -> Json:
    result = cast(Any, ctx.run("check_batch", args))
    return {
        "name": name,
        "args": args,
        "required": rotation.as_json(result.required),
        "pending": rotation.as_json(result.log.state.pending),
        "head": result.log.head.to_json(),
    }


MEMBER_AGENT_COUNT = AGENTS_COUNT + 1


def member_agent_builder() -> LogBuilder:
    """The agents log, then bob (a member with the agent `bot5`) gets read access to W1: bob can rotate neither the
    org scope nor W1 by its own rights."""
    log = agents_builder()
    log.add("grant", "alice", subject=pid("bob"), wg=W1, access="read")  # 20
    return log


def _member_agent_batches(ctx: Context, now: str) -> tuple[list[Json], list[Json]]:
    """log-rotation.json, ADR-082 section 4: a member that is no admin removes or replaces its agent and rotates its
    R (org scope, a read-only workgroup) itself; another member may not."""
    member = member_agent_builder()
    base = {"log": _ref("member_agent", MEMBER_AGENT_COUNT), "founder": sig_pk("alice"), "now": now}
    every = (("org", ORG), ("wg", W0), ("wg", W1))

    def batch(*build: Callable[[LogBuilder], object]) -> list[Json]:
        prefix = member.copy()
        for step in build:
            step(prefix)
        return prefix.entries[MEMBER_AGENT_COUNT:]

    def rotates(by: str) -> Callable[[LogBuilder], object]:
        return lambda b: [b.add("rotate", by, scope=scope, scope_id=sid, epoch=3) for scope, sid in every]

    def wraps_for(by: str, agent: str | None) -> list[Json]:
        readers = {
            ("org", ORG): ["alice", "bob", "bot", "carol", "device"],
            ("wg", W0): ["alice", "bob", "bot", "carol"],
            ("wg", W1): ["alice", "bob", "bot", "device"],
        }
        return [
            make_wrap(by, scope, sid, 3, r)
            for (scope, sid), names in readers.items()
            for r in sorted([*names, *([agent] if agent else [])])
        ]

    def remove_bot5(b: LogBuilder) -> object:
        return b.add("remove", "bob", subject=pid("bot5"))

    def add_bot7(by: str) -> Callable[[LogBuilder], object]:
        return lambda b: b.agent_add(by, "bot7", "bob", 3)

    valid = [
        _batch_case(
            ctx,
            name,
            {**base, "append": append, "wraps": wraps_for(wrapper, agent), "wrapped_by": pid(wrapper)},
        )
        for name, append, wrapper, agent in (
            ("member_removes_its_agent_and_rotates", batch(remove_bot5, rotates("bob")), "bob", None),
            ("member_replaces_its_agent_and_rotates", batch(add_bot7("bob"), rotates("bob")), "bob", "bot7"),
            ("member_agent_replaces_itself_and_rotates", batch(add_bot7("bot7"), rotates("bot7")), "bot7", "bot7"),
        )
    ]
    invalid = [
        ctx.negative(
            "owed_rotation_by_other_member",
            "check_batch",
            {**base, "append": batch(remove_bot5, rotates("carol")), "wraps": [], "wrapped_by": pid("carol")},
            "log_rights",
        ),
        ctx.negative(
            "member_rotates_without_removing_its_agent",
            "check_batch",
            {**base, "append": batch(rotates("bob")), "wraps": [], "wrapped_by": pid("bob")},
            "log_rights",
        ),
    ]
    return valid, invalid


# --- log-wraps.json ---


def build_wraps() -> Json:
    """W1-W3 against the final state of the main log, `cred` wraps and catch-up plans (sections 5.11, 6.3)."""
    main, plat, edge, admit = main_builder(), platform_builder(), edge_builder(), admit_builder()
    agents, other = agents_builder(), other_org_builder()
    ctx = Context(
        {
            "main": main.entries,
            "platform": plat.entries,
            "edge": edge.entries,
            "admit": admit.entries,
            "agents": agents.entries,
            "agents_other_org": other.entries,
        }
    )
    full = len(main.entries)

    def edge_args(wrapper: str, scope: str, scope_id: str, epoch: int, recipient: str, **extra: object) -> Json:
        return {
            "log": _ref("edge", EDGE_COUNT),
            "founder": sig_pk("alice"),
            "wrap": make_wrap(wrapper, scope, scope_id, epoch, recipient),
            "wrapped_by": pid(wrapper),
            **extra,
        }

    def args(wrapper: str, scope: str, scope_id: str, epoch: int, recipient: str, **extra: object) -> Json:
        return _main_args(
            full, wrap=make_wrap(wrapper, scope, scope_id, epoch, recipient), wrapped_by=pid(wrapper), **extra
        )

    valid = []
    for name, case in (
        ("W1_self_current_epoch", args("pdev", "wg", W1, 2, "pdev")),
        ("W1_self_older_epoch", args("pdev", "wg", W1, 1, "pdev")),
        ("W2_creator_for_device", args("alice", "wg", W1, 2, "device")),
        ("W2_device_for_device", args("device", "wg", W1, 2, "pdev")),
        ("W2_org_older_epoch_for_device", args("alice", "org", ORG, 1, "device")),
        ("W2_former_holder_of_old_epoch", args("bob", "wg", W1, 1, "device")),
        ("W2_former_admin_held_epoch_by_inheritance", args("carol", "wg", W1, 1, "device")),
        ("W2_member_for_admin_without_explicit_access", edge_args("bob", "wg", W1, 1, "carol")),
        ("W2_device_for_admin_without_explicit_access", edge_args("dev2", "wg", W1, 1, "carol")),
        ("W2_admin_by_inheritance_for_member", edge_args("carol", "wg", W1, 1, "bob")),
        ("W2_admin_by_inheritance_for_device", edge_args("carol", "wg", W1, 1, "dev2")),
        ("W2_for_admin_on_archived_workgroup", edge_args("alice", "wg", W2, 1, "carol")),
        # Key model KM-10: the positive control of org_key_for_device_without_workgroup (dev2 serves W1).
        ("W2_org_key_for_device_with_workgroup", edge_args("alice", "org", ORG, 1, "dev2")),
    ):
        ctx.run("check_wrap", case)
        valid.append({"name": name, "args": case})
    cred_valid = []
    for name, case in (
        ("cred_owner_for_device", {**args("alice", "cred", pid("alice"), 2, "device"), "current_epoch": 2}),
        ("cred_device_for_platform_device", {**args("device", "cred", pid("alice"), 1, "pdev"), "current_epoch": 2}),
        ("cred_owner_self", {**args("alice", "cred", pid("alice"), 2, "alice"), "current_epoch": 2}),
        # Key model KM-15: the positive control of log.json cred_device_of_org_without_consent in the same log state
        # (alice founded the org, so pdev on W1 is a reader of alice's credential key; bob's admission is not).
        (
            "cred_device_of_org_with_consent",
            {
                "log": _ref("admit", ADMIT_COUNT - 1),
                "founder": sig_pk("alice"),
                "platform": _platform_ref(PLATFORM_COUNT),
                "wrap": make_wrap("alice", "cred", pid("alice"), 2, "pdev"),
                "wrapped_by": pid("alice"),
                "current_epoch": 2,
            },
        ),
        # Key model KM-35 (ADR-078): the `cred` scope of the org itself (company credentials) is read by the devices
        # assigned to its workgroups only; an active admin wraps each epoch for them, a device wraps for a device.
        ("cred_org_admin_for_device", {**args("alice", "cred", ORG, 1, "device"), "current_epoch": 1}),
        ("cred_org_device_for_device", {**args("device", "cred", ORG, 1, "pdev"), "current_epoch": 1}),
        ("cred_org_device_self", {**args("pdev", "cred", ORG, 1, "pdev"), "current_epoch": 1}),
        (
            "cred_org_admin_by_inheritance_for_device",
            {**edge_args("carol", "cred", ORG, 1, "dev2"), "current_epoch": 1},
        ),
    ):
        ctx.run("check_cred_wrap", case)
        cred_valid.append({"name": name, "args": case})
    neg = ctx.negative
    bad_sig = args("alice", "wg", W1, 2, "device")
    bad_sig["wrap"] = {**bad_sig["wrap"], "sig": make_wrap("hub", "wg", W1, 2, "device")["sig"]}
    invalid = [
        neg("recipient_not_entitled", "check_wrap", args("device", "wg", W0, 4, "device"), "wrap_unauthorized"),
        neg("recipient_removed", "check_wrap", args("alice", "org", ORG, 3, "bob"), "wrap_unauthorized"),
        neg("epoch_in_future", "check_wrap", args("alice", "wg", W1, 3, "device"), "wrap_unauthorized"),
        neg("removed_member_wraps_new_epoch", "check_wrap", args("bob", "wg", W1, 2, "device"), "wrap_unauthorized"),
        neg(
            "removed_admin_at_hub",
            "check_wrap",
            args("carol", "wg", W1, 1, "device", require_active=True),
            "wrap_unauthorized",
        ),
        neg(
            "cred_admin_inherited_read_not_eligible",
            "check_cred_wrap",
            {**edge_args("carol", "cred", pid("carol"), 1, "dev2"), "current_epoch": 1},
            "wrap_unauthorized",
        ),
        neg("hub_forges_wrap", "check_wrap", args("hub", "wg", W1, 2, "device"), "wrap_unauthorized"),
        neg(
            "removed_wrapper_at_hub",
            "check_wrap",
            args("bob", "wg", W1, 1, "device", require_active=True),
            "wrap_unauthorized",
        ),
        neg("signature_of_other_key", "check_wrap", bad_sig, "signature_invalid"),
        neg(
            "recipient_decertified",
            "check_wrap",
            args("alice", "wg", W1, 2, "pdev", excluded=[pid("pdev")]),
            "wrap_unauthorized",
        ),
        neg("cred_via_scope_rule", "check_wrap", args("alice", "cred", pid("alice"), 1, "alice"), "wrap_unauthorized"),
        neg("unknown_scope_id", "check_wrap", args("alice", "wg", pid("wg/none"), 1, "alice"), "wrap_unauthorized"),
        neg(
            "wrap_fields_missing",
            "check_wrap",
            {**args("alice", "wg", W1, 2, "device"), "wrap": {"scope": "wg"}},
            "header_invalid",
        ),
        neg(
            "cred_user_not_owner",
            "check_cred_wrap",
            {**args("alice", "cred", pid("alice"), 2, "bob"), "current_epoch": 2},
            "wrap_unauthorized",
        ),
        neg(
            "cred_epoch_in_future",
            "check_cred_wrap",
            {**args("alice", "cred", pid("alice"), 3, "device"), "current_epoch": 2},
            "wrap_unauthorized",
        ),
        neg(
            "cred_wrapper_not_eligible",
            "check_cred_wrap",
            {**args("carol", "cred", pid("alice"), 2, "device"), "current_epoch": 2},
            "wrap_unauthorized",
        ),
        # Key model KM-35: no user receives the org's credential key, not even an admin; a member that is no admin
        # and a removed admin do not wrap it; an epoch after the current one is refused.
        neg(
            "cred_org_admin_is_no_recipient",
            "check_cred_wrap",
            {**args("alice", "cred", ORG, 1, "alice"), "current_epoch": 1},
            "wrap_unauthorized",
        ),
        neg(
            "cred_org_member_is_no_wrapper",
            "check_cred_wrap",
            {**edge_args("bob", "cred", ORG, 1, "dev2"), "current_epoch": 1},
            "wrap_unauthorized",
        ),
        neg(
            "cred_org_removed_admin_is_no_wrapper",
            "check_cred_wrap",
            {**args("carol", "cred", ORG, 1, "device"), "current_epoch": 1},
            "wrap_unauthorized",
        ),
        neg(
            "cred_org_epoch_in_future",
            "check_cred_wrap",
            {**args("alice", "cred", ORG, 2, "device"), "current_epoch": 1},
            "wrap_unauthorized",
        ),
        # Key model (docs/platform/key-model.md): KM-10, a device without a workgroup is no reader of the org key
        # (ADR-007); KM-06, only principals of the log receive wraps (an ID outside the log, e.g. an agent's, is none).
        neg(
            "org_key_for_device_without_workgroup",
            "check_wrap",
            edge_args("alice", "org", ORG, 1, "device"),
            "wrap_unauthorized",
        ),
        neg(
            "principal_outside_log_is_no_recipient",
            "check_wrap",
            args("alice", "wg", W1, 2, "agent"),
            "wrap_unauthorized",
        ),
    ]
    plat_state = platform.verify_platform_log(plat.entries, sig_pk("root")).state
    log = verify.verify_log(main.entries, sig_pk("alice"), platform=plat_state)
    existing = [
        wraps.WrapSlot("org", ORG, 3, pid("alice")),
        wraps.WrapSlot("wg", W0, 4, pid("alice")),
        wraps.WrapSlot("wg", W1, 2, pid("alice")),
        wraps.WrapSlot("wg", W2, 1, pid("alice")),
        wraps.WrapSlot("wg", W1, 2, pid("device")),
    ]
    plans = []
    for wrapper, excluded in (("alice", []), ("device", []), ("pdev", []), ("alice", [pid("pdev")])):
        plans.append(
            {
                "wrapper": pid(wrapper),
                "excluded": excluded,
                "missing": [slot.to_json() for slot in catchup.missing_wraps(log, pid(wrapper), existing, excluded)],
            }
        )
    cred_existing = [wraps.WrapSlot("cred", pid("alice"), 2, pid("alice"))]
    cred_plans = [
        {
            "owner": pid("alice"),
            "wrapper": pid(wrapper),
            "current_epoch": 2,
            "missing": [
                slot.to_json()
                for slot in catchup.missing_cred_wraps(
                    [log], pid("alice"), pid(wrapper), current_epoch=2, existing=cred_existing
                )
            ],
        }
        for wrapper in ("alice", "device", "bob")
    ]
    org_cred_existing = [wraps.WrapSlot("cred", ORG, 1, pid("device"))]
    org_cred_plans = [
        {
            "owner": ORG,
            "wrapper": pid(wrapper),
            "current_epoch": 1,
            "missing": [
                slot.to_json()
                for slot in catchup.missing_cred_wraps(
                    [log], ORG, pid(wrapper), current_epoch=1, existing=org_cred_existing
                )
            ],
        }
        for wrapper in ("device", "pdev", "alice", "bob")
    ]
    rotated = edge.copy()
    rotated.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2)  # 13: carol needs W1 epochs 1 and 2
    edge_log = verify.verify_log(rotated.entries, sig_pk("alice"))
    edge_existing = [wraps.WrapSlot("wg", W1, 2, r) for r in (pid("alice"), pid("bob"), pid("dev2"))]
    admin_plans = [
        {
            "wrapper": pid(wrapper),
            "excluded": [],
            "missing": [slot.to_json() for slot in catchup.missing_wraps(edge_log, pid(wrapper), edge_existing)],
        }
        for wrapper in ("dev2", "bob", "carol")
    ]
    agent_section, agent_invalid = _agent_wraps_part(ctx, agents, edge)
    revocation_logs = revocation_builders()
    ctx.logs.update({name: log.entries for name, log in revocation_logs.items()})
    revocation, revocation_invalid = _revocation_part(ctx)
    agent_section["revocation"] = revocation
    agent_invalid += revocation_invalid
    return {
        "about": "Wrap rules W1-W3 of 04-crypto.md section 6.3 against the final state of the main log of log.json "
        "and, for admins without explicit workgroup access (ADR-058), the edge log of log-rotation.json; cred wraps, "
        "and catch-up plans (section 5.11), also for the credential key of an organization (devices only, KM-35). See "
        "README.md, 'Log vectors'.",
        "identities": _identities(),
        "ids": _ids(),
        "founder": sig_pk("alice"),
        "logs": {
            "main": main.entries,
            "platform": plat.entries,
            "edge": edge.entries,
            "edge_rotated": rotated.entries,
            "admit": admit.entries,
            "agents": agents.entries,
            "agents_other_org": other.entries,
            "edge_agent": agent_section.pop("edge_agent"),
            **{name: log.entries for name, log in revocation_logs.items()},
        },
        "scope_keys": {
            f"{scope}/{scope_id}/{epoch}": b64u_encode(scope_key(scope, scope_id, epoch))
            for scope, scope_id, epoch in (
                ("org", ORG, 1),
                ("org", ORG, 3),
                ("wg", W1, 1),
                ("wg", W1, 2),
                ("cred", pid("alice"), 2),
            )
        },
        "valid": valid,
        "cred_valid": cred_valid,
        "catch_up": {"existing": [slot.to_json() for slot in existing], "plans": plans},
        "cred_catch_up": {"existing": [slot.to_json() for slot in cred_existing], "plans": cred_plans},
        "org_cred_catch_up": {"existing": [slot.to_json() for slot in org_cred_existing], "plans": org_cred_plans},
        "admin_catch_up": {
            "log": _ref("edge_rotated", EDGE_COUNT + 1),
            "existing": [slot.to_json() for slot in edge_existing],
            "plans": admin_plans,
        },
        "agents": agent_section,
        "invalid": invalid + agent_invalid,
    }


def _agent_wraps_part(ctx: Context, agents: LogBuilder, edge: LogBuilder) -> tuple[Json, list[Json]]:
    """log-wraps.json, ADR-082: wraps to and by agents, credential readers over two logs, catch-up of agents."""
    founder = sig_pk("alice")
    agents_ref = {"log": _ref("agents", AGENTS_COUNT), "founder": founder}
    other_ref = {"log": _ref("agents_other_org", 2), "founder": founder}

    def args(wrapper: str, scope: str, scope_id: str, epoch: int, recipient: str, **extra: object) -> Json:
        wrapped = make_wrap(wrapper, scope, scope_id, epoch, recipient)
        return {**agents_ref, "wrap": wrapped, "wrapped_by": pid(wrapper), **extra}

    def cred(wrapper: str, recipient: str, logs: Sequence[Json]) -> Json:
        return {**args(wrapper, "cred", pid("alice"), 1, recipient), "current_epoch": 1, "logs": list(logs)}

    valid = []
    for name, case in (
        ("W2_user_for_its_agent", args("alice", "wg", W1, 2, "bot")),
        ("W2_older_epoch_for_agent", args("alice", "wg", W1, 1, "bot")),
        ("W1_agent_self", args("bot", "org", ORG, 2, "bot")),
        ("W2_agent_for_device", args("bot", "wg", W1, 2, "device")),
        ("W2_returned_agent_for_its_user", args("bot5", "wg", W0, 2, "bob")),
    ):
        ctx.run("check_wrap", case)
        valid.append({"name": name, "args": case})
    cred_valid = []
    for name, case in (
        ("cred_owner_for_its_agent", cred("alice", "bot", [agents_ref])),
        ("cred_agent_for_device", cred("bot", "device", [agents_ref])),
        ("cred_superseding_agent", cred("alice", "bot6", [agents_ref, other_ref])),
    ):
        ctx.run("check_cred_wrap", case)
        cred_valid.append({"name": name, "args": case})
    neg = ctx.negative
    invalid = [
        neg("wrap_to_agent_that_left", "check_wrap", args("alice", "org", ORG, 2, "bot4"), "wrap_unauthorized"),
        neg("wrap_to_replaced_agent", "check_wrap", args("alice", "org", ORG, 1, "bot2"), "wrap_unauthorized"),
        neg("wrap_to_removed_agent", "check_wrap", args("alice", "org", ORG, 1, "bot3"), "wrap_unauthorized"),
        neg(
            "agent_receives_no_scope_its_user_cannot_read",
            "check_wrap",
            args("alice", "wg", W1, 2, "bot5"),
            "wrap_unauthorized",
        ),
        neg(
            "agent_wraps_epoch_it_never_held",
            "check_wrap",
            args("bot5", "wg", W1, 1, "device"),
            "wrap_unauthorized",
        ),
        neg(
            "cred_superseded_agent_is_no_reader",
            "check_cred_wrap",
            cred("alice", "bot", [agents_ref, other_ref]),
            "wrap_unauthorized",
        ),
        neg(
            "cred_no_consent_of_org_joined_through_revoked_agent",
            "check_cred_wrap",
            cred("alice", "device", [agents_ref, other_ref]),
            "wrap_unauthorized",
        ),
        neg(
            "cred_left_agent_is_no_reader",
            "check_cred_wrap",
            {**cred("carol", "bot4", [agents_ref]), "wrap": make_wrap("carol", "cred", pid("carol"), 1, "bot4")},
            "wrap_unauthorized",
        ),
    ]
    readers = [
        {"logs": logs, "owner": pid(owner), "readers": ctx.run("cred_readers", {"logs": logs, "owner": pid(owner)})}
        for logs, owner in (
            ([agents_ref], "alice"),
            ([agents_ref, other_ref], "alice"),
            ([agents_ref], "bob"),
            ([agents_ref], "carol"),
        )
    ]
    revoked = [
        {"logs": logs, "revoked": ctx.run("revoked_agents", {"logs": logs})}
        for logs in ([agents_ref], [other_ref], [agents_ref, other_ref])
    ]
    log = ctx.log(agents_ref)
    existing = [
        wraps.WrapSlot("org", ORG, 1, pid("alice")),
        wraps.WrapSlot("org", ORG, 2, pid("alice")),
        wraps.WrapSlot("wg", W1, 1, pid("alice")),
        wraps.WrapSlot("wg", W1, 2, pid("alice")),
        wraps.WrapSlot("wg", W0, 2, pid("alice")),
    ]
    plans = [
        {
            "wrapper": pid(wrapper),
            "missing": [slot.to_json() for slot in catchup.missing_wraps(log, pid(wrapper), existing)],
        }
        for wrapper in ("alice", "bot", "bob")
    ]
    cred_existing = [wraps.WrapSlot("cred", pid("alice"), e, pid("alice")) for e in (1, 2)]
    cred_plans = [
        {
            "owner": pid("alice"),
            "wrapper": pid(wrapper),
            "current_epoch": 2,
            "missing": [
                slot.to_json()
                for slot in catchup.missing_cred_wraps(
                    [log], pid("alice"), pid(wrapper), current_epoch=2, existing=cred_existing
                )
            ],
        }
        for wrapper in ("alice", "bot")
    ]
    # an admin without explicit access and its agent get every epoch of the workgroup (ADR-058 section 2)
    edge_agent = edge.copy()
    edge_agent.agent_add("carol", "bot3", "carol", 1)  # 13
    edge_agent.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2)  # 14
    edge_log = verify.verify_log(edge_agent.entries, founder)
    edge_existing = [wraps.WrapSlot("wg", W1, 2, r) for r in (pid("alice"), pid("bob"), pid("dev2"))]
    edge_missing = catchup.missing_wraps(edge_log, pid("alice"), edge_existing)
    section = {
        "valid": valid,
        "cred_valid": cred_valid,
        "cred_readers": readers,
        "revoked_agents": revoked,
        "catch_up": {
            "log": _ref("agents", AGENTS_COUNT),
            "existing": [slot.to_json() for slot in existing],
            "plans": plans,
        },
        "cred_catch_up": {"existing": [slot.to_json() for slot in cred_existing], "plans": cred_plans},
        "admin_catch_up": {
            "log": _ref("edge_agent", EDGE_COUNT + 2),
            "existing": [slot.to_json() for slot in edge_existing],
            "plans": [
                {
                    "wrapper": pid("alice"),
                    "missing": [slot.to_json() for slot in edge_missing],
                }
            ],
        },
        "edge_agent": edge_agent.entries,
    }
    return section, invalid


ORG_C = pid("org/c")
ORG_D = pid("org/d")


def revocation_builders() -> dict[str, LogBuilder]:
    """Logs of the global revocation (ADR-082 section 9.1): in org C bob's agent `bot2` is removed by the admin alice
    (`agent_removed_by_admin`, it only leaves org C) or by bob itself (`agent_removed_by_owner`, revoked everywhere);
    in org D, founded by bob, `bot2` stays active. `agent_rejoined` gives the catch-up of an agent whose user rejoined
    after rotations: no epoch from before the user's current membership."""
    logs: dict[str, LogBuilder] = {}
    for name, remover in (("agent_removed_by_admin", "alice"), ("agent_removed_by_owner", "bob")):
        log = LogBuilder(ORG_C)
        log.genesis(default_wg=pid("wg/c"))  # 0
        log.invite_user("alice", "bob")  # 1
        log.accept("bob", agent=agent_member("bot2", "bob", 1))  # 2
        log.add("remove", remover, subject=pid("bot2"))  # 3
        logs[name] = log
    own = LogBuilder(ORG_D)
    own.genesis("bob", default_wg=pid("wg/d"), agent="bot2")  # 0
    logs["bob_org"] = own
    rejoined = LogBuilder()
    rejoined.genesis()  # 0
    rejoined.invite_user("alice", "bob")  # 1
    rejoined.accept("bob", agent=agent_member("bot2", "bob", 1))  # 2
    rejoined.add("remove", "alice", subject=pid("bob"))  # 3 (bot2 with its user)
    rejoined.add("rotate", "alice", scope="org", scope_id=ORG, epoch=2)  # 4
    rejoined.add("rotate", "alice", scope="wg", scope_id=W0, epoch=2)  # 5
    rejoined.invite_user("alice", "bob-again")  # 6
    rejoined.accept("bob", code_label="bob-again", agent=agent_member("bot2", "bob", 1))  # 7
    rejoined.add("rotate", "alice", scope="wg", scope_id=W0, epoch=3)  # 8
    logs["agent_rejoined"] = rejoined
    return logs


def _revocation_part(ctx: Context) -> tuple[Json, list[Json]]:
    """log-wraps.json: only its user (or the agent itself) revokes an agent everywhere; and catch-up after a rejoin."""
    by_admin = {"log": _ref("agent_removed_by_admin", 4), "founder": sig_pk("alice")}
    by_owner = {"log": _ref("agent_removed_by_owner", 4), "founder": sig_pk("alice")}
    bob_org = {"log": _ref("bob_org", 1), "founder": sig_pk("bob")}

    def cred(logs: Sequence[Json]) -> Json:
        wrapped = make_wrap("bob", "cred", pid("bob"), 1, "bot2")
        return {"wrap": wrapped, "wrapped_by": pid("bob"), "current_epoch": 1, "logs": list(logs)}

    readers = [
        {"logs": logs, "owner": pid("bob"), "readers": ctx.run("cred_readers", {"logs": logs, "owner": pid("bob")})}
        for logs in ([by_admin, bob_org], [by_owner, bob_org])
    ]
    revoked = [
        {"logs": logs, "revoked": ctx.run("revoked_agents", {"logs": logs})}
        for logs in ([by_admin, bob_org], [by_owner, bob_org])
    ]
    ctx.run("check_cred_wrap", cred([by_admin, bob_org]))
    invalid = [
        ctx.negative(
            "cred_agent_revoked_by_its_user_in_another_org",
            "check_cred_wrap",
            cred([by_owner, bob_org]),
            "wrap_unauthorized",
        )
    ]
    rejoined_ref = {"log": _ref("agent_rejoined", 9), "founder": sig_pk("alice")}
    log = ctx.log(rejoined_ref)
    existing = [
        wraps.WrapSlot(scope, sid, epoch, pid("bob")) for scope, sid in (("org", ORG), ("wg", W0)) for epoch in (1, 2)
    ]
    section = {
        "cred_readers": readers,
        "revoked_agents": revoked,
        "cred_valid": [{"name": "cred_agent_removed_by_admin_elsewhere", "args": cred([by_admin, bob_org])}],
        "rejoin_catch_up": {
            "log": rejoined_ref["log"],
            "existing": [slot.to_json() for slot in existing],
            "plans": [
                {
                    "wrapper": pid("alice"),
                    "missing": [slot.to_json() for slot in catchup.missing_wraps(log, pid("alice"), existing)],
                }
            ],
        },
    }
    return section, invalid


# --- log-events.json ---

TASK = pid("task")
SESSION = pid("session")


def build_events() -> Json:
    """The kind matrix and event checks E1-E9 for readers and the hub against the main log (section 6.4)."""
    main, plat, agents = main_builder(), platform_builder(), agents_builder()
    ctx = Context({"main": main.entries, "platform": plat.entries, "agents": agents.entries})
    full = len(main.entries)

    def ev(
        author: str, kind: str, scope: str, scope_id: str, epoch: int, /, *, lh: int, label: str, **extra: str
    ) -> Json:
        return make_event(author, kind, scope=scope, scope_id=scope_id, epoch=epoch, lh=lh, label=label, **extra)

    reader_cases = {
        "org_profile": ev("alice", "org.profile", "org", ORG, 1, lh=0, label="org-profile"),
        "wg_profile": ev("alice", "wg.profile", "org", ORG, 1, lh=1, label="wg-profile", wg=W1),
        "member_profile": ev("bob", "member.profile", "org", ORG, 1, lh=3, label="member-profile", ref=pid("bob")),
        "prompt_at_lh": ev("bob", "prompt", "wg", W1, 1, lh=12, label="prompt-bob", wg=W1, task=TASK),
        "session_output": ev(
            "device", "session.output", "wg", W1, 2, lh=26, label="output", wg=W1, task=TASK, session=SESSION
        ),
        "operation_params": ev(
            "device", "operation.params", "wg", W1, 2, lh=26, label="op-params", wg=W1, ref=pid("op")
        ),
        "platform_read": ev("pdev", "platform.read", "wg", W1, 2, lh=26, label="read", wg=W1, ref="tasks.list"),
        "task_meta_device": ev("device", "task.meta", "wg", W1, 2, lh=26, label="title", wg=W1, task=TASK),
        "whiteboard_org": ev("alice", "whiteboard.entry", "org", ORG, 3, lh=26, label="wb-org", ref=pid("entry/org")),
        "whiteboard_task": ev(
            "alice", "whiteboard.edit", "wg", W0, 4, lh=26, label="wb-task", wg=W0, task=TASK, ref=pid("entry")
        ),
        "device_profile": ev(
            "alice", "device.profile", "org", ORG, 3, lh=26, label="device-profile", ref=pid("device")
        ),
        "member_name": ev("alice", "member.name", "org", ORG, 3, lh=26, label="member-name", ref=pid("bob")),
        "member_name_invitation": ev(
            "alice", "member.name", "org", ORG, 3, lh=26, label="member-name-inv", ref=secrets("bob").invite_id
        ),
        "settings": ev("alice", "settings.workgroup", "wg", W1, 2, lh=26, label="settings", wg=W1),
    }
    valid = []
    for name, sealed in reader_cases.items():
        args = _main_args(full, event=sealed)
        if name == "session_output":
            args["session_device"] = pid("device")
        ctx.run("check_event", args)
        valid.append({"name": name, "args": args, "scope_key": b64u_encode(scope_key(*_scope_of(sealed)))})
    now = ts(200)
    hub_valid = []
    grace_event = ev("device", "operation.result", "wg", W1, 1, lh=26, label="grace", wg=W1, ref=pid("op"))
    for name, args in (
        (
            "session_output",
            _main_args(full, event=reader_cases["session_output"], now=now, session_device=pid("device")),
        ),
        ("settings", _main_args(full, event=reader_cases["settings"], now=now)),
        ("grace_previous_epoch", _main_args(full, event=grace_event, now=ts(23), rotated_at=ts(18))),
    ):
        ctx.run("check_event_hub", args)
        hub_valid.append({"name": name, "args": args})
    neg = ctx.negative
    invalid = [
        neg(
            "E1_org_mismatch",
            "check_event",
            _main_args(full, event=_other_org(reader_cases["settings"])),
            "header_invalid",
        ),
        neg(
            "E2_version",
            "check_event",
            _main_args(full, event=_with_header(reader_cases["settings"], v=2)),
            "header_invalid",
        ),
        neg(
            "L10_lh_beyond_head",
            "check_event",
            _main_args(full, event=ev("alice", "org.profile", "org", ORG, 3, lh=27, label="future")),
            "log_fork",
        ),
        neg(
            "author_unknown_at_lh",
            "check_event",
            _main_args(full, event=ev("bob", "member.profile", "org", ORG, 1, lh=2, label="early", ref=pid("bob"))),
            "author_not_writer",
        ),
        neg(
            "author_removed_at_lh",
            "check_event",
            _main_args(full, event=ev("carol", "org.profile", "org", ORG, 2, lh=20, label="removed")),
            "author_not_writer",
        ),
        neg(
            "E3_prompt_by_device",
            "check_event",
            _main_args(full, event=ev("device", "prompt", "wg", W1, 2, lh=26, label="dev-prompt", wg=W1, task=TASK)),
            "kind_not_allowed",
        ),
        neg(
            "E3_output_by_user",
            "check_event",
            _main_args(
                full,
                event=ev(
                    "alice",
                    "session.output",
                    "wg",
                    W1,
                    2,
                    lh=26,
                    label="user-output",
                    wg=W1,
                    task=TASK,
                    session=SESSION,
                ),
            ),
            "kind_not_allowed",
        ),
        neg(
            "E3_org_kind_in_wg_scope",
            "check_event",
            _main_args(full, event=ev("alice", "org.profile", "wg", W1, 2, lh=26, label="org-in-wg", wg=W1)),
            "kind_not_allowed",
        ),
        neg(
            "E3_unknown_kind",
            "check_event",
            _main_args(full, event=ev("alice", "chat.message", "wg", W1, 2, lh=26, label="unknown", wg=W1)),
            "kind_not_allowed",
        ),
        neg(
            "header_ref_not_author",
            "check_event",
            _main_args(
                full, event=ev("alice", "member.profile", "org", ORG, 3, lh=26, label="ref-other", ref=pid("bob"))
            ),
            "header_invalid",
        ),
        neg(
            "header_prompt_with_session",
            "check_event",
            _main_args(
                full,
                event=ev(
                    "alice", "prompt", "wg", W1, 2, lh=26, label="prompt-session", wg=W1, task=TASK, session=SESSION
                ),
            ),
            "header_invalid",
        ),
        neg(
            "header_wg_profile_unknown_target",
            "check_event",
            _main_args(
                full, event=ev("alice", "wg.profile", "org", ORG, 3, lh=26, label="wg-unknown", wg=pid("wg/none"))
            ),
            "header_invalid",
        ),
        neg(
            "header_read_target_empty",
            "check_event",
            _main_args(full, event=ev("pdev", "platform.read", "wg", W1, 2, lh=26, label="read-empty", wg=W1)),
            "header_invalid",
        ),
        neg(
            "E4_forged_by_hub",
            "check_event",
            _main_args(full, event=_resigned_event(reader_cases["settings"], "hub")),
            "signature_invalid",
        ),
        neg(
            "E5_reader_prompts",
            "check_event",
            _main_args(full, event=ev("bob", "prompt", "wg", W1, 1, lh=3, label="read-only", wg=W1, task=TASK)),
            "author_not_writer",
        ),
        neg(
            "E5_member_names_a_member",
            "check_event",
            _main_args(full, event=ev("bob", "member.name", "org", ORG, 1, lh=3, label="not-admin", ref=pid("bob"))),
            "author_not_writer",
        ),
        neg(
            "header_member_name_ref_text",
            "check_event",
            _main_args(full, event=ev("alice", "member.name", "org", ORG, 3, lh=26, label="ref-text", ref="bob")),
            "header_invalid",
        ),
        neg(
            "header_member_name_ref_device",
            "check_event",
            _main_args(
                full, event=ev("alice", "member.name", "org", ORG, 3, lh=26, label="ref-device", ref=pid("device"))
            ),
            "header_invalid",
        ),
        neg(
            "header_member_name_ref_joined_after_lh",
            "check_event",
            _main_args(full, event=ev("alice", "member.name", "org", ORG, 1, lh=2, label="ref-later", ref=pid("bob"))),
            "header_invalid",
        ),
        neg(
            "header_member_name_ref_unknown",
            "check_event",
            _main_args(
                full, event=ev("alice", "member.name", "org", ORG, 3, lh=26, label="ref-unknown", ref=pid("nobody"))
            ),
            "header_invalid",
        ),
        neg(
            "header_member_name_ref_device_invitation",
            "check_event",
            _main_args(
                full,
                event=ev(
                    "alice",
                    "member.name",
                    "org",
                    ORG,
                    3,
                    lh=26,
                    label="ref-device_invitation",
                    ref=secrets("pdev").invite_id,
                ),
            ),
            "header_invalid",
        ),
        neg(
            "E5_member_writes_org_profile",
            "check_event",
            _main_args(full, event=ev("bob", "org.profile", "org", ORG, 1, lh=3, label="not-admin")),
            "author_not_writer",
        ),
        neg(
            "E5_device_not_assigned",
            "check_event",
            _main_args(
                full,
                event=ev("device", "operation.params", "wg", W0, 4, lh=26, label="not-assigned", wg=W0, ref=pid("op")),
            ),
            "author_not_writer",
        ),
        neg(
            "E5_archived_workgroup",
            "check_event",
            _main_args(full, event=ev("alice", "settings.workgroup", "wg", W2, 1, lh=26, label="archived", wg=W2)),
            "author_not_writer",
        ),
        neg(
            "E5_decertified_device",
            "check_event",
            _main_args(full, event=reader_cases["platform_read"], excluded=[pid("pdev")]),
            "author_not_writer",
        ),
        neg(
            "E7_reader_other_session_device",
            "check_event",
            _main_args(full, event=reader_cases["session_output"], session_device=pid("pdev")),
            "author_not_writer",
        ),
        neg(
            "hub_E5_at_receipt",
            "check_event_hub",
            _main_args(full, event=reader_cases["prompt_at_lh"], now=now),
            "author_not_writer",
        ),
        neg(
            "hub_E6_old_epoch",
            "check_event_hub",
            _main_args(full, event=ev("alice", "org.profile", "org", ORG, 2, lh=26, label="old-epoch"), now=now),
            "epoch_stale",
        ),
        neg(
            "hub_E6_future_epoch",
            "check_event_hub",
            _main_args(full, event=ev("alice", "org.profile", "org", ORG, 4, lh=26, label="new-epoch"), now=now),
            "epoch_stale",
        ),
        neg(
            "hub_E6_grace_over",
            "check_event_hub",
            _main_args(full, event=grace_event, now=ts(29), rotated_at=ts(18)),
            "epoch_stale",
        ),
        neg(
            "hub_E7_session_on_other_device",
            "check_event_hub",
            _main_args(full, event=reader_cases["session_output"], now=now, session_device=pid("pdev")),
            "author_not_writer",
        ),
        neg(
            "hub_E7_session_unknown",
            "check_event_hub",
            _main_args(full, event=reader_cases["session_output"], now=now),
            "author_not_writer",
        ),
        neg(
            "E9_same_id_other_bytes",
            "check_replay",
            {
                "stored": reader_cases["settings"],
                "incoming": {**reader_cases["settings"], "ct": reader_cases["prompt_at_lh"]["ct"]},
            },
            "event_conflict",
        ),
    ]
    agent_section, agent_invalid = _agent_events_part(ctx)
    return {
        "about": "Event rules E1-E9 of 04-crypto.md section 6.4 for readers and the hub against the main log of "
        "log.json, and the kind matrix of section 4.3 (CON-03, equal to contracts/enums/event-kinds.json). "
        "See README.md, 'Log vectors'.",
        "identities": _identities(),
        "ids": {**_ids(), "task": TASK, "session": SESSION},
        "founder": sig_pk("alice"),
        "logs": {"main": main.entries, "platform": plat.entries, "agents": agents.entries},
        "matrix": events.matrix(),
        "grace_seconds": int(events.GRACE.total_seconds()),
        "valid": valid,
        "hub_valid": hub_valid,
        "replay": {"stored": reader_cases["settings"], "incoming": reader_cases["settings"], "replayed": True},
        "agents": agent_section,
        "invalid": invalid + agent_invalid,
    }


def _agent_events_part(ctx: Context) -> tuple[Json, list[Json]]:
    """log-events.json, ADR-082: events of agents with their users' rights (against the agents log)."""

    def ev(
        author: str, kind: str, scope: str, scope_id: str, epoch: int, /, *, lh: int, label: str, **extra: str
    ) -> Json:
        return make_event(author, kind, scope=scope, scope_id=scope_id, epoch=epoch, lh=lh, label=label, **extra)

    cases = {
        "prompt_by_agent": ev("bot", "prompt", "wg", W1, 2, lh=8, label="agent-prompt", wg=W1, task=TASK),
        "prompt_by_agent_of_writer": ev("bot2", "prompt", "wg", W1, 2, lh=8, label="agent2-prompt", wg=W1, task=TASK),
        "member_profile_by_agent": ev(
            "bot", "member.profile", "org", ORG, 1, lh=8, label="agent-profile", ref=pid("alice")
        ),
        "org_profile_by_agent_of_admin": ev("bot", "org.profile", "org", ORG, 1, lh=8, label="agent-org-profile"),
        "member_name_by_agent_of_admin": ev(
            "bot", "member.name", "org", ORG, 1, lh=8, label="agent-member-name", ref=pid("bob")
        ),
    }
    valid = []
    for name, sealed in cases.items():
        args = _agents_args(AGENTS_COUNT, event=sealed)
        ctx.run("check_event", args)
        valid.append({"name": name, "args": args, "scope_key": b64u_encode(scope_key(*_scope_of(sealed)))})
    hub = _agents_args(AGENTS_COUNT, event=cases["prompt_by_agent"], now=ts(200))
    ctx.run("check_event_hub", hub)
    neg = ctx.negative
    invalid = [
        neg(
            "agent_member_profile_ref_is_agent",
            "check_event",
            _agents_args(
                AGENTS_COUNT,
                event=ev("bot", "member.profile", "org", ORG, 1, lh=8, label="agent-profile-self", ref=pid("bot")),
            ),
            "header_invalid",
        ),
        neg(
            "agent_of_member_writes_org_profile",
            "check_event",
            _agents_args(AGENTS_COUNT, event=ev("bot2", "org.profile", "org", ORG, 1, lh=8, label="agent2-org")),
            "author_not_writer",
        ),
        neg(
            "agent_writes_session_output",
            "check_event",
            _agents_args(
                AGENTS_COUNT,
                event=ev(
                    "bot", "session.output", "wg", W1, 2, lh=8, label="agent-output", wg=W1, task=TASK, session=SESSION
                ),
            ),
            "kind_not_allowed",
        ),
        neg(
            "agent_of_reader_prompts",
            "check_event",
            _agents_args(
                AGENTS_COUNT,
                event=ev("bot2", "prompt", "wg", W1, 1, lh=6, label="agent2-read-only", wg=W1, task=TASK),
            ),
            "author_not_writer",
        ),
        neg(
            "replaced_agent_at_lh",
            "check_event",
            _agents_args(
                AGENTS_COUNT,
                event=ev("bot2", "prompt", "wg", W1, 2, lh=13, label="agent2-replaced", wg=W1, task=TASK),
            ),
            "author_not_writer",
        ),
        neg(
            "member_name_of_an_agent",
            "check_event",
            _agents_args(
                AGENTS_COUNT,
                event=ev("bot", "member.name", "org", ORG, 1, lh=8, label="agent-name-agent", ref=pid("bot2")),
            ),
            "header_invalid",
        ),
        neg(
            "hub_replaced_agent_at_receipt",
            "check_event_hub",
            _agents_args(AGENTS_COUNT, event=cases["prompt_by_agent_of_writer"], now=ts(200)),
            "author_not_writer",
        ),
    ]
    return {"valid": valid, "hub_valid": [{"name": "prompt_by_agent", "args": hub}]}, invalid


def _scope_of(sealed: Json) -> tuple[str, str, int]:
    header = sealed["header"]
    return header["scope"], header["scope_id"], header["epoch"]


def _with_header(sealed: Json, **changes: object) -> Json:
    return {**sealed, "header": {**sealed["header"], **changes}}


def _other_org(sealed: Json) -> Json:
    return _with_header(sealed, org=pid("org/other"))


def _resigned_event(sealed: Json, signer: str) -> Json:
    text = signed.signature_text(event.FORMAT, sealed["header"], b64u_decode(sealed["ct"]))
    return {**sealed, "sig": b64u_encode(keys.ed25519_sign(identity(signer).sig, text))}


SECTIONS: tuple[Section, ...] = (
    Section("log", build_log),
    Section("log-rotation", build_rotation),
    Section("log-wraps", build_wraps),
    Section("log-events", build_events),
    Section("platform-log", build_platform_log),
)
