// Confirming the pairing of the user's AI agent in the browser (11-agent-access.md section 5.1, ADR-056 section 3,
// ADR-082 sections 2 and 9):
//
// 1. `lookupPairing`: `GET /v1/agent-pairings/{id}` (anonymous); the keys the hub returns must have the fingerprint of
//    the link (`pairing_mismatch`, a client-only code: the hub swapped the keys or the link is forged) and the pairing
//    must be `open` (else `pairing_unknown`: expired, used or cancelled).
// 2. `pairingGen`: the same request signed by the user's session after the passkey gesture also names `gen`, the
//    generation the user's certificate must carry (the hub counts it per user).
// 3. `preparePairing`: draws the handover secret `r` (32 bytes) and computes the fixed part of `POST /v1/me/agents` –
//    the delegation certificate signed by the user's identity, the user's public keys and the certificate sealed to
//    the agent under `k` (`user_ct`), the label sealed to the user's own identity key, the claim hash. The agent never
//    receives the user's identity. The fixed part is computed once per screen: a retry after
//    `pairing_gesture_required` (a new passkey gesture) sends the same values, so a confirmation that reached the hub
//    but whose answer was lost is answered alike, not `id_conflict`.
// 4. `confirmationBatches`: per organization of the user (`GET /v1/me`), one append batch built from the verified log
//    by `buildBatch` (`agent_add` by the user; when the user's previous main orchestrator is in that log it has a
//    lower generation and is replaced at the same `seq`, so the batch also rotates R and wraps the new epochs for every
//    remaining reader; every epoch the browser holds of every scope the user reads is wrapped to the new agent), and
//    `cred_wraps`: every epoch of the user's `CK` the browser opens, wrapped to the agent.
// 5. `confirmPairing`: `POST /v1/me/agents`, signed by the session of the pairing-tagged login ceremony. The log job of
//    every org runs first (with the jobs it starts: agents, credential-key rotation), and the batches and CK wraps are
//    built and sent under the CK lock (`exclusive`), so a rotation the jobs owe never races the confirmation; after
//    `seq_conflict`, `agent_batches_incomplete` or `epoch_stale` the logs and CK epochs are read again and the
//    confirmation rebuilt (at most `BATCH_ATTEMPTS` times). Then the logs are read again from the hub (the hub may have
//    answered a repeat without applying its batches), the confirmed agent and its certificate are remembered in this
//    browser's preferences (`Prefs.confirmedAgents`), the new scope keys of a batch the hub applied are kept, and the
//    user's `CK` is rotated when the verified logs owe it (a replaced orchestrator lost its entitlement:
//    `rotateCredentialKey`, remembered as handled), before the page hands over to the agent.
// 6. `loopbackForm`: the top-level form POST the same tab submits to the agent's loopback listener; only there does
//    `r` leave the browser. `r` is a form field, never part of a URL (it would stay in the browser history).
//
// A pairing from another computer (ADR-067, section 5.4): the link's mode must equal the hub's `remote`
// (`pairing_mismatch` otherwise, both ways), the hub's 16-byte nonce yields the agent code the user must type
// (`agentCode`, computed with the page's origin after the fingerprint check), the confirmation carries `handover_ct`
// (`r` and the platform root key sealed to the agent's key) instead of a loopback form, and the page shows the return
// code derived from `r`. `pairingOutcome` reads the result of the claim from the user's agent list.
import {
  claim,
  claimHash,
  MAX_GEN,
  type PublicKeys,
  returnCode,
  sealHandover,
  sealLabel,
  sealUser,
  signCert,
} from '../crypto/agent';
import { fail } from '../crypto/errors';
import { requireInt } from '../crypto/fields';
import type { Identity } from '../crypto/keys';
import { Secret } from '../crypto/secret';
import { b64uEncode } from '../encoding/b64u';
import { formatTime } from '../encoding/time';
import type { Prefs } from '../account/prefs';
import { ownKeys } from '../credentials/records';
import { handledFor, rememberHandled, rotateCredentialKey } from '../credentials/rotation';
import { exclusive } from '../jobs/credential-rewrap';
import { buildWraps } from '../log/catchup';
import type { Entry, LogFields } from '../log/entries';
import { buildBatch, type BuiltBatch } from '../log/rotation';
import { epochKey } from '../log/state';
import { compareSlots, CRED, type WrapRecord } from '../log/wraps';
import { HubError, type SignedClient } from '../net/signedFetch';
import { actorOf, BATCH_ATTEMPTS, keyRing, verifiedPlatform } from '../org/batch';
import { isRecord } from '../registry';
import type { AgentEntry } from '../store/slices/agents';
import { addKeys, orgEntry } from '../store/slices/orgs';
import type { Store } from '../store/store';
import type { PairingLink } from './link';

import type { Pairing } from './lookup';

export { lookupPairing, PAIRING_TTL_MS, type Pairing, REMOTE_PAIRING_TTL_MS } from './lookup';

/** The generation the user's certificate of the pairing must name (`gen` of the signed lookup). */
export async function pairingGen(client: Pick<SignedClient, 'request'>, pairingId: string): Promise<number> {
  const body = await client.request('GET', `/v1/agent-pairings/${encodeURIComponent(pairingId)}`);
  if (!isRecord(body) || body.pairing_id !== pairingId) fail('header_invalid');
  return requireInt(body.gen, 1, MAX_GEN);
}

/** The new agent as its `agent_add` names it. */
export interface NewAgent {
  readonly id: string;
  readonly pub: PublicKeys;
  readonly gen: number;
  readonly cert: string;
}

export interface PreparedPairing {
  readonly pairingId: string;
  /** The confirming user (the agent needs it to derive the claim and to open its anchor). */
  readonly userId: string;
  readonly secret: Secret;
  /** The return code (`XXXXXXXX`) the page shows after the confirmation of a remote pairing, else null. */
  readonly returnCode: string | null;
  readonly agent: NewAgent;
  /** The fixed part of the confirmation (the batches are built from the logs at each attempt). */
  readonly body: {
    readonly pairing_id: string;
    readonly label_ct: string;
    readonly claim_hash: string;
    readonly gen: number;
    readonly cert: string;
    readonly user_ct: string;
    /** Remote only: `r` and the platform root key sealed to the agent. */
    readonly handover_ct?: string;
  };
}

/** The handover secret and the fixed confirmation for `userId` (module comment); `rootKey` is the build's. */
export async function preparePairing(
  pairing: Pairing,
  link: PairingLink,
  userId: string,
  identity: Identity,
  rootKey: string,
  gen: number,
): Promise<PreparedPairing> {
  const secret = Secret.random(32);
  const id = pairing.pairingId;
  const label = { host: link.host, name: link.name };
  const pub = { kem: b64uEncode(pairing.kemPub), sig: b64uEncode(pairing.sigPub) };
  const cert = await signCert(identity.sig.privateKey, { agent: id, pub, user: userId, gen });
  const own = identity.public();
  const body = {
    pairing_id: id,
    label_ct: await sealLabel(identity.kemPublic(), id, userId, label),
    claim_hash: b64uEncode(await claimHash(await claim(secret, id, userId))),
    gen,
    cert,
    user_ct: await sealUser(pairing.kemPub, secret, id, userId, { kem: own.kem, sig: own.sig, cert }),
  };
  const agent = { id, pub, gen, cert };
  if (!pairing.remote) return { pairingId: id, userId, secret, returnCode: null, agent, body };
  return {
    pairingId: id,
    userId,
    secret,
    returnCode: await returnCode(secret, id, userId),
    agent,
    body: { ...body, handover_ct: await sealHandover(pairing.kemPub, id, userId, secret, rootKey) },
  };
}

/**
 * Refusals after which the confirmation reads the logs and the credential-key epochs again and is rebuilt: another
 * client appended (`seq_conflict`), the orgs or the current CK epoch changed (`agent_batches_incomplete`), a CK epoch
 * the hub no longer takes (`epoch_stale`).
 */
export const RETRY_CODES: ReadonlySet<string> = new Set(['seq_conflict', 'agent_batches_incomplete', 'epoch_stale']);

/** What the confirmation needs of the signed-in browser (`AppSession.tasks` provides it). */
export interface ConfirmContext {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  readonly now: () => number;
  /** Re-reads and verifies the org's log (the log job). */
  readonly refreshLog: (org: string) => Promise<unknown>;
  /** This browser's preferences: the confirmed pairing is remembered there (the job `agents` relies on it). */
  readonly prefs?: Pick<Prefs, 'addConfirmedAgent' | 'handledLosses' | 'setHandledLosses'>;
}

/** One append batch of the confirmation. */
export interface ConfirmBatch {
  readonly org: string;
  readonly expected_seq: number;
  readonly entries: Entry[];
  readonly wraps: WrapRecord[];
}

/** The batches and `CK` wraps of one attempt, with the built batches by org. */
export interface Confirmation {
  readonly batches: ConfirmBatch[];
  readonly cred_wraps: WrapRecord[];
  readonly built: ReadonlyMap<string, BuiltBatch>;
}

/** The orgs of the user's `GET /v1/me` profile (malformed items left out), sorted. */
export function profileOrgs(store: Store): string[] {
  const orgs = store.getState().me.profile?.orgs;
  const ids = Array.isArray(orgs) ? orgs.filter(isRecord).map((org) => org.org_id) : [];
  return ids.filter((id): id is string => typeof id === 'string').sort();
}

/**
 * The batches of every org of the user and the `CK` wraps to the agent (module comment, step 4); an org without a
 * verified log refuses with its code (`log_empty`, the code of a blocked org).
 */
export async function confirmationBatches(context: ConfirmContext, prepared: PreparedPairing): Promise<Confirmation> {
  const { store } = context;
  const { agent, userId } = prepared;
  const add: LogFields = {
    type: 'agent_add',
    subject: agent.id,
    user: userId,
    pub: agent.pub,
    gen: agent.gen,
    cert: agent.cert,
  };
  const ts = formatTime(context.now());
  const batches: ConfirmBatch[] = [];
  const built = new Map<string, BuiltBatch>();
  for (const org of profileOrgs(store)) {
    const actor = actorOf(store, org);
    const result = await buildBatch({
      log: actor.log,
      author: { id: userId, identity: actor.identity },
      entries: [add],
      keys: keyRing(store, org),
      platform: verifiedPlatform(store),
      ts,
    });
    const { expected_seq: expectedSeq, entries, wraps } = result.body;
    batches.push({ org, expected_seq: expectedSeq, entries, wraps });
    built.set(org, result);
  }
  const identity = store.getState().me.identity!;
  const states = [...built.values()].map((result) => result.log.state);
  const held = states.length === 0 ? [] : await ownKeys(context, userId, identity);
  const slots = held
    .map((key) => ({ scope: CRED, scope_id: userId, epoch: key.epoch, recipient: agent.id }))
    .sort(compareSlots);
  const ring = new Map(held.map((key) => [key.epoch, key.key]));
  const credWraps = await buildWraps(identity, slots, (_s, _i, epoch) => ring.get(epoch)!, states);
  return { batches, cred_wraps: credWraps, built };
}

/** Keeps the new scope keys of a batch the hub applied (the refreshed log ends where the batch ended). */
function keepKeys(store: Store, org: string, built: BuiltBatch): void {
  const log = orgEntry(store.getState().orgs, org).log;
  if (log?.head.hash !== built.log.head.hash) return;
  // every scope the batch rotates is one the replaced agent read through the user: the user reads it too
  const keys = new Map(built.newKeys.map((key) => [epochKey(key.scope, key.scope_id, key.epoch), key.key]));
  addKeys(store, org, keys);
}

/**
 * `POST /v1/me/agents` with the batches of the current logs (module comment, step 5); the hub answers with the agent
 * ID, which is the pairing ID.
 */
export async function confirmPairing(context: ConfirmContext, prepared: PreparedPairing): Promise<void> {
  const { store } = context;
  const orgs = () => profileOrgs(store);
  let confirmation: Confirmation | null = null;
  for (let attempt = 1; confirmation === null; attempt += 1) {
    // the owed background jobs of every org run first (log, agents, credential key), so the batches and the CK wraps
    // are built from their result; the build and the request hold the CK lock, so no rotation slips in between
    for (const org of orgs()) await context.refreshLog(org);
    confirmation = await exclusive(store, async () => {
      const built = await confirmationBatches(context, prepared);
      const body = { ...prepared.body, batches: built.batches, cred_wraps: built.cred_wraps };
      try {
        const answer = await context.client.request('POST', '/v1/me/agents', { body });
        if (!isRecord(answer) || answer.agent_id !== prepared.pairingId) fail('header_invalid');
        return built;
      } catch (error) {
        const again = error instanceof HubError && RETRY_CODES.has(error.code) && attempt < BATCH_ATTEMPTS;
        if (!again) throw error;
        return null;
      }
    });
  }
  context.prefs?.addConfirmedAgent(prepared.userId, prepared.agent.id, prepared.agent.cert);
  for (const [org, built] of confirmation.built) {
    await context.refreshLog(org);
    keepKeys(store, org, built);
  }
  const handled = handledFor(store, context.prefs);
  const result = await exclusive(store, () => rotateCredentialKey(context, handled));
  if (result?.status === 'rotated') rememberHandled(store, handled, context.prefs);
}

/** What became of a confirmed remote pairing, from the user's agent list: claimed, still waiting, or failed. */
export function pairingOutcome(
  entries: readonly AgentEntry[] | null,
  pairingId: string,
): 'waiting' | 'connected' | 'failed' {
  const entry = entries?.find((item) => item.agentId === pairingId);
  if (entries === null || entry?.state === 'confirmed') return 'waiting';
  return entry?.state === 'active' ? 'connected' : 'failed';
}

export interface LoopbackForm {
  /** `http://127.0.0.1:<port>/agent-pairing`. */
  readonly action: string;
  /** The form fields in order: pairing ID `p`, user ID `u`, `r` (b64u) and the platform root key `k` of the build. */
  readonly fields: readonly (readonly [string, string])[];
}

/** The handover to the agent (module comment). */
export function loopbackForm(port: number, prepared: PreparedPairing, rootKey: string): LoopbackForm {
  return {
    action: `http://127.0.0.1:${port}/agent-pairing`,
    fields: [
      ['p', prepared.pairingId],
      ['u', prepared.userId],
      ['r', prepared.secret.reveal((raw) => b64uEncode(raw))],
      ['k', rootKey],
    ],
  };
}
