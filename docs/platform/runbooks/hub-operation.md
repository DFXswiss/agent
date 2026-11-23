# Runbook: hub operation (process, database, limits, audit)

Scope: running `agent_hub` on the hub device ([03 §1–§2](../spec/03-api.md#2-configuration-of-the-hub-agent_hub_-fail-closed-like-agent_coreconfig),
[02 §6](../spec/02-data-model.md#6-migrations), ADR-002, ADR-003, ADR-019). The legacy hub (`python -m agent_core`, port
8787) is independent and stays untouched ([00 §7](../spec/00-overview.md#7-coexistence-with-the-legacy-code)).
Deployment behind the production name is part of the cutover plan ([CUTOVER.md](../CUTOVER.md)), not of this runbook.

## 1. Operating rules

- **Exactly one hub process per database.** Rate limits, the recovery-attempt limit, the live streams (SSE to
  browsers, WebSocket to devices), reminders and the `metrics` frames are held in the process. A second process on
  the same database splits every limit and sends reminders and frames twice (AUD-01 D-03). Run `agent-hub serve`
  (one uvicorn worker, the default) under a supervisor that restarts it; never start workers or replicas.
- The hub holds no secret content (zero knowledge, ADR-003). Its own secrets are the signing seed
  `AGENT_HUB_SIGNING_KEY` and the database credentials; keep them in the service's environment, never in files of
  the repository.
- `TEST`-variables (`AGENT_HUB_TEST_*`) are for the test suites only.
- **Update the hub before the runners.** A release may relax a limit the runners rely on; the hub of a release
  admits the runners of the release before it, not always the other way round. Example (ADR-048): runners that push
  every 100 ms meet `429` at a hub that still admits one push per 250 ms. Roll out the hub first, then the runners.

## 2. Configuration (`AGENT_HUB_*`, fail-closed)

`PUBLIC_URL` (origin; its host is the WebAuthn RP ID – changing it invalidates every passkey), `DATABASE` (writer
DSN), `SIGNING_KEY` (Ed25519 seed hex), `HUB_KEY_ID` (`[a-z0-9-]{1,64}`), `PLATFORM_ROOT_KEY`, `HOST`, `PORT` (8788),
`STATIC_DIR`, `DEVICE_ONLINE_SECONDS` (30), `PLATFORM_BUDGET_USD_PER_ORG_DAY` (empty = unlimited), `MODEL_CATALOG`,
`TRUSTED_PROXIES`, `FORWARDED_HEADER` (§4).
`OWNER_DATABASE` is for `migrate` and `restore` only and refused in the hub process. Any other `AGENT_HUB_*` name is refused; the
error names the variable and the rule, never the value.

## 3. Database and start

1. PostgreSQL database `agent_hub`, schema `hub`, two roles: the owner (migrations) and the writer (hub process;
   append-only tables are protected by grants and triggers).
2. `AGENT_HUB_OWNER_DATABASE=<owner DSN> AGENT_HUB_DATABASE=<writer DSN> agent-hub migrate` – applies new migrations,
   refuses a changed checksum, prints `unchanged` when nothing is new. Run before every start of a new release.
3. `agent-hub serve` (or `python -m agent_hub`). Health: `GET /v1/health`. The web client is served from
   `STATIC_DIR` (`index.html` no-cache, hashed assets immutable).
4. After a release, compare the build hash on `/security` with the published one.

## 4. Rate limits and many users behind one address

Limits per minute ([03 §1](../spec/03-api.md#1-conventions)), token buckets with one minute of burst:

| Class | Limit | Key |
|---|---|---|
| anonymous (unsigned requests, enrollment, invitation lookup, registration and recovery begin) | 30 | **source address** |
| failed signature checks | 30 | source address |
| browser session | 600 | session key |
| device | 6000 | device key |
| platform root (operator CLI) | 600 | root key |

**Source address.** Without further settings the source is the TCP peer. The hub serves plain HTTP and WebAuthn
needs HTTPS, so production always has a TLS reverse proxy in front: set `AGENT_HUB_TRUSTED_PROXIES` to the proxy's
address or network and, if the proxy writes `Forwarded` instead of `X-Forwarded-For`, `AGENT_HUB_FORWARDED_HEADER=forwarded`.
The hub then takes the rightmost untrusted address of that one header (KOR-06; prefix length 0, networks with host
bits and IPv4-mapped networks are refused at start). **Without these settings behind a proxy, all users share one
anonymous bucket.**

**Idle connections behind a proxy.** `agent-hub serve` keeps an idle HTTP connection for 120 s (uvicorn's default
would be 5 s). On every hop the client must close an idle connection before the server does, otherwise a request sent
at that moment is lost (KOR-18). So the reverse proxy must reuse its upstream connections to the hub for **less than
120 s** (cloudflared `originRequest.keepAliveTimeout`, default 90 s; nginx `keepalive_timeout` of an upstream block,
default 60 s), and its own idle limit towards the clients must stay above the runner's 2 s (edges keep idle
connections for minutes). **Symptom of a wrong order:** runners log `push_failed reason=hub_unreachable reused=yes`
in bursts, or the proxy reports upstream connections reset by the hub.

**Effect behind NAT:** many users behind one public address (office NAT) still share the anonymous and the
failed-signature bucket of that address. **Symptom:** `429 rate_limited` with `Retry-After` on sign-in, invitation
acceptance, registration or recovery while signed-in use keeps working. **Configuration:** the limits themselves are
constants of `agent_hub/http/limits.py` in v1 (no setting); onboarding a team in one room may wait up to a minute.

## 5. Audit and integrity

- `agent-hub audit verify` checks the global audit chain and the newest checkpoint of the hub key (exit 0 ok, 1 broken,
  2 configuration error, 3 database unavailable). Run it daily and after every incident or restore.
- A broken chain is an incident: stop the hub, keep the database for analysis, restore from the last verified export
  ([export-restore.md](export-restore.md)).

## 6. Faults

| Symptom | Cause | Remedy |
|---|---|---|
| start exits `config_invalid …` | missing or malformed setting | fix the named variable |
| browsers show "inconsistent membership history" (`log_rollback`/`log_fork`) | the hub served an old or forked log | treat as incident; compare heads; do not "fix" logs in the database |
| devices offline in the UI while running | WebSocket blocked, clock skew, `DEVICE_ONLINE_SECONDS` too low | `agent-runner doctor` on the device (hub, clock) |
| `503` with retry under load | audit-lock deadlock, retried once (decisions HUB-06/HUB-10/HUB-12) | transient; repeated: check database load |
| reminders or monitoring values appear twice | more than one hub process | stop the extra process (§1) |
