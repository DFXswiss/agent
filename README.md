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
export AGENT_CORE_HOST=127.0.0.1
export AGENT_CORE_PORT=8787
python -m agent_core
```

Open http://127.0.0.1:8787 and sign in.

Every variable above is required. The process exits if one is missing.

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

```bash
pip install -e ".[test]"
pytest
```

## License

MIT. See [LICENSE](LICENSE).
