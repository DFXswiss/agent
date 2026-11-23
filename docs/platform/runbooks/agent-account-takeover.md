# Runbook: account taken over through a stolen agent key (ADR-056)

Under the owner's decision an agent may do everything its user may (ADR-056 §4). A stolen agent key (with the agent
tool's state) is a full account takeover: the agent cannot confirm a pairing with its own key, but it can replace
the recovery code, register a passkey of its own through the recovery and with that passkey pair further agents
(ADR-056 §4a). Disconnecting the agents alone is therefore not enough. This runbook is for the operator when a user reports it or the user's
security activity shows actions they did not take.

## 1. The user can still sign in with a passkey
1. Settings → "Connected agents" → **Disconnect all agents** (`DELETE /v1/me/agents`). Effective at the next
   request; open live streams of the agents are closed.
2. Settings → "Passkeys": remove every passkey the user does not recognise (a passkey added through recovery
   shows as a notification of a completed account recovery and in the security activity). Removing a passkey ends its
   browser sessions.
3. Settings → "Passkeys" → create a new recovery code (the attacker may know the current one, or have replaced it).
4. Settings → "Connected agents": check again and **Disconnect all agents** once more – an agent paired with an
   attacker's passkey before step 2 would otherwise remain.
5. Settings → "Security activity" (`GET /v1/me/audit`): check what agents did (passkeys, recovery code, pairings
   and disconnected agents). Every row of an agent carries `agent_id`. Check members and invitations of every
   organization the user administers (audit "by agent"), replace credentials the agent wrote (Claude, GitHub).
6. Only now pair a fresh main orchestrator from the user's own computer (`agent-platform pair`), with new keys; never
   reuse the old agent state.

## 2. The user is locked out (passkeys removed, recovery code replaced)
The hub cannot restore an identity (it never holds one). Operator-assisted recovery means **a new identity**:
1. Confirm the person out of band (the operator knows the user; no self-service).
2. Disable the old principal: `UPDATE hub.principal SET disabled_at = now() WHERE id = '<user>'` (writer role, in a
   transaction; this refuses every session and agent of the user at once – `key_revoked`).
3. Admins of the user's organizations remove the old principal (rotation batch) and invite the person again; a
   platform invitation (`agent-hub platform invite-user`) gives a new private organization.
4. Data that only the old identity could read (a private organization with a single member, the old credentials) is
   lost; shared organizations are re-wrapped to the new member by the other key holders.
5. Record the case (time, user, actions seen in the audit) in the operations log.

## 3. Prevention (tell users)
- Pair agents only from your own computer, only right after asking your agent to connect.
- Disconnect agents you no longer use; the list shows your one main orchestrator, the ones it replaced and when each
  was last used. Pairing a new one with your passkey disconnects the old one at once.
