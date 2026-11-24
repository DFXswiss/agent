// Job 6 of 06-web.md section 11 (04-crypto.md section 5.2 step 2, W1): after `accept` the package keys are wrapped
// for the user's own identity – only those without a wrap of the user yet, only scopes the verified log lets the user
// read and epochs that exist – and the queue empties after a run that sent them.
import { describe, expect, it } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { unwrapKey, verifyWrap } from '../../../src/core/crypto/wrap';
import { Secret } from '../../../src/core/crypto/secret';
import type { JobContext } from '../../../src/core/jobs';
import { job, queuedSelfWraps, queueSelfWraps, storeSelfWraps } from '../../../src/core/jobs/self-wraps';
import { epochKey, ORG, WG } from '../../../src/core/log/state';
import { createStore } from '../../../src/core/store';
import { orgEntry, setLog } from '../../../src/core/store/slices/orgs';
import { openEvent } from '../../../src/core/crypto/event';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { idOf } from '../log/world';
import { orgWorld } from '../org/world';

async function joined(options: { admitted?: boolean } = {}) {
  const hidden = await idOf('wg hidden');
  const w = await orgWorld({
    as: 'bob',
    setup: async (world, a) => {
      if (options.admitted !== false) await world.admitUser(a.ada!, a.bob!, 'member', {});
      await world.batch(a.ada!, [{ type: 'wg_create', wg: hidden, epoch: 1 }]);
      if (options.admitted !== false) {
        await world.batch(a.ada!, [{ type: 'rotate', scope: 'wg', scope_id: world.defaultWg, epoch: 2 }]);
      }
    },
  });
  const ada = w.world.ring(w.others.ada.id);
  const key = (scope: string, scopeId: string, epoch: number) => ({
    scope,
    scope_id: scopeId,
    epoch,
    key: ada(scope, scopeId, epoch) ?? Secret.of(new Uint8Array(32).fill(epoch)),
  });
  const context = { store: w.store, client: w.client, persist: {} as never, rootKey: '', now: () => 0 } as JobContext;
  const run = () => ({ org: w.org, trigger: 'log' as const, generation: w.store.generation });
  return { w, hidden, key, context, run, me: w.me };
}

describe('self-wraps job', () => {
  it('drops the queue of another user (sign-out and another sign-in in the same tab)', async () => {
    const { w, key, context, run } = await joined();
    queueSelfWraps(w.store, w.org, w.others.cy.id, [key(WG, w.world.defaultWg, 1)], 'Cy');
    expect(await storeSelfWraps(context, run())).toBe(0);
    expect(queuedSelfWraps(w.store, w.org)).toBeNull();
    expect(w.client.calls).toEqual([]);
    // nothing runs without a signed-in, unlocked user
    queueSelfWraps(w.store, w.org, w.me.id, []);
    const anonymous = { ...context, store: createStore() };
    expect(await storeSelfWraps(anonymous, run())).toBe(0);
    // an org without a verified log yet
    queueSelfWraps(w.store, 'unknown-org', w.me.id, []);
    expect(await storeSelfWraps(context, { ...run(), org: 'unknown-org' })).toBe(0);
    expect(queuedSelfWraps(w.store, 'unknown-org')).not.toBeNull();
  });

  it('sends a pending member.profile once the current org key is held', async () => {
    const { w, context, run, me } = await joined();
    // the org rotated: the store has the new log but not yet the key of the new epoch
    const before = orgEntry(w.store.getState().orgs, w.org).keys;
    await w.world.batch(w.others.ada, [{ type: 'rotate', scope: 'org', scope_id: w.org, epoch: 2 }]);
    setLog(w.store, w.org, w.world.log!);
    expect(before.has(epochKey(ORG, w.org, 2))).toBe(false);
    queueSelfWraps(w.store, w.org, me.id, [], 'Bob B.');
    expect(await storeSelfWraps(context, run())).toBe(0);
    expect(queuedSelfWraps(w.store, w.org)).toEqual({ userId: me.id, keys: [], profile: 'Bob B.' });
    await w.sync();
    expect(await storeSelfWraps(context, run())).toBe(1);
    const [post] = w.client.of('POST /v1/events');
    const event = (post!.body as { events: { event: unknown }[] }).events[0]!.event;
    const key = orgEntry(w.store.getState().orgs, w.org).keys.get(epochKey(ORG, w.org, 2))!;
    const opened = await openEvent(key, event, me.identity.sigPublic());
    expect(JSON.parse(fromUtf8(opened))).toEqual({ display_name: 'Bob B.' });
    expect(queuedSelfWraps(w.store, w.org)).toBeNull();
  });

  it('wraps the queued package keys the user has no wrap for yet, and only those W1 allows', async () => {
    const { w, hidden, key, context, run, me } = await joined();
    const dwg = w.world.defaultWg;
    // the accept batch carried no wrap of the old default-workgroup epoch (rotated since)
    const index = w.hub.stored.findIndex(
      (x) => x.recipient_id === me.id && x.scope === WG && x.scope_id === dwg && x.epoch === 1,
    );
    if (index >= 0) w.hub.stored.splice(index, 1);
    const keys = [key(ORG, w.org, 1), key(WG, dwg, 1), key(WG, dwg, 2), key(WG, hidden, 1), key(WG, dwg, 5)];
    queueSelfWraps(w.store, w.org, w.me.id, keys);
    expect(queuedSelfWraps(w.store, w.org)!.keys).toHaveLength(5);
    expect(await storeSelfWraps(context, run())).toBe(1);
    const [post] = w.client.of('POST /v1/wraps');
    const [wrap] = (
      post!.body as {
        wraps: { scope: string; scope_id: string; epoch: number; recipient_id: string; wrap: string; sig: string }[];
      }
    ).wraps;
    expect(wrap).toMatchObject({ scope: WG, scope_id: dwg, epoch: 1, recipient_id: me.id });
    const slot = { scope: WG, scope_id: dwg, epoch: 1, recipient: me.id };
    await verifyWrap(me.identity.sigPublic(), slot, wrap!.wrap, wrap!.sig);
    const opened = await unwrapKey(me.identity.kem, slot, wrap!.wrap);
    expect(opened.reveal(b64uEncode)).toBe(keys[1]!.key.reveal(b64uEncode));
    // one GET per scope of the queue
    expect(w.client.of('GET /v1/wraps').map((call) => call.target)).toEqual([
      `/v1/wraps?scope=org&scope_id=${w.org}`,
      `/v1/wraps?scope=wg&scope_id=${dwg}`,
      `/v1/wraps?scope=wg&scope_id=${hidden}`,
    ]);
    expect(queuedSelfWraps(w.store, w.org)).toBeNull();
    expect(await storeSelfWraps(context, run())).toBe(0);
    expect(w.client.of('GET /v1/wraps')).toHaveLength(3);
  });

  it('sends nothing when every key is wrapped, and clears the queue', async () => {
    const { w, key, context, run } = await joined();
    queueSelfWraps(w.store, w.org, w.me.id, [key(ORG, w.org, 1)]);
    expect(await storeSelfWraps(context, run())).toBe(0);
    expect(w.client.of('POST /v1/wraps')).toEqual([]);
    expect(queuedSelfWraps(w.store, w.org)).toBeNull();
  });

  it('waits for the verified log with the accept, the identity and a queue', async () => {
    const { w, key, context, run } = await joined({ admitted: false });
    expect(queuedSelfWraps(createStore(), w.org)).toBeNull();
    expect(await storeSelfWraps(context, run())).toBe(0);
    queueSelfWraps(w.store, w.org, w.me.id, [key(ORG, w.org, 1)]);
    expect(await storeSelfWraps(context, run())).toBe(0);
    expect(queuedSelfWraps(w.store, w.org)!.keys).toHaveLength(1);
    expect(w.client.calls).toEqual([]);
  });

  it('ignores foreign wraps in the answer, fails on a malformed one and writes nothing after a sign-out', async () => {
    const { w, key, context, run, me } = await joined();
    const keys = [key(WG, w.world.defaultWg, 1)];
    queueSelfWraps(w.store, w.org, w.me.id, keys);
    w.client.on('GET /v1/wraps', () => ({ wraps: 'no' }));
    await expect(storeSelfWraps(context, run())).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on('GET /v1/wraps', () => null);
    await expect(storeSelfWraps(context, run())).rejects.toMatchObject({ code: 'unavailable' });
    const slot = { scope: WG, scope_id: w.world.defaultWg, epoch: 1 };
    const started = run();
    w.client.on('GET /v1/wraps', () => {
      // a sign-out while the run waits for the hub
      w.store.reset();
      return {
        wraps: [
          7,
          { ...slot, recipient_id: 'other' },
          { ...slot, scope: ORG, recipient_id: me.id },
          { ...slot, scope_id: 'x', recipient_id: me.id },
        ],
      };
    });
    expect(await storeSelfWraps(context, started)).toBe(0);
    expect(w.client.of('POST /v1/wraps')).toEqual([]);
  });

  it('keeps a queue that was replaced during the run', async () => {
    const { w, key, context, run } = await joined();
    const first = [key(ORG, w.org, 1)];
    const second = [key(WG, w.world.defaultWg, 2)];
    queueSelfWraps(w.store, w.org, w.me.id, first);
    w.client.on('GET /v1/wraps', () => {
      queueSelfWraps(w.store, w.org, w.me.id, second);
      return { wraps: [] };
    });
    await storeSelfWraps(context, run());
    expect(queuedSelfWraps(w.store, w.org)!.keys).toEqual(second);
  });

  it('is registered for sign-in, log changes and the interval after catch-up', async () => {
    const { w, key, context, run } = await joined();
    expect(job).toMatchObject({ name: 'self-wraps', order: 35, triggers: ['login', 'log', 'interval'] });
    queueSelfWraps(w.store, w.org, w.me.id, [key(WG, w.world.defaultWg, 1)]);
    await job.run(context, run());
    expect(queuedSelfWraps(w.store, w.org)).toBeNull();
    expect(epochKey(ORG, w.org, 1)).toContain(w.org);
  });
});
