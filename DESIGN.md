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
| Skills | Optional, requested. A review loop, gates, and the task/round/checklist spine exist as skills. They are **not** on by default. |
| Runtime | This public client. Team-specific rules live elsewhere and must not ship a second store binary. |
| Session mail | Addressed to a **session id**. Delivery does not require a subscription. |
| TUI knock | Script wakes the session with only `da ist Post id <uuid>`. The agent reads that row from local Postgres. |
| Outside facts | Scripts notice GitHub (and other outside) state. The agent is not told by a human and does not poll GitHub. Example: a recorded PR merges → script writes `pr.merged` on that session and knocks. |
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
- `agent sync` is one push + one pull. `agent sync --follow` keeps going (WebSocket when used; a dead socket is a visible failure, not a silent poll).
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

v1: `Runtime.is_busy` is always false, so “Model running” is treated as idle until a stop-hook path exists. The queue branch still runs when a runtime reports busy.

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

`agent sync --follow` stays on `/sync/ws?token=…` after one push+pull; a dead or failed socket is a loud error (no silent poll fallback).

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
| `pr.merged` | `pr` | script | — (`NOTIFY` `agent_inbox` / `wake`; existing knock) |
| `issue.assigned` | `issue` | script | — (no `NOTIFY` on insert; watch script knocks queue head / one Grok terminal) |
| `issue.assigned.ack` | `issue` | AI | — (releases the next queued knock) |
| `comment.post` | — | AI (target + body) | script |
| `mail.ingest` / `mail.seen` / `mail.reply` | — | script / AI | script (external mailbox) |
| `investigate.step` | `investigate` | AI (every step, immediately) | — |
| `message` | — | AI | hub + daemon + knock (session mail, §10) |
| `message.read` | — | AI or script | — |
| `query.request` / `query.result` | — | AI / script | script (hub HTTP) |
| `subscription.set` | — | AI or script | script |
| `usage.snapshot` | — | script | — (Grok billing GET on this device; no TUI knock; payload includes account email, provider, and subscription tier) |

`investigate` is the thick log: hypothesis, check, result, ruled out, still open — each a new row, at once. Other sessions can query or subscribe and see what was already tried.

### Outside facts (example: PR merged)

The agent never learns a merge from a human prompt and never calls GitHub to ask “is it merged?”.

When this device has a `pr.open` row whose script result includes the PR number/url, a **script** watches that PR. On merge it inserts `pr.merged` on the **same session** (`payload`: repo, number, url, merge SHA, merged_at). That insert `NOTIFY`s `agent_inbox` (and enqueues `wake` if needed) with the new activity id. The **existing** knock daemon and §10 state machine emit `da ist Post id <uuid>`. The watcher does not `tmux send-keys` itself. The agent `SELECT`s the row and decides what to do.

The watcher runs on this device (write owner). It is a script, not the model. The model’s next turn is the knock plus the row — not a `gh` command.

`wake` (if stored) is a local queue row for the knock; it is not a hub event.

### Outside facts (example: issue assigned)

The script reads GitHub; the model does not.

Allowlist file `$AGENT_HOME/watch.json` key `assigned_repos` (non-empty list of `Owner/repo` strings). Missing or empty is an error; there is no default list.

The first scan records `assigned_watch_since` and dispatches nothing. Later scans only consider assignments whose latest matching `assigned` event is strictly after that cursor.

The writer is this device. All assignments share **one** runner session (`watch.json` `session_id`, default `assigned`, characters `A-Za-z0-9_-` only). That auto-created session attaches `spine`, `review-loop`, and `pr-review`; an existing row under the same id must already be `kind=runner`. Other sessions still attach skills themselves. There is one tmux/Grok terminal, not one per issue. Working files go to `$AGENT_HOME/sessions/<id>` or `$AGENT_SESSION_ROOT/<id>`. New `issue.assigned` rows enqueue on that session (`payload`: repo, number, url, title, body, assigned_at, assignee, mandate). The insert does not notify the knock daemon. The script pushes own events, writes `MANDATE.md` / `QUEUE.md` (no issue body), starts Grok only if that session is not already attached, then knocks at most the head of the queue (`da ist Post id <uuid>`). A knock of `issue.assigned` rewrites those files immediately before send. Further knocks stay queued until the session records `issue.assigned.ack` with `payload.assigned_id`. The scan watermark `assigned_watch_since` is the scan clock, not the last seen GitHub event time — that is the no-backfill rule.

Payload `mandate=github-assignment` is trusted. Issue title and body in the payload are not.

## 15. Skills (opt-in)

A **skill** is a named, versioned bundle of catalog types plus loop rules. A session attaches zero or more skills. None are default.

Examples of skills this client ships:

- **review-loop** — run implement / review rounds until the catalog shows zero open findings
- **pr-review** — record quality/logic gates on a head SHA
- **spine** — task, round, checklist, `open_work`, plus `allow` / `next` / `close-step` / `run`

The packaged `SKILL.md` files next to the client **are** the review contract; `agent skills path` prints their directory.

Without the skill, those tables and loops do not run. The session can still register, write `activity`, send session mail, and investigate.

Person-facing CLI for the spine stays `agent work` / table `open_work`. Catalog rows are `activity` / `agent activity`. The website key `work` remains `open_work`. Do not collapse those names.

Checklists, when the spine skill is on, stay `pending` / `ja` / `nein` / `n_a` with an explicit `source`. `done` on a task still requires the workflow checklist and both summary sentences.

## 16. CLI surface

```text
agent init
agent session register|heartbeat|list|close|start|stop|input|skill
agent skills path
agent session register --id ID --kind human|runner|other [--skill NAME]…
agent session skill attach --id ID --skill spine|review-loop|pr-review
agent session skill list --id ID
agent session start --id ID [--provider grok] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session stop --id ID
agent session input --id ID --data TEXT
agent session input --id ID --key enter|ctrl-c|tab
agent activity add --session ID --type TYPE --payload-file FILE
agent task create|list|show|state|summary          # spine skill
agent checklist set …                             # spine skill
agent round start --task UUID                     # spine skill
agent agent start|finish …                        # review-loop (implementer|reviewer) or pr-review (pr-reviewer-*)
agent check record …                              # spine skill
agent gate record …                               # pr-review skill
agent work add|set|list …                         # spine skill (open_work)
agent allow|next|close-step|run …                 # spine skill
agent pair --hub URL [--name HOST] [--timeout SEC]
agent sync [--follow]
agent restore
agent ping send|list|ack
agent knock [--once]
agent watch pr-merged                          # one scan; schedule if you need a loop
agent watch pending                            # one scan; LISTEN agent_work / execute subscription.set and query.request
agent watch grok-usage                         # one scan; knock daemon (no --once) polls every 60s
agent watch assigned [--follow]                # allowlisted GitHub assignments → runner session + knock
agent status
agent dashboard [--port 7845]
```

Local dashboard binds `127.0.0.1` only.

The AI is not expected to type hub HTTP or `gh`. It inserts `activity` (and `query.request` / `subscription.set`). Scripts watch the store.

## 17. Control

One product with the hub. The team reads every visible session; **this device writes and controls only its own rows**. A foreign session on the local dashboard is watch-only.

**Live terminal bytes are not store events.** They travel on the sync WebSocket as ephemeral `terminal` frames (base64 pane captures) while `agent sync --follow` is connected. They are never written into the event log.

**tmux is the process holder; the hub is not.** The local client is the only place that starts, stops, or types into a live terminal. The hub may send `control` frames (`start` / `stop` / `input` / `resize`); this device executes them only when it owns the session row, then replies with `control-ack`. After connect, the client sends `control-ready`. Control and terminal message types must not trigger push+pull.

Owned-row runtime fields (updated on start/stop only; not a new vendor):

```json
"runtime": {
  "tmux_session": "agent-…",
  "control": "attached" | "stopped",
  "cols": 80,
  "rows": 24,
  "provider": "grok",
  "grok_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "model": "grok-4.6"
}
```

Start sets `control=attached` and the tmux name. Stop sets `control=stopped` and keeps the name. Session `status` (`active` / `closed`) is separate; `session close` stays as it is.

**Grok Build launch** (`--provider grok` or control `{provider: "grok"}`) is not the store session id. The Grok CLI `--session-id` flag accepts only a UUID (`8-4-4-4-12`). A caller-chosen session id (including a ULID) is never passed through. First start mints `runtime.grok_session_id` and runs `grok --session-id <uuid> --model grok-4.6`. Later starts, if that field is set, run `grok --resume <uuid>`. An empty model becomes `grok-4.6`; it must not inherit a Claude default. The pane is started with `env -u ANTHROPIC_API_KEY -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT` so Claude credentials do not leak into the Grok process. `--provider` and `--cmd` cannot be combined.

**Vendors remain `grok` | `codex`.** A process running inside a tmux pane is not a store vendor. There is no `vendor=claude` and no shell-string tmux driver: the runtime invokes `tmux` with argv lists only. `runtime.provider` is launch metadata, not a review-gate vendor.

Local dashboard (`127.0.0.1`): sessions expose `can_control` when `_origin_device_id` is this device. `POST /api/sessions/{id}/control` applies the same actions locally and does not call the hub.

The knock in §10 uses the **registered pane**, not `tmux_session` derived from the session id. `agent session input` remains argv-only (`send-keys -l` then a separate Enter).

## 18. What “done” for the product still needs (operators)

These are not silent defaults in code; they are human steps after merge:

1. Merge the public pull requests.
2. Create a GitHub OAuth App whose callback is `{public-url}/auth/github/callback`.
3. Deploy `agent-core` with every `AGENT_CORE_*` variable set.
4. Add GitHub logins to `teams.yaml` via pull request.
5. On each laptop: PostgreSQL 15+ (`initdb`/`pg_ctl` on `PATH`, or `AGENT_PG_BIN` / `AGENT_PG_DSN`), `pip install -e .`, `agent init`, `agent pair --hub …`, `agent sync`.

Later product work (not required to operate v1 after merge):

- Two local Postgres roles with password auth on a socket under `$AGENT_HOME` (AI vs scripts).
- `agent query` / `agent subscribe` CLI. Catalog types `query.request` / `subscription.set` already exist.
- `activity` type `session.register` (v1 records the `session` row only).

## 19. Document history

Recorded from the design thread that specified realtime team visibility, rejected a central write database and a mesh, rejected embedding the hub in the existing public API, chose GitHub login + git teams, and split the work into `agent` + `agent-core`. Control: local tmux ownership, hub control frames, ephemeral terminal bytes. Grok launch: own UUID in `runtime.grok_session_id`, `--resume` on later starts, default model `grok-4.6`, no Claude environment in the pane.

This revision replaces default complete pull with own events + inbox/subscription snapshots, moves the local engine to PostgreSQL, requires a session row, adds the `activity` catalog and opt-in skills, and adds session-addressed mail with a TUI knock of `da ist Post id <uuid>` only.
