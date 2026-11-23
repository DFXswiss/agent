// Job 2: wrap verification – accepted wraps open into the key ring, violations are ignored and counted
// (`wrap_unauthorized` counter), foreign scopes are skipped, undecryptable wraps are refused, nothing runs without log
// or identity.
import { beforeEach, describe, expect, it } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { signWrap } from '../../../src/core/crypto/wrap';
import { epochKey } from '../../../src/core/log/state';
import { job, fetchWraps, verifyOrgWraps } from '../../../src/core/jobs/wrap-verify';
import { verifyOrgLog } from '../../../src/core/jobs/log-verify';
import { orgEntry, wrapUnauthorized } from '../../../src/core/store/slices/orgs';
import { actors, type Actor, idOf } from '../log/world';
import { JobHub, signedIn } from './hub';

let alice: Actor, bob: Actor, carol: Actor;
let hub: JobHub;
let wg: string;

beforeEach(async () => {
  [alice, bob, carol] = await actors('alice', 'bob', 'carol');
  hub = await JobHub.create(alice);
  wg = await idOf('wrap wg');
  await hub.world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
  await hub.world.admitUser(alice, bob, 'member', { [wg]: 'read' });
});

async function ready(actor: Actor) {
  const setup = await signedIn(hub, actor);
  await verifyOrgLog(setup.context, hub.world.org);
  setup.changed.length = 0;
  return setup;
}

const keyOf = (scope: string, scopeId: string) => epochKey(scope, scopeId, 1);

describe('wrap verification job', () => {
  it('opens the wraps of the org into the key ring', async () => {
    const { store, context, changed } = await ready(bob);
    await job.run(context, { org: hub.world.org, trigger: 'wraps', generation: 0 });
    const entry = orgEntry(store.getState().orgs, hub.world.org);
    const expected = [keyOf('org', hub.world.org), keyOf('wg', hub.world.defaultWg), keyOf('wg', wg)].sort();
    expect([...entry.keys.keys()].sort()).toEqual(expected);
    const own = entry.keys.get(keyOf('wg', wg))!.reveal((raw) => b64uEncode(raw));
    expect(own).toBe(hub.world.currentKey(bob.id, 'wg', wg));
    expect(wrapUnauthorized(entry)).toBe(0);
    expect(changed).toEqual([hub.world.org]);
    await verifyOrgWraps(context, hub.world.org);
    expect(changed).toEqual([hub.world.org]);
  });

  it('ignores and counts unauthorized, forged and undecryptable wraps; skips foreign scopes', async () => {
    const { store, context } = await ready(bob);
    const valid = hub.wraps.find((w) => w.recipient_id === bob.id && w.scope === 'wg' && w.scope_id === wg)!;
    const garbage = b64uEncode(new Uint8Array(80).fill(7));
    const target = { scope: 'org', scope_id: hub.world.org, epoch: 2, recipient: bob.id };
    hub.wraps.push(
      { ...valid, epoch: 2 },
      { ...valid, wrapped_by: carol.id, scope_id: hub.world.defaultWg },
      {
        scope: 'org',
        scope_id: hub.world.org,
        epoch: 1,
        recipient_id: bob.id,
        wrap: garbage,
        sig: await signWrap(alice.identity.sig.privateKey, { ...target, epoch: 1 }, garbage),
        wrapped_by: alice.id,
      },
      { ...valid, scope: 'org', scope_id: await idOf('other org') },
      { ...valid, scope_id: await idOf('unknown wg') },
      { ...valid, recipient_id: carol.id },
      { ...valid, scope: 'cred', scope_id: bob.id },
    );
    // the undecryptable wrap shadows nothing: the valid org wrap for epoch 1 is opened first and kept
    hub.wraps.sort((a, b) => Number(b.wrap === garbage) - Number(a.wrap === garbage));
    await verifyOrgWraps(context, hub.world.org);
    const entry = orgEntry(store.getState().orgs, hub.world.org);
    expect(wrapUnauthorized(entry)).toBe(2);
    expect([...entry.refusedWraps.values()].sort()).toEqual(
      ['decrypt_failed', 'signature_invalid', 'wrap_unauthorized'].sort(),
    );
    expect(entry.keys.has(keyOf('org', hub.world.org))).toBe(true);
  });

  it('does nothing without identity or verified log; fails on a malformed answer', async () => {
    const setup = await signedIn(hub, bob);
    hub.requests.length = 0;
    await verifyOrgWraps(setup.context, hub.world.org);
    setup.store.update('me', (me) => ({ ...me, userId: null }));
    await verifyOrgWraps(setup.context, hub.world.org);
    setup.store.update('me', (me) => ({ ...me, identity: null }));
    await verifyOrgWraps(setup.context, hub.world.org);
    expect(hub.requests).toEqual([]);
    hub.fail.set('/v1/wraps', () => ({ wraps: [{ scope: 'org' }] }));
    await expect(fetchWraps(setup.context)).rejects.toThrow('unavailable');
    hub.fail.set('/v1/wraps', () => []);
    await expect(fetchWraps(setup.context)).rejects.toThrow('unavailable');
  });

  it('works against a hub without a platform log', async () => {
    hub.serve.platform = () => ({ entries: [], head: null });
    const { store, context } = await ready(bob);
    await verifyOrgWraps(context, hub.world.org);
    expect(store.getState().orgs.platform).toBeNull();
    expect(orgEntry(store.getState().orgs, hub.world.org).keys.size).toBe(3);
  });

  it('writes nothing when the store was reset during the run', async () => {
    const { store, context } = await ready(bob);
    const generation = store.generation;
    const wraps = hub.wraps.map((wrap) => ({ ...wrap, created_at: '2026-11-21T12:00:00.000Z' }));
    hub.fail.set('/v1/wraps', () => {
      store.reset();
      return { wraps };
    });
    const log = orgEntry(store.getState().orgs, hub.world.org);
    await verifyOrgWraps(context, hub.world.org, generation);
    expect(log.keys.size).toBe(0);
    expect(store.getState().orgs.byId).toEqual({});
  });

  it('runs without a change listener', async () => {
    const setup = await ready(bob);
    const { changed: _changed, ...context } = setup.context;
    await verifyOrgWraps(context, hub.world.org);
    expect(orgEntry(setup.store.getState().orgs, hub.world.org).keys.size).toBe(3);
  });
});
