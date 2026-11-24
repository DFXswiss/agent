// A signed-in user with an identity and a scriptable client for the agent flows (`core/agents`), and a world with a
// verified org, the user's credential key and a hub that confirms pairings with their log batches (ADR-082).
import { vi } from 'vitest';
import type { PairingLink } from '../../../src/core/agents/link';
import type { ConfirmBatch, Pairing } from '../../../src/core/agents/pairing';
import { checkBatch } from '../../../src/core/log/rotation';
import type { WrapRecord } from '../../../src/core/log/wraps';
import { HubError } from '../../../src/core/net/signedFetch';
import { setLog } from '../../../src/core/store/slices/orgs';
import { credWorld } from '../credentials/world';
import { type Actor, T0, TS } from '../log/world';
import { Identity } from '../../../src/core/crypto/keys';
import { createStore } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';

export const USER = '01a1ee56-5e00-77be-8cf3-1f27bb0e92a2';
export const AGENT = '01a1ee56-5e00-7242-9e24-f87629ad2977';
export const OTHER = '01a1ee56-5e00-79bc-ad68-1fafbfc5d917';

export type Handler = (method: string, target: string, body: unknown) => unknown;

export async function agentWorld(handler: Handler = () => ({})) {
  const store = createStore();
  const identity = await Identity.generate();
  setSession(store, USER, { keyId: 'session.x', expiresAt: Date.now() + 3_600_000 });
  setIdentity(store, identity);
  const request = vi.fn(async (method: string, target: string, options: { body?: unknown } = {}) =>
    handler(method, target, options.body),
  );
  return { store, identity, client: { request }, context: { store, client: { request } } };
}

/** The confirmation hub of `confirmWorld`: applies each batch with the hub's batch rules, stores the `CK` wraps. */
export interface ConfirmHub {
  /** Bodies of `POST /v1/me/agents` in order. */
  readonly bodies: Record<string, unknown>[];
  /** Answers before applying anything (e.g. `seq_conflict`); a function returns the answer itself. */
  readonly before: (HubError | (() => unknown))[];
}

/**
 * The credential world (an org with a verified log, the user's `CK` self-wrap, a credential store) and a hub that
 * confirms pairings (`POST /v1/me/agents`); `refreshLog` puts the hub's log into the store like the log job.
 */
export async function confirmWorld(options: { member?: boolean } = {}) {
  const t = await credWorld(options);
  const hub: ConfirmHub = { bodies: [], before: [] };
  t.context.refreshLog.mockImplementation(async (org: string) => {
    if (org === t.org) setLog(t.store, t.org, t.world.log!);
    return true;
  });
  t.client.on('POST /v1/me/agents', async (raw) => {
    const body = raw as Record<string, unknown> & { batches: ConfirmBatch[]; cred_wraps: WrapRecord[] };
    hub.bodies.push(body);
    const next = hub.before.shift();
    if (next instanceof HubError) throw next;
    if (next !== undefined) return next();
    for (const batch of body.batches) {
      if (batch.expected_seq !== t.world.log!.head.seq + 1) throw new HubError('seq_conflict', 409);
      const result = await checkBatch(t.world.log, batch.entries, batch.wraps, t.me.id, {
        platform: t.world.platform,
      });
      t.world.log = result.log;
      await t.world.store(batch.wraps);
    }
    for (const wrap of body.cred_wraps) t.hub.wraps.push({ ...wrap, wrapped_by: t.me.id, created_at: TS });
    return { agent_id: body.pairing_id };
  });
  return { ...t, confirm: hub };
}

/** A pairing of `agent` as `lookupPairing` returns it. */
export function pairingOf(agent: Actor, remote = false): Pairing {
  return {
    pairingId: agent.id,
    kemPub: agent.identity.kemPublic(),
    sigPub: agent.identity.sigPublic(),
    expiresAt: T0 + 600_000,
    requestedAt: T0,
    remote,
    agentCode: null,
  };
}

/** The pairing link of `agent`. */
export function linkOf(agent: Actor, name = 'Claude Code'): PairingLink {
  return { mode: 'loopback', pairingId: agent.id, fingerprint: 'f', port: 40123, name, host: 'Mac', message: null };
}
