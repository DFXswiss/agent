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
| Write owner | The device that created a row. The hub never becomes the author of that row, except website-created person pings, which use the named origin `web:<login>`. |
| Hub role | Full replica + fan-out + always-on website. Not a shared write database. |
| Login | Any GitHub account may sign in. Membership is *not* the GitHub org. |
| Authorization | Hardcoded teams in git. Change members with a pull request. |
| Visibility | Self always. A team only if the GitHub login is listed on that team. Several teams → union. |
| Sync (this device) | Always: own events, gapless. Also: implicit **inbox** snapshots (session mail to a session this device owns), **pings** snapshots (person pings this login sent or received), plus optional table subscriptions and one-shot queries. Not a default full pull of every visible origin. |
| Inbound apply | Own origin stays gapless (`last+1`). Foreign inbound is **row snapshots**, not gapless replay of the sender’s `origin_seq`. Same-device session mail (own origin, other session) is already in the event log; apply is idempotent on `row_id` and still `NOTIFY`s the knock. |
| Restore | Required. A wiped device comes back from the hub: own events, inbox snapshots (session mail), and pings snapshots (person pings this login sent or received). |
| Identity | GitHub login, lowercase. Device = stable UUID, not the hostname. |
| Session | No work without a `session` row. The start hook inserts it. |
| Store engine | Local PostgreSQL on loopback / Unix socket under `$AGENT_HOME`. Not world-reachable. |
| Catalog | Generic `activity` rows (`type` + payload). Session tags are the types present. |
| Skills | Optional, requested. `spine`, `review-loop`, `pr-review`, and `error-fix` exist as skills. They are **not** on by default. |
| Runtime | This public client. Team-specific rules live elsewhere and must not ship a second store binary. |
| Session mail | Addressed to a **session id**. Delivery does not require a subscription. |
| TUI knock | Script wakes the session with only `da ist Post id <uuid>`. The agent reads that row from local Postgres. |
| Device daemon | Always-on user service on this device. `agent init` installs and starts it with knock (`LISTEN` plus usage / pending / github pending / mail pending / `pr.merged` polls) and the local dashboard; daemon `sync --follow` starts only after `agent pair`, once `device.json` has token and hub URL. |
| Outside facts | Scripts notice GitHub (and other outside) state. The agent is not told by a human and does not poll GitHub. Example: a recorded PR merges → script writes `pr.merged` on that session and knocks. |
| AI vs scripts | The AI inserts local intent. Scripts perform every side effect that leaves the machine. Model text is never a state transition. |
| Checks and gates | A **check** records a fact (`agent check record`). A **gate** is a policy verdict over evidence (`agent gate record`). A model claim is neither. Confidence is not proof. |
| Pull request done | A draft plus local tests is not done. CONTRIBUTING.md is the contract for this repository. When spine and pr-review are attached, grok then Codex on this head are the gates; `agent allow --action pr-ready` only checks task state. A human merges. |
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

**Task as the product center.**  
A task exists only when the spine skill is attached. The durable unit is the session and its activity log.

**Error-to-PR as the default platform workflow.**  
Production-error ingestion is not the product core and not a default skill. The `error.*` spellings in §14 belong to the opt-in **error-fix** skill (§21). A hub `READY_FOR_PR` state is refused even as a skill: task state stays on this device (§2 Write owner, §20).

**Worker capability ontology / scheduler.**  
Vendors remain `grok` | `codex`. Scripts are not store workers. Do not add capability tables until two real workflows share them.

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

A Grok (or other TUI) runtime UUID is **not** the session id. See §17.

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

**Sign-in** is GitHub OAuth (`read:user` only). After login the browser is the team dashboard.

**Pairing is not automatic from “I opened the website”.** The browser does not know the laptop. Linking is a deliberate step in the same GitHub session.

Normal path (from the laptop):

1. `agent pair --hub <url>` (or `AGENT_HUB`) creates or reuses `device_id` and a challenge, persists them in `device.json`, and calls `POST /pair/prepare`.
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

`Runtime.is_busy` is the Grok TUI working probe in the registered pane (`Thinking…`, `Waiting for response`, `Preparing …`, `[stop]`, `Esc:cancel`, `command still running`, queued `Enter to send now`). “Model running” is that probe. The queue branch still runs when a runtime reports busy.

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
| `issue.assigned` | `issue` | script | — (no `NOTIFY` on insert; watch script knocks queue head / one Grok terminal) |
| `issue.assigned.ack` | `issue` | script | — (releases the next queued knock; supervise only) |
| `comment.post` | — | AI (target + body) | script |
| `review.post` | — | gate record (repo, number, body) | script |
| `mail.ingest` / `mail.seen` / `mail.reply` | — | script / AI | script (external mailbox) |
| `investigate.step` | `investigate` | AI (every step, immediately) | — |
| `message` | — | AI | hub + daemon + knock (session mail, §10) |
| `message.read` | — | AI or script | — |
| `query.request` / `query.result` | — | AI / script | script (hub HTTP) |
| `subscription.set` | — | AI or script | script |
| `usage.snapshot` | — | script | — (Grok billing GET on this device; no TUI knock; payload includes account email, provider, and subscription tier). Missing or expired Grok login is not a scan failure: `agent watch grok-usage` prints `usage.snapshot skipped`; the knock daemon stays silent. |
| `error.seen` | `error` | script | — (`NOTIFY` `agent_inbox`; error-fix skill, §21) |
| `error.skip` | `error` | AI | — |
| `error.fix` | `error` | AI | script + spine implement (draft pull request) |
| `supervise.event` | `supervise` | script | — (supervise follow bookkeeping / approve; optional closed-question path in tests; skip rows may carry a truncated pane excerpt; no TUI knock) |

`investigate` is the thick log: hypothesis, check, result, ruled out, still open — each a new row, at once. Other sessions can query or subscribe and see what was already tried.

### Outside facts (example: PR merged)

The agent never learns a merge from a human prompt and never calls GitHub to ask “is it merged?”.

When this device has a `pr.open` row whose script result includes the PR number/url, a **script** watches that PR. On merge it inserts `pr.merged` on the **same session** (`payload`: repo, number, url, merge SHA, merged_at). That insert `NOTIFY`s `agent_inbox` (and enqueues `wake` if needed) with the new activity id. The device daemon’s knock child and §10 state machine emit `da ist Post id <uuid>`. The watcher does not `tmux send-keys` itself. The agent `SELECT`s the row and decides what to do.

The watcher runs on this device (write owner). It is a script, not the model. The model’s next turn is the knock plus the row — not a `gh` command.

`wake` (if stored) is a local queue row for the knock; it is not a hub event.

### Outside facts (example: issue assigned)

The script reads GitHub; the model does not.

Allowlist file `$AGENT_HOME/watch.json` key `assigned_repos` (non-empty list of `Owner/repo` strings). Missing or empty is an error; there is no default list.

The first successful scan records `assigned_watch_since` and the assigned `session_id` and dispatches nothing. Later scans consider assignments whose latest matching `assigned` event is at or after that cursor, skipping ones not newer than the stored `(assigned_at, event_id)` marker — same-second events only pass when the candidate's `event_id` is known and either the stored marker has none or the candidate's is higher, so a genuinely later same-second assignment discovered in a later scan is not silently dropped. Changing `session_id` after that pin is an error. The scan uses this device’s paired GitHub login; a missing pair or a `gh api user` mismatch is an error. The queue head is the already-knocked inflight item if any, then remaining items ordered by `(assigned_at, event_id)` — oldest first, and by `event_id` ascending among any that share a timestamp — so two rows a same-second reassignment left pending process in their real GitHub order.

The writer is this device. All assignments share **one** runner session (`watch.json` `session_id`, default `assigned`, characters `A-Za-z0-9_-` only). That auto-created session attaches `spine`, `review-loop`, and `pr-review`; an existing row under the same id must already be `kind=runner`. Other sessions still attach skills themselves. There is one tmux/Grok terminal, not one per issue. Working files go to `$AGENT_HOME/sessions/<id>` or `$AGENT_SESSION_ROOT/<id>`. New `issue.assigned` rows enqueue on that session (`payload`: repo, number, url, title, body, assigned_at, assigned_by, event_id (scan-only, see below), assignee, mandate). The insert does not notify the knock daemon. The script pushes own events, writes `MANDATE.md` / `QUEUE.md` (no issue body), starts Grok only if that session is not already attached, then knocks at most the head of the queue (`da ist Post id <uuid>`). A knock of `issue.assigned` rewrites those files immediately before send. Further knocks stay queued until the **supervise script** records `issue.assigned.ack` with `payload.assigned_id`. The model must not insert that ack. The scan watermark `assigned_watch_since` is the scan clock, not the last seen GitHub event time — that is the no-backfill rule.

Optional `$AGENT_HOME/policy.json` uses the same fail-closed `admits()` gate as job admission. Before `supervise` commissions a queue head — including when the session's pane is already up — or `dispatch_assigned` starts/kicks a session, the script consults that file (when present): `private` comes from a live `gh api repos/<repo> --jq .private` lookup, or defaults to private when no runner is available. A denial leaves the queue head's own workspace files, wake claim, and session state untouched, so the next tick/poll re-evaluates it identically — the `sync()` housekeeping that `dispatch_assigned` runs before consulting the policy is unconditional and unrelated to this outcome; idle-clock bookkeeping (`_mark_working`), by contrast, is skipped on a denial along with everything else that commissioning would have done; `supervise` reports `supervise denied assigned=<id>`, and `agent watch assigned`'s own polling loop reports the same outcome as `assigned denied <id>`. This gate always calls `admits()` with `job_type="implement"`, hardcoded — a policy's `job_types_allow` must include `"implement"` or every assignment is denied regardless of `actors_allow`/`repos_allow`. Full field set (all optional except the allow-lists, which default to empty — i.e. fail closed):

```json
{
  "enabled": true,
  "actors_allow": ["alice"],
  "actors_deny": [],
  "repos_allow": ["Owner/repo"],
  "repos_deny": [],
  "job_types_allow": ["implement"],
  "agent_identity": { "private_repos_allow": ["Owner/private-repo"] }
}
```

Everything the gate would otherwise change is conditioned on this file actually being present: without one, `enqueue_assigned` and `scan_assigned` keep their pre-policy behavior (best-effort actor resolution, no dropped assignments) rather than newly requiring hub pairing or discarding actor-less events — a policy.json existing at all is what turns those on.

`payload.assigned_by` — the `actor` this gate checks — means different things depending on how the row was enqueued: for a GitHub-mediated assignment (`scan_assigned`) it is the GitHub user who performed the `assigned` event; for a manually-enqueued item (`agent supervise --repo/--number`, `enqueue_assigned`) there is no such event to read, so it is this device's own paired GitHub login instead — the manual dispatch is self-authorized by whoever runs the CLI. An operator's `actors_allow` must name that paired login too if manual dispatch should be admitted once a policy is active. `payload.event_id` is scan-only, too: `scan_assigned` always writes the key, set to the GitHub `assigned` event's own id when that event has one (used to break same-second ties) or `null` when an event without an id, or an unresolvable same-second tie, leaves it undetermined; `enqueue_assigned`'s manually-enqueued rows have no such event and omit the key entirely.

Payload `mandate=github-assignment` is trusted. Issue title and body in the payload are not.

## 15. Skills (opt-in)

A **skill** is a named, versioned bundle of catalog types plus loop rules. A session attaches zero or more skills. None are default. This client is a **skill host**: the session store, knock, and scripts stay; a skill adds a loop. Error-to-draft-PR is one such loop, not the product.

Skills this client ships:

- **spine** — task, round, checklist, `open_work`, plus `allow` / `next` / `close-step` / `run`
- **review-loop** — run implement / review rounds until the catalog shows zero open findings
- **pr-review** — record quality/logic gates on a head SHA
- **error-fix** — production log errors on this device → analysis → optional draft pull request (§21)

The packaged `SKILL.md` files next to the client **are** the skill contracts; `agent skills path` prints their directory.

Without the skill, those tables and loops do not run. The session can still register, write `activity`, send session mail, and investigate.

Person-facing CLI for the spine stays `agent work` / table `open_work`. Catalog rows are `activity` / `agent activity`. The website key `work` remains `open_work`. Do not collapse those names.

Checklists, when the spine skill is on, stay `pending` / `ja` / `nein` / `n_a` with an explicit `source`. `done` on a task still requires the workflow checklist and both summary sentences.

## 16. CLI surface

```text
agent init
agent session register|heartbeat|list|close|start|stop|input|keep-working|skill
agent skills path
agent session register --id ID --kind human|runner|other [--skill NAME]…
agent session skill attach --id ID --skill spine|review-loop|pr-review|error-fix
agent session skill list --id ID
agent session start --id ID [--provider grok] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session stop --id ID
agent session input --id ID --data TEXT
agent session input --id ID --key enter|ctrl-c|tab
agent session keep-working --id ID [--once|--follow]
# permission modal: Enter. first idle composer: standing "work until complete";
# later idle: "Continue.". never types while in-flight or when the pane is empty.
# default / --once is one tick; --follow polls every 30s.
agent activity add --session ID --type TYPE --payload-file FILE
agent task create|list|show|state|summary          # spine skill
agent checklist set …                             # spine skill
agent round start --task UUID                     # spine skill
agent agent start|finish …                        # review-loop (implementer|reviewer) or pr-review (pr-reviewer-*)
agent check record …                              # spine skill
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
agent daemon [--install|--uninstall]           # always-on supervisor; init installs the user service
agent knock [--once]                           # --once drains; without --once is foreground; user service is the supported always-on path
agent watch pr-merged                          # one scan; device daemon covers the loop
agent watch pending                            # one scan; LISTEN agent_work / execute subscription.set and query.request
agent watch grok-usage                         # one scan; knock child (under the device daemon) polls every 60s
agent watch assigned [--follow]                # allowlisted GitHub assignments → runner session + knock
agent watch errors                             # one scan; $AGENT_HOME/error-fix.json; knock daemon polls with grok-usage
agent watch error-fix                         # one scan; find-or-create implement task + isolated worktree; knock daemon polls with grok-usage
agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]
agent status
agent dashboard [--port 7845]
```

Local dashboard binds `127.0.0.1` only.

The AI is not expected to type hub HTTP, `gh`, or himalaya. It inserts `activity` (and `query.request` / `subscription.set` / `mail.reply` / `mail.seen`). Scripts watch the store. `agent github pending` is the GitHub executor for owned pending `pr.open`, `comment.post`, `review.post`, and `issue.write` rows. `agent mail pending` is the mailbox executor for owned pending `mail.reply` and `mail.seen` rows. The knock poll (device daemon knock child) also runs `scan_github` and `scan_mail`. `agent run` git-pushes (no force) when `pushed` is open and measures GitHub mergeability and checks when `mergeable` is open.

## 17. Control

One product with the hub. The team reads every visible session; **this device writes and controls only its own rows**. A foreign session on the local dashboard is watch-only.

**Live terminal bytes are not store events.** They travel on the sync WebSocket as ephemeral `terminal` frames (base64 pane captures) while `agent sync --follow` is connected. They are never written into the event log.

**tmux is the process holder; the hub is not.** The local client is the only place that starts, stops, or types into a live terminal. The hub may send `control` frames (`start` / `stop` / `input` / `resize`); this device executes them only when it owns the session row, then replies with `control-ack`. After connect, the client sends `control-ready`. Control and terminal message types must not trigger push+pull.

Owned-row runtime fields (start/stop set control and tmux; `keep-working` may also update `keep_working`; not a new vendor):

```json
"runtime": {
  "tmux_session": "agent-…",
  "control": "attached" | "stopped",
  "cols": 80,
  "rows": 24,
  "provider": "grok",
  "grok_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "model": "grok-4.6",
  "keep_working": { "standing_sent": true }
}
```

Start sets `control=attached` and the tmux name. Stop sets `control=stopped` and keeps the name. `agent session keep-working` updates `runtime.keep_working.standing_sent` on idle ticks so the standing instruction is sent once. Session `status` (`active` / `closed`) is separate; `session close` stays as it is.

**Grok Build launch** (`--provider grok` or control `{provider: "grok"}`) is not the store session id. The Grok CLI `--session-id` flag accepts only a UUID (`8-4-4-4-12`). A caller-chosen session id (including a ULID) is never passed through. First start mints `runtime.grok_session_id` and runs `grok --always-approve --session-id <uuid> --model grok-4.6`. Later starts, if that field is set, run `grok --always-approve --resume <uuid> --model grok-4.6`. An empty model becomes `grok-4.6`; it must not inherit a Claude default. The pane is started with `env -u ANTHROPIC_API_KEY -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT` so Claude credentials do not leak into the Grok process. `--provider` and `--cmd` cannot be combined.

**Vendors remain `grok` | `codex`.** A process running inside a tmux pane is not a store vendor. There is no `vendor=claude` and no shell-string tmux driver: the runtime invokes `tmux` with argv lists only. `runtime.provider` is launch metadata, not a review-gate vendor.

Local dashboard (`127.0.0.1`): sessions expose `can_control` when `_origin_device_id` is this device. `POST /api/sessions/{id}/control` applies the same actions locally and does not call the hub.

The knock in §10 uses the **registered pane**, not `tmux_session` derived from the session id. `agent session input` remains argv-only (`send-keys -l` then a separate Enter).

## 18. What “done” for the product still needs (operators)

These are not silent defaults in code; they are human steps after merge:

1. Merge the public pull requests.
2. Create a GitHub OAuth App whose callback is `{public-url}/auth/github/callback`.
3. Deploy `agent-core` with every `AGENT_CORE_*` variable set.
4. Add GitHub logins to `teams.yaml` via pull request.
5. On each laptop: PostgreSQL 15+ (`initdb`/`pg_ctl` on `PATH`, or `AGENT_PG_BIN` / `AGENT_PG_DSN`), `pip install -e .`, `agent init` (installs and starts the user-service daemon for knock, usage, pending, github pending, mail pending, `pr.merged`, and the local dashboard; daemon `sync --follow` starts only after pair, once `device.json` has token and hub URL), `agent pair --hub …`. Do not leave a separate `agent knock` or `agent sync --follow` as the always-on path; one-shot `agent sync` remains fine after pairing.

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
| Spine task state | Spine CLI + local constraints | `task` state, checklist keys, `close-step` / `run` |
| Whether a head is reviewed | Gate after the two vendor stages | `agent gate record` |
| Whether a command ran and what it returned | The process that ran it | `agent check record` (name, command, `pass`/`fail`/`skip`, output) |
| Merge | Human | Never a client command |

A worker report such as “analysis complete” or “tests passed” is **input**. It is not the transition. Opening a draft is not done.

No transition that needs deterministic evidence may be satisfied by model text alone. Malformed structured output is rejected (unknown `activity.type` → `execution_status=error`; empty, partial, timeout, or unavailable review output is not zero findings). A patch that does not apply is a failed check, not a debate.

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

Write owner, hub role, opt-in skills, required session row, and the generic `activity` catalog stay as in §2. Spine task states stay the spine skill’s states. They are not replaced by a hub machine such as `CREATED` / `ANALYZING` / `READY_FOR_PR`.

### 19.6 Pull request done

A draft plus local tests is not done. A check records the local suite. When spine and pr-review are attached, a gate records each vendor dimension on **this** head, and leave-draft is four lane verdicts (grok quality and grok logic, then Codex quality and Codex logic) on this head plus CI green on this head. Without those skills, the target repository’s written contributing rules apply. `agent allow --action pr-ready` only checks that a task is in `pushing` or `pr-review`; it is not the leave-draft verdict. `task-done` still needs the workflow checklist and both summary sentences.

Quality and logic of one vendor stage run together. Vendors are `grok`, then `codex`. Codex runs only after both grok dimensions are `approved`. The session that authored the diff does not sit those PR reviews. If a vendor cannot run, abort loudly; do not record `approved`; do not substitute another vendor. Empty, partial, timeout, or unavailable review output is not zero findings.

CI on this head is a script-measured fact. `skipped` and `cancelled` are not green unless the workflow documents that skip. Stay draft until that holds. One comment whose review-pass count is those four `approved` verdicts on this head, then ready. A retry reuses the existing draft. A human merges.

## 20. Refused: hub as a coding control plane

An external architecture draft proposed turning the hub into an authoritative Error-to-PR control plane: hub-owned Task objects, leases, a worker scheduler, production-error ingestion as the first workflow, and `READY_FOR_PR` then deterministic pull-request creation.

That draft is **not** this product. Hub authorship, hub leases, a hub task machine, a parallel `external_events` store, autonomous merge, and a capability scheduler stay refused even as a skill.

| Draft idea | Why refused |
|---|---|
| Task as the durable center; sessions are operational | Product center is the session store. Tasks are spine, opt-in. |
| Hub assigns, leases, and reassigns work | Hub is replica + fan-out, not author. A disappeared laptop is restore or unread, not a lease expiry that moves authorship. |
| Formal `CREATED` → `READY_FOR_PR` machine as platform | Spine already has a local task machine. A second hub machine would be a second truth. |
| Normalized `external_events` table + fingerprint service | Outside facts are script-written `activity` rows (`pr.merged`, …). |
| Worker capability ontology and scheduler | Vendors are `grok` \| `codex`. Scripts are not store workers. |
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

`session_id`, `url`, and `query` are required. The default fetch is a Loki-compatible `query_range` GET (`query`, `start`, `end`, `limit`, `direction=forward`). The JSON body is `{ "data": { "result": [ { "stream": {}, "values": [[ns, line], ...] } ] } }`. Optional `service`, `environment`, `repo`, `limit`, `line_must_match`, `line_must_not_match`. Cursor is nanoseconds in `$AGENT_HOME/error-fix.cursor`; the next `start` is that value plus one. Credentials come from netrc or `AGENT_ERROR_FIX_USER` / `AGENT_ERROR_FIX_PASSWORD` — never from `error-fix.json`, the store, or the URL. The knock daemon (`agent knock` without `--once`) polls this scan on the same 60s interval as grok-usage when the config file exists.

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
3. Spine implement: mandatory checks must `pass`, then `pr.open` opens a **draft** (spine `pushed`). Title/body may be model-drafted; the GitHub API call is a script. A retry finds an existing draft for this fingerprint instead of opening a second one.
4. pr-review gates run on that head after `pushed`.
5. A human merges. `pr.merged` knocks as today.

The model never receives production credentials. Analysis that only reads the excerpt does not need write access to the origin branch.

`agent watch error-fix` find-or-creates the implement task and clones `https://github.com/<repo>.git` into `$AGENT_HOME/error-fix-work/<task_id>`; `agent github pending` still opens drafts, and a retry draft uses the existing head `error-fix-<id8>`.

### 21.6 Not in this revision

- A second hub state machine, leases, or autonomous merge

## 22. Static supervise loop (v1)

A second model must not orchestrate the first. `agent supervise` is a **script** with locked questions and locked answers. Model text is not a state transition.

- One pending `issue.assigned` at a time (same queue as `agent watch assigned`).
- `--repo` / `--number` enqueues that issue as `github-assignment` without hub pairing when no `$AGENT_HOME/policy.json` is present; with one, pairing is required so the enqueued `assigned_by` can be checked against it (§14). Title and body stay untrusted payload.
- Busy means the Grok TUI in the tmux pane shows an in-flight turn (`Thinking…`, `Waiting for response`, `Preparing …`, `[stop]`, `Esc:cancel`, `command still running`, or a queued follow-up with `Enter to send now`). `Runtime.is_busy` is that probe. The script does not type while busy.
- Follow (`ask=False`, the CLI default) does not knock an existing session, does not ask closed questions, and does not auto-continue. It only confirms a Grok tool-approval modal (`1/3:select` plus `Tab:next option` → Enter). Closed questions remain available to `tick(..., ask=True)` for tests. Consecutive idle ticks (`supervise quiet` / `supervise stalled`) are follow-loop bookkeeping, not Telegram pages. The footer badge `always-approve` is not a working signal.
- When `ask=True`, `Ja` or a blocking problem → `issue.assigned.ack` and the next queue item. A blocking problem also stores a truncated pane excerpt on `supervise.event` (`kind=skip`).
- `supervise.event` is script-only and does not knock.
- Optional Telegram status posts (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) are a script HTTP side-effect. They are not person pings and not the hub. Missing env is a no-op. Telegram `not working` is posted only when the Grok tmux session is gone (`runtime.exists` is false), once until the session returns. An idle prompt after `Worked for` is not an outage. A send failure does not stop the loop.

Phase 1 is this loop plus a backlog. Smarter questions are later.

## 23. Document history

Recorded from the design thread that specified realtime team visibility, rejected a central write database and a mesh, rejected embedding the hub in the existing public API, chose GitHub login + git teams, and split the work into `agent` + `agent-core`. Control: local tmux ownership, hub control frames, ephemeral terminal bytes. Grok launch: own UUID in `runtime.grok_session_id`, `--resume` on later starts, default model `grok-4.6`, no Claude environment in the pane.

This revision replaces default complete pull with own events + inbox/subscription snapshots, moves the local engine to PostgreSQL, requires a session row, adds the `activity` catalog and opt-in skills, and adds session-addressed mail with a TUI knock of `da ist Post id <uuid>` only.

Deterministic core (§19) and the refused hub control plane (§20) lock the split that was already in §§1–17: scripts execute, checks measure, gates decide, and model text is never a transition. Error-to-PR is not the product core and not a hub workflow; it is the opt-in **error-fix** skill on this device (§21). The static supervise loop (§22) is a script with locked questions; a second model does not orchestrate the first.
