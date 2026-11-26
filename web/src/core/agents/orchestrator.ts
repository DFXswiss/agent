// A running session on DFX hardware as the user's main orchestrator (11-agent-access.md section 11, ADR-082 sections 6
// and 9.6-9.9). The hub admits only a running main session of a task the user owns in the user's private organization
// on a platform device (`available`, else `reason`); the session page offers the step only then.
//
// 1. `requestOrchestrator`: `POST /v1/sessions/{id}/orchestrator` (the browser session of the task owner); the runner
//    draws the keys in the session's VM and registers them. `waitForPairing` reads
//    `GET /v1/sessions/{id}/orchestrator` every `POLL_MS` until the open pairing is there (at most `WAIT_MS`).
// 2. `checkSessionPairing`: the session's organization must be the owner's private one in the verified log (private,
//    founded by the owner, no other active user member; else `orchestrator_unavailable`, `privateOf`), the
//    pairing's keys must have the fingerprint `fp`, and `device_sig` must be the signature of
//    the session's device over `P || "orchestrator-key\0" || canonical({"fp","pairing_id","session"})` under the
//    device's key from the verified platform log (a platform device certified there); else `pairing_mismatch` (a
//    client-only code: the hub swapped the keys or the device). The checked pairing then goes through the ordinary
//    confirmation (`pairing.ts`: the login ceremony tagged with the pairing, certificate, batches, replacement).
// 3. `handOver`: after the confirmation the browser seals `{k, r}` to the session's key in the remote handover format
//    of ADR-067 and signs the ciphertext with the user's identity
//    (`P || "orchestrator-handover\0" || canonical({"ct","pairing","session"})`); the runner checks that signature
//    under the task owner's key before it passes the handover into the VM, where the tool claims.
//
// `sessionLabel`: the label the browser seals for itself (it names what the agent is and its device in the agent list).
import {
  fingerprint,
  isLabelText,
  LABEL_TEXT_MAX,
  orchestratorHandoverText,
  orchestratorKeyText,
  sealHandover,
  signText,
  verifyText,
} from '../crypto/agent';
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import { b64uDecode } from '../encoding/b64u';
import { parseTime } from '../encoding/time';
import type { PlatformState } from '../log/platform';
import { type OrgState, USER } from '../log/state';
import { HubError, type SignedClient } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { PairingLink } from './link';
import type { Pairing } from './lookup';
import type { PreparedPairing } from './pairing';

type Client = Pick<SignedClient, 'request'>;

/** How often the page reads the session's pairing while the session draws its keys, and for how long at most. */
export const POLL_MS = 1_000;
export const WAIT_MS = 60_000;
/** The reasons the hub names for a session that cannot become the main orchestrator (`analysis`: a session of an
 *  analysis task, ADR-097 section 2). */
export const REASONS = ['not_main', 'not_private', 'not_platform', 'not_running', 'analysis'] as const;
export const PAIRING_STATES = ['open', 'confirmed', 'active'] as const;

export interface SessionPairing {
  readonly pairingId: string;
  readonly pub: { readonly kem: string; readonly sig: string };
  readonly fp: string;
  readonly deviceSig: string;
  readonly state: (typeof PAIRING_STATES)[number];
  readonly expiresAt: number;
  readonly handover: boolean;
}

export interface OrchestratorView {
  readonly sessionId: string;
  readonly deviceId: string | null;
  readonly available: boolean;
  readonly reason: string | null;
  readonly requestedAt: number | null;
  readonly pairing: SessionPairing | null;
}

const path = (sessionId: string) => `/v1/sessions/${encodeURIComponent(sessionId)}/orchestrator`;

function parsePairing(value: unknown): SessionPairing | null {
  if (value === null) return null;
  if (!isRecord(value) || !isRecord(value.pub)) fail('header_invalid');
  const state = value.state;
  if (!(PAIRING_STATES as readonly unknown[]).includes(state) || typeof value.handover !== 'boolean') {
    fail('header_invalid');
  }
  if (typeof value.pairing_id !== 'string' || typeof value.fp !== 'string' || typeof value.device_sig !== 'string') {
    fail('header_invalid');
  }
  const kem = value.pub.kem;
  const sig = value.pub.sig;
  if (typeof kem !== 'string' || typeof sig !== 'string') fail('header_invalid');
  return {
    pairingId: value.pairing_id,
    pub: { kem, sig },
    fp: value.fp,
    deviceSig: value.device_sig,
    state: state as SessionPairing['state'],
    expiresAt: parseTime(value.expires_at),
    handover: value.handover,
  };
}

/** The checked answer of `GET` and `POST /v1/sessions/{id}/orchestrator` (`header_invalid` when malformed). */
export function parseView(body: unknown, sessionId: string): OrchestratorView {
  if (!isRecord(body) || body.session_id !== sessionId || typeof body.available !== 'boolean') fail('header_invalid');
  const device = body.device_id;
  const reason = body.reason;
  if (device !== null && typeof device !== 'string') fail('header_invalid');
  if (reason !== null && typeof reason !== 'string') fail('header_invalid');
  return {
    sessionId,
    deviceId: device,
    available: body.available,
    reason,
    requestedAt: body.requested_at === null ? null : parseTime(body.requested_at),
    pairing: parsePairing(body.pairing),
  };
}

/** Whether the session may become the main orchestrator, with the session's device and its current pairing. */
export async function loadOrchestrator(client: Client, sessionId: string): Promise<OrchestratorView> {
  return parseView(await client.request('GET', path(sessionId)), sessionId);
}

/** Asks the session's runner for keys (module comment, step 1). */
export async function requestOrchestrator(client: Client, sessionId: string): Promise<OrchestratorView> {
  return parseView(await client.request('POST', path(sessionId), { body: {} }), sessionId);
}

export interface Waiting {
  readonly now: () => number;
  readonly sleep: (ms: number) => Promise<void>;
  /** False once the page gave up waiting (closed dialog). */
  readonly live?: () => boolean;
}

/**
 * The view once the session's open pairing is there: read every `POLL_MS` from `view`; `orchestrator_unavailable`
 * when the session can no longer become the orchestrator, `unavailable` after `WAIT_MS` without keys.
 */
export async function waitForPairing(
  client: Client,
  view: OrchestratorView,
  waiting: Waiting,
): Promise<OrchestratorView> {
  const until = waiting.now() + WAIT_MS;
  let current = view;
  while (current.pairing?.state !== 'open') {
    if (!current.available) throw new HubError('orchestrator_unavailable', 409, { reason: current.reason });
    if (waiting.now() >= until || waiting.live?.() === false) throw new HubError('unavailable', 0);
    await waiting.sleep(POLL_MS);
    current = await loadOrchestrator(client, view.sessionId);
  }
  return current;
}

/**
 * The session's pairing checked against its fingerprint and the device's signature under the device key of the
 * verified platform log (module comment, step 2), in the form of a looked-up pairing.
 */
export async function checkSessionPairing(
  view: OrchestratorView,
  platform: PlatformState | null,
  pairing: SessionPairing,
  org: OrgState | null,
  owner: string,
): Promise<Pairing> {
  if (!privateOf(org, owner)) throw new HubError('orchestrator_unavailable', 409, { reason: 'not_private' });
  const kemPub = b64uDecode(pairing.pub.kem, 32);
  const sigPub = b64uDecode(pairing.pub.sig, 32);
  const certified = view.deviceId === null ? undefined : platform?.devices.get(view.deviceId);
  const certification = certified === undefined ? null : platform!.certification(certified);
  if (certification === null || certification.decertifiedSeq !== null) throw new HubError('pairing_mismatch', 0);
  if ((await fingerprint(kemPub, sigPub)) !== pairing.fp) throw new HubError('pairing_mismatch', 0);
  try {
    const text = orchestratorKeyText(pairing.fp, pairing.pairingId, view.sessionId);
    await verifyText(b64uDecode(certification.sig, 32), pairing.deviceSig, text);
  } catch {
    throw new HubError('pairing_mismatch', 0);
  }
  return {
    pairingId: pairing.pairingId,
    kemPub,
    sigPub,
    expiresAt: pairing.expiresAt,
    requestedAt: view.requestedAt ?? pairing.expiresAt,
    remote: false,
    agentCode: null,
  };
}

/**
 * The session's organization in the verified log is the owner's private one (ADR-082 section 9.6): private, founded
 * with the owner's key, and the owner its only active user member.
 */
export function privateOf(org: OrgState | null, owner: string): boolean {
  if (org === null || !org.private || org.member(owner)?.sig !== org.founder) return false;
  return [...org.principals.values()].every((p) => p.kind !== USER || p.removedSeq !== null || p.id === owner);
}

/** `text` cut to the label length (a device name may be longer). */
function clip(text: string): string {
  const chars = [...text.trim()];
  return chars.slice(0, LABEL_TEXT_MAX).join('').trim();
}

/** The label the browser seals for itself: what the agent is (`name`, e.g. "A session on DFX hardware") and the
 *  session's device (`host`, its name), under the label rules; never an ID (ADR-073). */
export function sessionLabel(deviceName: string | null, name: string): PairingLink {
  const host = deviceName === null ? '' : clip(deviceName);
  return {
    mode: 'loopback',
    pairingId: '',
    fingerprint: '',
    port: null,
    name: clip(name),
    host: isLabelText(host) ? host : 'DFX',
    message: null,
  };
}

/** The signed handover to the session's key (module comment, step 3). */
export async function handOver(
  client: Client,
  prepared: PreparedPairing,
  pairing: Pairing,
  sessionId: string,
  identity: Identity,
  rootKey: string,
): Promise<void> {
  const id = prepared.pairingId;
  const ct = await sealHandover(pairing.kemPub, id, prepared.userId, prepared.secret, rootKey);
  const sig = await signText(identity.sig.privateKey, orchestratorHandoverText(ct, id, sessionId));
  const answer = await client.request('POST', `/v1/agent-pairings/${encodeURIComponent(id)}/orchestrator-handover`, {
    body: { ct, sig },
  });
  if (!isRecord(answer) || answer.pairing_id !== id || answer.session_id !== sessionId) fail('header_invalid');
}
