# HOT-14 – decisions

A personal connection must never be used on behalf of another person (KONZEPT A15, Idee 36, E-19; ADR-068).

1. **Who instructs a turn.** The verified author of the accepted prompt (`event.author` of the signed `prompt`
   event, stored as `runner.prompt_event.author_id`), taken over by the guest's `session.state prompt_accepted` in
   the journal's order. A session's claim is never used. The author is looked up only through a delivery row of the
   same session, so a prompt ID of another session or of no stored prompt (also one that is no UUID) counts as an
   unknown author.
2. **Several people in one turn.** A second accepted prompt of another or an unknown author in a turn that has not
   reached `idle` leaves the turn without acting user. The next turn starts fresh.
3. **After `idle`.** The last acting user stays for records the guest journals after the turn's end (a late
   operation request of that turn); a turn only starts with an accepted prompt (pings never start one).
4. **Stamp at receipt.** The acting user is stored with the operation when its request is journaled; a held
   operation keeps it. Executing with "whoever instructs now" would hand a held operation to the next writer.
5. **No acting user: rejected, not held.** The package asks to hold; a hold could only ever end at its timeout
   (the stamp never changes, nobody can be asked), so the operation is rejected at once with `github_not_connected`
   and a fixed text (`acting.NO_ACTOR`). No connection is used either way.
6. **Children.** A child session takes the acting user of its `subagent.request` (`operation.child_session_id`,
   indexed by migration 0016), never its own initial prompt (whose ID is the operation ID).
7. **Inventory of credential kinds.** `github` is the only personal connection a session's operations use
   (`acting.PERSONAL_KINDS`). Model access (`claude_subscription`, API keys) is the session's model access chosen at
   its start, its consumption carried by the organisation (A15); following the instructing person for it is part of
   KOR-45/KOR-46. `mailbox` and the login kinds serve only the verified user of that user's own device request.
   The harness line keeps naming the task owner's GitHub login at the start (a fact, no token used); the structural
   test allows exactly that reader.
8. **Audit.** The `operation.result` event (signed by the device, encrypted for the workgroup) names the user whose
   connection an executed operation used (`connection_of`, optional, additive). The hub's plaintext audit is not
   changed (no hub release needed); the task's record is the trustworthy place.
9. **Waiting card.** `for_user` is added to the `waiting` body (additive); the web folds cards per reason and
   addressee and offers the connect controls only to the addressee. A body without it (older runner) keeps the old
   meaning, the task owner.
10. **Operator's GitHub.** More than one user is `config_invalid` with the name
    `OPERATOR_GITHUB_USERS:one_user_only`, so the CLI and doctor say why (the closed error format carries no
    text). Credentials stored for other users before stay until the token is revoked at GitHub; the runbook says so.
11. **"Connected as …" (goal 3).** Checked, nothing built: the operator path answers `{step:"done", login}` with the
    login GitHub returns for the token (`GET /user`), stored in the credential; settings and card show it
    ("Connected as {login}"); `tests/platform/runner/github/test_operator_github.py` covers it.
12. **E2E.** The scenario of the package (A owns, B without GitHub writes, B connects, A writes again) runs in
    `tests/platform/runner/github/test_acting_user.py` against the GitHub double with the real pipeline, store,
    hub double and credential records, tokens told apart per request. A browser E2E with two signed-in people and a
    running VM needs a second user in the E2E fixtures that no scenario provides yet; it is left to KOR-46.
