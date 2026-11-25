# KOR-69 decisions (every Claude subscription logged in centrally)

Design: [ADR-090](../adr/090-every-subscription-logged-in-centrally.md); rules [E-30](../KONZEPT.md#e-30),
[E-31](../KONZEPT.md#e-31), [E-07](../KONZEPT.md#e-07), [E-04](../KONZEPT.md#e-04), [E-25](../KONZEPT.md#e-25).

1. **E-30 is entered here too.** The rule numbers of KONZEPT part B must stay gapless; KOR-68 (which brings E-30)
   runs in parallel. E-30 carries the wording KOR-68's work package prescribes and the state "offen – KOR-68"; the
   package that merges second keeps one entry.
2. **The primary record keeps its old form.** The deployed web client opens the primary `claude_subscription` record,
   refuses any other body and would then block a rotation of the user's `CK` (design review, finding 1). Name, note,
   use and "not logged in" therefore exist only on further subscriptions; the primary one is shown as
   `own sessions` and keeps serving the owner's own sessions as before.
3. **No endpoint moves the primary mark.** A hub-held mark would let a hub make a measure-only subscription serve the
   owner's own sessions (design review, finding 3); the GitHub pattern of ADR-078 has such an endpoint, this one
   does not.
4. **A runner feature `subscriptions`.** An older runner answers a `claude.login` with unknown fields
   `upstream_error`; clients send the new fields only to a device that announced the feature (design review,
   finding 2). `GET /v1/me/devices` already lists the features.
5. **Re-sealing by the measuring device.** The web client rotates `CK` without knowing further subscriptions; they
   would stay under the old epoch and become unreadable for devices that join later (design review, finding 6). The
   device that measures an owner re-seals them under the newest epoch it holds; the hub accepts such a write without
   a request only for a non-primary subscription whose stored epoch is older than the current one.
6. **Editing name, note and use opens the record in the owner's client.** The token stays in memory there for the
   moment of re-sealing, as for every credential the web client and the agent tool open today; it is never stored,
   shown or sent anywhere else. A separate metadata record or an edit through a device request (design review,
   finding 4) would add a credential kind or a request kind and keep the overview dependent on an online device;
   "nie auf einen Rechner des Nutzers" of the work package is read as: the login, the storage and the renewal never
   happen on a user's computer.
7. **The use mirror is jsonb.** The hub's export file has no form for `uuid[]` (`backup.tables`); the organizations
   are a JSON array, queried with `?|`.
8. **The operator pool for shared subscriptions.** `use.pool` counts only for an owner with the platform role
   `operator` in the verified platform log and only for organizations the log grants (no fallback to
   `OPERATOR_SUBSCRIPTION_ORGS`): for anybody else the root's list is not the owner's own switch.
9. **Limits.** At most 40 subscriptions per user (the size of KOR-68's reported list) and 50 organizations per use;
   both are listed as `open` in `fixed_limits.json` like the 20 GitHub records (to become settings, A14).
10. **Rollback by the hub stays possible** for a device that never saw the newer version of a record (it acts on the
    older `use`); a device never goes back to an older version. Bounding this further would be a new protection layer
    (E-25).
11. **The web view waits for KOR-68** (instruction of the orchestrator): no change of the settings page; the served
    copy of the agent guide (`web/public/agents.md`) is the only file under `web/`.
12. **Findings of the independent code review**, all fixed: shared subscriptions are listed and re-read on every
    `credentials_changed` and every `credentials` hint (`sync_owner`), so a narrowed use or a new login is not missed
    (high); a further subscription's body served through the legacy endpoint does not serve the owner's own sessions
    (`provision.owner_secret` refuses a body with `name`, `note` or `use`); shared entries are read for their quota
    and exhaustion like the files (`exhaustion.token_of`, `quota.operator`); a failed re-seal waits `POLL_S`; a token
    file named `sub-...` is no part of the pool; 04-crypto section 7 names the use mirror; ADR-090 section 4 no longer
    promises the mirror in the list.
13. **The web dialogs** (second merge): a settings section of their own ("Claude subscriptions") and the two entry
    points of KOR-68's page; no device choice (the first online device that announced `subscriptions`), the relay of
    the credentials section reused. Findings of the review of the dialogs, fixed: one login per attempt (a re-render
    started a second one), the mailbox option, removing a record that does not open, left organizations and the pool
    stay switchable, a hub answer without the primary mark is refused like the primary record. Kept: the dialog inside
    the table cell of the page (KOR-68's layout), no busy state on log out and remove (the hub answers an idempotent
    repeat), the primary record is logged in through the credentials section as before.

