# ADR-101: Agent vault – a vault of agent.dfx.swiss only for agents: they use secrets without seeing them

Status: Proposed (2026-11-26, DOC-11; concept only – **not commissioned**: nothing is built until the owner decides,
[E-38](../KONZEPT.md#e-38))

Concept for [E-38](../KONZEPT.md#e-38). Builds on [E-37](../KONZEPT.md#e-37) (what the platform can do itself is a
platform tool; a session never holds the secret it names), [E-33](../KONZEPT.md#e-33) (parallel, each operation in its
own sandbox) and [E-32](../KONZEPT.md#e-32) (a working mode is never forced and never on by default).

## Context
Agents need accesses all the time: mailboxes, API keys, logins of web services, SSH keys, second factors. Today such
secrets reach a task as plain text in its prompt – on 2026-11-26 five mailbox passwords for the registration of new
subscriptions went into task prompts, readable by everyone who reads the task, kept in the chat and in the session.
The platform already seals credentials of a user (Claude subscriptions, API keys, GitHub tokens, one mailbox) under the
user's credential key, but only for its own purposes; there is no place where an organization keeps arbitrary secrets
for its agents, and no way for an agent to use one without holding it.

The operator also runs an internal password manager for its staff (a Vaultwarden behind passkey sign-in). That one is
for people and stays internal; agent.dfx.swiss gets its own vault, separate in data, keys and operation.

## Decision (proposed)
1. **Items and collections.** An item holds one secret of a kind: login (address, user, password, sign-in URL),
   API key or token, mailbox (IMAP/SMTP), SSH key, TOTP seed, certificate, secure note. Items live in collections of an
   organization or a workgroup, with name, purpose, owner, tags and an optional expiry. Sealed end to end under the
   keys of the existing key model (the collection's scope key); the hub stores ciphertext and metadata it needs for
   routing only.
2. **Use without seeing (the default).** A task refers to an item by name or ID; platform tools put the secret in place
   on the execution device, never in the session, the chat, a log or the audit:
   - platform browser: "sign in with item X" fills the login form in the browser sandbox;
   - mailbox: "newest mail from Y in item Z" (the tool of E-37);
   - web requests: a request with the item's key as header; secrets in the answer are masked;
   - SSH and Git: the key is forwarded into the work VM as an agent connection, never as a file;
   - second factor: the session receives only the current one-time code, never the seed;
   - tests and builds: secrets as environment of a test VM, masked in every output.
3. **Plain text only by explicit permission.** Per item a person may allow that a session reads the plain value
   (for software that cannot be driven otherwise); off by default, limited in time, audited.
4. **Who may use what.** Per item a policy names the workgroups, tasks, working modes and tools that may use it. A
   request outside the policy is refused with a visible reason; an agent may ask for an item it lacks – the owner
   approves with the passkey (E-05 initiates, never decides).
5. **People.** A vault view on agent.dfx.swiss: add, share with workgroups, edit, rotate, revoke; passkey-protected
   like every write; parity in the agent tool and MCP (E-04) without any plain value in their output.
6. **Audit and alarms.** Every use is a row of the audit (who, which item, which tool, when, result) without content;
   unusual use (new tool, new device, burst) raises a notification. Revocation takes effect at once on every device.
7. **Separate from the internal vault.** No synchronisation with the operator's staff vault; an item moves only when a
   person copies it by hand.

## Stages (when commissioned)
1. Items, collections, sharing, audit, web and tool; use without seeing for mailbox, browser sign-in, web requests and
   test VMs.
2. One-time codes, SSH forwarding, requests by agents with approval, rotation reminders.
3. Short-lived credentials where providers offer them (GitHub app tokens, cloud roles) and automatic rotation.

## Consequences
- Prompts and chats never need secrets again; the mailbox tool of KOR-74 and the per-subscription mailbox of KOR-73
  become the first item kinds when this is built.
- Every tool that uses an item is a platform operation with a class (ADR-094) and runs on the execution device.

## Alternatives considered
- **Use the operator's staff vault.** Rejected by the owner: it is internal to the operator; agent needs its own.
- **Secrets as files in work VMs.** Rejected: a VM image, a log or a compromised session would expose them (E-37).
