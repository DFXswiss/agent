-- The last audited list of OPERATOR_GITHUB_USERS (KOR-47, ADR-076 section 7; `github.operator_audit`): one row per
-- listed user, holding only HMAC-SHA256(storage key, user ID) in hex, never the ID. The runner compares the configured
-- list with these rows at each start and audits a change once (`runner.operator_github_users {count, added, removed}`).
CREATE TABLE runner.operator_github_user (
    user_hmac text PRIMARY KEY CHECK (user_hmac ~ '^[0-9a-f]{64}$')
);
