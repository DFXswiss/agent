# Zero knowledge at the hub (E2E-09) and role topology (E2E-32) – acceptance evidence (SCN-09)

Devices by role only: **runner device** (T1: everything; T2: runner, VMs, doubles) and **second device** (T2: hub,
PostgreSQL, browsers, orchestrator). Golden image: shared TEST_MODE cache of the integration state (ADR-036), warm pool
of 2 VMs, model and GitHub doubles. Decisions: `docs/platform/decisions/SCN-09.md`; column classification: ADR-046.

## Runs

| Run | Result |
|---|---|
| `e2e.sh SCN-09 --topology t1 --only E2E-09 --repeat 3` | 3/3 iterations green |
| `e2e.sh SCN-09 --topology t2 --only E2E-09,E2E-32 --repeat 3` | 3/3 iterations green (both specs) |

End checks of every iteration: no egress violation, model and GitHub doubles `state.ok`, audit chain verified, runners
alive, the run's VMs deleted. Per iteration, `e2e-09/zero-knowledge.json` and (T2) `e2e-32/egress-report.json` are
kept with the run's artefacts; they name canaries by label only.

## E2E-09: what was put in, where it was looked for

Canaries: random per run, one ASCII and one non-ASCII value (Greek, umlaut, CJK, an emoji outside the basic plane)
per field, plus their shared random part; invitation and recovery codes in every form a client could send (text,
without separators in both cases, decoded bytes as base64url and hex); the GitHub token the double issued.

| Field (00-overview.md section 3) | Flow |
|---|---|
| prompts, answers (model double), session title (background request) | E2E-06 first task, E2E-27 API-key turn, E2E-21 chain |
| task title (`task.set_title` by the session, an operation parameter) | E2E-06 |
| whiteboard entries on three levels, one edit | E2E-25 |
| names: two display names of new users, a member, an organization and its profile name, a workgroup, the device | E2E-01, org invitation, org creation, device rename |
| credentials: Anthropic API key, mailbox host, user and password, GitHub login and token | E2E-27, E2E-20/21 |
| operation parameters and results: repository, branch, file, commit message, pull-request title and body, comment, results that echo them | E2E-21 |
| invitation codes (operator, platform, organization) and three recovery codes | E2E-01, org invitation |

Hub side, every canary absent in every encoding (raw UTF-8, other normal form, JSON escapes, percent-encoding, hex,
base64/base64url at three alignments): full `pg_dump` of the hub database; every hub and hub-PostgreSQL log; every byte
that crossed the browser link and the runner link in both directions (requests, responses, SSE frames, WebSocket frames
unmasked and inflated) as recorded on the hub's side of the relays; the export of every organization (`agent-hub export
--org <id>`, three organizations per run). A fresh probe value put through each of these sources was
found in each, so the scan demonstrably sees them. Per iteration: about 120 sources (11.5 MiB), about 305 SSE frames and
37 WebSocket messages decoded (none undecodable), no finding.

Positive control: the canaries are shown decrypted in the browsers of the admin and of a member (names, boards, title,
answers), and the runner device holds prompts and every GitHub operation parameter in plaintext (model and GitHub
double journals, runner logs, runner store).

Columns: all 410 columns of schema `hub` map onto a row of `04-crypto.md` section 7 or onto ciphertext (ADR-046);
SQL types fit; every text-like metadata value (including JSON keys) consists of tokens only. No deviation.

## E2E-32: role topology

Full E2E-06 (first task under 10 s on the warm pool) and E2E-21 (GitHub chain) with the hub on the second device and
the runner on the runner device. Egress report of both devices: the hub listens on a private IPv4 LAN address and is
reached under a loopback origin; the runner device reaches the hub only through its relay to exactly that address and
port; every other connection of the run's processes stays on its device (doubles, store, VM ports) or is the guest
operating system's time service through the Lima host agent (ADR-034); no relay targets anything else; no process of
the run is a tunnel. No problem in any iteration.
