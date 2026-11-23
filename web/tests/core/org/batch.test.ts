// U-WEB-log (WEB-06): the append flow `submitBatch` against the simulated hub of tests/core/org/world.ts – the batch
// of a removal (entry + R + wraps for every remaining recipient), the automatic rebuild after `seq_conflict` (at most
// three attempts), a change another admin already made, refusals, the retry reports and what is kept afterwards.
import { describe, expect, it } from 'vitest';
import { CryptoError } from '../../../src/core/crypto/errors';
import { ORG, WG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import {
  actionErrorCode,
  actorOf,
  BATCH_ATTEMPTS,
  install,
  OrgActionError,
  requireHead,
  submitBatch,
  type SubmitProgress,
  verifiedPlatform,
} from '../../../src/core/org/batch';
import { removePlan, ungrantPlan } from '../../../src/core/org/members';
import { createStore } from '../../../src/core/store';
import { blockOrg, orgEntry } from '../../../src/core/store/slices/orgs';
import { setIdentity } from '../../../src/core/store/slices/me';
import { idOf } from '../log/world';
import { holds, orgWorld } from './world';

async function withMembers(options: { as?: 'ada' | 'bob' | 'cy' } = {}) {
  const one = await idOf('wg one');
  const two = await idOf('wg two');
  const w = await orgWorld({
    ...options,
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.batch(a.ada!, [{ type: 'wg_create', wg: two, epoch: 1 }]);
      await world.admitUser(a.ada!, a.bob!, 'member', { [one]: 'read' });
      await world.admitUser(a.ada!, a.cy!, 'admin', {});
      await world.admitDevice(a.ada!, a.dev1!, [one]);
    },
  });
  return { w, one, two };
}

describe('submitBatch', () => {
  it('removes a member with R and wraps for every remaining recipient, and keeps the new keys', async () => {
    const { w, one } = await withMembers();
    const { bob, cy, dev1 } = w.others;
    const steps: SubmitProgress[] = [];
    const { built, attempts } = await submitBatch(w.context, w.org, removePlan(bob.id), (s) => steps.push(s));
    expect(attempts).toBe(1);
    // bob read the org, the default workgroup (always) and workgroup one
    expect(built!.body.entries.map((e) => e.body.type)).toEqual(['remove', 'rotate', 'rotate', 'rotate']);
    expect(built!.required).toHaveLength(3);
    expect(built!.required).toEqual(
      expect.arrayContaining([
        { scope: ORG, scope_id: w.org },
        { scope: WG, scope_id: one },
        { scope: WG, scope_id: w.world.defaultWg },
      ]),
    );
    // only a new attempt is reported (key handling stays out of sight, ADR-059 section 4)
    expect(steps).toEqual([]);
    // the hub took it: everyone but bob holds the new epochs
    expect(w.world.state.member(bob.id)).toBeNull();
    expect(holds(w, cy, ORG, w.org)).toBe(true);
    expect(holds(w, dev1, WG, one)).toBe(true);
    expect(holds(w, dev1, ORG, w.org)).toBe(true);
    expect(holds(w, bob, ORG, w.org)).toBe(false);
    // the store has the batch's log and the new keys
    const entry = orgEntry(w.store.getState().orgs, w.org);
    expect(entry.log!.head).toEqual(w.world.log!.head);
    expect(entry.keys.has(`org ${w.org} 2`)).toBe(true);
    expect(entry.keys.has(`wg ${one} 2`)).toBe(true);
  });

  it('keeps the new keys of the scopes the user reads only', async () => {
    const { w, one } = await withMembers({ as: 'cy' });
    const { bob } = w.others;
    const { built } = await submitBatch(w.context, w.org, ungrantPlan(bob.id, one));
    expect(built!.required).toEqual([{ scope: WG, scope_id: one }]);
    const entry = orgEntry(w.store.getState().orgs, w.org);
    expect(entry.log!.state.epoch(WG, one)).toBe(2);
    // cy reads workgroup one as admin (ADR-059)
    expect(entry.keys.has(`wg ${one} 2`)).toBe(true);
    // bob no longer reads it: installed for bob, the new key is not kept
    const other = createStore();
    install(other, w.org, bob.id, built!);
    expect(orgEntry(other.getState().orgs, w.org).keys.size).toBe(0);
  });

  it('rebuilds after seq_conflict on the re-read log', async () => {
    const { w, two } = await withMembers();
    const { ada, bob } = w.others;
    w.hub.before.push(() => w.hub.foreign(ada, { type: 'grant', subject: bob.id, wg: two, access: 'write' }));
    const steps: SubmitProgress[] = [];
    const { built, attempts } = await submitBatch(w.context, w.org, removePlan(bob.id), (s) => steps.push(s));
    expect(attempts).toBe(2);
    expect(w.context.refreshLog).toHaveBeenCalledTimes(1);
    expect(steps).toContainEqual({ step: 'retry', attempt: 2 });
    // the rebuilt batch rotates the workgroup bob got meanwhile, too
    expect(built!.required.map((r) => r.scope_id)).toContain(two);
  });

  it('gives up after three conflicts', async () => {
    const { w } = await withMembers();
    const { ada, bob } = w.others;
    for (let i = 0; i < BATCH_ATTEMPTS; i += 1) {
      w.hub.before.push(() => w.hub.foreign(ada, { type: 'role', subject: bob.id, role: i % 2 ? 'member' : 'admin' }));
    }
    await expect(submitBatch(w.context, w.org, removePlan(bob.id))).rejects.toMatchObject({ code: 'seq_conflict' });
    expect(w.context.refreshLog).toHaveBeenCalledTimes(BATCH_ATTEMPTS - 1);
  });

  it('is done when another admin made the change first', async () => {
    const { w } = await withMembers();
    const { cy, bob } = w.others;
    w.hub.before.push(async () => {
      await w.world.batch(cy, [{ type: 'remove', subject: bob.id }]);
    });
    const { built, attempts } = await submitBatch(w.context, w.org, removePlan(bob.id));
    expect(built).toBeNull();
    expect(attempts).toBe(2);
  });

  it('passes other refusals on without a retry', async () => {
    const { w } = await withMembers();
    const { bob } = w.others;
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new HubError('forbidden', 403);
    });
    await expect(submitBatch(w.context, w.org, removePlan(bob.id))).rejects.toMatchObject({ code: 'forbidden' });
    expect(w.context.refreshLog).not.toHaveBeenCalled();
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new Error('network');
    });
    await expect(submitBatch(w.context, w.org, removePlan(bob.id))).rejects.toThrow('network');
  });

  it('refuses an answer that does not name the new head', async () => {
    const { w } = await withMembers();
    const { bob } = w.others;
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => ({ head: { seq: 1, hash: 'x' } }));
    await expect(submitBatch(w.context, w.org, removePlan(bob.id))).rejects.toMatchObject({ code: 'unavailable' });
    const log = orgEntry(w.store.getState().orgs, w.org).log!;
    expect(() => requireHead(null, log)).toThrow(HubError);
    expect(() => requireHead({ head: { seq: log.head.seq, hash: 'other' } }, log)).toThrow(HubError);
    expect(() => requireHead({ head: log.head }, log)).not.toThrow();
  });
});

describe('without a verified platform log', () => {
  it('builds for an org without platform devices', async () => {
    const w = await orgWorld({
      noPlatform: true,
      setup: async (world, a) => world.admitUser(a.ada, a.bob, 'member', {}),
    });
    expect(verifiedPlatform(w.store)).toBeNull();
    const { built } = await submitBatch(w.context, w.org, removePlan(w.others.bob.id));
    expect(built!.required).toHaveLength(2);
  });
});

describe('actorOf and codes', () => {
  it('needs an unlocked user and a verified, unblocked log', async () => {
    const { w } = await withMembers();
    expect(actorOf(w.store, w.org).userId).toBe(w.me.id);
    expect(() => actorOf(w.store, 'other')).toThrow(expect.objectContaining({ code: 'log_empty' }));
    blockOrg(w.store, w.org, { code: 'log_fork', stored: null, served: null, lh: 3 });
    expect(() => actorOf(w.store, w.org)).toThrow(expect.objectContaining({ code: 'log_fork' }));
    setIdentity(w.store, null);
    expect(() => actorOf(w.store, w.org)).toThrow(expect.objectContaining({ code: 'unauthorized' }));
  });

  it('maps every failure to a catalog code', () => {
    expect(actionErrorCode(new OrgActionError('last_admin'))).toBe('last_admin');
    expect(actionErrorCode(new HubError('seq_conflict', 409))).toBe('seq_conflict');
    expect(actionErrorCode(new CryptoError('log_rights'))).toBe('log_rights');
    expect(actionErrorCode(new Error('x'))).toBe('unavailable');
    expect(new OrgActionError('x').name).toBe('OrgActionError');
  });
});
