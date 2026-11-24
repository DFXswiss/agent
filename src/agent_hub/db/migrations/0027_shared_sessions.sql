-- 0027_shared_sessions: who triggered a session and a charge, the consumption per person and the withdrawal of a
-- waiting prompt (KOR-46, ADR-083 sections 2 and 3; 02-data-model.md sections 2.4 and 2.8).
--
-- `triggered_by` names the person whose instruction a session or a consumption item follows from (the operation's
-- column came with 0022, KOR-47): the author of the prompt a main session was created for or the triggering person of
-- the `subagent.request` of a child session (`hub.session`), the acting user the runner stamped on a consumption item
-- (`hub.charge`, only when that person wrote a prompt of the task); NULL when unknown, when the turn had several
-- authors or none, and for every row written before (an older runner sends nothing). The user columns carry no
-- foreign key (like `hub.audit`): an attribution outlives a removed member.
-- `charge_person_daily` sums the charges per org, UTC day of the measurement, person and kind in the transaction of
-- the booking, like `charge_daily` per workgroup and device. A primary key cannot hold NULL, so unattributed
-- consumption is the row of the nil UUID `00000000-0000-0000-0000-000000000000` (no user has it: user IDs are
-- UUIDv7); the API answers it as `user_id` null. Charges stay booked to the organization (ADR-083 section 2).
-- `prompt_withdrawal` is the request of a prompt's author to take back a prompt that was not delivered yet: the hub
-- sets `withdrawn_at` at once for a session without a device, else when the device reports `session.prompt_withdrawn`
-- (ADR-083 section 3); a withdrawn prompt leaves the routing of the task's prompts. The notification kind
-- `prompt.waiting_for_device` tells a prompt's author that it waits for a device (added NOT VALID, as in 0013, 0021
-- and 0025, keeping every kind of 0025).

ALTER TABLE hub.session ADD COLUMN triggered_by uuid NULL;
ALTER TABLE hub.charge ADD COLUMN triggered_by uuid NULL;

CREATE TABLE hub.charge_person_daily (
  org_id           uuid   NOT NULL,
  day              date   NOT NULL,
  user_id          uuid   NOT NULL,
  kind             text   NOT NULL CHECK (kind IN ('tokens', 'runtime')),
  amount_micro_usd bigint NOT NULL DEFAULT 0 CHECK (amount_micro_usd >= 0),
  quantity         bigint NOT NULL DEFAULT 0 CHECK (quantity >= 0),
  PRIMARY KEY (org_id, day, user_id, kind)
);

CREATE TABLE hub.prompt_withdrawal (
  event_id     uuid        PRIMARY KEY,
  task_id      uuid        NOT NULL REFERENCES hub.task (id) ON DELETE CASCADE,
  requested_by uuid        NOT NULL,
  requested_at timestamptz NOT NULL,
  withdrawn_at timestamptz NULL CHECK (withdrawn_at >= requested_at)
);
CREATE INDEX prompt_withdrawal_task ON hub.prompt_withdrawal (task_id, requested_at DESC, event_id DESC);

ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'task.attention', 'invite.accepted', 'rotation.required',
  'agent.account_change', 'org.admitted', 'operator.request', 'device.needed', 'model.switched',
  'prompt.waiting_for_device'))
  NOT VALID;

REVOKE ALL ON hub.charge_person_daily, hub.prompt_withdrawal FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.charge_person_daily, hub.prompt_withdrawal TO {writer};
