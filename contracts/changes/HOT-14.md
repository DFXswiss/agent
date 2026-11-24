# HOT-14 – contract changes

ADR-027 section 3. Additive only: every message that was valid stays valid.

## `events/session.status`: `for_user` of the `waiting` variant

Optional `for_user` (UUID): the acting user of the held operation – the person whose instruction requested it and
whose own GitHub connection it will use (ADR-068). A personal connection must never be used on behalf of another
person, so only this person is asked to connect; everybody else sees whom the card waits for. Runners of this change
always write it; a body without it (an older runner) is addressed to the task owner as before. Fixtures:
`valid/variant-4.json`, `invalid/waiting-for-user-invalid.json`.
