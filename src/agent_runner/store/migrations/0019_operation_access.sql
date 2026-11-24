-- Held for repository access (KOR-65, rule E-29): a `github.*` operation whose pre-check finds that the acting user's
-- GitHub connection does not reach the repository (`repo_not_accessible`: not seen, an organisation's restriction of
-- OAuth Apps or classic tokens, SAML single sign-on) is held like one without a connection, with the waiting reason
-- `github_access`. `held_next` is the one next step of its `session.status` event (`{action, owner, url?}`,
-- `contracts/events/objects.json` `github_next_step`); a changed step is announced again.
ALTER TABLE runner.operation DROP CONSTRAINT operation_held_reason_check;
ALTER TABLE runner.operation
    ADD CONSTRAINT operation_held_reason_check CHECK (held_reason IN ('github_connection', 'github_access')),
    ADD COLUMN held_next jsonb;
