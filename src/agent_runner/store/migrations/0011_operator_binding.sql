-- Operator subscriptions (KOR-23, ADR-052; 05-runner.md section 10.1). A session of an approved organization that
-- uses the operator's pool is bound to one token file of `AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR`, never to a token
-- value: the gateway is provisioned from the current content of that file whenever it changes. The binding survives a
-- restart of the daemon (the guest keeps running and keeps its gateway). Only the file name is stored, never a token.
CREATE TABLE runner.operator_binding (
    session_id uuid PRIMARY KEY,
    file_name text NOT NULL CHECK (file_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    bound_at timestamptz NOT NULL DEFAULT now()
);
