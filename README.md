# agent-core

Hub for the [agent](https://github.com/DFXswiss/agent) session store.

Anyone can sign in with GitHub. Each person sees their own work. People listed on a team in `teams.yaml` also see that team. Add or remove members with a pull request.

The hub stores a full copy of every paired device. Sync is bidirectional: the device is the write owner of its own rows; the hub fans those events out and can restore a wiped device.

This repository does not describe a particular deployment environment. Run it anywhere that can reach GitHub OAuth and serve HTTPS.

Image builds on `develop` / `main` push `dfxswiss/agent-core:beta` / `:latest` and the git SHA. After a successful push they notify the configured infrastructure repo (`DISPATCH_TOKEN` + `DISPATCH_REPO`) so the running hub pulls the new tag. If those secrets are unset, the image is still published.

## Run locally

Create a GitHub OAuth App whose callback is `http://127.0.0.1:8787/auth/github/callback`. The hub requests scope `read:user repo` so people can sign in, the website can list open pull requests, and owners can create and answer agent assignment issues with their own token. After sign-in the website leads with open PRs from GitHub (`GET /api/prs`); usage, sessions, tasks, agents, pings, and devices come from the local hub replica. Then:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
export AGENT_CORE_PUBLIC_URL=http://127.0.0.1:8787
export AGENT_CORE_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export AGENT_CORE_GITHUB_CLIENT_ID=...
export AGENT_CORE_GITHUB_CLIENT_SECRET=...
export AGENT_CORE_GITHUB_AUTHORIZE_URL=https://github.com/login/oauth/authorize
export AGENT_CORE_GITHUB_TOKEN_URL=https://github.com/login/oauth/access_token
export AGENT_CORE_GITHUB_USER_URL=https://api.github.com/user
export AGENT_CORE_DATABASE=./hub.sqlite
export AGENT_CORE_TEAMS="$(pwd)/teams.yaml"
export AGENT_CORE_KEYS="$(pwd)/keys.yaml"
export AGENT_CORE_SIGNING_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export AGENT_CORE_HUB_KEY_ID=hub-local-1
export AGENT_CORE_HOST=127.0.0.1
export AGENT_CORE_PORT=8787
export AGENT_CORE_RECORD_DATABASE="postgresql://record_writer:<64 hex>@127.0.0.1:5432/record?sslmode=disable"
python -m agent_core
```

Before the first start, create the record database and its two roles on a PostgreSQL 16 server
(RECORD-PROTOCOL.md section 11.2) and run `python -m agent_core.record migrate` with
`AGENT_CORE_RECORD_OWNER_DATABASE` and `AGENT_CORE_RECORD_DATABASE` (see below). Unset
`AGENT_CORE_RECORD_OWNER_DATABASE` again before you start the hub.

Open http://127.0.0.1:8787 and sign in.

Every variable above is required. The process exits if one is missing.

- `AGENT_CORE_PUBLIC_URL` has no user information, path, query or fragment; a single trailing `/` is allowed. An international host name is written in its ASCII form (`xn--…`); otherwise the process exits.
- `AGENT_CORE_KEYS` is the path of the keys file (`keys.yaml` next to `teams.yaml`).
- `AGENT_CORE_SIGNING_KEY` is the 32-byte seed of the hub's Ed25519 signing key as 64 lowercase hex characters. It is a secret: every environment has its own seed, and the hub never logs or returns it.
- `AGENT_CORE_HUB_KEY_ID` is the id of the hub's signing key (lowercase letters, digits and `-`, 3 to 63 characters, starting and ending with a letter or digit). It is not a secret.

- `AGENT_CORE_RECORD_DATABASE` is the connection string of the writer role of the team record:
  `postgresql://<role>:<password>@<host>[:<port>]/<database>?sslmode=<mode>`, with a password of
  64 lowercase hex characters and `sslmode` `disable` (only for a loopback address or
  `localhost`), `require` or `verify-full` (RECORD-PROTOCOL.md section 15). It is a secret. The
  hub refuses to start when `AGENT_CORE_RECORD_OWNER_DATABASE` is set in its environment, also
  when it is empty.

At start the hub logs one line `hub key <id> <public key>`; callers pin that pair. Before it
serves, it checks the record database and verifies the whole record (RECORD-PROTOCOL.md section
11.4) and logs `record verified <position> <record hash>` (`record verified 0 -` for an empty
record). When that start fails it logs `record start failed <step>`, with `<step>` one of
`config`, `schema`, `writer`, `verify` and `verify_read`, and exits; `record start failed verify`
always follows a line `record verify failed <position> <failure>` that names the failure.

## Teams

Edit `teams.yaml` and open a pull request:

```yaml
teams:
  dfx:
    members:
      - your-github-login
      - agent-github-login

agents:
  agent-github-login:
    owners:
      - your-github-login
```

Logins are compared in lowercase. A login that appears on no team still signs in and still syncs; they only see themselves.

The optional `agents` map declares GitHub logins operated by autonomous agents. An agent must be a team member, and each owner must share at least one team with that agent. Owners can pair a device as the agent from the website, control its sessions, create GitHub issue assignments, and reply to assignment comments. An agent cannot own itself, duplicate owners and any key written twice in the file are rejected, and a malformed roster fails closed at startup.

## Protocol

See [PROTOCOL.md](PROTOCOL.md).

The wire format of key identity is in [KEY-PROTOCOL.md](KEY-PROTOCOL.md). The hub side of its first build step is built: the keys file `keys.yaml` (changed only by pull request), signed requests under `/api/key/`, the hub's signed answers, and `GET /api/key/whoami`.

Sign-in with passkeys, invites and the certified session key are specified, not yet built, in [PASSKEY-PROTOCOL.md](PASSKEY-PROTOCOL.md). The pure verification library of that step exists in `agent_core.passkey` (encodings, passkey responses, registrations, the statement, `verify_certificate` and the command text), with the shared test vectors in `tests/vectors/passkey.json`; the hub does not use it yet.

The release query `GET /api/key/release?repository=owner/name&pull_request=N` (KEY-PROTOCOL.md section 8) lets a `service` key with the scope `release:query` ask whether a pull request in one of its `subjects` may leave draft; the answer is signed. The hub computes it from the team record (RECORD-PROTOCOL.md section 9): a pull request that no task links gets `released: false` with `task_missing`, and a hub process that found the record broken answers the signed `503` `record_unverified`. The answer is one input for a guard and replaces no review rule.

The team record (third build step: signed events in PostgreSQL, tasks and security issues) is specified in [RECORD-PROTOCOL.md](RECORD-PROTOCOL.md) and built: the record library, the schema and `python -m agent_core.record migrate`, the commands `export`, `verify` and `restore`, and the record in the hub. The hub verifies the record at start, every 10 minutes and on request, makes a checkpoint attempt at every full hour, which signs the record hash only when the record is not empty, its head moved since the newest checkpoint of the hub key, and no other hub process already wrote the checkpoint of that hour, and serves the signed endpoints under `/api/key/record/` (RECORD-PROTOCOL.md section 7) to keys with the scopes `record:read`, `record:write`, `record:security` and `record:watch`.

## Control

Anyone who can see a session may watch its live terminal on the website. Start, stop, type, and resize are allowed only when the session's origin device row exists, is not revoked, and its `github_login` either equals the signed-in actor or is an agent login that actor owns; the origin device must also have a control-ready WebSocket. The hub relays control and terminal bytes; it does not run processes and does not author session rows. Details are in [PROTOCOL.md](PROTOCOL.md).

## Agent operations

The dashboard shows an **Agents** section to people who own at least one configured agent. It links agent sessions to their chronological timelines, shows current coordinator state, creates assignments, displays issue questions, and posts owner replies. Assignment creation responses and stored activity use GitHub's canonical repository case. Usage is available in lane-token, OpenRouter-account, and Grok tables.

Session timelines keep the newest 5,000 related rows when capped and return those rows in chronological order (oldest first) with `truncated: true`.
The static session page is served on direct entry without a sign-in cookie. If its timeline fetch returns 401, the page shows **Signed out — sign in on the dashboard** and links back to `/`; the page does not poll or expose session data.

Website API endpoints are documented in [PROTOCOL.md](PROTOCOL.md):

- `POST /api/assignments` and `GET /api/assignments?agent=<login>`
- `POST /api/assignments/{owner}/{name}/{number}/comments`
- `GET /api/usage`
- `GET /session?id=<id>`, `GET /api/session?id=<id>`, `GET /api/session/terminal?id=<id>`, `POST /api/session/control?id=<id>`, and `GET /api/session-timeline?id=<id>`

The hub uses only the signed-in human's stored OAuth token for GitHub issue and comment writes. Tokens never enter API responses, activity rows, or browser cookies. Devices remain the sole write owners of their rows; website actions use synthetic `web:<login>` activity origins.

Every assignment creation must include `deviation: {"declared": false}`. This is the assigning owner's explicit statement that no deviation from the target repository's contribution rules is authorized; any deviation instead needs an explicit grant on the issue. The dashboard requires the owner to actively check this statement for each assignment.

Assignment and reply writes accept an optional lowercase UUID `request_id`. User bodies cannot contain the reserved `<!-- agent-core-request:` marker line. The dashboard keeps the same UUID and exact request payload in `sessionStorage` across reloads and sign-in renewal after a timeout or network error, and the hub appends `<!-- agent-core-request:<request_id> -->` to the GitHub issue or comment. The hub binds the id to a fingerprint of the normalized request and reserves each actor, activity type, and request id while the write is in progress; a concurrent request returns 409, as does reuse with different content. A later retry searches back to the reservation's first-reserve time and reuses only an actor-authored marker match with the expected marker-free body, preventing duplicate issues and comments after an ambiguous timeout. Recording the activity and completing its reservation are one transaction, so a superseded lease owner cannot publish a second event.

## Tests

Node.js is required for the dashboard and session page tests.

PostgreSQL 16 (or later) is required for the team record tests. They read the connection string of
a superuser on a throwaway local test server from `AGENT_CORE_TEST_POSTGRES`; each test creates its
own database and roles with random names and drops them at the end, except the identifier test, which
uses the fixed names `grant`, `select` and `order` and removes roles and a database of these names
first. Without the variable every record
test that uses the database fails; none is skipped. The tests of the event text and of the connection
string need no database. The server must accept password logins over TCP on a loopback address or
`localhost`.

Use a throwaway PostgreSQL 16 instance for this and nothing else: it holds no data, the tests
create and drop databases and roles on it, and its superuser password `postgres` is a test
constant. It listens on the dedicated port 55432, so it never meets another PostgreSQL on 5432.
Start it in one of two ways.

With a container:

```bash
docker run -d --rm --name agent-core-test-postgres -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55432:5432 postgres:16
# Stop and remove it afterwards: docker stop agent-core-test-postgres
```

Or with a local `initdb` of PostgreSQL 16 (its `bin` directory on `PATH`):

```bash
TESTPG="$(mktemp -d)"
printf 'postgres\n' > "$TESTPG/password"
initdb -D "$TESTPG/data" -U postgres --auth=scram-sha-256 --pwfile="$TESTPG/password"
pg_ctl -D "$TESTPG/data" -l "$TESTPG/server.log" -o "-p 55432 -c listen_addresses=127.0.0.1 -k $TESTPG" start
# Stop and remove it afterwards: pg_ctl -D "$TESTPG/data" stop && rm -rf "$TESTPG"
```

Then run the tests:

```bash
export AGENT_CORE_TEST_POSTGRES=postgresql://postgres:postgres@127.0.0.1:55432/postgres
pip install -e ".[test]"
pytest
```

CI runs the same tests against a `postgres:16` service container on the same port 55432.

`python -m agent_core.record migrate` applies the schema of the team record (RECORD-PROTOCOL.md
section 11.4). It reads `AGENT_CORE_RECORD_OWNER_DATABASE` and `AGENT_CORE_RECORD_DATABASE`, and it
runs to completion before every start of the hub; the hub itself holds only
`AGENT_CORE_RECORD_DATABASE`.

`docker-compose.yaml` runs the three pieces of RECORD-PROTOCOL.md section 15: a `postgres:16`
service with TLS that creates the owner role `record_owner`, the writer role `record_writer` and the
database `record` from two secrets, a one-shot `migrate` service, and the hub, which starts only
after `migrate` completed. It needs `AGENT_CORE_RECORD_OWNER_PASSWORD`,
`AGENT_CORE_RECORD_WRITER_PASSWORD` (each 64 lowercase hex characters) and
`AGENT_CORE_RECORD_ADMIN_PASSWORD` (the superuser of that server) in its environment.

Three further commands export, check and restore the record (RECORD-PROTOCOL.md section 12.3). None
of them needs the hub to run, none reads `AGENT_CORE_RECORD_OWNER_DATABASE`, and each prints exactly
one line: no value of the configuration and no message of a database error.

| Command | Reads | Prints |
|---|---|---|
| `python -m agent_core.record export --out FILE` | `AGENT_CORE_RECORD_DATABASE` (writer role) | `exported <position> <record hash or ->` (exit 0); `refused config`, `refused file_exists`, `refused file_write`, `refused database` or `refused value_unwritable` (exit 1) |
| `python -m agent_core.record verify --keys KEYS --teams TEAMS --in FILE [--hub-key ID=PUBLIC_KEY]...` | the files named; no variable, no database | `verified <position> <record hash or ->` (exit 0); `refused config` or `failed <position> <failure>` (exit 1) |
| `python -m agent_core.record restore --in FILE [--hub-key ID=PUBLIC_KEY]...` | `AGENT_CORE_RECORD_DATABASE` (writer role), `AGENT_CORE_KEYS`, `AGENT_CORE_TEAMS` | `restored <position> <record hash or ->` (exit 0); `refused config`, `failed <position> <failure>`, `refused hub_key_missing`, `refused schema_missing (run python -m agent_core.record migrate first)`, `refused schema_version`, `refused writer_rights`, `refused not_empty` or `refused database` (exit 1) |

`export` creates `FILE` with mode `0600`, refuses an existing `FILE`, and removes `FILE` again when it
fails after creating it; an export counts only when it printed `exported`. The file contains security
issues: keep it encrypted and out of every repository, issue, pull request and log (section 12.4).
`verify` checks the file offline against the public keys file and teams file, and checks every
checkpoint whose hub key id is given with `--hub-key`. `restore` verifies the whole file first, then
inserts it into a migrated, empty database; run `migrate` before it, and give the public key of every
hub key id in the file. Without a command, or with an unknown one, the program prints
`usage: python -m agent_core.record {migrate,export,verify,restore}` to stderr and exits 2.

## License

MIT. See [LICENSE](LICENSE).
