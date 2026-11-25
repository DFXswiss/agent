-- Instructions of a child session (KOR-45, ADR-088 section 1; `subagents/instructions.py`).
--
-- One row per instruction of a child: its `subagent.request` (the first instruction, handed over with the start) and
-- every `subagent.instruct`; `operation_id` is the instruction ID and the guest's `prompt_id`. No text: the prompt is
-- the operation's parameter. `actor_id` is the acting user of the operation (ADR-070), who becomes the acting user of
-- the child's turn when the child accepts the prompt. Instructions of one child are handed over one at a time in the
-- order of `created_at` (the receipt of the operation): `delivered_at` once the guest registered the prompt,
-- `accepted_at` with the guest's `prompt_accepted`, `ended_at` with the `idle` that ends that turn. A row that was
-- never delivered and whose operation is no longer `executing` (a failed instruction) is no instruction any more.
CREATE TABLE runner.subagent_instruction (
    operation_id uuid PRIMARY KEY,
    child_session_id uuid NOT NULL,
    parent_session_id uuid NOT NULL,
    actor_id uuid,
    created_at timestamptz NOT NULL,
    delivered_at timestamptz,
    accepted_at timestamptz,
    ended_at timestamptz
);
CREATE INDEX subagent_instruction_child ON runner.subagent_instruction (child_session_id, created_at, operation_id);
