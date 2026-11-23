# ATS-02 decisions (attack suite extension ATK-33 … ATK-40)

Small decisions of the package that extends the attack suite to operations, MCP, gateway and file import/export
(07-testing.md section 2.3 and 8; 05-runner.md sections 4.4, 4.6, 5.3, 5.4, 5.5, 10.2, 12; 03-api.md sections 7.1, 8,
9; ADR-008, ADR-010, ADR-012; ADR-027). Results: `docs/platform/atk/M2.md`. Suite conventions of ATS-01 apply
(decisions ATS-01).

## Shape of the new attacks
1. **Scenario plus plan.** The surfaces of ATK-33 … ATK-40 cannot be attacked by tool calls alone: a session has no
   tool that makes links, sends raw HTTP or types into its pane. Each attack therefore has a scenario (what Claude
   Code attempts) and a plan in `agent_doubles.atk.extended.PLANS` that the driver calls at fixed points: before the
   snapshot (`before_start`: links, hard links, FIFO, device, root-owned canary), after the credential
   (`while_running`: the session-side client), after the scenario (`after_finished`: forged prompts), for every
   `operation.request` of the session (`on_operation`: the runner stand-in) and at the end (`check`: the new criterion
   `specific`). The seven criteria of ATS-01 stay unchanged and apply to every new attack in both modes.
2. **Session-side client.** Code inside a session cannot run (Landlock, `NoExecPaths`); what it could send if it
   could is sent by `session_client.py` (stdlib), started by root through `systemd-run --uid=<session uid>` in a
   transient unit `atk-client-<hex>`: the session's UID and network rules, outside the sandbox. The process sampler
   now records each process's systemd unit, and `no_execution` ignores processes in `atk-client-*` units (a session
   cannot create a unit). The session token is read by root from the Claude process's environment and handed to the
   client through `--setenv` (test VM only; never written to the results).
3. **Runner stand-in.** The suite runs no hub and no runner daemon, so nobody would answer `request_operation`.
   `tests/platform/atk/runner_standin.py` answers with the runner's real code where the attack is aimed: the formal
   checks of `agent_runner.operations.checks` (settings allowing `atk/repo`, `atk/links`, `atk/export` and the
   `atk/evil-*` repositories, default protected branches, a connected owner, the packaged catalog), the tarball
   rewrite `agent_runner.github.tarsafe.repack` of `github.checkout`, and the GitHub double seeded with
   `fixtures/github-seed.json`. It executes nothing: an operation that passes every formal check is answered `failed`
   and reported. It lives under `tests/` (it imports `agent_runner`, which `agent_doubles` must not), so it is tested
   by `test_atk_40_files.py` but not measured by the coverage gate; the plan code that uses it is measured.
4. **Malicious tarballs.** A git tree cannot hold `..`, absolute paths, hard links or devices, so the GitHub double
   cannot serve them. `fixtures/tarballs.py` builds them (GitHub shape: top directory, gzip) for `atk/evil-*`, as a
   compromised codeload host would; `atk/links` (symbolic links out of the workspace) comes from the GitHub double
   seed through its real tarball route. Expected outcomes: `..` and absolute paths refused (`path_invalid`); hard links
   and devices skipped, only the regular file imported (tarsafe skips links of a GitHub tarball by design, 05 section
   12.1: "regular files/dirs only"). Every archive is also sent raw to the guest's `files/import`, which must refuse it
   alone (`path_invalid`/`request_invalid`).
5. **Forged prompts.** ATK-38 pastes two prompts into the pane through the root tmux server like the guest's delivery
   (clear, bracketed paste, Enter) without registering them: the used initial prompt inside a forged
   `<pasted_content>` wrapper (same normalised hash, already used) and an unregistered text. `prompt_decisions` of the
   catalog makes mode `hooks` expect `allow`, `block`, `block`. In mode `fault` the hooks fail open (05 section 5.5),
   Claude Code takes the prompts and side steps of the scenario answer them with text; the pass criteria still hold.
6. **Initial prompt per attack.** `Attack.prompt` replaces the standard prompt (ATK-38: pasted text with an injected
   fake `<system>` notice).
7. **Outbound data.** `no_foreign_read` also searches `RunEvidence.outbound`: the workspace exports of ATK-40 (what a
   commit would send to GitHub), including the refused export probes.
8. **Test IDs.** The real runs are `test_atk_NN[hooks|fault]` in `tests/platform/atk/test_atk_NN_<slug>.py` (marker
   `atk`), so `-k "atk_33 or … or atk_40"` selects exactly them (see no. 18); `test_attack[ATK-NN-mode]` of ATS-01 covers the same
   verdicts. `scripts/run-atk.sh` passes arguments after `--` to pytest. The plan unit tests in the same files always
   run.

## Guest changes (gaps found)
9. **Session tokens bound to the connecting UID; failed checks limited and journaled (ATK-36).** The guest API
   neither rate-limited nor journaled failed token checks, and any process holding a token could use it. Now the API
   process looks up the UID owning the client socket in `/proc/net/tcp` (`agent_guest/peer.py`,
   `AGENT_GUEST_PEER_TABLE`, set in `agent-guest-api.service`; empty switches the binding off for tests and other
   operating systems; binding on but no entry = UID -1, fail-closed) and sends it with the token hash to
   `session.resolve`. The control process accepts a token only from its session's UID; every failure (missing,
   unknown, inactive or foreign token) is counted per connecting UID (`agent_guest/authguard.py`): at most 10 per
   60 s are answered `401` and journaled `guest.error auth_failed` (with the session of that UID), then one
   `auth_rate_limited` record and `429` (`rate_limited`, gateway `rate_limit_error`) with `Retry-After` until the
   window has room. A valid token of the connecting UID is never limited, so a misbehaving session cannot lock others
   (or itself) out. Comparison stays constant-time over all sessions. Additive contract change in agent-core
   (`contracts/changes/ATS-02.md`): two `guest.error` codes and the guest error code `rate_limited`.
    Architecture record: ADR-044 (numbered ADR-043 before DOC-01).
10. **Gateway query parameters (ATK-39).** The gateway forwarded every query parameter to the provider; now only
    `beta` (Claude Code's `?beta=true`), so a session cannot add provider options. Other headers a session chooses
    (forwarding headers, `anthropic-version`, …) are still passed on: 05 §10.2 strips credentials, transport and
    hop-by-hop headers only, and none of the others carries a credential (checked by ATK-39).
11. **Golden bundle without the MCP tool schemas (found by ATK-33/37/40, high).** `agent_runner.vm.golden` skipped the
    contract directories `tools`, `changes`, … at every level, so `contracts/mcp/tools/` never reached
    `/opt/agent-guest/contracts`: `tools/list` failed with `503` in every golden image and no session could use a
    platform tool (Claude Code offered none). Another package fixed the same defect on the integration branch while
    this one was running (skip lists at the top level only, with tests); this package's own fix was dropped in the
    rebase. With the schemas present Claude Code 2.1.330 offers the platform tools as deferred tools (`defer_loading`)
    and calls them directly; no tool-search setting is needed.
12. **Claude Code escapes `<pasted_content` in prompts (found by ATK-38).** Before the UserPromptSubmit hook Claude Code
    2.1.330 turns `<pasted_content` into `<\pasted_content` and `</pasted_content` into `<\/pasted_content`. A registered
    prompt containing such a tag (pasted text that itself contains one) was refused as unregistered. `prompts.normalise`
    undoes exactly this escaping before the wrapper rule; registered and submitted text take the same steps, so a
    forged wrapper still hides nothing new (the used initial prompt inside a forged wrapper is refused, ATK-38).

## Evidence (open low points of the ACC-M1 review)
13. **Point 3 fixed:** a root-owned regular file that a run created and removed again (temporary capture index,
    `useradd` lock) is judged by its old state; such a removal is no longer a false deletion. **Point 2 partly
    fixed:** guest records and account locks must be regular files (a root-owned link or directory with such a name
    fails); their mode is still not checked (the guest's modes differ per file; left for ATS-03). **Point 1 left:**
    the victim's `capture-index.json` may still change (root writes it; a change of the victim's workspace itself
    fails directly) - not touched by the new attacks.

## Out of scope / deviations
14. A session cannot create a symbolic or hard link itself (no tool, imports refuse links; `/srv/sessions` is its own
    file system, so hard links to system files fail with `EXDEV` even for root);
    ATK-34/35/40 plant them as the suite. Links to world-readable files (e.g. the planted `/etc/platform` canary) are
    not used: the OS layer lets sessions read `/etc` on purpose (decisions ATS-01 no. 9), so such a read through a link
    would test layer 1 only, which is ATK-21/26's job.
15. Timing of the token comparison is checked coarsely (median of near and random guesses within 5 ms over the
    loopback); the constant-time property itself is `hmac.compare_digest` over every session (unit tests of GST-02).

## Review (independent subagent)
16. 0 high, 4 medium-high, 6 medium, low points. Fixed: the peer lookup also accepts half-closed client connections
    (FIN_WAIT1/2; before, a client that half-closed got `401` and all such clients shared one bucket), so ATK-39's
    bare-CR probe now reaches the forwarding path (its smuggled `Cookie`/`Proxy-Authorization` are stripped; headers a
    session could send directly are no finding); the seed exception of the evidence no longer covers the victim's
    directory, and ATK-40 lists the victim's secret as a target; ATK-34/35 require only `deny` decisions of the policy
    in mode `hooks`; ATK-34 has an Edit attempt; ATK-36 makes every surface fail before the limit and checks one
    record per foreign-token attempt; ATK-39 checks the journal of the refused model, the two credentials and the six
    paths; ATK-40 probes hostile import directories and a symlink-then-file archive; results in `M2.md`; numbering.
17. Kept, with reasons: the contract codes reach this repository only by the merge script's sync after the agent-core
    merge (ADR-027 section 3; the agent-core package merged first); the guest changes go beyond the package's file list
    because the package text asks to fix small gaps in the affected guest and runner modules - each gap is one commit
    with tests, the binding is recorded as ADR-044 and the evidence/driver/catalog of ATS-01 are the suite's extension
    points (no other package owns them now); `prompts.normalise` undoes the escape anywhere in the text (registered and
    submitted text take the same steps, prompts are single-use); no journal record for MCP calls a non-Claude client
    sends with invalid arguments (GST-07 decision 10), the timing check stays coarse (tokens are hashed before the
    comparison), the executors are not driven by ATK-33, protected names inside imported repositories
    (`.claude/`, `.mcp.json`, `CLAUDE.md` below `repos/`) are not refused by import - all for ATS-03.
18. The acceptance command's `-k "atk_3[3-9] or atk_40"` is no valid `-k` pattern (pytest matches substrings, no
    character classes); the runs used `-k "atk_33 or … or atk_40"` through `scripts/run-atk.sh … -- -k …`.
