-- GitHub tokens this device minted through the device flow (KOR-40, "GitHub trennen"): only the SHA-256 of the token
-- and its owner, never the token. When the owner disconnects GitHub, only the device that minted the token revokes it
-- at GitHub (`github.revoke`); the operator's token and transferred tokens are never in this table.
CREATE TABLE runner.github_minted (
    owner_id uuid NOT NULL,
    token_sha256 text NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    minted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, token_sha256)
);
