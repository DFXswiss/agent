"""U-HUB-outbox (registration): the handler chains of ADR-030 (base handler of the outbox area or, for `usage` and
`quota`, of the usage area; extensions of other areas after it), the copies of the contract enumerations, the push
pacer and the receipt pruning job."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.billing import outbox as billing_handlers
from agent_hub.errors import HubError
from agent_hub.http.limits import RateLimiter
from agent_hub.models import outbox as models_handlers
from agent_hub.outbox import bodies, chains, jobs
from agent_hub.outbox import outbox as base_handlers
from agent_hub.outbox.base import ItemContext, PushContext, moment
from agent_hub.outbox.pacing import BURST, MIN_INTERVAL_SECONDS, Pacer, pacer_of
from agent_hub.registry import OUTBOX_HANDLERS, RegistryError, discover
from agent_hub.usage import outbox as usage_handlers
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import FixedClock
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.outbox.support import Outbox, new_vm


def _enum(name: str, key: str) -> Any:
    return json.loads((CONTRACTS / "enums" / name).read_text(encoding="utf-8"))[key]


def test_the_copies_equal_the_contract_enumerations() -> None:
    assert list(bodies.ITEM_KINDS) == _enum("outbox-item-kinds.json", "kinds")
    assert list(bodies.OUTBOX_AUDIT_ACTIONS) == _enum("outbox-audit-actions.json", "actions")
    assert set(bodies.AUDIT_DETAILS) == set(bodies.OUTBOX_AUDIT_ACTIONS)
    codes = json.loads((CONTRACTS / "enums" / "operation-error-codes.json").read_text(encoding="utf-8"))
    assert list(bodies.OPERATION_ERROR_CODES) == [
        row if isinstance(row, str) else row["code"] for row in codes["codes"]
    ]
    assert set(bodies.BODIES) == set(bodies.ITEM_KINDS)


BASES = [
    ("outbox", base_handlers.HANDLERS),
    ("usage", usage_handlers.HANDLERS),
    ("billing", billing_handlers.HANDLERS),
    ("models", models_handlers.HANDLERS),
]
MODEL_KINDS = dict.fromkeys(models_handlers.BASE_KINDS, "models")


def test_the_outbox_area_registers_a_base_handler_for_every_kind_but_the_delegated_ones() -> None:
    found = dict(discover(OUTBOX_HANDLERS))
    assert found["outbox"] is base_handlers.HANDLERS
    assert found["usage"] is usage_handlers.HANDLERS
    claimed = chains.claimed_bases(discover(chains.BASE_KINDS))
    assert claimed == {"usage": "usage", "quota": "usage", "runtime": "billing", **MODEL_KINDS}
    assert set(base_handlers.HANDLERS) == set(bodies.ITEM_KINDS) - set(claimed)
    bases = {
        **base_handlers.HANDLERS,
        **usage_handlers.HANDLERS,
        "runtime": billing_handlers.HANDLERS["runtime"],
        **{kind: models_handlers.HANDLERS[kind] for kind in models_handlers.BASE_KINDS},
    }
    assert {kind: chain[0] for kind, chain in chains.chains().items()} == bases


def _noop(_item: ItemContext, _body: object) -> None:
    return None


@pytest.mark.parametrize(
    "found",
    [
        [("outbox", [])],
        [("outbox", {**base_handlers.HANDLERS, "credential": _noop}), BASES[1]],
        [("outbox", {**base_handlers.HANDLERS, "wrap": "not callable"}), BASES[1]],
        [("outbox", {kind: h for kind, h in base_handlers.HANDLERS.items() if kind != "wrap"}), BASES[1]],
        [
            ("outbox", {kind: h for kind, h in base_handlers.HANDLERS.items() if kind != "wrap"}),
            BASES[1],
            ("alpha", {"wrap": _noop}),
        ],
        # the base handler of a claimed kind belongs to the claiming area, not to the outbox area
        [("outbox", {**base_handlers.HANDLERS, "usage": _noop}), BASES[1]],
        # a claimed kind without a handler of its area: no base handler
        [("outbox", base_handlers.HANDLERS)],
        [("outbox", base_handlers.HANDLERS), ("alpha", {"quota": _noop})],
    ],
)
def test_broken_registrations_stop_the_discovery(found: list[tuple[str, object]]) -> None:
    with pytest.raises(RegistryError):
        chains.collect(found)


@pytest.mark.parametrize(
    "found",
    [
        [("outbox", {"wrap"})],
        [("alpha", "usage")],
        [("alpha", {"usage": 1})],
        [("alpha", 7)],
        [("alpha", {"credential"})],
        [("alpha", {"usage"}), ("beta", ["usage"])],
    ],
)
def test_broken_base_claims_stop_the_discovery(found: list[tuple[str, object]]) -> None:
    with pytest.raises(RegistryError):
        chains.claimed_bases(found)


def test_a_claim_makes_the_area_the_base_of_the_kind() -> None:
    found = [("outbox", {k: h for k, h in base_handlers.HANDLERS.items() if k != "wrap"}), ("alpha", {"wrap": _noop})]
    found.extend(BASES[1:])
    claimed = {"wrap": "alpha", "usage": "usage", "quota": "usage", "runtime": "billing", **MODEL_KINDS}
    assert chains.collect(found, claimed)["wrap"] == (_noop,)


def test_extensions_run_after_the_base_handler_in_area_order(outbox: Outbox) -> None:
    calls: list[str] = []

    def record(name: str, *, refuse: bool = False) -> Any:
        def handler(item: ItemContext, body: Any) -> None:
            calls.append(f"{name}:{item.origin_seq}:{type(body).__name__}")
            item.effects.signal("vm.recorded", vm=body.vm_id)
            if refuse:
                raise HubError("forbidden")

        return handler

    configured = chains.configure(
        [
            ("zeta", {"vm_state": record("zeta")}),
            ("outbox", base_handlers.HANDLERS),
            ("alpha", {"vm_state": record("alpha")}),
            *BASES[1:],
        ]
    )
    assert configured["vm_state"][0] is base_handlers.HANDLERS["vm_state"]
    vm = new_vm()
    assert outbox.statuses(outbox.vm(1, vm)) == [(1, "ok", None)]
    assert calls == ["alpha:1:VmStateBody", "zeta:1:VmStateBody"]
    # a refusing extension rejects the item and undoes the base handler's row
    chains.configure([*BASES, ("alpha", {"vm_state": record("alpha", refuse=True)})])
    other = new_vm()
    assert outbox.statuses(outbox.vm(2, other)) == [(2, "rejected", "forbidden")]
    assert outbox.desk.rows("SELECT count(*) FROM hub.vm WHERE id = %s", other) == [(0,)]


def test_the_device_view_is_loaded_once_per_push(outbox: Outbox, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[UUID] = []

    def view(_connection: object, device: UUID, _root: bytes) -> object:
        loaded.append(device)
        return SimpleNamespace(orgs=[])

    monkeypatch.setattr("agent_hub.outbox.base.device_view", view)
    push = PushContext(connection=None, device=UUID(outbox.device.id), now=outbox.hub.clock(), root_key=b"")  # type: ignore[arg-type]
    item = ItemContext(push=push, origin_seq=1, item_kind="usage")
    assert item.view() is item.view() is push.view()
    assert loaded == [UUID(outbox.device.id)]


def test_impossible_times_are_request_invalid() -> None:
    assert moment("2026-11-22T10:00:00.000Z") == datetime(2026, 11, 22, 10, tzinfo=UTC)
    with pytest.raises(HubError) as caught:
        moment("2026-02-30T10:00:00.000Z")
    assert caught.value.code == "request_invalid"


def test_the_pacer_keeps_a_reserve_per_device_and_forgets_old_devices() -> None:
    clock = FixedClock()
    pacer = Pacer(clock, max_devices=2)
    first, second, third = UUID(new_id()), UUID(new_id()), UUID(new_id())
    assert all(pacer.admit(first) is None for _ in range(BURST))
    assert pacer.admit(first) == 1
    clock.advance(1.01 * MIN_INTERVAL_SECONDS)  # one push's worth (timestamps are floats)
    assert pacer.admit(first) is None
    assert pacer.admit(second) is None
    assert pacer.admit(third) is None  # drops the least recently seen device (first) with its spent reserve
    assert all(pacer.admit(first) is None for _ in range(BURST))
    limiter = RateLimiter(clock)
    assert pacer_of(limiter, clock) is pacer_of(limiter, clock)
    assert pacer_of(RateLimiter(clock), clock) is not pacer_of(limiter, clock)


def test_old_receipts_are_pruned_except_the_newest_of_each_device(outbox: Outbox) -> None:
    _task, session = outbox.session()
    outbox.ok(outbox.usage(1, session), outbox.usage(2, session))
    outbox.ok(outbox.vm(1, new_vm()), device=outbox.scene.other_device)
    hub = outbox.hub
    context = SimpleNamespace(clock=lambda: hub.clock() + timedelta(days=7, seconds=1), pool=hub.pool)
    assert jobs.prune_receipts(context) == 1
    assert [row[0] for row in outbox.receipts()] == [2]
    assert [row[0] for row in outbox.receipts(outbox.scene.other_device)] == [1]
    assert jobs.prune_receipts(SimpleNamespace(clock=hub.clock, pool=hub.pool)) == 0
    assert [job.name for job in jobs.JOBS] == ["outbox.prune_receipts"]
    # a resend of a pruned origin_seq is a replay (ADR-030)
    assert outbox.statuses(outbox.usage(1, session, requests=5)) == [(1, "replayed", None)]


def test_a_failing_effect_after_the_commit_is_logged_and_the_push_stays_stored(
    outbox: Outbox, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_item: ItemContext, _body: object) -> None:
        def fail() -> None:
            raise RuntimeError("fan-out down")

        _item.after.append(fail)

    chains.configure([*BASES, ("alpha", {"vm_state": broken})])
    vm = new_vm()
    with caplog.at_level("ERROR", logger="agent_hub.outbox"):
        assert outbox.statuses(outbox.vm(1, vm)) == [(1, "ok", None)]
    assert "an outbox effect could not be sent" in caplog.text
    assert outbox.desk.rows("SELECT count(*) FROM hub.vm WHERE id = %s", vm) == [(1,)]
