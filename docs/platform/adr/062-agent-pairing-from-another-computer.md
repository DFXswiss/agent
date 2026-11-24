# ADR-062: Pairing an agent that runs on another computer

Status: Accepted (2026-11-24, KOR-42; revised after the design review with fresh context, findings in
`decisions/KOR-42.md`). Extends ADR-056 (§3 "Pairing: three bindings") and `11-agent-access.md` (§3, §4, §5, §8,
§9). Implements rule E-04 of `KONZEPT.md` ("an AI agent that connects only with SSH must be able to do everything a
person with a passkey can do – mandatory").

## Context
- ADR-056 binds the handover of the user's identity to the loopback: the browser that confirms the pairing posts
  the handover secret `r` to `http://127.0.0.1:<port>` of the same computer. An agent on a server, in a container or
  behind SSH has no listener on the user's computer and cannot be paired at all ("no code fallback, on purpose").
- E-04 names exactly this case and makes it mandatory. Rule E-03 (the user only creates the passkey, everything else
  is done by the agent) also holds for such an agent, as far as the security of the account allows (below).
- The loopback gives two guarantees at once: (1) `r` reaches only a process on the confirming computer (against a
  third party who sends the user a link of its own agent), and (2) the agent receives `r` from the user's browser,
  not from the hub (so a malicious hub cannot hand the agent an identity of its own choosing). A handover that leaves
  the computer must replace both, and the residual risk must be stated honestly.
- Mandatory countermeasures (KOR-24 proposal, work package KOR-42): the mode is chosen explicitly only, a clear
  warning, a short deadline, a notification to every signed-in client of the user, a code the user types instead of
  comparing.

## Decision

### 1. A second, explicit mode of the pairing: "remote"
- The agent creates the pairing with `POST /v1/agent-pairings {pairing_id, pub, pop, remote: true}`. The hub stores
  `remote = true`, draws a 16-byte `nonce` for the row and sets the confirmation deadline to **300 s** after creation
  (a loopback pairing keeps 600 s). The answer and `GET /v1/agent-pairings/{id}` return `remote` and `nonce`. Without
  the field (old agents) nothing changes. An old hub refuses the unknown field (strict request models); the tool then
  reports `hub_too_old` and, because it requires the answer to echo `remote: true` and a nonce, never proceeds with a
  loopback pairing it did not ask for.
- The agent tool uses this mode only with the explicit option `--remote` (`pair --remote`, `register --remote`); it
  never falls back from loopback to remote on its own. The link of a remote pairing is
  `<origin>/connect#p=<id>&f=<fp>&m=remote&a=<name>&h=<host>` – exactly these five parameters; a loopback link keeps
  `c=<port>`. The page refuses a link whose mode disagrees with the hub's `remote` ("This link is not valid"), so
  neither a changed link nor a hub that flips the flag turns one mode into the other. The hub requires `handover_ct`
  at the confirmation of a remote pairing and refuses it for a loopback one, so an old cached page that treats a
  remote pairing as loopback cannot confirm it.
- The agent cannot open a browser on the user's computer: the tool returns link and code at once and the agent gives
  both to its user in its conversation; a second call of the tool completes the pairing (§5). The link carries no
  secret.

### 2. The agent code: typed by the user on the page
- `code = crockford32(SHA-256(P‖"agent-code\0"‖canonical({"fp","mode":"remote","nonce","origin","pairing_id"})))[:10]`
  (Crockford base32: no `I`, `L`, `O`, `U`; input accepts lower case, spaces, dashes and maps `I`/`L`→`1`, `O`→`0`;
  shown as `XXXXX-XXXXX`). The agent tool computes and shows it; the page computes it from the link's fingerprint and
  pairing ID and the hub's nonce, after it checked the fingerprint against the hub's keys.
- The page does **not** show the agent code. It asks: "Type the code your AI agent shows you". The passkey button is
  enabled only when the typed code equals the computed one. Typing what the agent shows forces the user to look at the
  agent's conversation; comparing two displayed values is clicked through without reading.
- What it does: it binds the confirmed pairing to the one the user's own agent shows. It is not a secret (anyone with
  the link and the nonce computes it), so it does not stop a third party that sends the user a link and its code
  (§6). The hub's nonce makes the code depend on a value drawn after the pairing exists, so a pairing whose code
  collides with the user's real one cannot be ground offline; each attempt costs a registration at the hub (rate
  limit). Origin and mode in the input keep codes of different hubs and modes apart.

### 3. The handover through the hub, sealed to the agent, confirmed by a return code
- After the passkey gesture the browser draws `r` and computes `k`, the claim, the sealed identity and the sealed label
  exactly as in ADR-056 §2, and seals the handover to the agent's X25519 key:
  `handover_ct = b64u(HPKE_seal(agent kem_pub, info = P‖"agent-handover\0"‖ids, canonical({"k":<platform root key of
  the build>,"r":b64u(r)})))` (base mode, suite of `04-crypto.md` §1; `ids` as in `11-agent-access.md` §3). It sends
  it with the confirmation: `POST /v1/me/agents {…, handover_ct}` (`400 handover_required` for a remote pairing
  without it, `400 handover_unexpected` for a loopback pairing with it; the identical-repeat check includes it).
- **Return code.** HPKE base mode lets anyone who knows the agent's public key seal to it, the hub included: a
  malicious hub could seal an `r'` of its own around an identity it controls and the agent could not tell. The loopback
  excludes this because `r` comes from the user's browser directly. Here the page shows, after the confirmation, a
  return code `back = crockford32(HKDF(r, info = P‖"agent-return\0"‖ids))[:8]` (`XXXX-XXXX`) with the text "Give this
  code to your AI agent". The tool computes the same from the `r` it opened and accepts the handover only when the
  user's code matches; one mismatch ends the pairing (`pairing_failed`, the pending state is deleted). A hub that
  forged the handover does not know the code the page shows (it is derived from the browser's `r`), and it gets one
  attempt with 40 bits. Only after this check does the tool claim and store anything.
  This is the one extra step of the remote mode for the user (passing a code back to the agent); it replaces the
  integrity the loopback gave and is the price of E-04 without giving the hub the ability to impersonate the user.
- The agent fetches the handover with its own key: `GET /v1/agent/handover` (agent key in state `confirmed`, like
  `POST /v1/agent/claim`) → `{agent_id, user_id, handover_ct}`; on a loopback row `404 pairing_unknown`. The hub
  clears `handover_ct` at the claim, at revocation and at the claim deadline. Audit: read `agent.handover`.
- The claim deadline of a remote pairing is **600 s** after the confirmation (a loopback pairing: 60 s): a
  conversational agent may answer its user only at the end of a turn, and the handover is sealed to its key. The page
  shows "Waiting for your agent" and then the result, from `GET /v1/me/agents` (state of the new row).

### 4. Countermeasures (mandatory)
1. **Explicit mode only:** `--remote` in the tool; `m=remote` in the link and `remote` at the hub must agree.
2. **Clear warning on the page** (plain words, idea 30): "This AI agent runs on **another computer** (<host as the
   link states it>). Continue only if **you** just asked your own AI agent to connect and it showed you this page and
   a code. Whoever controls this agent gets full access to your account." Visually distinct from the loopback page;
   the replacement notice of ADR-056 §4a stays.
3. **Short deadline:** 300 s from creation to confirmation.
4. **Notification and banner:** the confirmation writes the notification `agent.account_change` with the new action
   `agent_paired_remote` (written for the browser session that confirmed – like `recovery_finished` – with an empty
   agent field: `ref` = `agent_paired_remote::<new agent ID>`), published on every open stream of the user and listed
   in every client. Because an agent may mark notifications as read, the clients additionally show a banner that no
   read state hides while the main orchestrator is a remote one: `GET /v1/me/agents` returns `remote` per row and the
   web client shows "Your main orchestrator runs on another computer (<host>), connected <time>" with "Disconnect" in
   the shell. Both are in-app only (no e-mail or push in this version). The account audit records `agent.confirm` and
   `agent.claim` with `remote: true` in the details.
5. **The codes** of §2 (typed on the page) and §3 (returned to the agent).

### 5. The agent tool
- `pair --hub <url> --remote [--name] [--host]`: refuses while a pending pairing exists unless `--replace-pending`
  (which deletes it first); creates the remote pairing; keeps the pending pairing in the state directory
  (`pending.json`, `0600`: hub, pairing ID, nonce, mode, the kind `pair`/`register`, deadline; the private keys like
  `agent.key`, in the keychain item `agent-platform` / `<pairing id>` where available) and returns at once
  `{"ok":true,"state":"waiting","connect_link":…,"code":…,"expires_at":…}`. An existing paired state stays in place
  until a new pairing completes (it is then replaced, as with loopback).
- `register --remote --display-name …` (invitation link on stdin, as before) does the same with the invitation: it
  returns the invitation link with the pairing parameters (`<origin>/invite#<code>&p=…&f=…&m=remote&a=…&h=…&u=…`) as
  `invite_link`, listed in `secret_fields` (the invitation code is meant for the user alone, as in `org invite`), and
  the code.
- `pair --wait [--return-code <code>] [--timeout <s>]`: works on the pending pairing (`no_pending_pairing` when there
  is none; a lock file keeps a second `--wait` out: `pairing_busy`). Without `--return-code` it polls
  `GET /v1/agent-pairings/{id}` every 2 s for at most `--timeout` (default 120, max 600) and returns
  `"state":"waiting"` while open or `"state":"confirmed"` with the next step "ask your user for the code the page
  shows". With `--return-code` it fetches and opens the handover, checks the code (§3), claims, opens the identity,
  stores the state, deletes the pending pairing and returns like `pair`. Expired before confirmation or claim:
  `pairing_timeout`; a handover that does not open or a wrong code: `pairing_failed`. In every terminal case
  (success, timeout, failure) `pending.json` and its key item are deleted.
- The loopback `pair` is unchanged; its `pairing_timeout` next step names `--remote` besides port forwarding.

### 6. The invitation path (new account)
- `/invite` accepts the remote pairing parameters as it accepts the loopback ones (ADR-056 §6). The page shows the
  warning of §4.2 and asks for the agent code **before** the passkey is created: a wrong or missing code creates no
  account. The mode travels in the route state to `/connect`, which confirms with the identity the registration
  unlocked, sends `handover_ct` and shows the return code.
- A new account has no other signed-in client, so the notification reaches nobody at that moment; the banner of
  §4.4 shows on every later sign-in. This is stated as a residual risk (§7).

### 7. Threats

| Attacker | Protection | Residual risk |
|---|---|---|
| Third party sends the user a remote link of its own agent (with its code) | explicit mode, warning, typed agent code, 300 s deadline, return code that the user must hand to *that* agent, notification on every client, banner while a remote orchestrator is active, list with its computer name, one-click "Disconnect" | **higher than loopback:** a user persuaded to open the third party's link, type its code and send the return code back confirms the third party's agent – under E-04 a full account access until revoked. Same class as device-code phishing. Accepted by rule E-04 and interpretation A3 of `KONZEPT.md` |
| Same through an invitation link (new account) | as above; the code is asked before the passkey is created | no other client receives the notification at that moment; the banner appears at the next sign-in |
| Malicious hub forges the handover (own `r'`, own identity) | return code derived from the browser's `r`, checked by the tool before anything is claimed or stored | 40 bits, one attempt |
| Malicious hub swaps the agent key or flips the mode | fingerprint from the agent's link; page compares `m` with `remote`; hub requires/refuses `handover_ct` by mode | as ADR-056 (malicious JavaScript of the hub) |
| Phished agent hides the notification (marks it read) | banner from the agent row, not from a read state; audit | the agent can disconnect itself and the banner goes away – then it has no access any more |
| Attacker grinds a pairing with the same agent code | hub nonce in the code input: one hub registration per attempt, rate-limited | – |
| Someone reads the link, the agent code or `handover_ct` | none is a secret; `handover_ct` sealed to the agent's key, single use, cleared at claim and deadline | – |
| Someone reads the return code | useful only to the holder of the agent's private key, within the claim deadline | – |

### 8. Rejected alternatives
- **Port forwarding only (`ssh -L`)** – needs a tunnel set up on the user's computer; an agent in a container or on
  a server cannot do it for the user (E-03). Stays documented as a loopback variant.
- **No return code** – leaves the malicious-hub forgery of §3 open (design review, finding 1).
- **Device-code flow without link** – needs a longer code to find the pairing, no fingerprint check against a hub
  that swaps keys, same phishing risk.
- **Comparing displayed values** – clicked through without reading.

## Consequences
- An additive migration (next free number, `<n>_agent_remote_pairing.sql`): `hub.agent.remote boolean NOT NULL
  DEFAULT false`, `hub.agent.nonce bytea NULL`, `hub.agent.handover_ct bytea NULL`. Rollback: before rolling the hub
  back, revoke unclaimed remote rows; the old hub ignores the columns, an old page refuses a remote link (no `c`).
- New route `GET /v1/agent/handover`; fields `remote`, `nonce` on pairing creation and lookup, `handover_ct` on
  confirmation, `remote` in `GET /v1/me/agents`; error codes `handover_required`, `handover_unexpected`; notification
  action `agent_paired_remote`; crypto items `agent-code`, `agent-handover`, `agent-return` with vectors in
  `contracts/vectors/agent.json` (hub, web, tool).
- The web pages `/connect` and `/invite` gain the remote mode, the shell the banner, the settings list "another
  computer"; the guide for AI agents explains when to use the mode and that the user passes one code back.
