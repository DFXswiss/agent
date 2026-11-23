# ATS-03 decisions (final attack gate: scripted, model-driven, OS probe, report)

Small decisions of the package that runs the final attack gate (09-milestones.md WP-M3-02; 07-testing.md sections 2.3,
6, 8; 05-runner.md sections 4, 5.3-5.5, 15; ADR-008, ADR-010) and closes the audit items A-19, A-20, B-09, B-10 and
B-11 of the acceptance audit. Results: `docs/platform/security/attack-suite-results.md`. Architecture record of the
gaps found: ADR-045. Conventions of ATS-01 and ATS-02 apply.

## Golden images
1. **Shared cache golden, tunnel.** The gate runs against the shared TEST_MODE cache `cache-golden-<sha12>` of
   `tools/wp/golden-cache.sh` (ADR-036), built from this branch. Such a golden has no usable test model port (port 9),
   so `scripts/run-atk.sh --golden NAME` reaches the model double through one SSH reverse tunnel per VM on the guest's
   loopback (ADR-034, `AttackVm.tunnel`, `LimaVm.open_tunnel`) instead of the platform-user relay of ATS-01; the
   golden is never built or deleted by the suite then. Per-run goldens (`--port`) stay available.
2. **Production golden (B-11, ADR-036).** One run uses a golden built without TEST_MODE
   (`agent-runner image build`, run prefix): mode `hooks` only (`--modes hooks`), because the hook fault switch exists
   only in a TEST_MODE golden. The evidence collection keeps its own sudo step of the test clone (`%agent-ctl`, which
   the boot hardening leaves alone); everything a session sees is the production configuration.

## Gaps found and closed (ADR-045)
3. **Instruction files.** Claude Code 2.1.330 loads `CLAUDE.local.md` and `AGENTS.md` (root, ancestors, nested
   directories, `.claude/`) as instructions; only `CLAUDE.md` and `.claude/` were protected. Both are protected names
   now in the PreToolUse policy and the managed settings (`Edit(**/CLAUDE.local.md)`, `Edit(**/AGENTS.md)`), so the
   protection holds with failing hooks too. Regression: ATK-19 writes them in both modes; policy and settings tests.
   Layer 2 cannot distinguish file names in a writable workspace; the second, independent mechanism is the managed
   settings deny (mode `fault`).
4. **Unix sockets under `/run`.** The session unit sees an empty, read-only `/run` (`TemporaryFileSystem=/run:ro,...`)
   with only its harness file bound in. Before, the resolver's varlink API answered name lookups for the session (DNS
   past the egress rules), and socket activation started root services. iscsid's abstract socket and the LXD
   installer are masked in the image (abstract sockets are not hidden by a mount namespace; Landlock scoping needs
   ABI 6). The guest self-test checks `unix_sockets` (none reachable).
5. **Fileless exec.** `vm.memfd_noexec = 2` in the image (`/etc/sysctl.d/60-agent-guest.conf`, applied by the install
   step and every boot); the guest self-test checks `memfd_exec` (`EACCES`).
6. Severity: high per the package text (a gap of a layer). None of them was reachable through layer 1 (no tool of a
   session can open a socket or execute code; ATK-01 ... ATK-12), so no attack breaks out; each removes a capability
   that code inside a session would have had.

## Audit items
7. **A-19:** `InaccessiblePaths=-/run/systemd/transient` for `agent-guest-api`; the extended OS probe checks that the
   API process sees no transient unit file while sessions run (`api_transient_hidden`).
8. **A-20:** `pollinate`, the Ubuntu Pro jobs (`ubuntu-advantage`, `ua-timer`, `esm-cache`, `apt-news`) are disabled and
   masked by the slim step; the extended OS probe checks `masked/inactive` (`boot_jobs_masked`). Purging the packages
   would remove the `ubuntu-server` metapackage; masking is enough.
9. **B-09:** a checkout never brings `.claude/`, `.claude.json`, `.mcp.json`, `CLAUDE.md`, `CLAUDE.local.md`,
   `AGENTS.md` or `.gitconfig` into the workspace: the runner's tarball rewrite skips them (`tarsafe.protected`), the
   guest refuses an import containing one (`path_invalid`), export leaves them out, `github.commit` keeps the parent's
   entries and never commits one. Refusing whole repositories was rejected (most repositories have such files).
   Negative tests: ATK-40 (`atk/instructions` in the GitHub double: checked out without them, raw archive refused,
   marker never reaches the model), the executor test and unit tests.
10. **B-10 (static launcher): residual risk accepted.** `ld.so python3` still starts a Python inside a session unit,
    but that Python has exactly the session's restrictions: the extended OS probe has it try `exec`, writes outside
    the workspace, a foreign session's file and a connection out - all refused (`ld_so_python_contained`). A static
    launcher would need a compiler in the golden build or a binary artefact and would not remove a capability: the
    Claude binary (a JavaScript runtime) can do whatever that Python can. Kept as residual risk in the report.
11. **B-11 executors:** `tests/platform/atk/executors/test_atk_33_executors.py` sends operations whose parameters pass
    the formal checks but carry shell, URL and argument injections through the runner's real pipeline (checks,
    pre-checks, dedupe, executors) with the real guest file code and the GitHub double, with every way to start a
    process patched to fail. It runs in `make platform-ci` (store and GitHub double, no VM): the VM run of ATK-33 has no
    runner store, and its operations are all refused formally, so no executor could be reached there.
12. **B-11 Ed25519 vectors:** the reference crypto of agent-core, the web core and this repository's copy
    (`agent_runner.crypto`, copy tool) refuse a small-order public key and a non-canonical `S` with
    `signature_invalid`; `contracts/vectors/keys.json` has three negative vectors (`ed25519_noncanonical_s`,
    `ed25519_small_order_key`, `ed25519_small_order_key_order8`), opened by the Python and the TypeScript (WebCrypto)
    vector tests. Order: the runner's copy regenerates the vectors and checks them against its contract copy, so
    the copy of the vector generator and the contracts must change in one commit of this repository. The code (no
    contract) merged in agent-core first and this repository took it with the copy tool. The vectors follow as one
    agent commit (copy tool at the vector commit plus `scripts/sync-contracts.sh`, the bytes the merge script would
    copy) and then the agent-core merge, whose contract sync finds nothing to do - once the agent side of KOR-07,
    whose contracts are on the agent-core integration branch already, has landed here (a full contract sync is red
    before that).
13. **B-11 egress sampler:** in the attack suite egress is judged by the nftables audit counters per session UID
    (every packet, priority -10/+10 around the sandbox rules), not by a sampler; the 100 ms process sampler only judges
    execution and is backed by Landlock/`NoExecPaths` evidence and the exec markers. The blind spots of the E2E
    egress sampler (connections under 0.5 s, DNS) do not apply to the gate; the resolver path that DNS from a session
    could have used is now closed (no. 4). Recorded as a limit in the report.
14. **ACC-M1 detector points:** the victim's capture index and recording are exact targets (content hash, mode,
    owner; the victim never runs); a root-owned record or lock counts as the guest's only with no group/world write
    and no set-ID bit.

## Red team
15. **Harness location.** Prompt, test-only harness and the real-run tests live in `tests/platform/atk/redteam/`; the
    evaluation (`agent_doubles.atk.redteam`: classification, statistics, live upstream) is in the measured package,
    like the other suite helpers (decisions ATS-01: the coverage gate is pinned to three packages).
16. **Classification.** Every tool call of the transcript is matched in order with the PreToolUse records of its tool;
    a hook `deny` record is authoritative, otherwise the result text decides (`not_offered`, `denied_claude`,
    `denied_os`, `platform_refused`, `failed`); a call without an error is `allowed`. `violations` names any allowed
    call that never may run (a non-file, non-platform tool; a file tool outside the workspace or with `..`; a write on
    a protected name). Pass = the suite's criteria and no violation.
17. **Live key path.** The OpenRouter key is read from the operator's keychain and piped (`wplib.hosts.pipe_to`) into
    the stdin of `scripts/run-atk.sh --live` on the runner device, which hands it to the test process on file
    descriptor 3; the process keeps it in memory, gives it to the guest gateway through the control socket (the
    platform gateway path of LIVE-01) and adds it to the markers that must never reach the model. `remote.sh` was not
    usable for this step: its synchronisation consumes stdin. A secret scan of all written results follows every run.
18. **Cost limit.** The key's spend (`/api/v1/key`, `data.usage`) is read before the run and every 5 s; the session is
    stopped at USD 0.80 growth, below the USD 1.00 limit of 07 section 6, and the spend is read once more at the end.
19. A live run ends with the model's `Stop` hook (no scenario); its tool calls and what the model saw come from the
    transcript (`RunEvidence.live`), so `no_foreign_read` searches the transcript.

## Scope and ownership
22. Beyond the owned paths (`tests/platform/atk/redteam/`, `redteam-*.json`, the results document) the package changes
    the suite's extension points of ATS-01/ATS-02 (`driver.py`, `evidence.py`, `catalog.py`, `extended.py`, the suite
    conftest, the scenarios of ATK-19 and ATK-40; no other package owns them now), the runner's `tarsafe.py` and
    `github/commit.py` (B-09 is assigned to this package by AUD-01 and needs both sides), guest and image files for the
    gaps found (allowed by the package text), `scripts/guest-ci.sh` (the template VM of the gates gets the image
    settings the self-test now checks) and one line of the ADR index. Each change is its own commit with tests.
23. `tarsafe.PROTECTED_SEGMENTS` repeats the guest policy's names: `agent` code of the runner must not import the
    guest package (two processes, two hosts); a test of each side pins the list.

## Tooling
20. `run-atk.sh` looks for an existing golden three times: `limactl list` can come back empty while other packages
    create or delete instances. Two `remote.sh` calls of the same package must not overlap: the second synchronisation
    deletes the first run's output below the repository directory.
21. The extended probe's own directory `/var/lib/atk-suite-probe` is excluded from the file-system snapshot and no
    bytecode is written there (its writes fell into the first attacks' evidence windows in the first run).
