"""The paired account and what the tool verifies itself (11-agent-access.md section 8.3).

`Account.open` loads the state and the private keys and opens the user's identity from the sealed bundle with the
agent's key and `r` (the authenticity comes from `k`, section 3). Everything the tool uses is verified like a browser
verifies it:

- membership logs: L1-L8 (`verify_log`) against the founder named by genesis, accepted only when the identity itself
  created the org (genesis by the user with the user's key) or accepted an invitation into it (an `accept` of the
  user signed with the user's key: its `prev` chain pins every entry before it, so the founder is authenticated);
  the served head must be the head of the verified entries;
- scope keys: only wraps that pass W1/W2 against the verified log (`check_wrap`); foreign wraps are ignored;
- the credential key `CK`: only self-wraps of the user signed by the user's identity (W1);
- the platform log: against the platform root key received over the loopback (or `--platform-root-key`);
- profile events (names): signature of the author from the verified log, admins only for org and workgroup names,
  `ref == author` for member names; events that fail are skipped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.errors import PlatformError
from agent_platform.hub import AgentClient, segment
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import wrap as wrap_format
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log.entries import Head, check_extends
from agent_runner.crypto.log.platform import PlatformState, VerifiedPlatformLog, verify_platform_log
from agent_runner.crypto.log.state import ORG, WG
from agent_runner.crypto.log.verify import VerifiedLog, verify_log
from agent_runner.crypto.log.wraps import CRED, WRAP_KEYS, check_wrap

PAGE = 1000
KeyRing = dict[tuple[str, str, int], bytes]


def unverified(reason: str) -> PlatformError:
    """An answer of the hub that does not verify (`reason`: a crypto code or a fixed reason)."""
    return PlatformError("hub_refused", reason=reason)


class LogUnverifiable(PlatformError):
    """An org log whose entries the hub served but that does not verify (the web's blocked org), as opposed to a
    refusal or an unreachable hub, which says nothing about the log."""


def _entries(page: Mapping[str, Any]) -> list[object]:
    items = page.get("entries")
    if not isinstance(items, list) or not all(isinstance(item, dict) and "entry" in item for item in items):
        raise unverified("response_invalid")
    return [item["entry"] for item in items]


def _wrap_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in WRAP_KEYS}


@dataclass
class Names:
    """Verified profile names of an org."""

    org: str | None = None
    workgroups: dict[str, str] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)


@dataclass
class Account:
    """The paired agent: state, keys, the opened identity and a signed client."""

    home: Home
    state: dict[str, Any]
    agent: Identity
    identity: Identity
    client: AgentClient
    platform_root: str | None = None
    _wraps: list[dict[str, Any]] | None = None
    _platform: VerifiedPlatformLog | None = None
    _logs: dict[str, VerifiedLog] = field(default_factory=dict)
    _learned: KeyRing = field(default_factory=dict)

    @classmethod
    def open(cls, home: Home, platform_root: str | None = None) -> Account:
        state = home.load()
        try:
            agent_id, user_id = str(state["agent_id"]), str(state["user_id"])
            origin, hub_key = str(state["hub"]), b64u_decode(state["hub_key"]["public_key"], 32)
            secret = b64u_decode(state["secret"], 32)
            agent = home.load_keys(agent_id, state.get("key_store"))
            identity = agent_crypto.open_identity(agent.kem, secret, agent_id, user_id, str(state["bundle_ct"]))
        except (KeyError, TypeError, ValueError, CryptoError):
            raise PlatformError("state_damaged") from None

        def revoked() -> None:
            home.delete(agent_id, state.get("key_store"))

        client = AgentClient(origin, agent_id, agent, hub_key, on_revoked=revoked)
        root = platform_root or cast(str | None, state.get("platform_root_key"))
        return cls(home, state, agent, identity, client, root)

    @property
    def user_id(self) -> str:
        return str(self.state["user_id"])

    @property
    def agent_id(self) -> str:
        return str(self.state["agent_id"])

    @property
    def origin(self) -> str:
        return str(self.state["hub"])

    @property
    def sig_public(self) -> str:
        return str(self.identity.public()["sig"])

    def call(self, method: str, path: str, body: object | None = None) -> dict[str, Any]:
        return self.client.call(method, path, body)

    def close(self) -> None:
        self.client.close()

    def me(self) -> dict[str, Any]:
        """`GET /v1/me`; its identity must be the opened identity (consistency check)."""
        me = self.call("GET", "/v1/me")
        identity = me.get("identity")
        expected = {"kem_pub": self.identity.public()["kem"], "sig_pub": self.sig_public}
        if me.get("user_id") != self.user_id or identity != expected:
            raise unverified("identity_mismatch")
        return me

    # --- logs ---

    def _pages(self, path: str) -> tuple[list[object], dict[str, Any]]:
        entries: list[object] = []
        after: int | None = None
        while True:
            query = {"limit": PAGE} if after is None else {"after": after, "limit": PAGE}
            page = self.call("GET", f"{path}?{urlencode(query)}")
            entries += _entries(page)
            following = page.get("next_after")
            head = page.get("head")
            if following is None:
                if not isinstance(head, dict):
                    raise unverified("response_invalid")
                return entries, head
            if not isinstance(following, int) or (after is not None and following <= after):
                raise unverified("response_invalid")
            after = following

    def platform(self) -> PlatformState:
        """The platform log verified against the platform root key (P1-P4)."""
        return self.platform_log().state

    def platform_log(self) -> VerifiedPlatformLog:
        """The verified platform log with its entries (`device_decertify` times)."""
        if self._platform is None:
            if not self.platform_root:
                raise PlatformError("invalid_argument", reason="platform_root_key_missing")
            entries, head = self._pages("/v1/platform/log")
            try:
                verified = verify_platform_log(entries, self.platform_root)
            except CryptoError as error:
                raise unverified(error.code) from None
            if head != verified.state.head_value().to_json():
                raise unverified("log_chain")
            self._platform = verified
        return self._platform

    def org_log(self, org: str) -> VerifiedLog:
        """The org's membership log, verified (module docstring)."""
        if org not in self._logs:
            entries, head = self._pages(f"/v1/orgs/{segment(org)}/log")
            self._logs[org] = self._verify(org, entries, head)
        return self._logs[org]

    def _verify(self, org: str, entries: Sequence[object], head: Mapping[str, Any]) -> VerifiedLog:
        try:
            genesis = cast(dict[str, Any], entries[0])["body"]
            founder = str(genesis["pub"]["sig"])
        except (IndexError, KeyError, TypeError):
            raise LogUnverifiable("hub_refused", reason="log_genesis") from None
        platform = self.platform() if _needs_platform(entries) else None
        try:
            log = verify_log(entries, founder, platform=platform)
        except CryptoError as error:
            raise LogUnverifiable("hub_refused", reason=error.code) from None
        if log.state.org != org or head != log.head.to_json():
            raise LogUnverifiable("hub_refused", reason="log_chain")
        if not self._trusts(log):
            raise LogUnverifiable("hub_refused", reason="founder_untrusted")
        return log

    def _trusts(self, log: VerifiedLog) -> bool:
        """The identity created the org, accepted an invitation into it or was admitted with its own keys (an `admit`
        whose admitter's identity rule L5 proved through the platform log, so a hub cannot invent the org; ADR-060)."""
        own = self.sig_public
        for entry in log.entries:
            body = cast(dict[str, Any], entry["body"])
            joined = body["type"] in ("genesis", "accept", "admit")
            if joined and body["subject"] == self.user_id and body["pub"]["sig"] == own:
                return True
        return False

    def forget(self, org: str) -> None:
        """Read the org's log again on next use (after `seq_conflict`)."""
        self._logs.pop(org, None)

    def remember(self, org: str, log: VerifiedLog) -> None:
        """The log after an accepted append (built and checked locally)."""
        self._logs[org] = log

    # --- keys ---

    def learn(self, keys: Mapping[tuple[str, str, int], bytes]) -> None:
        """Keep scope keys this run created (a batch's new epochs of scopes the user reads, like the browser does)."""
        self._learned.update(keys)

    def refresh_wraps(self) -> None:
        """Read the wraps again on next use (after the user's own `CK` changed)."""
        self._wraps = None

    def wraps(self) -> list[dict[str, Any]]:
        if self._wraps is None:
            answer = self.call("GET", "/v1/wraps")
            found = answer.get("wraps")
            if not isinstance(found, list) or not all(isinstance(item, dict) for item in found):
                raise unverified("response_invalid")
            self._wraps = found
        return self._wraps

    def keys(self, org: str) -> KeyRing:
        """The org and workgroup keys of `org` from wraps that pass W1/W2 against the verified log."""
        log = self.org_log(org)
        state = log.state
        ring: KeyRing = {
            slot: key
            for slot, key in self._learned.items()
            if (slot[0] == ORG and slot[1] == org) or (slot[0] == WG and slot[1] in state.workgroups)
        }
        for item in self.wraps():
            scope, scope_id = item.get("scope"), item.get("scope_id")
            if not ((scope == ORG and scope_id == org) or (scope == WG and scope_id in state.workgroups)):
                continue
            try:
                slot = check_wrap(_wrap_record(item), str(item.get("wrapped_by")), log)
                if slot.recipient != self.user_id:
                    continue
                key = wrap_format.unwrap_key(
                    self.identity.kem,
                    scope=slot.scope,
                    scope_id=slot.scope_id,
                    epoch=slot.epoch,
                    recipient=slot.recipient,
                    wrap=str(item["wrap"]),
                )
            except CryptoError:
                continue
            ring[(slot.scope, slot.scope_id, slot.epoch)] = key
        return ring

    def cred_keys(self) -> dict[int, bytes]:
        """The user's `CK` epochs from self-wraps signed by the identity (W1); other wraps are ignored."""
        found: dict[int, bytes] = {}
        for item in self.wraps():
            if item.get("scope") != CRED or item.get("scope_id") != self.user_id:
                continue
            if item.get("recipient_id") != self.user_id or item.get("wrapped_by") != self.user_id:
                continue
            epoch = item.get("epoch")
            try:
                if not isinstance(epoch, int) or isinstance(epoch, bool):
                    raise CryptoError("header_invalid")
                wrap, user = str(item.get("wrap")), self.user_id
                wrap_format.verify_wrap(
                    self.identity.sig_public(),
                    scope=CRED,
                    scope_id=user,
                    epoch=epoch,
                    recipient=user,
                    wrap=wrap,
                    signature=str(item.get("sig")),
                )
                found[epoch] = wrap_format.unwrap_key(
                    self.identity.kem, scope=CRED, scope_id=user, epoch=epoch, recipient=user, wrap=wrap
                )
            except CryptoError:
                continue
        return found

    def cred_key(self) -> tuple[int, bytes]:
        """The newest verified `CK` epoch and key."""
        found = self.cred_keys()
        if not found:
            raise unverified("cred_key_missing")
        epoch = max(found)
        return epoch, found[epoch]

    # --- names ---

    def names(self, org: str) -> Names:
        """Verified names of the org, its workgroups and members from the profile events of the org scope."""
        log = self.org_log(org)
        ring = self.keys(org)
        names = Names()
        query = urlencode({"kinds": "org.profile,wg.profile,member.profile", "limit": PAGE})
        after = 0
        while True:
            page = self.call("GET", f"/v1/scopes/{segment(org)}/events?{query}&after={after}")
            items = page.get("events")
            if not isinstance(items, list):
                raise unverified("response_invalid")
            for item in items:
                _apply_profile(names, log, ring, item.get("event") if isinstance(item, dict) else None)
            following = page.get("next_after")
            if not isinstance(following, int) or following <= after:
                return names
            after = following

    def display_name(self) -> str | None:
        """The user's display name from the first org whose member profile verifies (None if none)."""
        for org in self.me().get("orgs", []):
            try:
                found = self.names(str(org["org_id"])).members.get(self.user_id)
            except (PlatformError, KeyError, TypeError):
                continue
            if found:
                return found
        return None


def _needs_platform(entries: Sequence[object]) -> bool:
    """A platform device is assigned, or an `admit` anchors identities in the platform log (ADR-060)."""
    for entry in entries:
        body = entry.get("body") if isinstance(entry, dict) else None
        if not isinstance(body, dict):
            continue
        if (body.get("type") == "device_assign" and "platform_seq" in body) or body.get("type") == "admit":
            return True
    return False


def _apply_profile(names: Names, log: VerifiedLog, ring: KeyRing, event: object) -> None:
    try:
        header = cast(dict[str, Any], cast(dict[str, Any], event)["header"])
        author = log.state.principals[str(header["author"])]
        lh = cast(int, header["lh"])
        state = log.state_at(lh)
        key = ring[(ORG, log.state.org, cast(int, header["epoch"]))]
        body = parse_json(open_event(key, event, b64u_decode(author.sig)))
        kind = header["kind"]
        if not isinstance(body, dict):
            return
        if kind == "member.profile" and header["ref"] == author.id and isinstance(body.get("display_name"), str):
            names.members[author.id] = body["display_name"]
        elif kind in ("org.profile", "wg.profile") and state.is_admin(author.id) and isinstance(body.get("name"), str):
            if kind == "org.profile":
                names.org = body["name"]
            else:
                names.workgroups[str(header["wg"])] = body["name"]
    except (CryptoError, KeyError, TypeError):
        return


def check_package_head(head: Mapping[str, Any], entries: Sequence[Mapping[str, object]]) -> None:
    """L9 against the head pinned by an invitation package."""
    check_extends(Head(int(head["seq"]), str(head["hash"])), entries)
