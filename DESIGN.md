# Agent — product design

This document records the decisions for the team ledger: local write ownership, a hub that fans events out, GitHub login, team-scoped visibility, full bidirectional sync, restore, and peer pings.

The wire contract lives in [agent-core PROTOCOL.md](https://github.com/DFXswiss/agent-core/blob/develop/PROTOCOL.md). This file is the *why* and the product rules. Implementation details that drift should lose to this document until a pull request changes it.

## 1. Purpose

Every person who installs the client keeps a **local ledger** of sessions, tasks, rounds, checklists, review gates and pings.

The team also needs:

- a **live view** of who is working on what
- **every teammate able to read every teammate’s ledger** (within a team)
- **direct pings** between people (for example a review request)
- a **central website** after GitHub sign-in
- a **wiped laptop** that can be rebuilt from the hub

“Realtime” means push on write (milliseconds locally, typically well under a second across the hub), not a multi-second poll as the source of truth.

## 2. Locked decisions

| Topic | Decision |
|---|---|
| Write owner | The device that created a row. The hub never becomes the author of that row. |
| Hub role | Full replica + fan-out + always-on website. Not a shared write database. |
| Login | Any GitHub account may sign in. Membership is *not* the GitHub org. |
| Authorization | Hardcoded teams in git. Change members with a pull request. |
| Visibility | Self always. A team only if the GitHub login is listed on that team. Several teams → union. |
| Sync | Complete, both directions, every event. Not “open tasks only”. |
| Restore | Required. A wiped device must come back from the hub. |
| Identity | GitHub login, lowercase. Device = stable UUID, not the hostname. |
| Repos | Public MIT: `DFXswiss/agent` (client), `DFXswiss/agent-core` (hub). |
| Website host | `agent.dfx.swiss` (development: `dev.agent.dfx.swiss`). Singular product name. |
| License | MIT. |
| Not in the existing public API service | The customer API keeps its own auth, process and blast radius. |

## 3. Rejected alternatives

These were considered and rejected.

**One shared database everyone writes to.**  
Breaks offline work and the rule that the local ledger is the source of truth. A hub outage would stop every session.

**Postgres logical replication or a laptop mesh.**  
Serial IDs collide across machines. Laptops sleep and sit behind NAT. There is no always-on view when nobody is online.

**WireGuard / peer mesh between employee devices.**  
Even a single permissioned node in this organisation needed a public relay because inbound ports are not freely available. N laptops on hotel Wi‑Fi is worse. Local Postgres must never be exposed.

**Put the hub inside the existing public API service.**  
That service is the customer API: wallet/account JWT, staff mail + TOTP + KYC, a saturated event loop, crown-jewel data, 100 % coverage rules. GitHub-org membership is not a staff user. A leak or stall in sync would take payments and KYC with it. The only WebSocket there is checkout-device delivery. Patterns may be copied; the process must not be shared.

**Plane (or any issue tracker) as the ledger.**  
Planning is not session / round / gate / checklist. A second truth would appear.

**Cloudflare Access email + per-device service tokens as the product login.**  
The website is GitHub. One sign-in pairs the device and opens the team dashboard. Access tokens stay out of this product.

**Telegram as the ping transport.**  
Existing bots are product channels, not person-to-person. Pings are first-class ledger rows on the same event bus.

**Expose local ports (`5477`, `7845`) or the local database.**  
They stay on `127.0.0.1`. Nobody reads a teammate by opening their Postgres.

**Hostname `ledger.*`.**  
Collides with an unrelated public ledger product. The name is **agent**.

## 4. Two repositories

| Repo | Role |
|---|---|
| [DFXswiss/agent](https://github.com/DFXswiss/agent) | Client: CLI `agent`, local SQLite ledger, local dashboard, pair / sync / restore. |
| [DFXswiss/agent-core](https://github.com/DFXswiss/agent-core) | Hub: GitHub OAuth, pairing, team file, event store, website, pings. |

App code lives here. How a particular environment is deployed is out of scope for these public repos (no internal hostnames). Compose in `agent-core` is generic.

Default branch: `develop`. Image tags and environment mapping follow the organisation’s usual public/private split; this document does not name those hosts.

## 5. Identity

Three distinct IDs:

| ID | Meaning |
|---|---|
| GitHub login | The person. Always stored lowercase. Comes only from GitHub OAuth, never from a client field. |
| `device_id` | One machine. UUID persisted in `$AGENT_HOME/device.json`, **not** only inside `ledger.sqlite`. |
| Session id | One working session on that device (`human`, `runner`, `other`). |

One person, two laptops → two devices, one login.  
A runner on an already paired machine inherits that device’s login. No second OAuth.

The hub binds `origin_device_id` to the GitHub login of the session that **confirmed** pairing. The client cannot choose that login.

## 6. Visibility and teams

Anyone may log in. Seeing other people requires a team listing.

```yaml
# agent-core/teams.yaml — edit only via pull request
teams:
  dfx:
    members:
      - some-github-login
```

Rules:

- A login on **no** team still signs in, pairs, syncs and restores — and sees **only itself**.
- A login on a team sees **self ∪ every member of that team**.
- A login on several teams sees the **union**.
- Comparison is case-insensitive; stored lowercase. Duplicate members in one team are rejected.
- The hub **stores** everyone’s events regardless (otherwise restore dies). The file decides **read**, not write.
- Pings may only target a login the sender is allowed to see. Otherwise 403.
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

Website-first (phone, another machine): login shows the dashboard. No new device. Pairing still needs the CLI (or a short-lived pair mode). The public origin must not read the local ledger over CORS.

Revoke lives on the website later; a revoked token is 401. Removing a GitHub login from every team does not delete history; it only removes **read** of others.

## 8. Local ledger (this repo)

- Home: `$AGENT_HOME` if set and non-empty, otherwise `~/.local/share/agent`.
- Database: `ledger.sqlite` (WAL, foreign keys). Mode `0600`, directory `0700`.
- Identity: `device.json` next to it (`device_id`, token, login, hub URL, pending challenge). Wiping only the sqlite file must not mint a new device.
- All local mutations go through an append-only `ledger_event` stream with `origin_seq` starting at 1 on **this** device.
- Materialized `row_data` is what the dashboard and CLI read.
- A row whose `origin_device_id` is not this device is **read-only**. The CLI exits instead of updating it.
- Primary keys for tasks, agents, checks, gates, pings are UUIDs. Session ids stay caller-chosen strings.
- Session kinds: `human` | `runner` | `other`.
- Checklists stay `pending` / `ja` / `nein` / `n_a` with an explicit `source`. `done` on a task still requires the workflow checklist and both summary sentences.

The older private plugin ledger (local Postgres on a loopback port) is a predecessor. This product is the public client. It does not share that database.

## 9. Sync and restore

Each device is the write owner of its own events. The hub keeps a **complete** copy of every origin.

| Direction | What moves |
|---|---|
| Device → hub | Every local event, in `origin_seq` order, no gaps. |
| Hub → device | Every event of **visible** origins the device does not yet have. |
| Empty or behind device | Restore replays the full visible history. |
| Hub behind the device | The device pushes the missing seqs. |

Rules:

- `origin_seq` is per device, strictly `last+1`. A gap is 409 / a hard local error. The exact same event is idempotent. The same seq with different content (including `occurred_at`) is a conflict.
- Foreign `origin_device_id` on push is 403.
- A push must not steal a replica row owned by another device (same `table`+`row_id`).
- **Pull is per origin**, not a single global hub cursor. Query: `GET /sync/pull?cursor=<origin>:<last_seq>&…`. An origin without a cursor starts at 0. That is how a **new teammate** is backfilled after a `teams.yaml` change — a global cursor would skip their history.
- `GET /sync/restore` returns every visible event (`events`) plus the caller’s own subset (`own_events`). A wiped laptop applies `events` in origin order and records each origin’s last seq.
- `agent sync` is one push + one pull. `agent sync --follow` keeps going (WebSocket when used; a dead socket is a visible failure, not a silent poll).
- Missing hub URL or device token is a loud error. There is no default hub.

## 10. Pings

First-class rows, same bus, not a misuse of `open_work` or GitHub.

```text
agent ping send --to <github-login> --kind review-request|ping|question [--task UUID] [--note TEXT]
agent ping list
agent ping ack --id <uuid>
```

- Target is a **person** (GitHub login), not a session.
- Target must be visible to the sender.
- Ack is only allowed for the recipient.
- A ping received via sync is owned by the **sender’s** device. The recipient must **not** `store.write` that row. Ack goes to `POST /api/pings/{id}/ack` with the device token (or the website cookie). The hub records a recipient-side event and does not transfer ownership of the ping.
- Website-created pings are real ledger events (synthetic origin `web:<login>`), so `sync pull` delivers them. A replica-only write is not enough.

## 11. Realtime

| Place | Mechanism |
|---|---|
| Local dashboard | Materialized rows after each local write. A short UI refresh is display only. |
| Across people | The hub publishes to SSE (`/api/stream`) and WebSocket (`/sync/ws?token=…`) **filtered by visibility**. Unrelated logins must not see foreign activity metadata. |
| Offline | Events queue locally. On reconnect, per-origin catch-up. |

`agent sync --follow` stays on `/sync/ws?token=…` after one push+pull; a dead or failed socket is a loud error (no silent poll fallback). The previous local HTML dashboard that polled every three seconds is not the contract.

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
| Local ledger + `device.json` | Unique | Not rebuildable from git. |
| Hub event log + replicas | Unique | Only complete team copy; required for restore. |
| “Connected right now” | Rebuildable | Derived from open sockets. |

Evidence and command output travel with the replica. They are team-visible. Secrets do not belong in `evidence`.

## 14. CLI surface

```text
agent init
agent session register|heartbeat|list|close|start|stop|input
agent session start --id ID [--provider grok] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session stop --id ID
agent session input --id ID --data TEXT
agent session input --id ID --key enter|ctrl-c|tab
agent task create|list|show|state|summary
agent checklist set --task ID --key KEY --status ja|nein|n_a|pending --source human|runner|script
  [--evidence TEXT] [--deviation-declared true|false] [--deviation-granted true|false]
  [--granted-by TEXT] [--actor-session ID]
agent round start --task UUID
agent agent start --session ID --task UUID --role ROLE --vendor grok|codex [--round N]
  (implementer and reviewer require --round N and --vendor grok)
agent agent finish --id UUID --verdict VERDICT [--note TEXT]
agent check record --task UUID --name NAME --command CMD --result pass|fail|skip [--output TEXT]
agent gate record --task UUID --stage grok-pr|codex-pr --dimension quality|logic --vendor grok|codex --verdict approved|rejected --head SHA --agent UUID [--evidence TEXT]
agent work add --session ID --key KEY --closable-by agent|human [--note TEXT]
agent work set --session ID --key KEY --status open|done|cancelled --source human|runner|script [--actor-session ID]
agent work list [--session ID]
agent pair --hub URL [--name HOST] [--timeout SEC]
agent sync [--follow]
agent restore
agent ping send|list|ack
agent status
agent dashboard [--port 7845]
```

Local dashboard binds `127.0.0.1` only. SQLite connections used from the dashboard server must allow the request threads (`check_same_thread=False` or a connection per request).

## 15. What “done” for the product still needs (operators)

These are not silent defaults in code; they are human steps after merge:

1. Merge the public pull requests.
2. Create a GitHub OAuth App whose callback is `{public-url}/auth/github/callback`.
3. Deploy `agent-core` with every `AGENT_CORE_*` variable set.
4. Add GitHub logins to `teams.yaml` via pull request.
5. On each laptop: `pip install -e .`, `agent init`, `agent pair --hub …`, `agent sync`.

## 16. Control

One product with the hub. The team reads every visible session; **this device writes and controls only its own rows**. A foreign session on the local dashboard is watch-only.

**Live terminal bytes are not ledger events.** They travel on the sync WebSocket as ephemeral `terminal` frames (base64 pane captures) while `agent sync --follow` is connected. They are never written into `ledger_event` or `row_data`.

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

Start sets `control=attached` and the tmux name. Stop sets `control=stopped` and keeps the name. Ledger session `status` (`active` / `closed`) is separate; `session close` stays as it is.

**Grok Build launch** (`--provider grok` or control `{provider: "grok"}`) is not the ledger session id. The Grok CLI `--session-id` flag accepts only a UUID (`8-4-4-4-12`). A caller-chosen ledger id (including a ULID) is never passed through. First start mints `runtime.grok_session_id` and runs `grok --session-id <uuid> --model grok-4.6`. Later starts, if that field is set, run `grok --resume <uuid>`. An empty model becomes `grok-4.6`; it must not inherit a Claude default. The pane is started with `env -u ANTHROPIC_API_KEY -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT` so Claude credentials do not leak into the Grok process. `--provider` and `--cmd` cannot be combined.

**Vendors remain `grok` | `codex`.** A process running inside a tmux pane is not a ledger vendor. There is no `vendor=claude` and no shell-string tmux driver: the runtime invokes `tmux` with argv lists only. `runtime.provider` is launch metadata, not a review-gate vendor.

Local dashboard (`127.0.0.1`): sessions expose `can_control` when `_origin_device_id` is this device. `POST /api/sessions/{id}/control` applies the same actions locally and does not call the hub.

## 17. Document history

Recorded from the design thread that specified realtime team visibility, rejected a central write database and a mesh, rejected embedding the hub in the existing public API, chose GitHub login + git teams, required full bidirectional sync and restore, and split the work into `agent` + `agent-core`. Control: local tmux ownership, hub control frames, ephemeral terminal bytes. Grok launch: own UUID in `runtime.grok_session_id`, `--resume` on later starts, default model `grok-4.6`, no Claude environment in the pane.
