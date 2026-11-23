// U-WEB-sync (WEB-06): the jobs of 06-web.md section 11.3 (catch-up wrapping) and 11.4 (pending rotations) against
// the simulated hub of tests/core/org/world.ts: what the hub offers is checked against the verified logs, own `cred`
// slots are wrapped with the own CK, owed rotations and unassignments of decertified devices are sent silently,
// and the job registrations (order, triggers).
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { formatTime } from '../../../src/core/encoding/time';
import { Secret } from '../../../src/core/crypto/secret';
import { signWrap, wrapKey } from '../../../src/core/crypto/wrap';
import { backgroundJobs, type JobContext, type JobRun } from '../../../src/core/jobs';
import { catchUp, credWraps, INTERVAL_MS, job as catchUpJob, ownCredentialKey } from '../../../src/core/jobs/catch-up';
import { job as rotationJob, MAX_BATCHES, owedPlanner, rotatePending } from '../../../src/core/jobs/rotation';
import { buildPlatformEntries, entryHash } from '../../../src/core/log/entries';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { ORG, WG } from '../../../src/core/log/state';
import { VerifiedLog } from '../../../src/core/log/verify';
import { blockOrg, ensureOrg, orgEntry, setLog } from '../../../src/core/store/slices/orgs';
import { setIdentity } from '../../../src/core/store/slices/me';
import type { Actor } from '../log/world';
import { idOf, seed, T0 } from '../log/world';
import { holds, type OrgWorld, orgWorld } from '../org/world';

function jobContext(w: OrgWorld, rootKey = w.platform.root): JobContext {
  return {
    store: w.store,
    client: w.client,
    rootKey,
    now: () => w.hub.now,
    persist: {
      loadPinned: async () => null,
      savePinned: async () => undefined,
      loadPlatformHead: async () => null,
      savePlatformHead: async () => undefined,
    },
  };
}

const run = (w: OrgWorld, generation = w.store.generation): JobRun => ({ org: w.org, trigger: 'interval', generation });

async function setup(options: { as?: 'ada' | 'bob' | 'cy'; private?: boolean } = {}) {
  const one = await idOf('wg one');
  const w = await orgWorld({
    ...options,
    platformDevices: ['pdev'],
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada!, a.bob!, 'member', {});
      await world.admitUser(a.ada!, a.cy!, 'member', { [one]: 'write' });
      await world.admitDevice(a.ada!, a.dev1!, [one]);
      if (options.private) {
        await world.batch(a.ada!, [{ type: 'device_assign', device: a.pdev!.id, wg: one, platform_seq: 2 }]);
      }
    },
  });
  return { w, a: w.others as Record<string, Actor>, one };
}

/** A self-wrap of `owner`'s CK of `epoch` as the hub stores it. */
async function credWrap(owner: Actor, epoch: number, key: Secret, signer: Actor = owner) {
  const slot = { scope: 'cred', scope_id: owner.id, epoch, recipient: owner.id };
  const wrap = await wrapKey(b64uDecode(owner.identity.public().kem), slot, key);
  const sig = await signWrap(signer.identity.sig.privateKey, slot, wrap);
  return { scope: 'cred', scope_id: owner.id, epoch, recipient_id: owner.id, wrap, sig, wrapped_by: owner.id };
}

describe('catch-up job', () => {
  it('wraps the slots the verified log confirms and drops what it does not', async () => {
    const { w, a, one } = await setup();
    // bob was granted workgroup one by another admin without wraps
    await w.hub.foreign(a.ada!, { type: 'grant', subject: a.bob!.id, wg: one, access: 'read' });
    await w.sync();
    const outsider = await idOf('outsider');
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({
      missing: [
        { scope: WG, scope_id: one, epoch: 1, recipient_id: a.bob!.id },
        { scope: WG, scope_id: one, epoch: 1, recipient_id: a.bob!.id },
        { scope: WG, scope_id: one, epoch: 1, recipient_id: outsider },
        { scope: ORG, scope_id: w.org, epoch: 7, recipient_id: a.bob!.id },
      ],
    }));
    expect(await catchUp(jobContext(w), run(w))).toBe(1);
    expect(holds(w, a.bob!, WG, one)).toBe(true);
    expect(w.client.of('POST /v1/wraps')[0]!.body).toMatchObject({ wraps: [{ scope: WG, recipient_id: a.bob!.id }] });
  });

  it('skips confirmed slots whose key is not in memory', async () => {
    const { w, a, one } = await setup();
    await w.hub.foreign(a.ada!, { type: 'grant', subject: a.bob!.id, wg: one, access: 'read' });
    await w.sync();
    (w.store.getState().orgs.byId[w.org]!.keys as Map<string, unknown>).delete(`wg ${one} 1`);
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({
      missing: [{ scope: WG, scope_id: one, epoch: 1, recipient_id: a.bob!.id }],
    }));
    expect(await catchUp(jobContext(w), run(w))).toBe(0);
  });

  it('sends nothing when nothing is offered, confirmed or current, and refuses a malformed offer', async () => {
    const { w, a } = await setup();
    expect(await catchUp(jobContext(w), run(w))).toBe(0);
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({
      missing: [{ scope: ORG, scope_id: w.org, epoch: 1, recipient_id: a.bob!.id }],
    }));
    // a wrap the hub already has is recreated only once it is missing; here bob is entitled, so a run after a
    // sign-out (generation changed) sends nothing
    expect(await catchUp(jobContext(w), run(w, w.store.generation - 1))).toBe(0);
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({ missing: [{ scope: ORG }] }));
    await expect(catchUp(jobContext(w), run(w))).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => null);
    await expect(catchUp(jobContext(w), run(w))).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({
      missing: [{ scope: WG, scope_id: 'unknown', epoch: 1, recipient_id: a.bob!.id }],
    }));
    expect(await catchUp(jobContext(w), run(w))).toBe(0);
    expect(w.client.of('POST /v1/wraps')).toHaveLength(0);
    setIdentity(w.store, null);
    expect(await catchUp(jobContext(w), run(w))).toBe(0);
  });

  it('wraps the own CK for entitled devices only', async () => {
    const { w, a } = await setup();
    const ck = Secret.of(new Uint8Array(32).fill(7));
    const older = Secret.of(new Uint8Array(32).fill(8));
    w.hub.stored.push(
      await credWrap(a.ada!, 1, older),
      // the newest own wrap is signed by somebody else: skipped, the older one is used
      await credWrap(a.ada!, 3, ck, a.bob!),
      { ...(await credWrap(a.ada!, 2, ck)), wrapped_by: a.bob!.id },
      { ...(await credWrap(a.ada!, 2, ck)), epoch: 'x' as never },
    );
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({
      missing: [
        { scope: 'cred', scope_id: a.ada!.id, epoch: 1, recipient_id: a.dev1!.id },
        { scope: 'cred', scope_id: a.ada!.id, epoch: 2, recipient_id: a.dev1!.id },
        { scope: 'cred', scope_id: a.ada!.id, epoch: 1, recipient_id: a.bob!.id },
        { scope: 'cred', scope_id: a.bob!.id, epoch: 1, recipient_id: a.dev1!.id },
      ],
    }));
    expect(await catchUp(jobContext(w), run(w))).toBe(1);
    const sent = (w.client.of('POST /v1/wraps')[0]!.body as { wraps: { recipient_id: string; epoch: number }[] }).wraps;
    expect(sent).toMatchObject([{ scope: 'cred', recipient_id: a.dev1!.id, epoch: 1 }]);
    expect(await ownCredentialKey(jobContext(w), a.ada!.id, a.ada!.identity)).toMatchObject({ epoch: 1 });
    // orgs without a verified log and blocked orgs entitle nobody
    ensureOrg(w.store, 'unverified');
    blockOrg(w.store, w.org, { code: 'log_fork', stored: null, served: null, lh: 1 });
    const slot = { scope: 'cred', scope_id: a.ada!.id, epoch: 1, recipient: a.dev1!.id };
    expect(await credWraps(jobContext(w), a.ada!.id, a.ada!.identity, [slot])).toEqual([]);
  });

  it('wraps no CK for the devices of an org that admitted the user without consent (ADR-060)', async () => {
    const { w, a } = await setup();
    w.hub.stored.push(await credWrap(a.ada!, 1, Secret.of(new Uint8Array(32).fill(7))));
    const slot = { scope: 'cred', scope_id: a.ada!.id, epoch: 1, recipient: a.dev1!.id };
    expect(await credWraps(jobContext(w), a.ada!.id, a.ada!.identity, [slot])).toHaveLength(1);
    const log = orgEntry(w.store.getState().orgs, w.org).log!;
    const principal = log.state.principals.get(a.ada!.id)!;
    const admitted = log.state.with({
      principals: new Map(log.state.principals).set(principal.id, { ...principal, consented: false }),
    });
    setLog(w.store, w.org, new VerifiedLog(log.entries, [...log.states.slice(0, -1), admitted]));
    expect(await credWraps(jobContext(w), a.ada!.id, a.ada!.identity, [slot])).toEqual([]);
  });

  it('has no CK to wrap without an own self-wrap or a readable answer', async () => {
    const { w, a } = await setup();
    const slot = { scope: 'cred', scope_id: a.ada!.id, epoch: 1, recipient: a.dev1!.id };
    expect(await credWraps(jobContext(w), a.ada!.id, a.ada!.identity, [slot])).toEqual([]);
    w.client.on('GET /v1/wraps', () => null);
    expect(await ownCredentialKey(jobContext(w), a.ada!.id, a.ada!.identity)).toBeNull();
    expect(await credWraps(jobContext(w), a.ada!.id, a.ada!.identity, [])).toEqual([]);
    // orgs without a verified log do not entitle anybody, nor do blocked ones
    ensureOrg(w.store, 'unverified');
    w.client.on('GET /v1/wraps', () => ({ wraps: [] }));
    blockOrg(w.store, w.org, { code: 'log_fork', stored: null, served: null, lh: 1 });
    expect(await credWraps(jobContext(w), a.ada!.id, a.ada!.identity, [slot])).toEqual([]);
  });
});

describe('rotation job', () => {
  it('rotates what a member owed after leaving, silently', async () => {
    const { w, a, one } = await setup();
    await w.world.batch(a.cy!, [{ type: 'remove', subject: a.cy!.id }]);
    await w.sync();
    const pending = w.world.state.pendingScopes().length;
    expect(pending).toBe(3);
    expect(await rotatePending(jobContext(w), run(w))).toBe(1);
    expect(w.world.state.pendingScopes()).toEqual([]);
    expect(w.world.state.epoch(WG, one)).toBe(2);
    expect(holds(w, a.dev1!, WG, one)).toBe(true);
    expect(Object.keys(w.store.getState())).not.toContain('rotation');
    // nothing owed any more
    expect(await rotatePending(jobContext(w), run(w))).toBe(0);
  });

  it('rotates only what a writer may rotate', async () => {
    const { w, a, one } = await setup({ as: 'bob' });
    await w.world.batch(a.cy!, [{ type: 'remove', subject: a.cy!.id }]);
    await w.sync();
    const plan = owedPlanner(null)(w.world.log!, a.bob!.id)!;
    // bob writes the default workgroup only: neither the org scope nor workgroup one
    expect(plan.entries).toEqual([
      { type: 'rotate', scope: WG, scope_id: w.world.defaultWg, epoch: w.world.state.epoch(WG, w.world.defaultWg) + 1 },
    ]);
    expect(owedPlanner(null)(w.world.log!, a.dev1!.id)).toBeNull();
    expect(one).toBeTruthy();
  });

  it('unassigns decertified platform devices everywhere, then rotates what is owed (two batches)', async () => {
    const { w, a, one } = await setup({ private: true });
    w.hub.platformAccess = true;
    await w.world.batch(a.cy!, [{ type: 'remove', subject: a.cy!.id }]);
    await w.sync();
    const root = await ed25519KeyPair(await seed('platform root'));
    const last = w.platform.entries[w.platform.entries.length - 1]!;
    const head = { seq: w.platform.entries.length - 1, hash: await entryHash(last) };
    const decertify = [{ type: 'device_decertify', device: a.pdev!.id }];
    w.platform.entries.push(...(await buildPlatformEntries(head, root.privateKey, decertify, formatTime(T0))));
    w.hub.platform = await verifyPlatformLog(w.platform.entries, w.platform.root);
    expect(await rotatePending(jobContext(w), run(w))).toBe(MAX_BATCHES);
    expect(w.world.state.assigned(a.pdev!.id, one)).toBe(false);
    // the unassignment rotated workgroup one and the org (pdev's only workgroup); the leave owed the default one
    expect(w.world.state.epoch(ORG, w.org)).toBe(2);
    expect(w.world.state.pendingScopes()).toEqual([]);
  });

  it('rebuilds after seq_conflict on the log the job re-reads', async () => {
    const { w, a } = await setup();
    await w.world.batch(a.cy!, [{ type: 'remove', subject: a.cy!.id }]);
    await w.sync();
    w.hub.before.push(() => w.hub.foreign(a.ada!, { type: 'role', subject: a.bob!.id, role: 'admin' }));
    expect(await rotatePending(jobContext(w), run(w))).toBe(1);
    expect(w.client.of(`GET /v1/orgs/${w.org}/log`)).toHaveLength(1);
    expect(w.world.state.pendingScopes()).toEqual([]);
  });

  it('does nothing without identity or log, or after a sign-out', async () => {
    const { w, a } = await setup();
    await w.world.batch(a.cy!, [{ type: 'remove', subject: a.cy!.id }]);
    await w.sync();
    expect(await rotatePending(jobContext(w), run(w, w.store.generation + 1))).toBe(0);
    expect(await rotatePending(jobContext(w), { ...run(w), org: 'unknown' })).toBe(0);
    setIdentity(w.store, null);
    expect(await rotatePending(jobContext(w), run(w))).toBe(0);
    expect(w.client.of(`POST /v1/orgs/${w.org}/log`)).toHaveLength(0);
  });
});

describe('registrations', () => {
  it('runs catch-up and rotations after the verification jobs, on sign-in, log changes and the interval', () => {
    const names = backgroundJobs().map((job) => job.name);
    expect(names.indexOf('catch-up')).toBeGreaterThan(names.indexOf('wrap-verify'));
    expect(names.indexOf('rotation')).toBeGreaterThan(names.indexOf('catch-up'));
    expect(catchUpJob.triggers).toEqual(['login', 'log', 'interval']);
    expect(rotationJob.triggers).toEqual(['login', 'log', 'interval']);
    expect(INTERVAL_MS).toBe(600_000);
  });

  it('runs through the registrations', async () => {
    const { w, a } = await setup();
    await w.world.batch(a.cy!, [{ type: 'remove', subject: a.cy!.id }]);
    await w.sync();
    await catchUpJob.run(jobContext(w), run(w));
    await rotationJob.run(jobContext(w), run(w));
    expect(w.world.state.pendingScopes()).toEqual([]);
  });
});
