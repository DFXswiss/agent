"""Outbox item `session_transition` (03-api.md section 6.1): session row, transition, task derivation and frames.

The step itself is `agent_hub.sessions.transitions.apply` (HUB-09) with the device as reporter: it checks
`device:session` (`404 not_found` for an unknown session, `403 forbidden` for a session of another device), the reason
of the state and the state machine (`400 request_invalid` for a refused step, which the push acknowledges as
`rejected`); the current state again changes nothing. `at` is the device clock, the receive time is the push's.
A transition that names its VM (`vm_id`, ADR-060: a workgroup may have several VMs on a device) sets `session.vm_id`
unless that VM is known as another device's or another workgroup's; otherwise, on a device without the feature
`vm_choice` (one VM per workgroup), an open session without a VM takes the active VM of its device and workgroup
(`handlers_vm.link_session_vm`).
"""

from __future__ import annotations

from agent_hub.outbox.base import ItemContext, moment
from agent_hub.outbox.bodies import SessionTransitionBody
from agent_hub.outbox.handlers_vm import chooses_vms, link_session_vm
from agent_hub.sessions.transitions import apply


def handle(item: ItemContext, body: SessionTransitionBody) -> None:
    """Apply the reported transition and keep its frames and signals for after the commit."""
    applied = apply(
        item.connection,
        body.session_id,
        body.state,
        body.reason,
        moment(body.at),
        received_at=item.now,
        reported_by=item.device,
        agent_version=body.agent_version,
        model_access=body.model_access,
        model_id=body.model_id,
    )
    if body.vm_id is not None:
        item.connection.execute(
            "UPDATE hub.session s SET vm_id = %s WHERE s.id = %s AND s.device_id = %s AND NOT EXISTS (SELECT 1"
            " FROM hub.vm v WHERE v.id = %s AND (v.device_id <> s.device_id OR v.wg_id IS DISTINCT FROM s.wg_id))",
            (body.vm_id, body.session_id, item.device, body.vm_id),
        )
    elif not chooses_vms(item.connection, item.device):
        link_session_vm(item, body.session_id)
    item.effects.extend(applied.effects)
