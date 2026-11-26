-- A session that can never be checkpointed does not hold its VM forever, and the switch does not loop (HOT-24,
-- ADR-093 addendum HOT-24).
--
-- `context_session.checkpoint_state` gains `nothing` (the guest has no complete turn to export: `state_conflict` or
-- `context_incomplete`, and the session never had a checkpoint) and `impossible` (the guest of the session's VM
-- refused the export `not_found` - also a guest without the export route - or `size_exceeded`). `impossible_reason`
-- says why, `impossible_vm_id` on which VM, `impossible_turns` is the number of accepted prompts of the session at that
-- moment - it is not due again on that VM until it has more (it worked again).
ALTER TABLE runner.context_session DROP CONSTRAINT context_session_checkpoint_state_check;
ALTER TABLE runner.context_session ADD CONSTRAINT context_session_checkpoint_state_check
    CHECK (checkpoint_state IN ('full', 'partial', 'omitted', 'empty', 'dropped', 'nothing', 'impossible'));
ALTER TABLE runner.context_session
    ADD COLUMN impossible_reason text CHECK (impossible_reason ~ '^[a-z_]+:[a-z0-9_]{1,64}$'),
    ADD COLUMN impossible_turns integer CHECK (impossible_turns >= 0),
    ADD COLUMN impossible_vm_id text;

-- `golden_switch_hold.reported_at`: when the cause was first reported to a hub that shows it as it is (`vm_upgrade_version`
-- 2 or later); a request by hand covers a `checkpoint_impossible` hold only when it was reported before the request.
ALTER TABLE runner.golden_switch_hold ADD COLUMN reported_at timestamptz;

-- `golden_switch_attempt`: the starts of a stopped outdated VM for fresh checkpoints (`refresh`) and the failed
-- attempts of its switch (`failure`, with the hold reason), with the time of the next attempt - kept across restarts,
-- so a release does not start the same VM again (`vm.golden_switch`).
CREATE TABLE runner.golden_switch_attempt (
    vm_id text NOT NULL CHECK (vm_id ~ '^vm-[0-9a-f]{8}$'),
    kind text NOT NULL CHECK (kind IN ('refresh', 'failure')),
    count integer NOT NULL CHECK (count > 0),
    reason text NOT NULL,
    next_at timestamptz NOT NULL,
    PRIMARY KEY (vm_id, kind)
);

-- `golden_switch_request`: the VMs a member asked to switch by hand that the runner already took over (the desired
-- state's `vm_switch`); a request that is new resets the attempts of its VM, one that is still listed after a restart
-- does not.
CREATE TABLE runner.golden_switch_request (
    vm_id text PRIMARY KEY CHECK (vm_id ~ '^vm-[0-9a-f]{8}$'),
    requested_at timestamptz NOT NULL
);
