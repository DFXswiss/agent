"""U-RUN-inbox / reconcile: shape checks of the hub answers (`agent_runner.reconcile.wire`)."""

from __future__ import annotations

import pytest

from agent_runner.crypto.log.entries import Head
from agent_runner.reconcile import wire

from .rig import pid

HASH = "A" * 43


def test_valid_shapes() -> None:
    org = pid("o")
    assert wire.device_state({"state": "active"}) == "active"
    assert wire.desired_orgs({"orgs": [{"org_id": org, "log_head": {"seq": 3, "hash": HASH}}]}) == [
        (org, Head(3, HASH))
    ]
    entry = {"body": {"seq": 4}, "sig": "s"}
    page = wire.log_page({"entries": [{"seq": 4, "entry": entry}], "head": {"seq": 9, "hash": HASH}, "next_after": 4})
    assert page == ([entry], Head(9, HASH), 4)
    assert wire.log_page({"entries": [], "head": {"seq": 0, "hash": HASH}, "next_after": None})[2] is None
    assert wire.last_origin_seq({"last_origin_seq": 0}) == 0
    assert wire.uuid_text(org, "x") == org


@pytest.mark.parametrize(
    "call",
    [
        lambda: wire.device_state({"state": "decertified"}),
        lambda: wire.desired_orgs({"orgs": None}),
        lambda: wire.desired_orgs({"orgs": [[]]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": pid("o")}]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": "x", "log_head": {"seq": 0, "hash": HASH}}]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": pid("o").upper(), "log_head": {"seq": 0, "hash": HASH}}]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": pid("o"), "log_head": {"seq": 0, "hash": "short"}}]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": pid("o"), "log_head": {"seq": True, "hash": HASH}}]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": pid("o"), "log_head": {"seq": -1, "hash": HASH}}]}),
        lambda: wire.desired_orgs({"orgs": [{"org_id": 7, "log_head": {"seq": 0, "hash": HASH}}]}),
        lambda: wire.log_page({"entries": [{"seq": 1, "entry": {"body": {"seq": 2}}}], "head": None}),
        lambda: wire.log_page({"entries": [{"seq": 1, "entry": "x"}]}),
        lambda: wire.log_page({"entries": [{"seq": 1, "entry": {"body": {"seq": 1}}, "x": 1}]}),
        lambda: wire.log_page({"entries": [], "head": {"seq": 0, "hash": HASH}, "next_after": "1"}),
        lambda: wire.last_origin_seq({}),
    ],
)
def test_invalid_shapes_raise(call: object) -> None:
    with pytest.raises(wire.ResponseInvalid):
        call()  # type: ignore[operator]
