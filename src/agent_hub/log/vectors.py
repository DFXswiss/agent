"""Log vectors of 04-crypto.md section 10 (`contracts/vectors/log*.json`, `platform-log.json`), discovered by the
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

from agent_hub.crypto import enroll, event, invite, keys, signed, wrap
from agent_hub.crypto import identity as identity_proof
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, body_json, format_time, parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.vectors import Section, fixed, fixed_id
from agent_hub.log import catchup, events, platform, rotation, verify, wraps
from agent_hub.log.batch import check_batch
from agent_hub.log.entries import Head, check_extends, entry_hash, sign_log_entry, sign_platform_entry

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

    def genesis(self, founder: str = "alice", default_wg: str = W0, private: bool = False) -> Json:
        """`genesis` by the founder."""
        return self.add(
            "genesis", founder, subject=pid(founder), pub=pub(founder), default_wg=default_wg, private=private
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
        self, label: str, kind: str = "user", code_label: str | None = None, key_label: str | None = None
    ) -> Json:
        """`accept` by `label` with its own code (or `code_label`'s) and its own keys (or `key_label`'s), signed by
        the subject key and by `sk_inv`."""
        found = secrets(code_label or label)
        keys_of = key_label or label
        body = self.body("accept", label, invite_id=found.invite_id, subject=pid(label), kind=kind, pub=pub(keys_of))
        entry = sign_log_entry(body, identity(keys_of).sig, found.signing)
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
PLATFORM_COUNT = 17
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
                a["wrap"], a["wrapped_by"], [self.log(a)], a["current_epoch"], a.get("excluded", [])
            ),
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
        for name in (*NAMES, "root")
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
    ctx = Context(
        {"main": main.entries, "platform": plat.entries, "platform_recertified": recert.entries, "admit": admit.entries}
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
        "invalid": invalid,
    }


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
    }


def build_rotation() -> Json:
    """The rotate set R for every entry type and the edge cases, and batches with and without the exact R."""
    main, plat, edge, rejoin = main_builder(), platform_builder(), edge_builder(), _rejoin_builder()
    logs = {"main": main.entries, "platform": plat.entries, "edge": edge.entries, "rejoin": rejoin.entries}
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
        edge_case("rejoined_member_removed", lambda b: b.add("remove", "alice", subject=pid("bob")), "rejoin"),
    ]
    rejoin_accept = _r_case("rejoin_accept", "rejoin", rejoin, EDGE_COUNT + 2)
    cases.append(rejoin_accept)

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
    ]
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
        "batches": valid_batches,
        "invalid": invalid,
    }


# --- log-wraps.json ---


def build_wraps() -> Json:
    """W1-W3 against the final state of the main log, `cred` wraps and catch-up plans (sections 5.11, 6.3)."""
    main, plat, edge = main_builder(), platform_builder(), edge_builder()
    ctx = Context({"main": main.entries, "platform": plat.entries, "edge": edge.entries})
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
    ):
        ctx.run("check_wrap", case)
        valid.append({"name": name, "args": case})
    cred_valid = []
    for name, case in (
        ("cred_owner_for_device", {**args("alice", "cred", pid("alice"), 2, "device"), "current_epoch": 2}),
        ("cred_device_for_platform_device", {**args("device", "cred", pid("alice"), 1, "pdev"), "current_epoch": 2}),
        ("cred_owner_self", {**args("alice", "cred", pid("alice"), 2, "alice"), "current_epoch": 2}),
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
        # Key model (docs/platform/key-model.md): KM-10, a device without a workgroup is no reader of the org key
        # (ADR-007); KM-06, only principals of the log receive wraps (an agent key is none, KOR-57 changes this).
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
    return {
        "about": "Wrap rules W1-W3 of 04-crypto.md section 6.3 against the final state of the main log of log.json "
        "and, for admins without explicit workgroup access (ADR-058), the edge log of log-rotation.json; cred wraps, "
        "and catch-up plans (section 5.11). See README.md, 'Log vectors'.",
        "identities": _identities(),
        "ids": _ids(),
        "founder": sig_pk("alice"),
        "logs": {"main": main.entries, "platform": plat.entries, "edge": edge.entries, "edge_rotated": rotated.entries},
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
        "admin_catch_up": {
            "log": _ref("edge_rotated", EDGE_COUNT + 1),
            "existing": [slot.to_json() for slot in edge_existing],
            "plans": admin_plans,
        },
        "invalid": invalid,
    }


# --- log-events.json ---

TASK = pid("task")
SESSION = pid("session")


def build_events() -> Json:
    """The kind matrix and event checks E1-E9 for readers and the hub against the main log (section 6.4)."""
    main, plat = main_builder(), platform_builder()
    ctx = Context({"main": main.entries, "platform": plat.entries})
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
    return {
        "about": "Event rules E1-E9 of 04-crypto.md section 6.4 for readers and the hub against the main log of "
        "log.json, and the kind matrix of section 4.3 (CON-03, equal to contracts/enums/event-kinds.json). "
        "See README.md, 'Log vectors'.",
        "identities": _identities(),
        "ids": {**_ids(), "task": TASK, "session": SESSION},
        "founder": sig_pk("alice"),
        "logs": {"main": main.entries, "platform": plat.entries},
        "matrix": events.matrix(),
        "grace_seconds": int(events.GRACE.total_seconds()),
        "valid": valid,
        "hub_valid": hub_valid,
        "replay": {"stored": reader_cases["settings"], "incoming": reader_cases["settings"], "replayed": True},
        "invalid": invalid,
    }


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
