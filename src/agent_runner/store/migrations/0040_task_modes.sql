-- Working modes of the device's tasks and the state of their drivers (KOR-75, ADR-098 sections 3 and 4;
-- `modes/state.py`, `modes/drivers.py`).
--
-- `task_mode` keeps the last section `task_modes` of the desired state: the modes that are on for each task with a
-- session on this device, with the driver state the hub has (the last one any device reported). A restarted daemon
-- that cannot reach the hub keeps its modes; a mode the hub no longer lists is off.
-- `mode_driver` is the driver state on this device: the current step, counters and the time of the next step (each
-- change is reported to the hub right after it is stored). A row of a task whose mode is off is deleted at the next
-- run (the driver stopped at its step boundary). Metadata only: content travels in sealed events.
-- `mode_rate` is the rate budget of the drivers per GitHub identity (`modes.drivers.Budget`): requests in the current
-- window, shared by every driver of the device.
CREATE TABLE runner.task_mode (
    task_id uuid NOT NULL,
    mode text NOT NULL,
    version integer NOT NULL,
    chosen_by uuid NOT NULL,
    way text NOT NULL,
    changed_at timestamptz NOT NULL,
    driver jsonb,
    PRIMARY KEY (task_id, mode)
);
CREATE TABLE runner.mode_driver (
    task_id uuid NOT NULL,
    mode text NOT NULL,
    step text NOT NULL,
    counters jsonb NOT NULL,
    next_at timestamptz,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (task_id, mode)
);
CREATE TABLE runner.mode_rate (
    identity text PRIMARY KEY,
    window_start timestamptz NOT NULL,
    used integer NOT NULL CHECK (used >= 0)
);
