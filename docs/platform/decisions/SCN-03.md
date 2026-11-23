# SCN-03 – decisions (M2a scenarios: E2E-02/03/04/05/18/30/31/37/38/39)

1. **Expiry without a fake clock** (E2E-03): the hub has no clock hook (`agent_hub.clock` is injected in unit tests
   only; adding a test clock to the hub process would be a product change outside this package). The expiry case uses
   an invitation valid for 5 s, built through the API exactly as the members screen builds it (the screen offers hours
   only; `fixtures/identity.ts` `inviteByApi`), and waits for the real time; the package deletion is the hub's expiry
   job (every 60 s), polled for up to 120 s. `POST /clock/advance` stays `501 not_available`.
2. **Further runner instances** (`routes/runner_instances.py`): `POST /runners/runner-<n>/spawn` starts a runner with
   its own home `<runner scratch>/runner-<n>` and VM prefix `<run prefix>r<n>-` (covered by the run's VM cleanup),
   otherwise `runner-1`'s environment; it adopts the run's golden. `memory_reserve_mib` changes its capacity (E2E-38,
   E2E-05), `pool_size` its warm pool. `POST …/remove` stops it, deletes its VMs and home and forgets it, so a device
   that was unpaired or decertified does not fail the run's "runners still running" check. `POST …/cli` runs only
   `doctor` and `status [--json]`. The run's own runners (1..`E2E_RUNNERS`) cannot be spawned or removed.
3. **Which runner is which device**: `runner-1` stays the run's shared platform device (`platformDevice()`), never
   decertified or unpaired. E2E-04 (org device, unpaired), E2E-05 (platform device, decertified) and E2E-38 (a second
   platform device next to `runner-1`) use spawned instances (`runner-<n>`, n above every runner of the run) and remove
   them at the end; E2E-03 and E2E-04 use one more spawned instance for the negative `enroll` cases (an enrolled runner
   refuses a second enrollment with `already_enrolled` before it would look the code up). An instance that enrolls as
   an org device gets no platform OpenRouter key file (the runner refuses it on org devices).
4. **Auto-assignment in E2E-05**: the hub orders platform candidates online first, then by free VM slots (03-api.md
   section 4.5). The new platform device gets `AGENT_RUNNER_MEMORY_RESERVE_MIB=0`, so it has more free slots than
   `runner-1`, and the spec waits until the hub's capacity rows say so before the first prompt – deterministic without
   stopping `runner-1`.
5. **Observing `rotation_pending`** (E2E-05): the admin's browser performs the decertification batch by itself as soon
   as it learns of it. The spec cuts the browser link (`browser-hub`) before the operator decertifies, asserts the
   hub's refusal (`decertified`, offline) and `org.rotation_pending`, restores the link and reloads; the browser then
   sends `device_unassign` + the rotations.
6. **Forged author events** (E2E-18): the hub refuses an event without the author's right (`author_not_writer`, E5),
   so a forged event can only reach a reader through a tampered hub. `routes/hub_tamper.py` (`POST
   /hub-db/forge-event`) stores the member's refused, correctly signed whiteboard entry directly in `hub.event` (as the
   database superuser, next `scope_seq`, no audit row); the real hub then serves it signed. Response signatures stay
   intact, so the test proves the reader's own check (`whiteboard.item` `data-type=unverifiable`,
   `data-code=author_not_writer`, the text never shown) in the admin's and the member's browser.
7. **Refusal codes** (E2E-18): forbidden *writes* are `403 forbidden` (creating an org invitation, `POST /v1/tasks`
   without `wg:write`); a log entry without the right is the log rule's `400 log_rights`, an event without the right
   `400 author_not_writer` (03-api.md section 11). Forbidden *reads* are `404 not_found` – the hub's rights rule
   (`orgs/rights.py` `require`: reads and unknown scopes `not_found`, writes on a known scope `forbidden`) gives no
   existence oracle; 07's row speaks of forbidden writes. Each check pins one code. The invitation request carries a
   well-formed sealed package (the hub validates the body before the right).
8. **Anonymous rate limit** (03-api.md section 1, ACC-M1): every browser and runner of a T1 run reaches the hub from the
   loopback, the hub allows 30 anonymous requests per minute and source. The identity specs open invitation links,
   sign in, register and recover many times; `fixtures/identity.ts` keeps a mirror of the bucket (capacity 30, refill
   0.5/s, reserve 10 for requests it does not see) and waits before each step whose anonymous requests would empty it
   (`anonymousBudget`, `openInvite`, `signIn`, `visit`, `enrollAttempt`). Specs therefore run up to 600 s.
9. **Passkeys in E2E-30**: Chromium's virtual authenticators ignore the attachment hint of "on this device"; the PRF-less
   passkey is therefore added while it is the only authenticator present (the session of the page stays valid).
10. **E2E-39 `invite_registered`**: the org-invitation screen learns of a binding only when the join is sent (the
    lookup of an org invitation does not end the existing-account path); the spec signs in, joins and expects the
    `invalid` state with `invite_registered`, and the hub's `409 invite_registered` for the direct accept.
11. **Scheduler paths in E2E-38**: (3) free slots (3000 ms with a ready pool VM), (1) the running VM (2500 ms), (2) the
    stopped VM (6500 ms; the second device runs with session and VM idle intervals of 60 s, so the workgroup's VM stops
    after about two minutes) and the restriction to assigned devices (3000 ms after `poolReady`) are asserted exactly.
    The second ordering criterion (free VM memory when the free slots are equal) and the last one (smallest device ID)
    cannot be set up deterministically on one runner device without equal slot counts; they stay with the hub's
    scheduler unit tests (U-HUB-scheduler). Capacities differ through `AGENT_RUNNER_MEMORY_RESERVE_MIB=0`.
12. **Egress attribution**: the sampler attributed every `limactl` process with `cache-golden-` in its command line to
    the run, so other packages' golden builds on the same runner device failed unrelated runs. A fix (only the run's
    own cache name) was prepared; another package merged an equivalent one first (sampling only the run's own detached
    processes), so this package's commit was dropped at the rebase.
13. **E2E-11** (ACC-M1 hint): the scenario now checks the hub's `session.vm_id` against the workgroup's VM row; the
    runner store only maps the VM ID to the Lima instance the session's tmux server runs in.
14. **Closing checks per spec** (`routes/spec_checks.py`): each spec asserts at its end the egress violations recorded
    since the previous spec's checks (`GET /checks/egress`), `agent-hub audit verify` (`POST /checks/audit`) and every
    double of the run (no failures; `ok` with a loaded model scenario, always for GitHub/mail doubles); the run repeats
    them per iteration.
15. **Org devices have no model access without the owner's credential** (E2E-04): an org device never holds the
    platform OpenRouter key (05-runner.md section 10.1, `config_invalid` otherwise); a task on it is scheduled there, its
    VM of the workgroup is assigned and started, and the session then ends `failed` with `model_access_unavailable`.
    E2E-04 asserts exactly that; tasks that complete on org devices need deposited credentials (M2b: E2E-27/28/36).
16. **Device refusal evidence**: `agent-runner doctor` makes one signed `GET /v1/device/state` and prints the hub's
    answer; after unpairing (E2E-04) and decertification (E2E-05) it shows `detail=key_revoked` (unpairing also allows
    `forbidden`), before it `result=ok … value=active`. The runner's own teardown is asserted through `agent-runner
    status` (`revoked=key_revoked`) and its store (no workgroup VM, no keys).
17. **Proof of the same identity** (E2E-30): after every sign-in (second passkey, PRF-less passkey with the recovery
    code, recovered passkey) the app itself must decrypt a whiteboard entry written before; the comparison of the
    recovery slot's identity is additional.
18. **"Cannot decrypt"** (E2E-31, `openScopeEvents`): an event whose epoch key a principal does not hold is tried with
    every key of the scope it holds and counts as locked only if none opens it.
19. **R-11.3 on E2E-18**: `traceability.json` (ACC-owned) maps R-11.3 to E2E-22 and E2E-18; the tag follows it. E2E-18
    proves the hub's permission checks of browser requests and events; the operation checks H2–H4 of 03-api.md section
    8.3 are proven by E2E-22 (M2b, operations).
20. **Shared steps** `fixtures/identity.ts` is a new file of this package (the steps of the identity scenarios);
    later scenario packages may import it.
21. **Accepted review points**: E2E-05 performs the automatic batch after a reload (the hint arrives while the
    browser is cut off; pending rotations are performed at the next load, 06-web.md); in E2E-02 the invited org is the
    invitee's only org, so "default context" is the landing org after the join and after the next sign-in; the second
    platform device of E2E-38 stays certified (offline) until the iteration ends (the hub database is per iteration).
22. **Signed `key_revoked`** (component fix in the hub, `auth/keys.py`, `auth/gate.py`, `device/keys.py`,
    `http/pipeline.py`, test in `tests/platform/hub/device/test_keys.py`): the device resolver refused a decertified or
    removed device before the signature was checked, so the refusal went out unsigned – against 03-api.md section 1
    (every response to a signed request is signed), and the runner, which trusts only verified refusals, could never
    confirm `key_revoked` and tear down (RUN-06), so an unpaired org device kept its workgroup VM (E2E-04). The
    resolver now returns the revoked key with `refusal="key_revoked"`; the gate verifies the request's signature with
    it, then refuses with a signed answer – only the key's holder gets a signed refusal (a wrong key still gets the
    unsigned `signature_invalid`), and such a verified refusal does not count against the source's failed-signature
    limit. Browser session keys keep their unsigned refusals (browsers SHOULD verify only).
23. **Model double `GET /v1/key`**: the scenarios found that the platform spend poller's request was an unhandled
    route of the model double (a failure in whatever scenario was loaded). A fix was prepared in the agent repository;
    meanwhile another package merged an equivalent one (`198f1ca6`, journaled, not counted, not a failure), so this
    package's agent change was dropped and the agent repository is unchanged by SCN-03.
24. **Second review** (after decision 22): medium – a verified `key_revoked` bypassed the per-key limit (fixed: it counts
    against the key's own limit, test with a lowered limit); medium – E2E-38 showed the running VM only on the device
    that also had the most capacity (fixed: the platform-only workgroup gets the other device assigned too and its
    running VM on the device with fewer free slots still wins, 2500 ms). Low points fixed: signed refusals for removed,
    disabled and org-left devices tested, positive checks before "no control" checks, pinned `404 not_found` reads in
    E2E-31, all E2E-38 canaries scanned. Accepted (low): browser session keys keep unsigned refusals (browsers SHOULD
    verify only); "cannot decrypt" for unpaired/decertified devices is shown by the absence of every wrap and stored key
    of the new epochs (the device holds no candidate key to try); spawned runner names are not reserved (one worker).
