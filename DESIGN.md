# Agent — product design

This document records the decisions for the **session bus**: the device owns its writes, a hub fans events out, GitHub login, team-scoped visibility, restore, person-to-person pings, and session-addressed mail.

The wire contract lives in [agent-core PROTOCOL.md](https://github.com/DFXswiss/agent-core/blob/develop/PROTOCOL.md). This file is the *why* and the product rules for **this client**. Implementation details that drift should lose to this document until a pull request changes it.

## 1. Purpose

Every person who installs the client keeps a **local store** of sessions and of the work those sessions choose to record.

The team also needs:

- a **live view** of who is working on what
- **every teammate able to read every teammate’s store** (within a team), by asking the hub — not by mirroring every row onto every laptop
- **session-addressed mail** between live sessions, and **person pings** (for example a review request)
- a **central website** after GitHub sign-in
- a **wiped laptop** that can be rebuilt from the hub

“Realtime” means push on write (milliseconds locally, typically well under a second across the hub), not a multi-second poll as the source of truth.

The AI session talks **only** to the local database. Scripts perform every action that leaves the machine (GitHub, external mailbox, hub HTTP, tmux knock). A row in the local store is the intent; the script’s later write is the result.

## 2. Locked decisions

| Topic | Decision |
|---|---|
| Write owner | The device that created a row. The hub never becomes the author of that row, except website-created person pings and website-created `assignment.created` / `assignment.reply` rows recorded by the hub with origin `web:<login>` when a human owner issues or answers a mandate, either signed in on the website or through `agent assign` with the bearer of a device paired to that owner (never an agent's own device). In both cases the hub creates the GitHub issue and assignment with that human's own OAuth token, stored when the human signed in on the website. `web:` origins are reserved for the hub. |
| Hub role | Full replica + fan-out + always-on website. Not a shared write database. Planned: verifier and keeper of the signed team-record chain (§24). |
| Login | Any GitHub account may sign in. Membership is *not* the GitHub org. |
| Authorization | Hardcoded teams in git. Change members with a pull request. |
| Visibility | Self always. A team only if the GitHub login is listed on that team. Several teams → union. |
| Sync (this device) | Always: own events, gapless. Also: implicit **inbox** snapshots (session mail to a session this device owns), **pings** snapshots (person pings this login sent or received), plus optional table subscriptions and one-shot queries. Not a default full pull of every visible origin. |
| Inbound apply | Own origin stays gapless (`last+1`). Foreign inbound is **row snapshots**, not gapless replay of the sender’s `origin_seq`. Same-device session mail (own origin, other session) is already in the event log; apply is idempotent on `row_id` and still `NOTIFY`s the knock. |
| Restore | Required. A wiped device comes back from the hub: own events, inbox snapshots (session mail), and pings snapshots (person pings this login sent or received). |
| Identity | GitHub login, lowercase. Device = stable UUID, not the hostname. Planned replacement by registered key pairs: §24.2. |
| Session | No work without a `session` row. The start hook inserts it. |
| Store engine | Local PostgreSQL on loopback / Unix socket under `$AGENT_HOME`. Not world-reachable. |
| Catalog | Generic `activity` rows (`type` + payload). Session tags are the types present. |
| Skills | Optional, requested. `spine`, `review-loop`, `pr-review`, `error-fix`, and `conflicts` exist as skills. They are **not** on by default. |
| Initial configuration | GitHub accounts, AI accounts, roles, and their selections start unconfigured (`NULL`). Add them explicitly through configuration; no fixed account or role count. See §19.8. |
| Runtime | This public client. Team-specific rules live elsewhere and must not ship a second store binary. |
| Session mail | Addressed to a **session id**. Delivery does not require a subscription. |
| TUI knock | Script wakes the session with only `da ist Post id <uuid>`. The agent reads that row from local Postgres. |
| Device daemon | Always-on user service on this device. `agent init` installs and starts it with knock (`LISTEN` plus usage / pending / github pending / mail pending / `pr.merged` polls), the local dashboard, and `cli-bridge`; daemon `sync --follow` starts only after `agent pair`, once `device.json` has token and hub URL. |
| Outside facts | Scripts notice GitHub (and other outside) state. The agent is not told by a human and does not poll GitHub. Example: a recorded PR merges → script writes `pr.merged` on that session and knocks. |
| AI vs scripts | The AI inserts local intent. Scripts perform every side effect that leaves the machine. Model text is never a state transition. |
| Checks and gates | A **check** records a fact (`agent check record`). A **gate** is a policy verdict over evidence (`agent gate record`). A model claim is neither. Confidence is not proof. |
| Ready for review | Draft publication is immediate after the first signed task commit ([docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md)). A draft plus local tests is not done. CONTRIBUTING.md is the contract for this repository. When spine and pr-review are attached, model slot `first` then model slot `second` are the gates for this head; `ai-accounts.json` supplies the default provider behind each slot when the task is created, and the pin on the task governs and can be changed explicitly with `agent task models` (not for coordinator tasks) ([docs/ai-accounts.md](docs/ai-accounts.md)). A review stays valid when its head is this head or a commit after the pull request base that this head still contains with only `.md` files changed in between, and CI stays bound to this head. `agent allow --action pr-ready` only checks task state. Leave-draft is Ready for review, not completion. A human merges; claim completion only after that merge is verified. |
| Local CI report | The markers stay `<!-- DFX-LOCAL-CI:v1 -->`. Schema `dfx-local-ci/v1` in [docs/local-ci-v1.md](docs/local-ci-v1.md) stays valid as JSON; a comment the guard accepts still needs the outcome lines in [docs/a38.md](docs/a38.md). A newly written A38 report is `dfx-local-ci/v2` inside those markers. `agent local-ci verify` parses the block and computes pass/fail. Private product repositories attach the block to the ready comment. This public client still uses GitHub Actions for its own PRs. |
| A38 | The complete central process is [docs/a38.md](docs/a38.md) plus [docs/a38-guard.md](docs/a38-guard.md). An adopter keeps only `.github/a38.json`, its integration, and a contributing pointer; plugins use short central pointers. `agent a38` runs and validates the existing report format, while `agent pr-guard` checks live author evidence without executing PR code. Private trusted-base opt-in may provide local code-gate equivalence; public/default GitHub CI, independent required GitHub-only checks, review gates, technical merge restrictions, and human merge remain unchanged. |
| Merge | The client never merges. A human merges. |
| Repos | Public MIT: `DFXswiss/agent` (client), `DFXswiss/agent-core` (hub). |
| Website host | `agent.dfx.swiss` (development: `dev.agent.dfx.swiss`). Singular product name. |
| License | MIT. |
| Product scope | General session store and bus. Rules in this document are the client contract. |

## 3. Rejected alternatives

These were considered and rejected.

**One shared database everyone writes to.**  
Breaks offline work and the rule that the local store is the source of truth. A hub outage would stop every session.

**Postgres logical replication or a laptop mesh.**  
Serial IDs collide across machines. Laptops sleep and sit behind NAT. There is no always-on view when nobody is online.

**WireGuard / peer mesh between employee devices.**  
Even a single permissioned node in this organisation needed a public relay because inbound ports are not freely available. N laptops on hotel Wi‑Fi is worse. Local Postgres must never be exposed.

**Put the hub inside the existing public API service.**  
Separate auth and blast radius. Patterns may be copied; the process must not be shared.

**Plane (or any issue tracker) as the store.**  
Planning is not session / activity / skill. A second truth would appear.

**Cloudflare Access email + per-device service tokens as the product login.**  
The website is GitHub. One sign-in pairs the device and opens the team dashboard. Access tokens stay out of this product.

**Telegram as the ping transport.**  
Existing bots are product channels, not person-to-person. Person pings stay first-class rows on the same bus.

**Default full pull of every visible origin onto every laptop.**  
The hub still keeps the complete replica (restore and the website need it). Devices pull what they own, session mail to their sessions, person pings this login sent or received, and what they subscribed to or queried.

**Inject the session-mail body into the TUI.**  
The knock is only the id. The agent selects the row.

**`/loop` inside the agent as the inbox.**  
A device daemon `LISTEN`s on Postgres. The agent does not poll.

**The human (or the agent) polls GitHub to learn a PR merged.**  
A script watches `pr.open` rows and writes `pr.merged`, then knocks. The agent reads the row.

**Expose local ports or the local database.**  
They stay on `127.0.0.1` / a Unix socket. Nobody reads a teammate by opening their Postgres.

**Hostname `ledger.*`.**  
Collides with an unrelated public ledger product. The name is **agent**. The product language is **session store** / **session bus**, not “ledger”.

**Hub-owned task lifecycle and leases.**  
The hub does not assign work, grant leases, or move authorship when a laptop disappears. Write ownership stays on the origin device. A crashed session restarts on **that** device, or the work stays unread. Restore rebuilds a wiped device; it does not transfer authorship.
Recording a human-issued mandate under the §2 website-origin exception is not
the hub assigning or leasing work: the human owner assigns on GitHub
through the website or `agent assign`, and the execution device's coordinator still decides
whether to accept the assignment.

**Task as the product center.**  
A task exists only when the spine skill is attached. The durable unit is the session and its activity log.

**Error-to-PR as the default platform workflow.**  
Production-error ingestion is not the product core and not a default skill. The `error.*` spellings in §14 belong to the opt-in **error-fix** skill (§21). A hub `READY_FOR_PR` state is refused even as a skill: task state stays on this device (§2 Write owner, §20).

**Worker capability ontology / scheduler.**
Providers are the drivers that the operator describes in `$AGENT_HOME/drivers.json` ([docs/ai-accounts.md#drivers](docs/ai-accounts.md#drivers)); the repository ships no driver table. `ai-accounts.json` supplies the default for model slot `first` or `second` when the task is created, the pin on the task governs, and it can be changed explicitly with `agent task models` (not for coordinator tasks), not by a scheduler. Scripts are not store workers. Do not add capability tables until two real workflows share them.

**A second event envelope beside `activity`.**  
Outside facts enter as scripts writing catalog rows. Do not add a parallel `external_events` store.

## 4. Two repositories

| Repo | Role |
|---|---|
| [DFXswiss/agent](https://github.com/DFXswiss/agent) | Client: CLI `agent`, local PostgreSQL store, local dashboard, pair / sync / restore, skills, TUI knock. |
| [DFXswiss/agent-core](https://github.com/DFXswiss/agent-core) | Hub: GitHub OAuth, pairing, team file, event store, website, person pings. |

App code lives here. How a particular environment is deployed is out of scope for these public repos (no internal hostnames). Compose in `agent-core` is generic.

Default branch: `develop`. Image tags and environment mapping follow the organisation’s usual public/private split; this document does not name those hosts.

## 5. Identity

Three distinct IDs:

| ID | Meaning |
|---|---|
| GitHub login | The person. Always stored lowercase. Comes only from GitHub OAuth, never from a client field. |
| `device_id` | One machine. UUID persisted in `$AGENT_HOME/device.json`, **not** only inside the database. |
| Session id | One working session on that device (`human`, `runner`, `other`). |

One person, two laptops → two devices, one login.  
A runner on an already paired machine inherits that device’s login. No second OAuth.

The hub binds `origin_device_id` to the GitHub login of the session that **confirmed** pairing. The client cannot choose that login.

An interactive driver's runtime UUID is **not** the session id. See §17.

## 6. Visibility and teams

Anyone may log in. Seeing other people requires a team listing.

```yaml
# agent-core/teams.yaml — edit only via pull request
teams:
  example:
    members:
      - some-github-login
```

Rules:

- A login on **no** team still signs in, pairs, syncs and restores — and sees **only itself**.
- A login on a team sees **self ∪ every member of that team**.
- A login on several teams sees the **union**.
- Comparison is case-insensitive; stored lowercase. Duplicate members in one team are rejected.
- The hub **stores** everyone’s events regardless (otherwise restore dies). The file decides **read**, not write.
- Person pings may only target a login the sender is allowed to see. Otherwise 403.
- Empty `members: []` is valid and is the starting state. Names are not invented in this repo.

The hub reads the file from the deployed tree. After merge, the new roster applies on the next deploy of that environment. No live fetch against GitHub.

## 7. Website and pairing

`https://agent.dfx.swiss` (and `https://dev.agent.dfx.swiss`) is one origin: HTML dashboard + API + sync. No second API hostname.

**Sign-in** is GitHub OAuth (`read:user` only). After login the browser is the team dashboard. Planned replacement: key-pair sign-in without a third-party identity (§24.2).

**Pairing is not automatic from “I opened the website”.** The browser does not know the laptop. Linking is a deliberate step in the same GitHub session.

Normal path (from the laptop):

1. `agent pair --hub <url>` (or `AGENT_HUB`) creates or reuses `device_id`, creates a new challenge on every run, persists both in `device.json`, and calls `POST /pair/prepare`.
2. The printed URL opens `/pair?challenge=…`.
3. If signed out, GitHub OAuth must return to that same `/pair?…` URL (not `/`).
4. `POST /pair/confirm` with the cookie session binds the device to the GitHub login and issues a **device token** (HMAC, not a GitHub token).
5. The CLI polls `GET /pair/wait` until it receives the token **once**. A second wait does not get the token again. Challenges expire.

Website-first (phone, another machine): login shows the dashboard. No new device. Pairing still needs the CLI (or a short-lived pair mode). The public origin must not read the local store over CORS.

Revoke lives on the website later; a revoked token is 401. Removing a GitHub login from every team does not delete history; it only removes **read** of others.

The website is the full-replica view (summaries first; thick logs on demand). Laptops stay selective.

## 8. Local store (this repo)

- Home: `$AGENT_HOME` if set and non-empty, otherwise `~/.local/share/agent`. Directory mode `0700`.
- Database: PostgreSQL bound to `127.0.0.1` and/or a Unix socket under `$AGENT_HOME`. Data directory mode `0700`.
- Lifecycle: only `agent init` runs `initdb`. Commands that open the store start an existing stopped cluster and otherwise exit with `run agent init`; they never create one. `agent pg status` reports it without starting it; `agent pg stop` stops it unless a device daemon installed for this `AGENT_HOME` is present; `agent daemon --uninstall` stops it together with the service. `agent init` forwards `AGENT_PG_DSN` into the service unit. `AGENT_PG_DSN` bypasses all of this.
- Identity: `device.json` next to it (`device_id`, token, login, hub URL, pending challenge). Wiping only the database must not mint a new device.
- All **own** local mutations go through an append-only event log (`event_log`) with `origin_seq` starting at 1 on **this** device. Own seqs stay gapless (`last+1` or a hard error).
- Materialized tables (`session`, `activity`, and skill tables when a skill is on) are what the dashboard, CLI, and the AI read.
- A row whose `origin_device_id` is not this device is **read-only** for the AI and for mutating CLIs. Replica apply may insert it as a snapshot without taking write ownership.
- Typed foreign keys are **not** enforced on replica writes. Own-device writes may still validate `session_id` against a local session.
- Primary keys for activities, tasks, agents, checks, gates, pings are UUIDs. Session ids stay caller-chosen strings.
- Session kinds: `human` | `runner` | `other`.
- Reachability for the TUI knock (tmux pane, ACP endpoint, or `none`) is stored locally. It is not a hub event on every keystroke.

v1 uses a single Postgres role `agent` on `127.0.0.1` (managed cluster: trust auth, no unix socket). Two roles with password auth on a local socket (AI `INSERT`/`SELECT` vs scripts full DML + `LISTEN`/`NOTIFY`) remain later footgun-defense, not a hostile-TUI boundary.

## 9. Sync and restore

Each device is the write owner of its own events. The hub keeps a **complete** copy of every origin.

| Direction | What moves |
|---|---|
| Device → hub | Every **own** event, in `origin_seq` order, no gaps. |
| Hub → device | Own catch-up (gapless events). Inbox **snapshots**: each `activity.type=message` whose `payload.to_session` this device owns, plus the parent `session` row for that activity’s `session_id`. Pings **snapshots**: person-ping rows this login sent or received (not every team-visible ping). Subscription snapshots, only for origins this login may see under §6. Query answers are one-shot, also §6-filtered, and are not a pull. |
| Wiped device | Restore: `{own_events, inbox, pings}`. `inbox` = session-mail snapshots to sessions this device owns, each plus the parent `session` snapshot for that activity’s `session_id`. `pings` = person-ping snapshots this login sent or received. Snapshots, not a holey event stream. |
| Hub behind the device | The device pushes the missing **own** seqs. |

Rules:

- Own `origin_seq` is per device, strictly `last+1`. A gap is 409 / a hard local error. The exact same event is idempotent. The same seq with different content (including `occurred_at`) is a conflict.
- Foreign `origin_device_id` on push is 403.
- A push must not steal a replica row owned by another device (same `table`+`row_id`).
- **Pull of a subscription returns only matcher-passing snapshots**, never the rest of that origin. The matcher runs only inside origins this login may see under §6.
- Implicit **session mail** does **not** require `PUT` of a subscription. The hub fans those snapshots to the device that owns `payload.to_session`.
- Person pings this login **sent or received** are delivered the same way (snapshots on pull / WebSocket), independent of a subscription. Team-visible pings this login is not a party to stay on the website and on query; they are not laptop pull.
- Every inbox, restore, and live-push replica snapshot carries the hub-authenticated owning login on the row envelope as `github_login`. `_github_login` exists only in API/query payload copies. When the local replica applies a relayed `message` activity row, it drops any sender-supplied `_github_login` and `github_login` fields from the row payload, casefolds the envelope `github_login`, and stores that authenticated value as payload `_github_login` for mail policy to read. Other replica rows are unchanged.
- `GET /sync/restore` returns `own_events`, `inbox` (each session-mail snapshot addressed to a session this device owns, plus the parent `session` snapshot for that activity’s `session_id`), and `pings` (person-ping snapshots this login sent or received).
- `agent sync` is one push + one pull. `agent sync --follow` keeps going (WebSocket when used; a dropped socket is logged and retried with capped exponential backoff, not a process exit and not a silent poll).
- Missing hub URL or device token is a loud error. There is no default hub.

Matcher v1 (subscriptions): `AND` of equality or `IN` on allowlisted paths only (`type`, `payload.repo`, `payload.issue_key`, `payload.to_session`). Unknown paths are 400. The subscription set lives on the hub for that device; it is not a fan-out row in the event log.

## 10. Person pings and session mail

### Person pings

First-class rows, same bus, not a misuse of `open_work` or GitHub.

```text
agent ping send --to <github-login> --kind review-request|ping|question [--task UUID] [--note TEXT]
agent ping list
agent ping ack --id <uuid>
```

- Target is a **person** (GitHub login), not a session.
- Target must be visible to the sender.
- Ack is only allowed for the recipient.
- A ping is owned by the **sender’s** device. The recipient must **not** update that row locally. Ack goes to `POST /api/pings/{id}/ack` with the device token (or the website cookie). The hub records a recipient-side event and does not transfer ownership of the ping.
- Website-created pings are real store events (synthetic origin `web:<login>`), so pull delivers them. A replica-only write is not enough.

### Session mail

A session writes an `activity` row `type=message` with `payload.to_session` set to the **session id** of the recipient (not a login, not a device). Open session ids are unique on the hub; a colliding `session register` is 409. The hub resolves the id against the replica. The sender may write that row only if it is allowed to **see** the owning login of the target session under §6; otherwise the hub returns 403.

The hub delivers a snapshot to the device that owns that session. The local daemon `NOTIFY`s. The TUI knock is exactly:

```text
da ist Post id <activity-uuid>
```

The agent then `SELECT`s that id from local Postgres. The script never puts the body on the TUI.

Knock state machine (observable states only):

| State | Action |
|---|---|
| Idle, session in tmux, composer ready | `tmux send-keys -l` of the knock string, then Enter, to the **registered** pane |
| Model running | Stop hook: do not send-keys; feed the knock string so the agent reads the row |
| Unobservable (typing, permission modal, dashboard) | Queue; drain on next idle |
| Session not running | Unread until the next start; the agent `SELECT`s unread ids |
| No tmux and no ACP | No knock; unread until the next human turn |

`Runtime.is_busy` is the working probe of the interactive driver in the registered pane: the `interactive.pane.working` markers of that driver in `drivers.json`, or a complete `queued` marker group (a queued follow-up). “Model running” is that probe. The queue branch still runs when a runtime reports busy.

Ack of session mail is a **recipient-owned** `message.read` activity, not a mutation of the sender’s row.

## 11. Realtime

| Place | Mechanism |
|---|---|
| Local dashboard | Materialized rows after each local write. A short UI refresh is display only. |
| Website (browser cookie) | SSE (`/api/stream`): full replica **filtered by visibility** only. Not the laptop pull set. |
| This device (token) | WebSocket (`/sync/ws?token=…`): own events, session-mail inbox, person-ping snapshots this login sent or received, and subscriptions. |
| Local knock | `LISTEN` on channel `agent_inbox` after an activity insert that should wake a session (session-mail inbox, `pr.merged`, …), including replica apply. Payload = activity id (the uuid in `da ist Post id <uuid>`). |
| Scripts | `LISTEN` on `agent_work` (or poll `execution_status=pending`). |
| Offline | Own events queue locally. On reconnect: own catch-up, session-mail inbox, person-ping snapshots this login sent or received, and subscriptions. |

`agent sync --follow` stays on `/sync/ws?token=…` after one push+pull; a dropped socket is logged and retried with capped exponential backoff (not a process exit, not a silent poll fallback).

## 12. Auth and cookies

- Browser: signed session cookie after OAuth. `Secure` when `AGENT_CORE_PUBLIC_URL` is `https://`.
- CLI: device token from pairing. `Authorization: Bearer …`.
- Every `AGENT_CORE_*` environment variable is required. Missing or empty → process exit. No silent defaults.
- OAuth `next` may only be a `/pair?…` path. Open redirects are 400.
- Unauthenticated `POST /pair/prepare` is bounded (field lengths, expired rows deleted, pending-count cap).
- Pairing tokens are claimed in one locked transaction (one-shot).

## 13. Data class

| Data | Class | Why |
|---|---|---|
| Local store + `device.json` | Unique | Not rebuildable from git. |
| Hub event log + replicas | Unique | Only complete team copy; required for restore. |
| “Connected right now” | Rebuildable | Derived from open sockets. |

Evidence and command output travel with the replica. They are team-visible. Secrets do not belong in `evidence`.

## 14. Activity catalog

One table `activity`: `id`, `session_id`, `origin_device_id`, `type`, `payload` (object), `execution_status`, result columns, timestamps.

Session **tags** are the distinct `type` values (or the skill-facing tag the type declares) already present on that session.

The AI inserts intent (`execution_status=pending`). Scripts set result columns (`done` / `error`, external id, url). Unknown `type` → `execution_status=error`.

`mail.*` is the external mailbox. `message` / `message.read` is session-addressed mail on the bus (§10). The knock channel is `agent_inbox`.

v1 types (mechanism only):

| Type | Tag | Who writes | Who executes |
|---|---|---|---|
| `session.register` | — | later (v1 register is the `session` row) | — |
| `issue.write` | `issue` | AI | script |
| `pr.open` | `pr` | AI | script |
| `pr.merged` | `pr` | script | — (`NOTIFY` `agent_inbox` / `wake`; device daemon knock child) |
| `issue.assigned` | `issue` | script | — (no `NOTIFY` on insert; watch script knocks queue head / one interactive terminal) |
| `issue.assigned.ack` | `issue` | script | — (releases the next queued knock; supervise only) |
| `comment.post` | — | AI (target + body) | script |
| `review.post` | — | gate record (repo, number, body) | script |
| `mail.ingest` / `mail.seen` / `mail.reply` | — | script / AI | script (external mailbox) |
| `investigate.step` | `investigate` | AI (every step, immediately) | — |
| `message` | — | AI | hub + daemon + knock (session mail, §10) |
| `message.read` | — | AI or script | — |
| `query.request` / `query.result` | — | AI / script | script (hub HTTP) |
| `subscription.set` | — | AI or script | script |
| `usage.snapshot` | — | script | — (configured period-usage, key-spend or windows GET of a driver's usage adapter, on this device; no TUI knock). A missing or expired login of the usage session is not a scan failure: `agent watch session-usage` prints `usage.snapshot skipped`. A missing or expired windows login is not a scan failure either: `agent watch window-usage` prints `usage.snapshot skipped <account>: <reason>`. Usage-account failures, including HTTP 429, are reported and never stored as zero values. |
| `lane.run` | — | script | — (one lane process, including an unavailable output-file read; bounded redacted configured/reported models, token usage when supplied, bounded redacted tool transcript and final text; no argv, environment, config path or credential). Lanes ignore ambient proxy and CA variables; a usage adapter with `proxy_from_environment: false` uses `trust_env=False`. A windows adapter follows its configured `proxy_from_environment` setting. |
| `error.seen` | `error` | script | — (`NOTIFY` `agent_inbox`; error-fix skill, §21) |
| `error.skip` | `error` | AI | — |
| `error.fix` | `error` | AI | script + spine implement (draft pull request) |
| `supervise.event` | `supervise` | script | — (supervise follow bookkeeping / approve; optional closed-question path in tests; skip rows may carry a truncated pane excerpt; no TUI knock) |
| `order.event` | `order` | script | — (written by the coordinator at order transitions; persists until acknowledged; `agent order ack` or an answering reply, override, cancel or done sets the acknowledgement; no `NOTIFY`, no TUI knock) |
| `order.reply` | `order` | script (`agent order reply`, `agent order override`, `agent order cancel`, `agent order done`, or the same operations of the local dashboard) | script (consumed by the coordinator tick; a done mark of an order in state `failed` or `cancelled` is consumed by the operation itself when no tick of its worker runs) |

Both order types are script-only like `pr.merged`; `agent activity add` refuses
them and a model never writes them (§19.10).

`investigate` is the thick log: hypothesis, check, result, ruled out, still open — each a new row, at once. Other sessions can query or subscribe and see what was already tried.

### Outside facts (example: PR merged)

The agent never learns a merge from a human prompt and never calls GitHub to ask “is it merged?”.

When this device has a `pr.open` row whose script result includes the PR number/url, a **script** watches that PR. On merge it inserts `pr.merged` on the **same session** (`payload`: repo, number, url, merge SHA, merged_at). That insert `NOTIFY`s `agent_inbox` (and enqueues `wake` if needed) with the new activity id. The device daemon’s knock child and §10 state machine emit `da ist Post id <uuid>`. The watcher does not `tmux send-keys` itself. The agent `SELECT`s the row and supplies any analysis or implementation result; the script owns workflow progression (§19.1).

The watcher runs on this device (write owner). It is a script, not the model. The model’s next turn is the knock plus the row — not a `gh` command.

`wake` (if stored) is a local queue row for the knock; it is not a hub event.

### Outside facts (example: issue assigned)

The script reads GitHub; the model does not. The following describes the existing
assignment watcher. The required end-to-end workflow and its script-only
responsibilities are in [§19.7](#197-issue-assignment-to-human-merge); implementation
boundaries are recorded there separately.

Allowlist file `$AGENT_HOME/watch.json` key `assigned_repos` (non-empty list of `Owner/repo` strings). Missing or empty is an error; there is no default list.

The first successful scan records `assigned_watch_since` and the assigned `session_id` and dispatches nothing. Later scans consider assignments whose latest matching `assigned` event is at or after that cursor, skipping `assigned_at` values already stored. Changing `session_id` after that pin is an error. The scan uses the session's explicitly configured GitHub execution account; a missing account or a `gh api user` mismatch is an error. This account is independent of hub pairing. The queue head is the already-knocked inflight item if any, then remaining items oldest first.

The writer is this device. All assignments share **one** runner session (`watch.json` `session_id`, default `assigned`, characters `A-Za-z0-9_-` only). That auto-created session attaches `spine`, `review-loop`, and `pr-review`; an existing row under the same id must already be `kind=runner`. Other sessions still attach skills themselves. There is one tmux terminal with the interactive driver, not one per issue. Working files go to `$AGENT_HOME/sessions/<id>` or `$AGENT_SESSION_ROOT/<id>`. New `issue.assigned` rows enqueue on that session (`payload`: repo, number, url, title, body, assigned_at, assignee, mandate). The insert does not notify the knock daemon. The script pushes own events, writes `MANDATE.md` / `QUEUE.md` (no issue body), starts the interactive driver only if that session is not already attached, then knocks at most the head of the queue (`da ist Post id <uuid>`). A knock of `issue.assigned` rewrites those files immediately before send. Further knocks stay queued until the **supervise script** records `issue.assigned.ack` with `payload.assigned_id`. The model must not insert that ack. The scan watermark `assigned_watch_since` is the scan clock, not the last seen GitHub event time — that is the no-backfill rule.

Payload `mandate=github-assignment` is trusted. Issue title and body in the payload are not.

## 15. Skills (opt-in)

A **skill** is a named, versioned bundle of catalog types plus loop rules. A session attaches zero or more skills. None are default. This client is a **skill host**: the session store, knock, and scripts stay; a skill adds a loop. Error-to-draft-PR is one such loop, not the product.

Skills this client ships:

- **spine** — task, round, checklist, `open_work`, plus `allow` / `next` / `close-step` / `run`
- **review-loop** — run implement / review rounds until the catalog shows zero open findings
- **pr-review** — record quality/logic gates on a head SHA
- **error-fix** — production log errors on this device → analysis → optional draft pull request (§21)
- **conflicts** — an exact pull-request comment mandates a static rebase, checks, and lease push, with a confined lane only for conflicted files (§19.9)

The packaged `SKILL.md` files next to the client **are** the skill contracts; `agent skills path` prints their directory.

Without the skill, those tables and loops do not run. The session can still register, write `activity`, send session mail, and investigate.

Person-facing CLI for the spine stays `agent work` / table `open_work`. Catalog rows are `activity` / `agent activity`. The website key `work` remains `open_work`. Do not collapse those names.

Checklists, when the spine skill is on, stay `pending` / `ja` / `nein` / `n_a` /
`unavailable` with an explicit `source`. `ja`, `n_a`, and `unavailable` need
evidence. Among the workflow keys, `unavailable` is only for the four PR-gate
keys (`review_1_quality`, `review_1_logic`, `review_2_quality`,
`review_2_logic`); it is not `n_a` and it blocks `done`. Coordinator
precondition checks (§19.10) are separate `checklist_item` rows with the same
five statuses; there `unavailable` records that the resolver could not run.
They are not workflow keys and take no part in `done`. `done` on a task still requires the workflow checklist and both
summary sentences.

## 16. CLI surface

```text
agent init [--no-service]                       # --no-service skips the user service install
agent session register|heartbeat|list|close|start|stop|input|keep-working|skill
agent skills path
agent session register --id ID --kind human|runner|other [--skill NAME]…
agent session skill attach --id ID --skill spine|review-loop|pr-review|error-fix|conflicts
agent session skill list --id ID
agent session start --id ID [--provider DRIVER] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session stop --id ID
agent session input --id ID --data TEXT
agent session input --id ID --key enter|ctrl-c|tab
agent session keep-working --id ID [--once|--follow]
# permission modal: Enter. first idle composer: standing "work until complete";
# later idle: "Continue.". never types while in-flight or when the pane is empty.
# default / --once is one tick; --follow polls every 30s.
agent activity add --session ID --type TYPE --payload-file FILE
agent task create|list|show|state|summary|pr|models|... # spine skill
agent task close-from-pr (--id ID | --session ID) # verified human merge; device only
agent checklist set …                             # spine skill
agent round start --task UUID                     # spine skill
agent agent start|finish …                        # review-loop (implementer|reviewer) or pr-review (pr-reviewer-*)
agent check record …                              # spine skill
agent small-change --task UUID [--repo PATH] [--json] # spine skill; exit 0 small, 2 not small
agent local-ci verify|parse|render [--file PATH] [--require-ids id,id] [--expect-head SHA] [--expect-private] [--json]
agent a38 policy --file PATH                    # validate an A38 manifest
agent a38 run --repo PATH --policy PATH --base-sha SHA --output PATH --logs-dir PATH
agent a38 verify --policy PATH --file PATH --repo OWNER/NAME --head SHA --private
agent pr-guard --repo OWNER/NAME --pr NUMBER [--dry-run]
agent pr-guard --repo OWNER/NAME --all-open [--dry-run]
agent gate record …                               # pr-review skill
agent work add|set|list …                         # spine skill (open_work)
agent allow|next|close-step|run …                 # spine skill; run: [--dry-run] [--head SHA] [--cwd PATH] [--spec-file PATH] [--no-tmux]
agent github pending                           # one scan; pr.open, comment.post, review.post, issue.write via gh
agent query --match-file PATH                  # hub POST /sync/query; prints {"rows":[…]}
agent subscribe list|set --file PATH|clear     # hub GET/PUT /sync/subscriptions
agent mail pending|ingest                      # mail.reply / mail.seen via himalaya; envelope ingest
agent pair --hub URL [--name HOST] [--timeout SEC]
agent sync [--follow]
agent restore
agent ping send|list|ack
agent assign --no-deviation --agent LOGIN --repo OWNER/NAME --title TEXT (--body TEXT|--body-file PATH)
agent daemon [--install|--uninstall]           # always-on supervisor; init installs the user service
agent knock [--once]                           # --once drains; without --once is foreground; user service is the supported always-on path
agent watch pr-merged                          # one scan; device daemon covers the loop
agent watch pending                            # one scan; LISTEN agent_work / execute subscription.set and query.request
agent watch session-usage                      # one scan; knock child (under the device daemon) polls every 60s
agent watch account-usage                      # one scan of explicitly configured usage accounts
agent watch window-usage                       # one scan of explicitly configured window-usage accounts
agent watch assigned [--follow]                # allowlisted GitHub assignments → runner session + knock
agent watch errors                             # one scan; $AGENT_HOME/error-fix.json; knock daemon polls with session-usage
agent watch error-fix                         # one scan; find-or-create implement task + isolated worktree; knock daemon polls with session-usage
agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]
agent coordinate --session ID [--follow]             # static issue and order workflow; coordinator.json
agent conflicts --session ID [--once|--follow] [--dry-run]
agent order submit --repo OWNER/NAME --title TEXT --body-file PATH [--base BRANCH] [--worker SESSION] [--notify-session ID] [--skip-check KEY --reason TEXT]... [--max-rounds N] [--json]
agent order list [--open] [--json]
agent order show --id UUID [--json]
agent order events [--after CURSOR] [--notify-session ID] [--wake-only] [--unacked] [--json]
agent order wait [--after CURSOR] [--notify-session ID] [--timeout SECONDS]
agent order ack --event ID
agent order reply --id UUID --body-file PATH
agent order override --id UUID --check KEY --reason TEXT
agent order cancel --id UUID --reason TEXT
agent order done --id UUID --reason TEXT
agent order report --id UUID [--json]
agent status
agent dashboard [--port 7845]
agent cli-bridge [--port 7846]
```

Local dashboard binds `127.0.0.1` only.
Its order routes are described in §17.

The AI must not call hub HTTP, `gh`, or himalaya; scripts perform these operations. It inserts `activity` (and `query.request` / `subscription.set` / `mail.reply` / `mail.seen`). Scripts watch the store. `agent github pending` is the GitHub executor for owned pending `pr.open`, `comment.post`, `review.post`, and `issue.write` rows. `agent mail pending` is the mailbox executor for owned pending `mail.reply` and `mail.seen` rows. The knock poll (device daemon knock child) also runs `scan_github` and `scan_mail`. `agent run` git-pushes (no force) when `pushed` is open and measures GitHub mergeability and checks when `mergeable` is open.

## 17. Control

One product with the hub. The team reads every visible session; **this device writes and controls only its own rows**. A foreign session on the local dashboard is watch-only.

**Live terminal bytes are not store events.** They travel on the sync WebSocket as ephemeral `terminal` frames (base64 pane captures) while `agent sync --follow` is connected. They are never written into the event log.

**tmux is the process holder; the hub is not.** The local client is the only place that starts, stops, or types into a live terminal. The hub may send `control` frames (`start` / `stop` / `input` / `resize`); this device executes them only when it owns the session row, then replies with `control-ack`. After connect, the client sends `control-ready`. Control and terminal message types must not trigger push+pull.

A local file `$AGENT_HOME/runtime-targets.json` may map a session id to an argv list prepended to every `tmux` invocation for that session. A missing key means tmux runs on this device as today. The file is not a hub event and is not stored on the session row. One Runtime instance looks the prefix up per session id so mixed local and prefixed sessions can share knock, keep-working, and control.

The daemon also serves `agent cli-bridge` on loopback (default `127.0.0.1:7846`; bind `AGENT_CLI_BRIDGE_BIND` must be `127.0.0.1` or `::1`). A remote process may send `{"argv":[…]}` (`AGENT_CLI_BRIDGE=host:port` on the stub) and receive stdout/stderr/exit. The allowlist is store and spine only (`session register|heartbeat|list|close|skill`, `task`, `next`, `close-step`, `gate`, `check`, `models`, `status`, `skills`, …). Control (`session start|input|keep-working`), `run`, GitHub, mail, pair, and sync are refused. Git and `gh` stay in the process that owns the worktree. The bridge is not a hub event.

Owned-row runtime fields (start/stop set control and tmux; `keep-working` may also update `keep_working`; not a new vendor):

```json
"runtime": {
  "tmux_session": "agent-…",
  "control": "attached" | "stopped",
  "cols": 80,
  "rows": 24,
  "provider": "driver-a",
  "interactive_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "model": "operator-selected-model",
  "keep_working": { "standing_sent": true }
}
```

Start sets `control=attached` and the tmux name. Stop sets `control=stopped` and keeps the name. `agent session keep-working` updates `runtime.keep_working.standing_sent` on idle ticks so the standing instruction is sent once. Session `status` (`active` / `closed`) is separate; `session close` stays as it is.

**Interactive driver launch** (`--provider DRIVER` or control `{provider: "DRIVER"}`, or the dashboard's "Start AI session" with `{interactive: true}`) resolves
an explicit interactive role from `ai-accounts.json`; its driver must have an
`interactive` section in `drivers.json`. The model and provider
profile directory are required configuration; there is no built-in model.
The session id format is a UUID, so first start creates `runtime.interactive_session_id`
(and `runtime.interactive_start_pending` until the tmux session is attached) and
prints `interactive=<uuid>`. Runtimes of an earlier version that stored these
under keys named after the driver are read as the neutral keys and rewritten on
the next session write.
A later start may resume that ID only with the same recorded AI binding
(role, account, provider, model, access, and configuration-directory hash).
Changing that binding, or adopting an old unbound ID, requires a new session.
Provider credential environment variables are cleared in the child process,
and the driver's `home_env` selects the configured profile. `--provider` and `--cmd` cannot
be combined; `--model` requires `--provider` and, when supplied, must match the configured role.
A read-only role appends the driver's `interactive.read_only_args`; every start
appends its `interactive.hardening_args`.
Interactive provider starts require the current device to be a declared lane
host. The local harness's lane starter starts them on that external host;
Agent does not provide its own remote lane transport or workspace mirroring.
Interactive tmux sessions may still use `$AGENT_HOME/runtime-targets.json` after
the `models` and `lane_execution` checks pass.

**Lane providers are the drivers with a `lane` section in `drivers.json`; `ai-accounts.json` supplies the default for the model slots `first` and `second` when the task is created, and the pin on the task governs and can be changed explicitly with `agent task models` (not for coordinator tasks).** A process running inside a tmux pane is not a store vendor. The interactive runtime is the driver's `interactive` section; a lane is the headless `lane` section of a driver, not this tmux pane. A driver with `lane.credential_file` reads its key only inside a generated shell wrapper; the key is not argv or lane evidence. There is no shell-string tmux driver: the runtime invokes `tmux` with argv lists only. `runtime.provider` is launch metadata, not a review-gate vendor.

Local dashboard (`127.0.0.1`): the main page (`/`) keeps the orders and order-events sections — the order table, detail and events — and links to the new-order form on its own page, `/orders/new`, and to one loopback subpage per remaining section (`/usage`, labelled Subscriptions, which joins the optional `ai-subscriptions.json` inventory with the newest usage snapshots and shows one compact line per provider, summarised on the main page, `/sessions`, `/tasks`, `/rounds`, `/agents`, `/checks`, `/gates`, `/work`, `/pings`). Sessions expose `can_control` when `_origin_device_id` is this device. `POST /api/sessions/{id}/control` applies the same actions locally and does not call the hub. Order reads use `GET /api/orders/<name>` and order writes use `POST /api/orders/<name>` with a JSON object. `GET /api/orders/schema` returns `enabled`, `skippable`, and `operations`. These routes call the same order operations as the command line. Every route below `/api/orders/` requires `Host` to be `127.0.0.1:<port>` or `localhost:<port>`, with any port. When `Origin` is present and nonempty, it must equal `http://` plus that `Host`; otherwise the route returns 403 with `cross-origin request refused`. The blocking `wait` operation has no dashboard route; the dashboard polls `events`.

The pages change only the display; routes, lookups and stored data keep the full values. A UUID shows as its first 8 characters on a button that copies the full value and then shows `Copied` or `Copy failed` for 1.5 seconds; it never selects the row it sits in. A refresh rewrites a table or chip only when its content changed, and a keyboard focus on a replaced copy button moves to the same occurrence of that value without scrolling. Every state or status shows an icon from one mapping table, with the word as the cell's `title` and `aria-label`, and a neutral fallback icon for an unknown word. In the order views a repository shows as `Organization` and `Repo`, a pull request URL, with or without one trailing slash, as a `#NUMBER` link, and `updated_at` as `updated_before`, the elapsed time with the timestamp as the cell's `title`; an ISO date-time without a zone designator counts as UTC, like the stored timestamps, and a missing or empty `updated_at` shows `—`. The orders list leaves out `head`, which the order detail and report keep. The three pages carry an identical shared script block for this, because each page stays one self-contained file.

The knock in §10 uses the **registered pane**, not `tmux_session` derived from the session id. `agent session input` remains argv-only (`send-keys -l` then a separate Enter).

A human owner may issue work with `agent assign` from a device paired to that
owner (never an agent's own device). The client sends the paired device bearer
only to the configured hub's `POST /api/assignments`; the hub authorizes the
human owner, creates and verifies the GitHub issue assignment, and records
`assignment.created` under origin `web:<login>`, as §2 describes. The client sends
the repository exactly as supplied; the hub canonicalizes its repository
spelling. `agent assign` requires `--no-deviation` and sends
`deviation: {"declared": false}`; a deviation instead needs an explicit grant
on the issue. The client does not print or persist the bearer anywhere new, and
hub failure detail or recovery URLs are bounded and redacted after the exact
bearer is removed. A successful response is accepted only with a positive,
non-boolean issue number and a credential-free HTTPS GitHub issue URL that names
the same repository and number.

## 18. What “done” for the product still needs (operators)

These are not silent defaults in code; they are human steps after merge:

1. Merge the public pull requests.
2. Create a GitHub OAuth App whose callback is `{public-url}/auth/github/callback`.
3. Deploy `agent-core` with every `AGENT_CORE_*` variable set.
4. Add GitHub logins to `teams.yaml` via pull request.
5. On each laptop: PostgreSQL 15+ (`initdb`/`pg_ctl` on `PATH`, or `AGENT_PG_BIN` / `AGENT_PG_DSN`), `pip install -e .`, `agent init` (installs and starts the user-service daemon for knock, usage, pending, github pending, mail pending, `pr.merged`, the local dashboard, and cli-bridge; daemon `sync --follow` starts only after pair, once `device.json` has token and hub URL), `agent pair --hub …`. Do not leave a separate `agent knock` or `agent sync --follow` as the always-on path; one-shot `agent sync` remains fine after pairing. On a headless host where the agent user never has a GUI login, use `agent init --no-service` and put the printed daemon argv and environment under an operator-provided system supervisor (for example a launchd LaunchDaemon with `UserName`).

Later product work (not required to operate v1 after merge):

- Two local Postgres roles with password auth on a socket under `$AGENT_HOME` (AI vs scripts).
- `activity` type `session.register` (v1 records the `session` row only).

## 19. Deterministic core

This client is a session store that may employ AI. It is not an AI agent with scripts attached.

The rules below were already implied by §§1–17. They are now explicit so a later coding-automation draft cannot invert them.

**Scripts execute. Checks measure. Gates decide. This device owns its rows. AI reasons and proposes. Humans retain every authority this client has not delegated — and this client delegates merge to no one.**

### 19.1 Responsibility split

| Work | Who | Store mechanism |
|---|---|---|
| Semantic diagnosis, hypothesis, patch text, review findings, PR title/body draft | AI | `activity` intent (`investigate.step`, …); implementer/reviewer rows when those skills are on |
| Auth, Git, GitHub HTTP, CI status, mail, hub HTTP, tmux knock, retries | Script | Result columns on the intent row; `pr.merged` and other watch types; `LISTEN agent_work` |
| Assignment acceptance and its confirmation in the GitHub issue, before starting the implementation lane | Static script on the execution device | Required ordering; see §19.7 |
| Start implementation and review lanes, restart the implementer for improvements, and start any other agents | Static script on the execution device | The model never orchestrates another model |
| Execute tests, builds, and checks, including local tests | Static script on the execution device | Script-produced results supplied to the lanes |
| Monitor or wait for CI, reviews, merges, and other external events; notify a lane when there is useful work | Static script on the execution device | Script-observed facts, not a waiting or polling model |
| Preconditions of every coordinator action | Static script on the execution device | Registry of checks; one resolver per check; facts in the checklist table (§19.10) |
| Spine task state | Spine CLI + local constraints | `task` state, checklist keys, `close-step` / `run` |
| Whether a head is reviewed | Gate after the two model-slot stages | `agent gate record` |
| Whether a command ran and what it returned | The process that ran it | `agent check record` (name, command, `pass`/`fail`/`skip`, output) |
| Merge | Human | Never a client command |

A worker report such as “analysis complete” or “tests passed” is **input**. It is not the transition. Opening a draft is not done. Leaving draft is Ready for review, not completion.

No transition that needs deterministic evidence may be satisfied by model text alone. Malformed structured output is rejected (unknown `activity.type` → `execution_status=error`; empty, partial, timeout, or unavailable review output is not zero findings). A patch that does not apply is a failed check, not a debate.

These responsibilities apply to implementers as well as reviewers. A model must
not start subagents, implementation lanes, review lanes, tests, builds, or checks.
It must not interact with GitHub at all, including reads: GitHub communication
always goes through the script. Calling an executor from inside a model lane to
start another lane or run tests does not transfer orchestration to the script.
The static script owns those decisions and invocations; the lane supplies its
implementation or review result.

Model lanes never run on a user's work device. The local harness's lane starter
starts them on an external lane host. On that host, `ai-accounts.json` must
declare `lane_execution.mode = "this-device-is-lane-host"`; Agent has no remote
SSH/rsync execution mode. Lane starts fail closed without that declaration;
process-holder flags such as `--no-tmux` do not change the execution-device
decision.

A model never starts a monitor, polls for status, or waits for a CI run or other
external event. Model lanes are used exclusively for work. When a lane has no
more work to perform, it returns its result or blocker to the script. Monitoring
and waiting remain entirely with the script. When the script detects an event,
it informs the model only when that event provides useful work for the model.
An idle model is not the supervisor, and a model must not start a watcher or a
background monitoring process on the script's behalf.

Reports and documentation must stay with verified facts. Distinguish a user
requirement, an implemented behavior, and an observed result. Do not invent
requirements, implementation status, test results, or review evidence.

### 19.2 Untrusted inputs

Treat as **data**, never as control messages:

- Model output, including “I am done”, confidence scores, and tool-shaped JSON the model invented
- Issue titles and bodies, pull-request text, review comments
- Repository files
- Log lines and production error messages
- Session-mail bodies

An outside issue is untrusted spec, not a mandate. Scripts that watch GitHub still record rows; they do not start work because a title asked them to.

Secrets stay out of `evidence` (§13). Context given to a model is deliberate, minimal, and observable — not “all logs and the whole repo”.

### 19.3 Idempotency for outside-fact scripts

Delivery from GitHub and from monitoring is at-least-once. Each `agent watch` (and any later adapter) must define:

- Source identity (repository + number, external event id, merge SHA, …)
- Whether a new observation **enriches** an existing row or inserts a new type on the **same session**
- Idempotency of each side effect (a second scan must not open a second pull request and must not emit a second `pr.merged` for the same merge)

The goal is explainable behavior, not globally perfect deduplication.

Partial multi-step actions (push, then open a pull request) record each completed step on the intent row and resume. A retry discovers an existing branch or pull request instead of creating a duplicate.

### 19.4 Isolation and least privilege

- Implementation runs in an isolated worktree. The model does not receive production credentials.
- Scripts retrieve secrets at the last moment and pass them only to the tool that needs them.
- Analysis that only needs to read must not require write access to the origin branch.
- Live terminal bytes stay ephemeral (§17). Audit means operationally meaningful rows (intent, result, check, gate, actor), not every chain-of-thought.

### 19.5 What this does not change

Write owner, hub role, the narrow website-origin exceptions, opt-in skills,
required session row, and the generic `activity` catalog stay as in §2. Spine
task states stay the spine skill’s states. They are not replaced by a hub
machine such as `CREATED` / `ANALYZING` / `READY_FOR_PR`.
The terminal `merged` state records a verified human merge whose remaining
checklist evidence still blocks `done`; only `task close-from-pr` writes it.

### 19.6 Ready for review

Draft publication timing is [docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md): open the draft as soon as the first signed task commit exists; full tests and reviews are Ready-for-review gates, not draft gates. A draft plus local tests is not done. A check records the local suite. When spine and pr-review are attached, a gate records each vendor dimension on **this** head, and leave-draft (Ready for review) needs two review runs. Each names this head, or a commit after the pull request base that this head still contains with only `.md` files changed in between. CI on this head must be green. Two review runs are required. The first review run is required, and so is the second. One review is enough only when the pull request opener is listed in one_review_authors in the trusted .github/pr-guard.json on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count. Without those skills, the target repository’s written contributing rules apply. `agent allow --action pr-ready` only checks that a task is in `pushing` or `pr-review`; it is not the leave-draft verdict. `task-done` still needs the workflow checklist and both summary sentences; ledger `task-done` is not pull-request completion.

Quality and logic of one model slot run together. The order is model slot `first`, then model slot `second`. Which provider (a driver with a `lane` section in `drivers.json`) fills a review slot is device configuration in `ai-accounts.json` (`models`); each task pins the device's `models` when it is created, with that file supplying the default. Later checks use the pinned values. To run a slot with another provider, change the pin with `agent task models --id <uuid> --slot first|second|implementer --vendor <provider> --reason "<why>"`; the change is recorded on the task and applies to later agent rows of that slot, and for a review slot (`first`, `second`) also to later gate rows, while rows already recorded keep their vendor. For a PR reviewer that cannot run, the order is `agent agent finish --id <reviewer-uuid> --verdict unavailable` for its row, then `agent gate record --verdict unavailable`, then `agent close-step --status unavailable` for the matching gate key, and then the pin change; do not record `approved`. For an implementer that cannot run, the order is `agent agent finish --id <implementer-uuid> --verdict blocked` for its row (the task becomes `failed`), then the implementer pin change with `agent task models`, then `agent round start`. Never record a gate for a provider the task does not pin; do not substitute another vendor until the pin is changed, and never substitute silently or automatically. `agent task models` refuses `done`, `merged` and `superseded` tasks, and coordinator tasks. Empty, partial, timeout, or unavailable review output is not zero findings. The session that authored the diff does not sit those PR reviews.

Besides the `one_review_authors` one-review exception above, the only other
second-slot exception is a coordinator A38 task pinned with
`models.second: null`. Its second review is an external review by a team member
delivered as session mail, bound to the approved `review-1` head and recorded as
`review_2_*` checklist evidence. The coordinator launches no second-slot model
for that task. `models.second: null` is refused everywhere else.

An A38 coordinator worker that sets `a38_second_review: "model"` is no
exception. Its tasks pin a provider in `models.second`, and the script runs the
second pair. For an order the second review is the check `review_2` (§19.10).
Only an override that names it waives it, and only where the
`one_review_authors` exception above applies.

CI on this head is a script-measured fact whose applicability comes from the target repository's written rules. The frozen `dfx-local-ci/v1` format and legacy `agent local-ci verify` behavior do not by themselves adopt A38 or grant permission to skip GitHub CI. For A38 adopters, follow the central [A38 standard](docs/a38.md) and [guard guide](docs/a38-guard.md): private visibility alone is not opt-in, and private local code-gate equivalence requires trusted-base opt-in through a valid A38 manifest, assessment against the canonical active policy, and a separate live join against the newest author report that meets the requirements and was created after the head commit, falling back to an older qualifying report when a newer one does not meet them. Public A38 adopters publish and validate the author report in addition to retaining cumulative GitHub CI; private repositories without that trusted-base opt-in and non-A38 repositories retain their existing written CI rules. For applicable GitHub CI checks, `skipped` and `cancelled` are not green unless the workflow documents that skip. A verified author A38 report on this head may treat a matching required check that concluded `skipped` or `neutral` as green; `cancelled` and failed still block. Independently required GitHub-only checks, technical merge restrictions, review gates, and human merge remain required. Stay draft until the applicable rules hold. Two review runs are required. The first review run is required, and so is the second. One review is enough only when the pull request opener is listed in one_review_authors in the trusted .github/pr-guard.json on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count. One comment whose lanes are the first review run, and a later comment whose lanes are the second review run. The first comment stays valid when a later commit still contains that head, that head is after the pull request base, and every file changed between it and the current head ends with `.md`. Then mark Ready for review (`isDraft=false`). A retry reuses the existing draft. Ready for review is still not merge and not completion. A human merges; claim completion only after that merge is verified.

### 19.7 Issue assignment to human merge

**Required workflow.** The user creates an issue on GitHub, assigns it to an
agent account, and receives a pull request ready for human review and merge on
GitHub. The static script runs on the configured execution device. Deployment
hostnames belong in the deployment configuration, not this public repository.

1. The script detects assignments in explicit repositories or repositories of
   explicitly approved GitHub owners. Owner discovery uses one owner-qualified
   search per configured owner and fails that owner's scan when GitHub reports
   incomplete results or a result count larger than the fetched set. For an
   owner-scoped repository it pins
   the default branch and a direct-push or verified fork publication route at
   acceptance. When configured to require a hub mandate, it accepts only a
   matching `assignment.created` query row. It queries the configured
   repository spelling plus issue number as `payload.issue_key`, its
   case-folded form, and GitHub's canonical `<owner>/<name>#<number>` spelling
   from the issue or assignment-event response, then compares repositories
   case-insensitively. The compatible hub adds that canonical issue key to every
   `assignment.created` payload. The row must have envelope `github_login`
   equal to the GitHub assignment-event actor and an authorized reply login,
   `origin_device_id` exactly equal to `web:<envelope github_login>`, and inner
   payload `actor` case-insensitively equal to that envelope login. A matching
   device-origin row is not a mandate. This property requires a hub that refuses
   device ids and pushed origins beginning with `web:`; only the hub may create
   that named website origin. A definite no-match retries for 120
   seconds from `assignment_at`. A capped query (at least 500 rows) is never a
   definite no-match; it is a recoverable configuration blocker published once
   with a reply checkpoint, as are unpaired devices and hub HTTP 400/401/403
   results. Other hub failures retry and publish at most one blocker per hour.
   The script then
   confirms acceptance in the GitHub issue. This acceptance and confirmation
   are script work, never model work, and happen before the script starts the
   implementation lane.
   A local order submitted on the execution device is a second source of work.
   Its mandate is that local submission by the device's paired GitHub login.
   The script verifies no hub assignment for it, and acceptance posts nothing
   on GitHub.
2. The script starts the implementation lane with the assigned work. The model
   implements the change and returns its result to the script.
3. The script starts the review lane. When improvements are needed, the script
   starts the implementer again with the findings. The script owns every lane
   start in the implementation/review loop; no model starts another model.
4. The script executes the required tests and checks and supplies the results to
   the lanes. The implementer may change tests as part of the implementation but
   may not execute them. Local test execution and any additional agents are
   exclusively script responsibilities.
5. The script performs all GitHub communication, including issue and PR reads,
   comments, PR publication, and status updates. A lane may provide content for
   the script, but never communicates with GitHub itself. User-facing questions
   and blockers, when needed, are communicated through GitHub by the script;
   GitHub replies return through the script. The user need not operate a terminal
   or a separate dashboard to manage the assigned issue.
   Questions and blockers of an order go to its order events and, once the
   draft exists, to pull-request comments. Answers return through `agent order
   reply`, `agent order override`, `agent order cancel`, `agent order done`, or
   an authorized pull-request comment.
   When the lane has no further work, it returns to the script; it does not
   monitor GitHub or wait for CI. The script observes subsequent events and
   informs a lane when further work is useful.
6. Publication and readiness follow the existing
   [pull-request lifecycle](docs/pull-request-lifecycle.md): the script publishes
   a draft immediately after the first signed task commit, while tests and
   reviews may still be pending. The configured readiness owner marks Ready for
   review only after the required checks and reviews hold on the final head: the
   coordinator does so for the ordinary path, while the PR guard alone does so
   for explicit coordinator A38 mode. These steps do not
   wait for this numbered list to finish before publishing the first draft.
   For orders, and for issue workers with `"draft_on_accept": true`, the script
   first creates and pushes one signed empty commit with message `Open the draft
   pull request.` and opens the draft before implementation. That empty start
   commit is allowed only on these paths. A `no-change` result while it is the
   only commit closes the draft and fails the task.
7. The human reviews and merges on GitHub. Completion requires verification of
   that human merge, as defined in the lifecycle.

**Existing implementation boundaries.** This requirement is not a claim that
the workflow is enabled on a deployment:

- The optional [`coordinator.py`](src/agent_cli/coordinator.py) entrypoint and
  [`coordinator_runtime.py`](src/agent_cli/coordinator_runtime.py) provide the
  script-owned issue workflow. Its explicit device configuration, evidence
  requirements and recovery boundaries are described in
  [docs/issue-coordinator.md](docs/issue-coordinator.md). `agent coordinate
  --session ID` performs a bounded advancement; `--follow` is the script's loop.
  No worker, account or role is installed by default.

- [`hub_assign.py`](src/agent_cli/hub_assign.py) implements the paired-device
  `agent assign` request to the hub. The hub, not a model lane, owns GitHub issue
  creation and the `assignment.created` web event.

- [`watch.py`](src/agent_cli/watch.py) implements assignment scanning, a queue,
  workspace files, and session dispatch. `dispatch_assigned` does not publish an
  acceptance comment before starting the session.
- [`daemon.py`](src/agent_cli/daemon.py) starts knock, dashboard, CLI bridge, and
  paired sync, plus each explicitly configured coordinator worker. Changes to
  the daemon's worker set require a daemon restart. A daemon or coordinator
  restart does not interrupt a running model lane whose process record is
  already durable. A stop before that record is written ends the lane. The
  coordinator started afterwards adopts that lane by its saved process id and
  start time, waits for it, and records its result once. Check and test jobs
  still stop with the coordinator. The daemon does not start the legacy
  `agent watch assigned --follow`. Legacy dispatch and `supervise` refuse
  sessions selected for the coordinator.
- `agent init --no-service` creates the same local store and identity without
  installing a user service, so a headless host's system supervisor can run the
  unchanged `agent daemon` process with the printed argv and environment. It is
  refused while a user service is installed for the same `AGENT_HOME`, and stops
  when an existing unit cannot be read or records no `AGENT_HOME`.
- The shipped `ask=False` path in
  [`supervise.py`](src/agent_cli/supervise.py) does not acknowledge completion of
  an assignment from verified PR results. Its optional closed-question path is
  not that verification.
- [`github_act.py`](src/agent_cli/github_act.py) provides the GitHub activity
  executor. Its existence alone does not establish that model lanes cannot
  access GitHub or execute tests or other agents.
- An explicitly configured coordinator A38 worker keeps the same deterministic
  boundary with a different readiness owner. With the external second review,
  `a38_second_review: "external"`, which is the default, its tasks are pinned
  with `models.second: null`: the script runs and verifies the repository A38 suite,
  publishes the author report and two validated author review comments, and
  relays an allowlisted external team-member second-review result from session
  mail. That result is bound to the approved `review-1` head and recorded as
  `review_2_*` checklist evidence; no second-slot model is launched. For that
  review path the hub remains the sanctioned
  person-ping/session-mail relay; a required assignment mandate is separately
  verified from its website-origin `assignment.created` event with the envelope,
  `web:<login>` origin, issue key, and inner actor all bound to the same login.
  The owner-scoped A38 default requires that mandate even when the general hub
  assignment option is disabled, and closes the deviation keys only when its
  inner payload is exactly `deviation: {"declared": false, "stated_by":
  "<same actor>"}`. For an order on that default there is no hub mandate. The
  script closes
  `deviation_declared` and `deviation_granted` as `n_a` from the local
  submission, with evidence that orders declare no deviation. With
  `a38_second_review: "model"`, the task pins `models.second`; after the first
  review comment the script runs the second-slot quality and logic lanes and
  publishes their guard-format second comment as the pull-request author. A
  rejection blocks merge and returns every rejecting dimension to the
  implementer. The guard still owns Ready. Before the
  owner-scoped A38 default writes `contributing_ok=ja` with `conformity-a
  approved at <head>`, it requires the recorded `review-1` quality gate to be
  approved on that exact head. The coordinator never posts a formal
  approval and never marks the pull request Ready; it observes the PR guard
  making the unchanged head Ready, then waits for the verified human merge. If
  the human merge arrives during the A38 second-review, CI, readiness, or Ready
  wait, merge observation runs the same readiness closing on that merged head
  before `task-done` only when the merged base equals the promoted evidence base
  and a report or waiver plus the local gate are bound to that base, unless
  readiness already ran on exactly that head. A pending base, a missing evidence
  base, or a base move first observed after merge instead stops at a
  non-recoverable `task-done` boundary and does not run readiness. Non-A38 workers
  retain the formal-review and leave-draft path described above.

- [`preconditions.py`](src/agent_cli/preconditions.py) declares the action and
  check registry. [`coordinator_checks.py`](src/agent_cli/coordinator_checks.py)
  contains static resolvers, and
  [`coordinator_check_lane.py`](src/agent_cli/coordinator_check_lane.py)
  contains the single-purpose check lane.

- [`order_ops.py`](src/agent_cli/order_ops.py) is the shared table of order
  operations. [`orders.py`](src/agent_cli/orders.py) generates the command-line
  interface from it, and
  [`dashboard_orders.py`](src/agent_cli/dashboard_orders.py) exposes the same
  operations locally. [`coordinator_orders.py`](src/agent_cli/coordinator_orders.py)
  owns order creation, events and answers;
  [`order_report.py`](src/agent_cli/order_report.py) compiles the acceptance
  report. No worker accepts orders unless its configuration sets
  `"orders": true`.

This section defines the required responsibility boundary. It does not claim
that a sandbox or other technical enforcement has been implemented.

### 19.8 Configuration starts empty

**Required default: `NULL`.** Installing Agent must not preconfigure or select
GitHub accounts, AI accounts, roles, or account/role assignments. Operators add
accounts and define roles explicitly through configuration, with no hard-coded
limit on their number. A missing setting is unconfigured, not permission to
select a built-in identity, provider account, or role.

The device-local [GitHub account configuration](docs/github-accounts.md) implements
explicit GitHub accounts and session bindings for the static executors that load
it: `agent github pending`, assignment and PR-merge scans, supervised issue
reads, and the `pushed` / `mergeable` steps of `agent run`. An absent or empty
configuration does not authorize those covered paths. On those paths the
executor verifies the selected login and never falls back to an ambient login
or a different configured account. GitHub execution identities do not change the
device's hub identity or transfer ownership of store rows.

[AI configuration](docs/ai-accounts.md) supplies arbitrarily named account
profiles and roles, explicit models/access and session-to-role bindings. It is
used by lane launches, interactive session starts and optional usage reads.
Every driver it names is described in the operator file `drivers.json`
([docs/ai-accounts.md#drivers](docs/ai-accounts.md#drivers)): argv templates,
spec and output passing, the configuration-directory variable, credential
variables to scrub, capability flags, the usage adapter, the display name and
the pane markers. The repository ships no driver table and no driver default;
a missing or incomplete description fails loudly.
Window-usage reads are a usage adapter; their login, requests and response keys
come from that driver's `drivers.json` configuration.
Its `lane_execution` declaration is required for every vendor-lane start and
only explicitly marks the current device as a lane host. The local harness's
lane starter must start Agent on the external lane host; absence, invalid
configuration, and the unsupported `remote` mode authorize no fallback.
`lane_execution` gates provider execution on this device; recording an Agent
for a lane started elsewhere does not need it.
Its required `models` block names the provider of model slot `first` and `second`, and
its optional `implementer` names a separate implementation provider;
device-wide `lanes` bind slots for every session. An unset `models` block
is unconfigured and fails closed; no provider assignment is built in. Tasks
recorded before model slots existed read the slots of `recorded_slots` in
`drivers.json` only when their existing agent and gate rows are read or
displayed; the same entry keeps stage, checklist-key and phase names that an
earlier version derived from driver ids readable.
They cannot start a new lane or write a new agent or gate row and must be
recreated with the configured device selection. Task creation and `agent models`
read both selections.
Absent configuration authorizes no AI launch. Fixed workflow kinds are
protocol capabilities and drivers are operator configuration; neither is an
installed account or role selection. A38 visibility lookup uses an explicitly bound GitHub
session, or operator-supplied visibility without a GitHub lookup.

The standalone PR guard retains its explicit workflow-token configuration.
Trusted script APIs and raw terminal commands are not a sandbox. The optional
issue coordinator and its deployment boundaries are documented in §19.7;
technical enforcement of every model tool restriction remains separate work.

### 19.9 Comment mandate: conflicts

**Required.** An operator may explicitly configure a `conflicts` worker. A
human pull-request comment that normalizes to exactly
`@<worker-login> conflicts`, authored by a configured GitHub user, unedited,
and on an open pull request, is a mandate for that static worker. Comment text
remains data; a longer message that merely contains the phrase is not a
mandate. With no configured worker, no comment starts work.

The script owns discovery, reaction, pull-request reads, the isolated worktree,
the rebase, lane starts, verification, configured checks, the
`--force-with-lease` push, and the one idempotent result or blocker comment. It
starts a confined implementer lane only when Git reports a content conflict,
and no model output is a transition. Non-content conflicts, branches with merge
commits, and repositories without checks are refused in this revision. The
config and deployment boundaries are documented in
[docs/conflict-mandate.md](docs/conflict-mandate.md).

The coordinator starts the same script-owned resolution on its own for an order
whose pull request has merge conflicts. This is the declared remedy of the
check `pr_mergeable` (§19.10). The mandate is the order, not a comment.

### 19.10 Orders and the precondition registry

The script is the orchestrator. The model between a human and the script is the
concierge. It submits the human's request, reads status and the acceptance
report, relays questions, passes back replies, overrides, cancellation or the
human's done mark, and returns a recommendation. It starts no lane, performs no
review and does not wait with model turns.

The command line, the orders view of the local dashboard and a concierge are
interchangeable front ends of the operation table in `order_ops.py`. Everything
the concierge can hand to the script or read from it, a human can do and read
in the dashboard. The dashboard does not expose the blocking `wait` operation;
it polls `events`. No front end is part of the coordinator flow. The device daemon runs `agent coordinate
--session ID --follow` for each configured worker.

An order is a work request submitted on the execution device with `agent order
submit`. The command takes repository, title and body-file arguments. It may
also take a base, worker, notification session, repeated skip-check and reason
pairs, and a round limit. Every option takes its value as the next argument.
The command prints the order id, which is the task UUID.

The order becomes an ordinary coordinator task. Its
`payload.coordinator.source.kind` is `order`; an issue task has no kind and is
read as `github-issue`. Orders use no second state machine and no new table.
A worker accepts them only when `coordinator.json` sets `"orders": true`.
An explicit `repositories` entry keeps its own base, publication repository and
checks; every other repository uses the worker's owner-level settings and pins
the submitted base. A non-A38 worker that lists explicit `repositories` must
also configure both `owner_check_argv` and `owner_readiness_argv` to accept an
order outside those entries. Without them submission is refused before any
task row or task text is written, and the message names the missing setting.
An A38 worker and a non-A38 worker with both owner-level commands accept every
repository. An unreadable or unpublishable repository is not refused at submit
time; acceptance ends `blocked` or `failed` with the GitHub reason.

With no explicit worker, submission selects the order-enabled worker that can
check the repository and has the fewest runnable tasks. When no enabled worker
can check it, submission says so, and that error differs from "orders are not
enabled on this device". An explicit repository requires the submitted base to
equal its configured base. For an owner-scoped repository the submitted base is
the branch pinned at acceptance. A new order stays in `queued` while that worker
has a runnable task. A tick with none promotes the oldest queued order to
`accept`.

The order text is at most 60,000 characters. It is stored once at
`<workspace_root>/.coordinator-control/<task id>/task-text.md`. The implementer
prompt contains the full text for orders and issues. Reviewers read it through
the review-context file. It is untrusted specification text, like an issue
body. The task row holds a preview of at most 2,000 characters.

The local submission by the device's paired GitHub login is the mandate. Every
operation that records who acted refuses an unpaired device with `device is not
paired`. The coordinator verifies no hub assignment for an order, even when
the worker requires hub assignment. Acceptance reads no issue and posts
nothing on GitHub. On the owner-scoped A38 default without
`owner_readiness_argv`, readiness closes both deviation keys as `n_a` from the
local submission, with evidence
`order <id> submitted by <who>: orders declare no deviation`. Order
submission cannot declare a deviation.

Order states are derived, not stored: `queued`, `running`, `waiting`, `ready`,
`merged`, `done`, `failed`, and `cancelled`. The ended states are `merged`,
`done`, `failed`, and `cancelled`; `agent order list --open` shows only the
others. Order state `merged` is derived from task state `done`. It is not the
spine task state `merged` of §19.5. Only `agent task close-from-pr` writes that
spine state, and that command refuses coordinator tasks.

The script writes one `order.event` at `accepted`, `draft-opened`, `question`,
`blocked`, `resumed`, `ready`, `merged`, `done`, `failed`, and `cancelled`. Its
payload contains the order id, kind, wake flag, per-device sequence, a redacted
digest of at most 1,500 characters, pull-request URL, phase, round, failed check,
accepted answers, round-limit opinion, notification session, time and
acknowledgement.

`question`, `blocked`, `ready`, `failed`, and `cancelled` wake. `merged` and
`done` never wake. A merge observed before Ready produces the `ready` and
`merged` events together. Events persist until acknowledged. `--notify-session` only filters.
Any later concierge session or the dashboard can list unacknowledged wake
events and acknowledge them.

`agent order reply`, `agent order override`, and `agent order cancel` write an
`order.reply` row for the next coordinator tick. A reply resumes the exact
`resume_phase` like an authorized pull-request or issue comment. A repeated
question requires a new reply. A cancel closes an open pull request of the
order with the fixed comment and ends the order as failed with kind
`cancelled`. Without an open pull request (none yet, or a closed one) it ends
the order the same way without a comment; a cancel is refused when the pull
request is already merged. An answer acknowledges the question or blocker it
answers.

`agent order done --id UUID --reason TEXT` marks an order completed on the
explicit decision of a human; a concierge calls it only on the human's
request. The reason follows the cancel rule: one line of 1 to 500
characters. It accepts every state except `done` and `merged`, and on those
it fails with `order UUID is already STATE`. For an open order it writes an
`order.reply` row, and the next tick ends the order the same way as a cancel
but without the pull-request closure: no further round, lane start or GitHub
write follows, and the state becomes `done`. For an order in state `failed`
or `cancelled`, the operation itself sets `done`, without a coordinator pass.
That includes a `failed` order whose blocker no reply can resume; it ends
the same way as an open order. The operation rewrites the order only while it
holds the lock of the order's worker (`coordinator-worker:<session_id>`), which
a tick holds throughout. It never waits for that lock: while a tick of that
worker runs, it leaves the `order.reply` row, and the next tick marks the order
done the same way. Marking done never merges, closes, reopens, comments on or
edits the pull request and never pushes or deletes a branch. The order records
who marked it done and the reason; the `done` event's digest is `Order marked
done by <who>: <reason>`. Like a cancel, it acknowledges the open question or
blocker. `reply`, `override`, and `cancel` on a done order fail with `order
UUID is already done`. The orders view of the dashboard offers the same
operation as `Mark done` with a required reason.

Events name accepted answers. `ready`, `failed`, and `cancelled` accept `ack`.
A question or blocker accepts `reply` unless its failed check requires only an
override, accepts `override` when the check is overridable, and always accepts
`cancel`. A blocker that only waits for execution configuration offers only
`cancel`. A refused answer creates a new unacknowledged question whose digest
starts `Previous answer refused: <text>`. A refused override with no pending
question creates no event. `agent order show` records its fate as
`last-answer`.

Once the draft exists, it is the public carrier. Questions, blockers and gate
rejections are pull-request comments. Authorized comments from `reply_logins`
resume the order. Before the draft exists, a question or blocker exists only as
an order event. Models never write either order activity type.

Every side-effecting coordinator action has an ordered list of precondition
checks in `preconditions.py`. Adding a check to an action requires one registry
entry and one resolver. A resolver is either a static script function or one
single-purpose check lane. The script resolver is preferred.

Each check is a store fact with status `ja`, `nein`, `n_a`, `pending`, or
`unavailable`, always with evidence, source and time. `n_a` is valid only where
declared. `unavailable` means the resolver could not run. There is no implicit
yes. An action runs only when every check is `ja`, or declared `n_a`.

A pending check starts its resolver or routes to the phase that produces the
fact. A no follows its declaration: remedy phase, retry, declared remedy,
question, reply checkpoint, or task end. Existing checklist keys, round
verdicts, gate rows and head-bound checkpoint values are referenced, not
copied.

The registry-only facts are `order_authorized`, `spec_available`,
`checkout_owned`, `duplicates_checked`, `no_live_lane`, `round_budget`,
`head_signed_clean`, `pr_mergeable`, `conflict_mandate`, `review_2`, and
`one_review_permitted`. Only the coordinator writes them through
`chain.close_allowed`. They are not workflow keys and do not affect `done`.
`agent checklist set` and `agent close-step` refuse them with the fixed
coordinator-precondition message.

Only `duplicates_checked` and `review_2` are overridable. Only
`duplicates_checked` is skippable at submit. A skip uses the exact sequence
`--skip-check KEY --reason TEXT`; the reason is one line of 1 to 500 characters.
Acceptance records status `ja`, source `human`, and evidence `skipped at submit
by <who>: <reason>`. A final human decision is not head-bound, and its resolver
never runs again. `agent order show` and the acceptance report list every skip.

The registry covers accept, checkout, draft opening, implementation, commit and
push, inner review, tests, both review pairs and their comments, CI, readiness,
Ready, merge observation, second-review waiver, conflict resolution and draft
closure. For issue tasks it also evaluates `no_live_lane` before implement,
inner review and both review phases, `round_budget` before implement, and
`pr_mergeable` before readiness and leave-draft. Other issue preconditions stay
in their phase handlers.

A check lane makes one judgment from a prompt compiled by the script. Untrusted
data is between nonce marker lines. The lane has no tools or file access, runs
in an empty directory, and returns only the fixed grammar of its check. It has
no `STATUS` line. The script validates the answer and never interprets free
text. This is process argv hardening, not a sandbox.

The check lane uses model slot `first` through `<first>:check`, bound to a
read-only role. A driver without `lane.argv.judgment` cannot run it. The duplicate check first lists the open
pull requests with bounded titles and descriptions. With none, the
script records yes without a lane. Otherwise the lane compares that list with
the order and returns `RESULT: duplicate <number>` or `RESULT: no-duplicate`,
then one `REASON:` sentence. The script verifies a duplicate number against the
list.

A duplicate opens no pull request and creates no start commit. It records no
and raises a question naming the pull request and `duplicates_checked`. Only an
override, cancellation or done mark resolves it, unless it was skipped at
submit. Invalid answers retry with backoff under `lane_retries`. More than 200
open pull requests, or exhausted lane retries, records `unavailable`; a plain
reply tries the check again.

Every round counts once; an implementer start reuses the current round while
its implementer has no verdict, so a lane retry stays in its round. The limit
is pinned at task
creation from `--max-rounds`, then `max_rounds_by_model`, then `max_rounds`,
then 10. Its origin is `submit`, `model`, `worker`, or `default`. No value turns
the limit off. Before exceeding it, the script starts one assessment check lane
over the task text and bounded findings from each round. It returns `continue`,
`change-approach`, or `abort`, with a reason and an approach only for
`change-approach`. The script stores and publishes the opinion but never acts
on it. A reply grants the same number of rounds and supplies its text as
untrusted guidance; cancellation ends the order. The proposed approach is not
passed to the implementer. Without `<first>:check`, an issue-only worker stores
the opinion as `unavailable`.

Every order pull request has review 1 and review 2. The roles bound to
`<first>:implementer`, `<first>:reviewer`, `<first>:pr-reviewer-quality`, and
`<first>:pr-reviewer-logic` must name the same model. Review 2 uses the distinct
provider in model slot `second`. The fact `review_2` means that review is free
of findings on the current head, or a human waived it for this order. It is
required by CI and readiness.

Only `agent order override --id UUID --check review_2 --reason TEXT` can waive
review 2, including while it is pending. The script refuses the waiver unless
the task has a model second review, review 1 is complete, no second review has
approved the pull request or rejected an A38 pull request, and the opener is in
`one_review_authors` on the trusted default branch. `review_2` cannot be skipped
at submit, and manual `n_a` review keys do not waive it. The script never drops
review 2 on its own.

`pr_mergeable` is required before readiness and leave-draft for orders and
issues. `UNKNOWN` retries the same phase. `CONFLICTING` is a no. An issue conflict is a resumable
`conflict` blocker; the coordinator starts nothing on its own, and the separate
comment-mandated worker does not act on the task. For an order the declared
remedy is the script-owned conflict resolution of §19.9.

The order is the conflict mandate. `resolve_conflicts` requires
`order_authorized`, `draft_open`, `head_published`, and `conflict_mandate`.
The last fact binds the order, submitter, pull request and head. The worker uses
`conflict_remedy` with `check_argv`, `max_rebase_steps` and optional
`max_attempts`; it does not read `conflicts.json`. Its session needs the
`conflicts` skill. State lives below
`<workspace_root>/.coordinator-control/<task id>/conflict-remedy/`.
Without `conflict_remedy`, no resolution starts and the order receives the
`pr_mergeable` question described below.

After a remedy push, the script adopts the published head, removes old-head
evidence and continues at tests. Tests, report, both review pairs and CI run
again; recorded overrides and skips remain. Open remedy outcomes retry under
`lane_retries`. Only a final blocker produces a pull-request conflict comment
and a `question` event for `pr_mergeable`, accepting `reply` and `cancel`. A
reply measures mergeability again and may start another resolution. At most
`max_attempts` resolutions start between human decisions. Start and success
produce no event.

`agent order report` compiles a bounded acceptance dossier from the store. It
makes no GitHub call, starts no lane and runs no Git process. It works in every
order state. It includes identity, state, repository and head; both summaries;
up to 50 changed files with line counts; rounds and limit origin; every skip and
override; the last 40 review runs; the last 12 bounded review comments; A38
report outcome, local checks and CI state.

Report verdicts are definitions, not judgments. A run is rejected when a lane
rejected, incomplete when a lane lacks a recorded verdict, and otherwise
approved. Defects count rejecting lanes; an external second review uses its
published defects value. A38 report state is `pass`, `fail`, `unreadable`,
`missing`, or `waived`. CI is `green`, `failed`, `pending`, or `unknown`. Text
is at most 12,000 characters, puts single-line facts before list details, and
ends `report truncated` when cut. A concierge may use it to recommend whether
the delivery plausibly matches the order. It never performs the full review.

The exact per-head order of implementation, inner review, local checks, both
review pairs, CI and readiness is in
[docs/issue-coordinator.md](docs/issue-coordinator.md#workflow-script-owned).

## 20. Refused: hub as a coding control plane

An external architecture draft proposed turning the hub into an authoritative Error-to-PR control plane: hub-owned Task objects, leases, a worker scheduler, production-error ingestion as the first workflow, and `READY_FOR_PR` then deterministic pull-request creation.

That draft is **not** this product. Hub authorship, hub leases, a hub task machine, a parallel `external_events` store, autonomous merge, and a capability scheduler stay refused even as a skill.

| Draft idea | Why refused |
|---|---|
| Task as the durable center; sessions are operational | Product center is the session store. Tasks are spine, opt-in. |
| Hub assigns, leases, and reassigns work | Hub is replica + fan-out, not author. A disappeared laptop is restore or unread, not a lease expiry that moves authorship. |
| Formal `CREATED` → `READY_FOR_PR` machine as platform | Spine already has a local task machine. A second hub machine would be a second truth. |
| Normalized `external_events` table + fingerprint service | Outside facts are script-written `activity` rows (`pr.merged`, …). |
| Worker capability ontology and scheduler | Providers are the drivers described in `drivers.json`; `ai-accounts.json` supplies the slot defaults when the task is created, and the task pin governs and can be changed explicitly with `agent task models` (not for coordinator tasks). Scripts are not store workers. |
| Policy language (allowed repos, forbidden paths, cost budgets) as core | Team-specific rules stay outside this public client (§2 Runtime). |
| Website as an operations console for leases, token cost, stuck coding tasks | Website is the visibility replica (§7). |
| Autonomous merge | Already forbidden. Stays forbidden. |
| Model-created state machine | Transitions are code and constraints, not prompt output. |
| Universal workflow DSL or a distributed queue in front of Postgres | The catalog and local Postgres until a real load proves otherwise. |

The wanted loop “production error → draft pull request” is the **error-fix** skill (§21). It still means: this device writes `activity`, a script on this device queries logs, the session analyses, scripts validate and open a **draft** pull request, a human merges. It must not move write ownership to the hub.

## 21. Skill: error-fix

Wanted. Opt-in. Runs on **this device** (a laptop that has the client, log credentials, a runner session, and git). It is one skill among many. It is not the session bus, and it is not a hub workflow.

Attach `error-fix` together with `spine`, `review-loop`, and `pr-review` on the runner session that owns the work. Without `error-fix` the session does not run this loop. `error.skip` and `error.fix` are members of the activity type allowlist (`agent activity add`); `error.seen` stays script-only.

### 21.1 Laptop as execution plane

| Piece | Where |
|---|---|
| Log credentials and adapter config | `$AGENT_HOME` on this device, not git, not the hub |
| Watcher process | This device. Script, not the model. |
| Analysis and “fix or skip” | The attached runner session on this device |
| Isolated worktree, checks, draft pull request | This device |
| Merge | A human |
| Hub | Replica + fan-out of the rows this device already wrote |

A crashed laptop does not transfer the task to another machine. Restart the session on **this** device, or the `error.seen` rows stay unread.

### 21.2 Log adapter

A script queries a configured log source on a schedule (same shape as `agent watch pr-merged`: one scan; the knock daemon may poll). This package does not name the log host. Mapping from stream → repository, query window, and redaction live in `$AGENT_HOME`.

The script:

1. Authenticates with credentials that never enter the store or `evidence`.
2. Pulls new lines since the last cursor (persisted next to the config).
3. Filters to incident lines only: HTTP access-log lines (`METHOD path status`) are dropped; lines with a logger level token `ERROR` / `FATAL` / `PANIC` / `CRITICAL` are kept; lines with an `*Error` / `*Exception` class are kept; other lines (including ones that merely mention the word "error") are dropped. Optional config strings `line_must_match` / `line_must_not_match` further filter (non-empty regexes; invalid values are rejected at load). Filtered lines advance the cursor but do not insert `error.seen`.
4. Redacts secrets and obvious personal data **before** any row is written.
5. Computes a fingerprint: service + error class + normalized stack signature + environment.
6. Inserts `error.seen` or **enriches** an existing **open** row with that fingerprint on this session (`count`, `last_seen`, optional extra excerpt, optional `line_fingerprint`). First insert knocks `da ist Post id <uuid>`. Enrichment never knocks. After skip or a terminal implement task, the next match is a new `error.seen` (new id, knocks).
7. Payload holds a **sanitized** excerpt plus an optional pointer to raw evidence on this disk. It does not hold the full log dump.

Log lines, stack traces, and error messages are untrusted data (§19.2). They are not a mandate to patch.

`agent watch errors` is one scan. An empty scan (no created, no enriched rows) prints nothing — not `error.seen none`. `agent watch error-fix` likewise prints nothing when there are no lines. Config is `$AGENT_HOME/error-fix.json`:

```json
{
  "session_id": "runner-session-id",
  "url": "https://logs.example/loki/api/v1/query_range",
  "query": "{job=\"api\"} |= \"ERROR\"",
  "limit": 100,
  "service": "api",
  "environment": "prod",
  "repo": "org/app"
}
```

`session_id`, `url`, and `query` are required. The default fetch is a Loki-compatible `query_range` GET (`query`, `start`, `end`, `limit`, `direction=forward`). The JSON body is `{ "data": { "result": [ { "stream": {}, "values": [[ns, line], ...] } ] } }`. Optional `service`, `environment`, `repo`, `limit`, `line_must_match`, `line_must_not_match`. Cursor is nanoseconds in `$AGENT_HOME/error-fix.cursor`; the next `start` is that value plus one. Credentials come from netrc or `AGENT_ERROR_FIX_USER` / `AGENT_ERROR_FIX_PASSWORD` — never from `error-fix.json`, the store, or the URL. The knock daemon (`agent knock` without `--once`) polls this scan on the same 60s interval as session-usage when the config file exists.

### 21.3 Payload shape (`error.seen`)

```json
{
  "fingerprint": "service|class|stack-sig|env",
  "service": "api",
  "environment": "prod",
  "class": "TimeoutError",
  "repo": "org/app",
  "count": 1,
  "first_seen": "2026-08-23T16:00:00Z",
  "last_seen": "2026-08-23T16:00:00Z",
  "excerpt": "sanitized log line…",
  "evidence": null,
  "line_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`repo` may be omitted when the adapter cannot map the stream; the session then `error.skip`s with reason `unmapped-repo`. `line_fingerprint` is optional: `sha256(server + newline + container + newline + exact line)` as 64 lowercase hex, computed from the raw line before redaction. Omit it when `server` or `container` is missing. Host adapters may print the hex on `error.fix` stdout; it is not a mandate and not a log-host name.

### 21.4 Analysis and eligibility

After the knock, the session reads the row and writes `investigate.step` immediately (hypothesis, check, ruled out — each a new row). Then it inserts **one** typed conclusion. Both conclusion payloads include `error_id` (the `error.seen` id) and `fingerprint`:

- `error.skip` — not a code fix (infra, noisy duplicate, unmapped repo, forbidden path, already an open draft for this fingerprint). Also `reason` (short token plus optional note).
- `error.fix` — `execution_status=pending`. Local intent only.

The model does not certify eligibility by saying “this is safe”. The typed row is the decision. Confidence scores are not stored as proof.

`agent activity add` now enforces the error-fix skill, payload, fingerprint, one-conclusion, unmapped-repo, and already-open-draft guards.

The adapter decides open vs closed by that `error_id` / `fingerprint`, plus the spine task whose `payload.error_id` matches. A later `error.skip` or a terminal task (`done` / `failed`) for the same `error_id` closes the incident. `pr.merged` knocks as today; it is not a second close signal. `agent task create` for a given `error_id` is find-or-create; a second `error.fix` does not open a second task.

Same fingerprint while the incident is **open**: enrich `error.seen`. Do not create a second task or a second pull request. After close: the next match is a **new** `error.seen` (new id, first insert knocks).

### 21.5 Patch and draft pull request

On `error.fix`:

1. `agent task create --workflow implement --error-id <error.seen-id>` on this session (find-or-create). That copies `error_id` and `repo` from the `error.seen` row into the task payload.
2. Isolated worktree of that task `payload.repo` at the allowed base revision. Git operations are scripts. `payload.repo` is already on the task because analysis refused `error.fix` when `repo` was missing. Never fall back to the origin checkout.
3. As soon as the first signed task commit exists, `pr.open` opens a **draft** through the existing executor flow ([docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md)). Title/body may be model-drafted; the GitHub API call is a script. A retry finds an existing draft for this fingerprint instead of opening a second one. Mandatory checks and the spine checklist key `pushed` remain final validated bookkeeping; they are not prerequisites for that first draft publication.
4. pr-review gates and applicable full checks run on the final head before Ready; closing spine `pushed` records the validated push step, not the early draft open.
5. A human merges. `pr.merged` knocks as today.

The model never receives production credentials. Analysis that only reads the excerpt does not need write access to the origin branch.

`agent watch error-fix` find-or-creates the implement task and clones `https://github.com/<repo>.git` into `$AGENT_HOME/error-fix-work/<task_id>`; `agent github pending` still opens drafts, and a retry draft uses the existing head `error-fix-<id8>`.
The watcher resolves the configured model pins before it creates a staging
directory or invokes Git. A missing or invalid selection records a stable
configuration error; unchanged configuration is not retried, while a changed
configuration is evaluated again. Before reusing an existing error-fix task,
the watcher validates that task's pinned `models`. A nonterminal legacy task
without model pins fails closed before any staging or Git operation. Replace it
explicitly with `agent task state <id> superseded`; the next scan ignores that
terminal task and creates a new task with the configured model pins. The stable
error fingerprint includes the model configuration plus the legacy task id and
state, so an unchanged scan stays quiet while either superseding the task or
changing the configuration permits one new evaluation.

### 21.6 Not in this revision

- A second hub state machine, leases, or autonomous merge

## 22. Static supervise loop (v1)

A second model must not orchestrate the first. `agent supervise` is a **script** with locked questions and locked answers. Model text is not a state transition.

- One pending `issue.assigned` at a time (same queue as `agent watch assigned`).
- `--repo` / `--number` enqueues that issue as `github-assignment` without hub pairing. Title and body stay untrusted payload.
- Busy means the interactive driver in the tmux pane shows an in-flight turn: an `interactive.pane.working` marker of that driver, or a complete `queued` marker group for a queued follow-up. `Runtime.is_busy` is that probe. The script does not type while busy.
- Follow (`ask=False`, the CLI default) does not knock an existing session, does not ask closed questions, and does not auto-continue. It only confirms a tool-approval modal (a `permission` marker plus one `permission_confirm` substring → Enter). Closed questions remain available to `tick(..., ask=True)` for tests. Consecutive idle ticks (`supervise quiet` / `supervise stalled`) are follow-loop bookkeeping, not Telegram pages. Interface lines listed in `interactive.pane.chrome` are not a working signal or an answer.
- When `ask=True`, `Ja` or a blocking problem → `issue.assigned.ack` and the next queue item. A blocking problem also stores a truncated pane excerpt on `supervise.event` (`kind=skip`).
- `supervise.event` is script-only and does not knock.
- Optional Telegram status posts (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) are a script HTTP side-effect. They are not person pings and not the hub. Missing env is a no-op. Telegram `not working` is posted only when the interactive driver's tmux session is gone (`runtime.exists` is false), once until the session returns. An idle prompt is not an outage. A send failure does not stop the loop.

Phase 1 is this loop plus a backlog. Smarter questions are later.

## 23. Document history

Recorded from the design thread that specified realtime team visibility, rejected a central write database and a mesh, rejected embedding the hub in the existing public API, chose GitHub login + git teams, and split the work into `agent` + `agent-core`. Control: local tmux ownership, hub control frames, ephemeral terminal bytes. Interactive launch: own UUID in `runtime.interactive_session_id`, a resume template on later starts, the historical default model and built-in driver table (now replaced by explicit AI and driver configuration), no credential environment of another driver in the pane.

This revision replaces default complete pull with own events + inbox/subscription snapshots, moves the local engine to PostgreSQL, requires a session row, adds the `activity` catalog and opt-in skills, and adds session-addressed mail with a TUI knock of `da ist Post id <uuid>` only.

Deterministic core (§19) and the refused hub control plane (§20) lock the split that was already in §§1–17: scripts execute, checks measure, gates decide, and model text is never a transition. Error-to-PR is not the product core and not a hub workflow; it is the opt-in **error-fix** skill on this device (§21). The static supervise loop (§22) is a script with locked questions; a second model does not orchestrate the first.

This revision adds the opt-in comment mandate `conflicts` (§19.9): one exact human pull-request comment authorizes a static script to rebase, run the configured checks, and push with `--force-with-lease`, while a confined model lane edits only conflicted files. Installation still enables no worker.

This revision also adds local orders; the precondition registry with one
resolver per check; skip at submit and override; acknowledged order events; the
immediate draft; bounded retries and lane recovery; the per-implementer-model
round limit and its assessment lane; the conflict remedy for orders; the A38
model second review; the acceptance report; and the orders view of the local
dashboard.

This revision records the proposed concept for a team record with key identity (§24): signed append-only events for tasks, security issues and discussions, no third-party identity, no deletion, and a time axis. It is not implemented; §§2–12 stay in force until each part ships.

This revision records the decision of 2026-11-17 to build the team record in agent-core and to use no outside tracker, and lists the questions still open in §24.

This revision records the decision of 2026-11-17 to store the team record in PostgreSQL, with a database role that may only insert and read events (§24.8).

This revision adds the proposal for central operation (§25): devices as principals with two levels, work handed over through the hub, tasks and a team log in place of outside trackers, complete recording, the full session view, one overview, and one interface for people and for AI. It is not decided and not implemented.

## 24. Team record and key identity (decided, not implemented)

Status: decided on 2026-11-17, not built. The team builds the record itself in agent-core; no outside tracker is used as the store or mirrored beside it. Until a part of this section ships, the rules in §§2–12 stay in force for that part. Each implementation pull request names the subsection it implements and updates the rows of §2 and §3 that it supersedes. An explanation for the whole team is in [docs/team-record.md](docs/team-record.md); this section stays the binding rule.

Still open, each decided by a human before the part it affects is built:

- Whether staff passkeys may be synced by the platform or must be bound to one device (§24.2, sign-in without GitHub).
- What "released" means in the answer to a release query (§24.10, step 2).
- Whether content readable by the network edge in transit is acceptable, or which objects need end-to-end protection (§24.2).

### 24.1 Purpose

The team needs one record it owns, beside the session store:

- open **tasks**
- known **security issues**
- **discussions**: between people, between agents, and between people and agents, including topics that must be settled internally
- a **read view of open pull requests** (§24.5)
- a **dashboard** that answers questions such as "how many security issues are open, in which repository, at which severity", for today and for any past date

Every change to the record stays attributable: who changed which object, which field, from which value to which value, and when. That attribution is cryptographic, not only logged.

The session store (§§8–11) is unchanged. Sessions, activities, session mail and person pings stay device-owned rows on the session bus. The team record is a separate stream with its own rules.

### 24.2 No third-party identity

Identity is a key pair. The private key never leaves the person's authenticator or the program's device. The hub stores only public keys and verifies every signature itself; no outside service is asked at sign-in or on a write.

There are three kinds of principal:

| Principal | Key | How it proves itself |
|---|---|---|
| Staff member (browser) | Passkey (WebAuthn credential) | Signs a hub challenge on sign-in; every write from the website is signed by that credential |
| Agent (CLI, scripts, model sessions) | Ed25519 key generated where it runs, stored under `$AGENT_HOME` with mode `0600` | Signs every request and every team-record event, like an SSH key |
| External program (for example a pull-request guard on a CI runner) | Scoped Ed25519 service key | Signs every request with its service key and verifies the hub's signature on every answer |

Rules for all principals:

- No GitHub OAuth, no password, no mailbox code and no outside identity provider decides who someone is. GitHub stays the home of code and pull requests (§24.5); it is not an identity source for the record.
- **Every request is authenticated.** The hub has no anonymous endpoint: website, API, sync, pairing, health and release queries all require a valid signature by a registered key (or a signed-in passkey session). A request without one gets no data, not even a count or a status.
- **No access gate in front of the hub.** The network edge provides the tunnel, TLS, protection against overload and rate limits, and filters that do not identify anyone (address or country rules). It runs no login of its own: no edge access policy, no mailbox code, no edge service token. The hub's own check is the only authentication.
- The edge terminates TLS and can read content in transit. It cannot forge a request or an answer, because both are signed (§24.2), and it cannot replay one, because both carry a nonce and a time.
- The edge access gate is removed only together with the release that enforces the hub's own authentication on every request, never before.
- **Answers are signed too.** The hub signs every answer to an agent or external program with its own Ed25519 key, bound to the request (subject, nonce, time). The caller verifies it against the hub public key pinned in its own repository or configuration. Both sides prove themselves on every exchange.
- Public keys are registered in a keys file next to `teams.yaml`, changed only by pull request. Each entry has a key id, kind (`webauthn` or `ed25519`), the principal kind, the handle of the person, agent or program it belongs to (a handle, never a legal name or mail address, because the file is public), the device and program label for agent and service keys, the team, and its scopes. Adding, rotating and revoking a key is a pull request.
- A revoked key can no longer read or write. Events it already signed stay valid and stay attributed to it.
- Every key has **scopes** (for example `record:read`, `record:write`, `release:query`). A key does only what its scopes allow. Staff passkeys get the team scopes; agent and service keys get the least they need.

Staff members:

- A staff member may register several passkeys (for example laptop and phone).
- The private key of a passkey never leaves the authenticator. If the platform syncs passkeys between a person's own devices, that sync is the platform's business; sign-in and every write are still verified by the hub alone.

Agents:

- **At least one key per device.** A key is never copied to a second device.
- **One key per program where it matters.** Programs on the same device that need different rights or must be told apart in the audit trail (for example a coordinator, a lane starter and a monitoring script) each get their own key with their own scopes.
- An agent key is never registered under a person. Agent events name the agent key and, where one exists, the person who runs it.

External programs:

- An external program has its own service key with the narrowest scopes (for example only `release:query`), bound to the repositories or subjects it serves, and with an expiry. Each use is recorded with that key id.
- **Preferred: the key is generated on hardware the team controls**, for example a self-hosted CI runner, and never leaves it, exactly like an agent device key.
- **Only where no such host exists**, the private key may sit in the CI system's secret store. That store then holds the key; it does not decide identity. The scopes stay minimal, the key is rotated, and workflows from forks never receive it.
- Identity tokens issued by the CI provider (for example OIDC tokens of a hosted CI) are **not** accepted: they would make the CI provider the identity source.
- A pull-request guard may use the release query to decide whether to leave draft. The query is an input to the guard, not a replacement for the review rules in CONTRIBUTING.md.

### 24.3 Events, not rows

The record is an append-only chain of signed events. The current state is computed from the chain.

Each event carries:

| Field | Meaning |
|---|---|
| `event_id` | UUID chosen by the writer |
| `object_type`, `object_id` | What changed (§24.4) |
| `changes` | Each changed field with its old and new value; a create has no old values |
| `actor_key_id` | The registered key that signed |
| `signed_at` | Writer clock, part of the signed bytes |
| `received_at` | Hub clock, set on append |
| `prev_hash` | Hash of the previous event in the chain |
| `signature` | Signature over the canonical bytes of all fields above except `received_at` |

Rules:

- **Nothing is deleted and nothing is rewritten.** A correction is a new event. There is no redaction event and no administrative delete, not in the hub, not in the database, not in a backup.
- Because nothing can be removed, **personal data, secrets, credentials and customer data must never be written into the record.** Objects refer to them by an external reference that does not reveal them. A review treats a field that invites such data as a defect.
- The hub rejects an event whose signature does not verify, whose key is unknown or revoked, whose `prev_hash` is not the current chain head, or whose old values do not match the current state (the writer retries against the new head). This makes concurrent edits explicit instead of losing one.
- The hub never authors an event. It only verifies, orders and stores.

### 24.4 Objects

| Object | Fields (minimum) |
|---|---|
| Task | title, description, assignee, status, links |
| Security issue | repository, short name (at most 30 characters, unique), severity, urgency 1–10, assignee, status (`open`, `in progress`, `done`), source reference, fix reference |
| Discussion | topic, audience (people, agents, mixed), status (`open`, `settled`), outcome |
| Message | discussion id, body |

Security issue workflow: whoever starts work on a security issue first writes an event that sets themselves as assignee and the status to `in progress`. Nobody takes over an issue that already has an assignee without that person's agreement. Completion is an event that sets `done` and the fix reference.

### 24.5 Pull requests are read, not owned

Open pull requests stay in GitHub. The hub reads them and shows them; the record stores at most a reference from a task or security issue to a pull request. A copy of pull-request state is a cache with its fetch time, never a second truth.

### 24.6 Time axis

The state at any past moment is the fold of all events with `received_at` up to that moment. The dashboard lets a viewer pick a date and shows the record as it was then, including counts per repository and severity. No separate snapshot store is needed; snapshots may exist only as a cache that can be rebuilt from the chain.

### 24.7 Visibility

Only registered keys read the record. Team scope follows §6. There is no public view of security issues, not even a count, unless a later pull request to this section allows a specific public view.

### 24.8 Integrity and backup

- The chain is the authoritative team record. Its data class is **unique** (§13); losing it loses history that exists nowhere else.
- The team record is stored in **PostgreSQL** (decided on 2026-11-17). The session store (§§8–11) keeps its own database; this decision does not move it.
- The database role the hub uses may only insert and read events; it has no right to update or delete them. The no-delete rule (§24.3) is enforced by the database, not only by the hub's code.
- Three copies, one of them off-site, with a restore that is tested and verifies the whole chain (signatures and hashes) before the hub serves it.
- The hub publishes the current chain head hash at a fixed interval, signed with its own key, so that a later rewrite of history is detectable from any earlier published head.

### 24.9 What this changes in the locked decisions

When the matching part ships, its pull request changes these rows in the same diff:

| Place | Today | With §24 |
|---|---|---|
| §2 Hub role | Not a shared write database | Still true for the session store; the hub is the verifier and keeper of the team-record chain |
| §2 Login, §2 Identity, §5, §7 Sign-in, §12 | GitHub login; some endpoints unauthenticated | Registered key pair on every request: passkey for staff, Ed25519 for agents, scoped service keys for external programs; hub-signed answers (§24.2) |
| §3 One shared database everyone writes to | Rejected | Still rejected for the session store; the team record is a signed append-only chain, not a shared mutable database |
| §3 Plane (or any issue tracker) as the store | Rejected | Still rejected; the team record replaces an outside tracker instead of mirroring one |
| §3 Hub-owned task lifecycle and leases | Rejected | Still rejected for session tasks (spine); record tasks are written by signed events of people and agents, never by the hub |

### 24.10 Project rules

- This section changes only by pull request. A change to identity (§24.2) or to the no-delete rule (§24.3) needs an explicit human decision recorded in that pull request.
- Code is public; record data never enters any repository, issue, pull request, log or error message.
- No private repository, internal hostname or internal infrastructure is named here or in code (CONTRIBUTING.md).
- Every implementation pull request carries tests for its signature checks, chain checks and refusal paths, not only the success path.
- Build order: keys file with scopes and signature verification first, then the authenticated release query with a hub-signed answer for external programs, then the chain and the security-issue object, then the dashboard with the time axis, then discussions and tasks, then sign-in without GitHub, then removal of GitHub login.

## 25. Central operation (proposed, not decided)

Status: proposed on 2026-11-17, not decided and not built. This section describes how the whole team works through the hub: devices connect once, work is handed over, watched, answered and recorded in one place. It builds on §24 (key identity and the team record) and changes nothing until a human decides it and each part ships. The open decisions are listed in §25.15.

### 25.1 Goal

A staff member connects a device to the hub once and can then leave it alone. From the hub they hand work to it, answer its questions, watch every running session in full detail and see what the whole team is doing. Tasks, the team log, pull requests, devices, sessions, AI usage and the key figures of the team's servers are in one overview. Nothing that was commanded can later be impossible to reconstruct.

The hub is the normal route, not the only one. Direct access to a device (for example a remote shell) stays allowed as an override. Whatever is done that way is still recorded by the device and synced (§25.7).

Handing work over is the default and stays voluntary. The client never forces a session on a controlling device to hand work to another device; that session may do the work itself.

### 25.2 The device is the principal

The hub's technical counterpart is a **device**, not a code-host account and not an AI model.

- A device proves itself with its own key pair (§24.2). The key is created on the device and never leaves it. Connecting a device is the login.
- Every device is assigned to exactly one staff member. The assignment is an entry in the keys file (§24.2) and changes by pull request.
- A device reports an **inventory**: the AI accounts and subscriptions configured on it, the code-host accounts it can act as, and the runtimes it offers (for example containers or virtual machines). The inventory holds names and states, never a credential.
- AI accounts are assigned to devices. Which account a device may use is device configuration ([docs/ai-accounts.md](docs/ai-accounts.md)); the hub shows it and does not choose it.
- Devices connect outbound to the hub. No device opens a port, and nobody reaches a device's local store from outside (§3).

### 25.3 Two device levels

| Level | May do |
|---|---|
| `executor` | Runs work it was given. Cannot give work to any device. |
| `controller` | Everything an executor may do, and may give work to executors of the same staff member. |

The level is a scope on the device key. A staff member signed in with a passkey has controller rights over all of their own devices.

### 25.4 Rights

- A staff member may **write** to all of their own devices without restriction: submit work, answer questions, type into a session, start and stop sessions.
- A staff member may **read** everything of every other team member: tasks, sessions in full detail, logs, usage. Team scope follows §6.
- Nobody writes to a device of another staff member. Agent devices keep the owner rule that exists today.

### 25.5 Work goes through the hub

- A **task** says what has to be done (§25.6). An **order** is one attempt to do it on one device and links to its task.
- Submitting an order is a signed event addressed to one executor. The hub verifies, stores and delivers it. The device accepts or refuses it and queues accepted orders one after another, exactly as local orders work today (§19.10). Order state stays derived on the device and is synced.
- The hub never authors work, never picks a device, grants no lease and moves no work when a device disappears. A human or a controller device chooses the executor.
- When a session needs a decision, the device records a **question** with its allowed answers. The owner answers on the website, in the phone app or through their own AI; the answer is a signed event. The first valid answer wins, a second one is refused.
- During a hub outage an executor keeps working on accepted orders and keeps recording locally. Orders may still be submitted on the device itself. Sync catches up afterwards without a gap.

### 25.6 Tasks and the team log replace outside trackers

Every piece of work the team wants done is a task in the team record. Issues on a code host, an outside planning tool and hand-kept lists in repositories are no longer where tasks live.

| Object | Fields (minimum) |
|---|---|
| Task | title, description, kind (for example `task`, `security issue`), repository reference, assignee, urgency 1–10, status, links to orders and pull requests |
| Log entry | text, author, repository reference, optional link to a task |
| Repository reference | organisation and repository, both optional when the work has no clear home |

- The security issue of §24.4 is a task kind with its extra fields; its workflow stays.
- A log entry is something everyone should know. It is written once and never edited; a correction is a new entry.
- **Repository registry.** One file in the hub's repository lists each organisation and where it is hosted (a public code host or the team's own git server). Every repository reference resolves through it. Moving an organisation to another host is one change in that file.
- Task status is `to do`, `in progress`, `ready for review`, `done`, `closed`. "Unassigned" and "no activity for 60 minutes" are derived views, not states.
- Existing issues and lists are imported once as signed events that name their source. After the import the old place gets a pointer and is no longer edited.

### 25.7 Everything is recorded

Three stores with three different rules:

| Store | Holds | Rule |
|---|---|---|
| Team record (§24) | Tasks, log entries, orders, questions, answers, every command | Signed by the actor, chained, never deleted, no secrets and no personal data |
| Session log | Everything a session shows and receives: output, input, prompts, tool calls, decisions | Recorded on the device first, synced completely and without gaps, append-only |
| Access log | Every authenticated request to the hub, including every page view and every read | Written by the hub, append-only |

- Every command is in the team record with the key that signed it. A command that is not recorded is not executed.
- This replaces the rule of §17 that live terminal bytes are never stored. They become part of the session log.
- A session log can contain a secret or personal data that appeared on a screen. That is why it is a separate store and not part of the chain that can never be deleted. The device redacts known secret patterns before anything leaves it. How long session logs are kept is an open decision (§25.15).

### 25.8 Watching and steering a session

On the hub a viewer picks a staff member, then a device, then a session, and sees it in the same detail as at the terminal of that device: the full history from the session log and the live stream.

The owner may type into the session and answer questions that pop up. Every input is a recorded command (§25.7). Other team members watch only.

So that typing does not need a passkey touch per keystroke, the browser creates a non-exportable session key at sign-in; the passkey signs that key once, and the session key signs each write. The record names both.

### 25.9 Overview

The start page shows the whole team at once:

- Tasks: done, in progress, in progress without activity for 60 minutes, ready for review, to do and not started, to do and unassigned.
- Pull requests: draft, ready for review, merged, closed. They are read from the code host and never owned (§24.5).
- Devices: connected, working, idle. Sessions: working, waiting for an answer, idle.
- AI usage: tokens and cost per device, per account and per day.
- Device health and server key figures (§25.10).
- Every figure can be opened down to the single task, session or event, and viewed for a past date (§24.6).

### 25.10 Monitoring sources

The hub shows data it does not own through read-only adapters:

- **Device health** (processor, memory, disk) from the team's metrics system, per device.
- **Server key figures**, for example the number of error log lines in the last 60 minutes, from the team's infrastructure monitoring.

An adapter only reads, holds a read-only credential in the hub's configuration and shows each value with its fetch time. Such a value is a cache, never a second truth. Which sources exist is deployment configuration; this public repository names none.

### 25.11 AI usage and an optional central account

Usage per device and per day comes from the usage snapshots devices already record.

Optional, later, and not part of the core: the team holds one central account at a model broker and the hub hands each device its own sub-key with a spending limit. The model traffic then still goes from the device straight to the broker, and the hub neither carries nor sees it. Passing all model traffic through the hub is the rejected variant: the hub would become a single point of failure for every session.

### 25.12 One interface for people and for AI

- Every function is an operation of one documented API. The website and the phone app are clients of that API and of nothing else.
- Nothing exists only in the website and nothing only in the API. A test fails the build when a website function has no API operation or the reverse.
- A staff member's own AI uses the same API with the key of one of their controller devices. It has that device's rights and every action is recorded under that key.
- The website can be installed on a phone as an app. It sends a push notification when a session has a question or an order ends. A push carries no content, only that something is waiting. The push services of the phone makers transport the encrypted notification; they decide no identity and can read nothing.

### 25.13 What this changes in the locked decisions

| Place | Today | With §25 |
|---|---|---|
| §2 Write owner | The device that created a row | Unchanged. A device still writes its own rows; commands to it are signed events of its owner |
| §2 Identity, §5 | Code-host login, device token | Device key assigned to a staff member; the level is a scope |
| §3 Task as the product center | Rejected | Tasks become the center of the team record; the session store stays the center of a device |
| §3 Plane (or any issue tracker) as the store | Rejected | Still rejected; tasks live in the team record and outside trackers are retired |
| §2 website-origin exception, §17 `agent assign` | The hub creates an issue on the code host | Replaced by tasks and orders once they ship |
| §17 Live terminal bytes | Ephemeral, never stored | Stored in the session log |
| §20 Website as an operations console | Refused | Accepted for watching, answering and handing over work by the owner |

### 25.14 What stays refused

- The hub authoring work, choosing a device, granting leases or moving work between devices.
- A capability scheduler.
- Autonomous merge. A human merges.
- Opening a port on a device or exposing a local store.
- A function that exists in only one front end.

### 25.15 Open decisions

Each is decided by a human before the part it affects is built:

- How long session logs are kept, and whether they follow the never-delete rule of the team record.
- Whether anyone besides the owner (for example a team lead) may write to a device.
- Whether a controller device may give work only to executors of the same staff member, or also to others with their consent.
- Whether sessions on a controller device that were not handed over are recorded in the same full detail.
- Whether push through the phone makers' services is acceptable under the rule against third-party dependencies (§24.2 covers identity only).
- Whether the optional central AI account (§25.11) is wanted.

### 25.16 Build order

After the steps of §24.10: device keys with levels and inventory; tasks, log entries and the repository registry, with the one-time import; orders and questions through the hub; the session log and the full session view; the overview; monitoring adapters; the phone app with push; the API parity test from the first of these steps on.
