-- Idle work VMs give their slot back (ADR-059 addendum 4; docs/platform/spec/05-runner.md section 3.7,
-- 13-work-vm.md section 7).
--
-- A slot is held only while its work VM needs it: a `stopped` VM keeps its disk in the slot's Lima home but holds
-- neither the slot nor RAM, so another VM may run in that slot meanwhile; it starts again through the normal slot
-- assignment when its slot is free. "One live VM per slot" becomes "one VM per slot that is not stopped".
DROP INDEX runner.workvm_slot_live;
CREATE UNIQUE INDEX workvm_slot_held ON runner.workvm (slot) WHERE state NOT IN ('deleted', 'stopped');

-- `workvm_wait`: the requests that wait for a slot now, in the order they came in (`seq`), so `agent-runner status`
-- shows them; `slot` is the slot a stopped VM waits for (its own), null for a new VM. The daemon's queue is the source;
-- the rows mirror it and are cleared at every start of the daemon (a waiting request does not survive a restart).
CREATE TABLE runner.workvm_wait (
    ticket text PRIMARY KEY CHECK (ticket ~ '^[0-9a-f]{16}$'),
    seq bigserial NOT NULL UNIQUE,
    slot integer CHECK (slot >= 0 AND slot < 100),
    since timestamptz NOT NULL
);
