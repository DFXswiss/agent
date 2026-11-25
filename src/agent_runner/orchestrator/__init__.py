"""The main orchestrator in a session of this device (KOR-57; ADR-082 sections 6 and 9.6-9.8; 05-runner.md section 17).

- `reconcile.py`: the reconcile step `orchestrator` works off the hub's list `orchestrators` of `GET /v1/device/state`
  (`work.py`): draws the session's keys in its VM, registers them signed by the device, checks the user's signed
  handover under the task owner's key from the verified log and passes it into the VM unopened.
- `hints.py`: the hint `session.orchestrator` wakes the reconcile run.
- `journal.py`: the guest's `hub.relay` records, forwarded unchanged to this device's hub for an orchestrator session.
- `store.py`: the rows of `runner.orchestrator`; `env.py`: the area's collaborators.
"""
