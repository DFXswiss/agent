# SCN-09 – decisions (E2E-09 zero knowledge, E2E-32 role topology, egress)

1. **Canaries per run, made by the orchestrator** (`orchestrator/routes/canary.py`): `POST /canary/new` makes a pair per
   label - `zk-<label>-<12 hex>` and the same random part between Greek, Latin-with-umlaut, CJK characters and an emoji
   outside the basic plane (surrogate pairs in JSON escapes) - and keeps them in the iteration's scratch directory,
   never in the artefacts. Values the spec meets on its way are registered as further canaries: every invitation and
   recovery code in each form a client could send (text shown, without separators in both cases, the decoded 16 bytes
   as base64url and hex) and the GitHub token the double issued to the device. Each pair is also searched by its
   shared random part alone (upper and lower case), so a truncated or re-cased copy is found. Every field gets both canaries of its label (`both()`); fields
   that only accept ASCII names (GitHub login, repository owner and name, branch, the API key) get the ASCII one. The
   scan answers labels instead of values, so its report goes into the artefacts.
2. **Hub-side sources** (`POST /canary/scan`): a fresh `pg_dump` of the hub database; every log of every hub process
   and of the hub's PostgreSQL (`hub*.log`, `postgres*.log` of the hub device, also those of earlier processes after a
   restart); every byte that crossed the browser link and the runner link **in both directions** (what the hub received
   as well as what it sent: requests, responses, SSE frames, WebSocket frames); the organization exports. "Server-side
   recorded SSE/WS frames" are the hub's side of the relays: the hub has no recording of its own, and the relay sits
   directly in front of its socket. The runner link's tap lives on the runner device (T2) and is scanned there with the
   same scanner (`python -m checks.plaintext_scan --report`, the agent checkout's Python, the synced agent-core checkout).
3. **Edits outside the package's files** (no extension point exists for relays, ADR-027 section 4 lists none): the
   package text asks for "server-side recorded SSE/WS frames", which only the relays in front of the hub can record.
   `orchestrator/relay.py` writes a `<n>.up` file next to `<n>.down` when tapping (8 lines), `orchestrator/run.py` taps
   the runner link (`runner-hub-tap` in the runner scratch, deleted with it at teardown; every run, because E2E-09 runs
   inside full suites as well), and `tests/test_relay.py` checks both directions. `routes/plaintext.py` (SCN-02) still
   reads only `*.down` and is unchanged. The cost is disk space in the iteration's scratch (some MiB per spec).
   **Live probe**: before each scan a fresh value goes through every hub-side source on purpose (a request path through
   both relays into the hub's access log, a row in a separate schema `e2e_probe` of the hub database, a failing
   statement into the PostgreSQL log); the spec requires the scan to find it in each, so a broken tap, log path or dump
   cannot pass silently. The hub's own schema stays untouched; the column check reads only schema `hub`.
4. **Scanner extension** (`checks/plaintext_scan.py`): canaries may be any printable text (≥ 8 characters, no
   surrounding white space). Encodings searched: raw UTF-8, the other normal form (NFC/NFD), JSON escapes (lower and
   upper case, surrogate pairs), URL percent-encoding (both cases), hex (both cases), base64/base64url at the three
   alignments. Requests are decoded like responses; after a WebSocket upgrade the frames are unmasked, fragments
   joined and `permessage-deflate` undone with one decompressor per direction (context takeover). Without this the
   device channel (compressed by default in `websockets` and uvicorn) would have been scanned only as deflate output;
   messages that cannot be inflated are counted (`ws_failures`) and the spec requires none. Responses that never have
   a body (1xx, 204, 304) do not swallow the next one; an unknown content coding stays undecoded (the hub uses none;
   the raw bytes are scanned in any case).
5. **Exports**: the scan runs `agent-hub export --org <id> --out <file>` (DR-01) for every organization and scans each
   file (three per run, no finding); while the command did not exist the scan noted "not available yet" instead.
6. **Column check** (`checks/hub_columns.py`, a new file next to the scanner; ADR-046): every one of the 410 hub columns maps onto
   a row of `04-crypto.md` section 7 (`ids`, `logs`, `auth`, `events`, `tasks`, `operations`, `metrics`,
   `notifications`, `audit`, `credentials`, `settings`) or onto `ciphertext` (content sealed under keys the hub never
   holds, ADR-003). One further category, `internal`, holds only `schema_version` (the hub's own migration bookkeeping,
   no user data, outside the scope of section 7). Interpretations where section 7 is not literal:
   - `device.os`, `arch`, `agent_version`, `capacity` and `vm.cpus`/`memory_mib`/`disk_gib`: resource metrics (the
     scheduler's capacity input, `03-api.md` section 4.6.2; shown on the device page);
   - `device_request`/`device_response`: the routing metadata (kind, state, IDs, times) counts as operation-like
     metadata, the envelope is ciphertext (HPKE to the device / to the user);
   - `ceremony`, `request_nonce`, `browser_session.statement`, `user_identity.recovery_id`: authentication (challenges,
     replay nonces, the signed session statement, the ID derived from the recovery code - not the code);
   - `whiteboard_entry`: entry IDs plus event references (event headers); `notify` belongs to notifications.
   The check compares live schema and map in both directions and the SQL type with the content type, and checks the
   values of every text-like metadata column (identifiers, kinds, states, codes, hashes, signed JSON documents with
   their strings and object keys, the session statement): only tokens `^[A-Za-z0-9_.:/+=-]*$`. This excludes free text
   with spaces or non-ASCII letters; a single ASCII word would pass, so the canary scan (with a one-word ASCII canary
   per field) stays the check for content. Offending values are counted, never copied into the report. A unit test
   keeps the map equal to the migrations' columns (added, dropped and renamed ones), so a new column without a decision
   fails the gate before E2E. The review asked to report the interpretations as deviations and amend section 7; the
   specification is not this package's to change, so ADR-046 records them as the binding classification.
7. **Positive control on the runner device** (`POST /canary/runner`): double journals, runner and double logs and a
   plain dump of every runner's store. The spec requires the labels the execution device must have handled in plaintext
   (prompts that reached the model double, every GitHub operation parameter that reached the GitHub double). Names and
   whiteboards are not required there: no session of these flows reads them; their positive control is the browser of
   a second user (the member), who sees the names, the three boards and the task title decrypted.
8. **Flow selection inside E2E-09** (the mailbox host is a canary as well): E2E-01 (operator invitation, `inviter` role, platform invitation, a second new
   user), E2E-06 (first task, extended by `task.set_title` so the title is an operation parameter written by the
   device), E2E-25 (entries on three levels, one edit, a member reading them in her own browser; the rights and
   negative cases stay in E2E-25), E2E-27 (API key and mailbox stored, one API-key turn against the double's credential
   check; the subscription variant is left out: its token comes from the login doubles of E2E-28, T1 only, and the
   credential scans of E2E-27/E2E-28 already cover it), E2E-21 (the full chain with its assertions). A further,
   non-private organization adds the organization name and a second profile name.
9. **Shared flows** (`fixtures/flows.ts`, a new file): the first task of E2E-06 and the GitHub chain of E2E-21 with their
   assertions, parameterised, so E2E-09 (with canaries) and E2E-32 (as they are) run the same flows without copying
   them. The REST sequence is a copy of the one in the E2E-21 spec (specs cannot be imported).
10. **Egress report of E2E-32** (`orchestrator/egress_report.py`, route `routes/egress_report.py` as a new file): stricter
    than the generic egress check of every run: the hub must listen on a private LAN address (not loopback, not public,
    not the shared carrier range of RFC 6598 that overlay tunnels use), the origin must be a loopback name
    (no public name), the runner device may reach the hub device only on the hub port and only through its relay to
    exactly that address, every relay of both devices targets the loopback or the hub, no process the run started is a
    tunnel program and SSH runs only to the run's Lima instances. "LAN" means an IPv4 address of RFC 1918 (no IPv6:
    overlay tunnels use unique-local IPv6 too). Connections between two sockets of one device (its own LAN address,
    e.g. the browser relay to the hub) count as local. The guest OS's NTP/DNS through the Lima host agent stays allowed
    (ADR-034, `07-testing.md` section 5.2; observed: NTP of the Ubuntu image to its public pool servers) and is listed
    separately as `vm-os` in the report. Limits: the process snapshot for the tunnel check is taken at report time and
    the egress sampler polls every 0.5 s, so a process or connection living only between two samples is not seen; the
    relays record every connection to the hub and the doubles in full.
11. **E2E-32 on T1** is skipped with the reason "needs the hub on the second device"; the traceability test counts the
    spec through its tag.
