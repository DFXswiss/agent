// Job 1: platform and membership-log verification, incremental refresh, L9 rollback, L10 fork, chain and signature
// refusals (blocking with evidence), founder pinning with the own-identity anchor, and non-blocking hub failures.
import { IDBFactory } from 'fake-indexeddb';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { entryHash } from '../../../src/core/log/entries';
import { checkLh, fetchLog, job, refreshPlatform, verifyOrgLog } from '../../../src/core/jobs/log-verify';
import { HubError } from '../../../src/core/net/signedFetch';
import { orgEntry } from '../../../src/core/store/slices/orgs';
import { setIdentity } from '../../../src/core/store/slices/me';
import { actors, type Actor } from '../log/world';
import { JobHub, signedIn } from './hub';

let alice: Actor, bob: Actor, carol: Actor;
let hub: JobHub;

beforeEach(async () => {
  [alice, bob, carol] = await actors('alice', 'bob', 'carol');
  hub = await JobHub.create(alice);
});

async function rejection(run: () => Promise<unknown>): Promise<string> {
  try {
    await run();
  } catch (error) {
    return (error as { code: string }).code;
  }
  return 'none';
}

describe('log verification job', () => {
  it('verifies platform and org log page by page, stores and pins the head', async () => {
    await hub.world.admitUser(alice, bob);
    const { store, persist, context, changed } = await signedIn(hub, alice);
    await job.run(context, { org: hub.world.org, trigger: 'login', generation: 0 });
    const entry = orgEntry(store.getState().orgs, hub.world.org);
    expect(entry.log!.head).toEqual(hub.world.log!.head);
    expect(entry.blocked).toBeNull();
    expect(store.getState().orgs.platform!.state.root).toBe(hub.platform.root);
    expect(await persist.loadPinned(hub.world.org)).toEqual({
      founder: b64uEncode(alice.identity.sigPublic()),
      head: hub.world.log!.head,
    });
    expect(await persist.loadPlatformHead()).toEqual({ seq: 0, hash: await entryHash(hub.platform.entries[0]) });
    expect(changed).toEqual([hub.world.org]);
    expect(hub.requests.filter((r) => r.includes('/log')).length).toBeGreaterThan(2);
  });

  it('extends the verified log incrementally and keeps it when nothing changed', async () => {
    const { store, context, changed } = await signedIn(hub, alice);
    await verifyOrgLog(context, hub.world.org);
    const first = orgEntry(store.getState().orgs, hub.world.org).log!;
    hub.requests.length = 0;
    await verifyOrgLog(context, hub.world.org);
    expect(orgEntry(store.getState().orgs, hub.world.org).log).toBe(first);
    expect(hub.requests).toEqual(['GET /v1/platform/log?after=0', `GET /v1/orgs/${hub.world.org}/log?after=0`]);
    await hub.world.admitUser(alice, bob);
    await verifyOrgLog(context, hub.world.org);
    expect(orgEntry(store.getState().orgs, hub.world.org).log!.head).toEqual(hub.world.log!.head);
    expect(changed).toEqual([hub.world.org, hub.world.org]);
  });

  it('L9: a log that does not extend the stored head blocks the org (log_rollback) with the evidence', async () => {
    await hub.world.admitUser(alice, bob);
    const factory = new IDBFactory();
    const first = await signedIn(hub, alice, factory);
    await verifyOrgLog(first.context, hub.world.org);
    const stored = hub.world.log!.head;
    hub.serve.org = async (served) => {
      const entries = served.entries.slice(0, 1);
      return { entries, head: { seq: 0, hash: await entryHash(entries[0]) } };
    };
    const reload = await signedIn(hub, alice, factory);
    expect(await rejection(() => verifyOrgLog(reload.context, hub.world.org))).toBe('log_rollback');
    expect(orgEntry(reload.store.getState().orgs, hub.world.org).blocked).toEqual({
      code: 'log_rollback',
      stored,
      served: { seq: 0, hash: await entryHash(hub.world.log!.entries[0]) },
      lh: null,
    });
  });

  it('L9 incrementally: a served head behind or beside the verified one is a rollback', async () => {
    const { store, context } = await signedIn(hub, alice);
    await verifyOrgLog(context, hub.world.org);
    hub.serve.org = (served) => ({ entries: served.entries, head: { seq: 0, hash: 'forged' } });
    expect(await rejection(() => verifyOrgLog(context, hub.world.org))).toBe('log_rollback');
    expect(orgEntry(store.getState().orgs, hub.world.org).blocked!.served).toEqual({ seq: 0, hash: 'forged' });
  });

  it('refuses a tampered entry (log_signature) and a head that does not match the entries (log_chain)', async () => {
    await hub.world.admitUser(alice, bob);
    hub.serve.org = (served) => {
      const entries = [...served.entries];
      const tampered = structuredClone(entries[1]) as { body: { expires: string } };
      tampered.body.expires = '2030-01-01T00:00:00.000Z';
      entries[1] = tampered;
      return { ...served, entries };
    };
    const tampered = await signedIn(hub, alice);
    expect(await rejection(() => verifyOrgLog(tampered.context, hub.world.org))).toBe('log_signature');
    hub.serve.org = (served) => ({ ...served, head: { seq: served.head!.seq, hash: 'x' } });
    const wrongHead = await signedIn(hub, alice);
    expect(await rejection(() => verifyOrgLog(wrongHead.context, hub.world.org))).toBe('log_chain');
    delete hub.serve.org;
    await verifyOrgLog(wrongHead.context, hub.world.org).catch(() => undefined);
    const fresh = await signedIn(hub, alice);
    await verifyOrgLog(fresh.context, hub.world.org);
    await hub.world.admitUser(alice, carol);
    hub.serve.org = (served) => ({ ...served, head: { seq: served.head!.seq, hash: 'x' } });
    expect(await rejection(() => verifyOrgLog(fresh.context, hub.world.org))).toBe('log_chain');
  });

  it('refuses entries served without a head (log_chain)', async () => {
    for (const head of [null, { seq: 99, hash: 'x' }]) {
      hub.serve.org = (served) => ({ ...served, head: head && { ...head, hash: served.head!.hash } });
      const setup = await signedIn(hub, alice);
      expect(await rejection(() => verifyOrgLog(setup.context, hub.world.org))).toBe('log_chain');
      hub.serve.platform = (served) => ({ ...served, head: head && { ...head, hash: served.head!.hash } });
      const platform = await signedIn(hub, alice);
      expect(await rejection(() => refreshPlatform(platform.context))).toBe('log_chain');
      delete hub.serve.platform;
    }
  });

  it('pins the founder from genesis only if the log contains the own identity', async () => {
    const stranger = await signedIn(hub, bob);
    expect(await rejection(() => verifyOrgLog(stranger.context, hub.world.org))).toBe('log_genesis');
    const nobody = await signedIn(hub, alice);
    nobody.store.update('me', (me) => ({ ...me, userId: null }));
    expect(await rejection(() => verifyOrgLog(nobody.context, hub.world.org))).toBe('log_genesis');
    await hub.world.admitUser(alice, bob);
    const member = await signedIn(hub, bob);
    await verifyOrgLog(member.context, hub.world.org);
    expect(orgEntry(member.store.getState().orgs, hub.world.org).blocked).toBeNull();
  });

  it('verifies a reload from seq 0 against the stored pin and head', async () => {
    const factory = new IDBFactory();
    await verifyOrgLog((await signedIn(hub, alice, factory)).context, hub.world.org);
    await hub.world.admitUser(alice, bob);
    const reload = await signedIn(hub, alice, factory);
    await verifyOrgLog(reload.context, hub.world.org);
    expect(orgEntry(reload.store.getState().orgs, hub.world.org).log!.head).toEqual(hub.world.log!.head);
  });

  it('a stored pin overrides genesis (L3: a replaced founder is log_genesis)', async () => {
    const factory = new IDBFactory();
    const setup = await signedIn(hub, alice, factory);
    await setup.persist.savePinned(hub.world.org, { founder: b64uEncode(bob.identity.sigPublic()), head: null });
    expect(await rejection(() => verifyOrgLog(setup.context, hub.world.org))).toBe('log_genesis');
  });

  it('refuses malformed genesis entries and an empty log', async () => {
    const cases: ((entries: unknown[]) => unknown[])[] = [
      () => [],
      (e) => ['x', ...e.slice(1)],
      (e) => [{ ...(e[0] as object), body: 'x' }, ...e.slice(1)],
      (e) => [{ ...(e[0] as object), body: { ...(e[0] as { body: object }).body, pub: 'x' } }],
      (e) => [{ ...(e[0] as object), body: { ...(e[0] as { body: object }).body, pub: { sig: 1 } } }],
    ];
    for (const change of cases) {
      hub.serve.org = (served) => ({ entries: change(served.entries), head: served.head });
      const setup = await signedIn(hub, alice);
      expect(await rejection(() => verifyOrgLog(setup.context, hub.world.org))).not.toBe('none');
      expect(orgEntry(setup.store.getState().orgs, hub.world.org).blocked).not.toBeNull();
    }
  });

  it('hub failures fail the run without blocking; nothing runs while locked', async () => {
    hub.fail.set(`/v1/orgs/${hub.world.org}/log`, new HubError('unavailable', 503));
    const setup = await signedIn(hub, alice);
    expect(await rejection(() => verifyOrgLog(setup.context, hub.world.org))).toBe('unavailable');
    expect(orgEntry(setup.store.getState().orgs, hub.world.org).blocked).toBeNull();
    hub.fail.set(`/v1/orgs/${hub.world.org}/log`, () => ({ entries: 'x' }));
    expect(await rejection(() => fetchLog(setup.context, `/v1/orgs/${hub.world.org}/log`, null))).toBe('unavailable');
    setIdentity(setup.store, null);
    hub.requests.length = 0;
    await verifyOrgLog(setup.context, hub.world.org);
    expect(hub.requests).toEqual([]);
  });

  it('runs without a change listener', async () => {
    const setup = await signedIn(hub, alice);
    const { changed: _changed, ...context } = setup.context;
    await verifyOrgLog(context, hub.world.org);
    expect(orgEntry(setup.store.getState().orgs, hub.world.org).log).not.toBeNull();
  });
});

describe('sign-out and concurrency', () => {
  it('writes nothing when the store was reset during the run', async () => {
    const setup = await signedIn(hub, alice);
    const generation = setup.store.generation;
    setup.store.reset();
    setIdentity(setup.store, alice.identity);
    setup.store.update('me', (me) => ({ ...me, userId: alice.id }));
    await verifyOrgLog(setup.context, hub.world.org, generation);
    expect(setup.store.getState().orgs).toEqual({ byId: {}, platform: null });
    expect(await setup.persist.loadPinned(hub.world.org)).toBeNull();
    expect(await setup.persist.loadPlatformHead()).toBeNull();
    hub.serve.org = (served) => ({ ...served, head: null });
    await expect(verifyOrgLog(setup.context, hub.world.org, generation)).rejects.toThrow('log_chain');
    expect(setup.store.getState().orgs.byId).toEqual({});
  });

  it('does not store a platform log verified for a signed-out store', async () => {
    const setup = await signedIn(hub, alice);
    const stale = setup.store.generation - 1;
    expect((await refreshPlatform(setup.context, stale))!.state.seq).toBe(0);
    expect(setup.store.getState().orgs.platform).toBeNull();
  });

  it('serializes platform refreshes of parallel org queues', async () => {
    const setup = await signedIn(hub, alice);
    hub.fail.set('/v1/platform/log', () => {
      hub.fail.clear();
      throw new HubError('unavailable', 503);
    });
    const [a, b] = [refreshPlatform(setup.context).catch(() => 'failed'), refreshPlatform(setup.context)];
    expect(await a).toBe('failed');
    expect((await b)!.state.seq).toBe(0);
  });

  it('keeps a pin another tab advanced and refuses a pin off the chain', async () => {
    const factory = new IDBFactory();
    const older = await signedIn(hub, alice, factory);
    await verifyOrgLog(older.context, hub.world.org);
    const first = hub.world.log!.head;
    await hub.world.admitUser(alice, bob);
    const newer = await signedIn(hub, alice, factory);
    await verifyOrgLog(newer.context, hub.world.org);
    const pinned = await newer.persist.loadPinned(hub.world.org);
    expect(pinned!.head).toEqual(hub.world.log!.head);
    // the older tab still holds the first head; an incremental run must not lower the pin
    await newer.persist.savePinned(hub.world.org, {
      founder: pinned!.founder,
      head: { seq: hub.world.log!.head.seq + 10, hash: 'x' },
    });
    await hub.world.admitUser(alice, carol);
    await verifyOrgLog(older.context, hub.world.org);
    expect((await older.persist.loadPinned(hub.world.org))!.head!.hash).toBe('x');
    expect(orgEntry(older.store.getState().orgs, hub.world.org).log!.head).toEqual(hub.world.log!.head);
    await newer.persist.savePinned(hub.world.org, {
      founder: pinned!.founder,
      head: { seq: first.seq, hash: 'off-chain' },
    });
    await hub.world.admitUser(alice, (await actors('dave'))[0]);
    expect(await rejection(() => verifyOrgLog(older.context, hub.world.org))).toBe('log_rollback');
  });
});

describe('platform log', () => {
  it('extends incrementally and refuses a rolled-back or inconsistent platform log', async () => {
    const factory = new IDBFactory();
    const setup = await signedIn(hub, alice, factory);
    const first = await refreshPlatform(setup.context);
    expect(await refreshPlatform(setup.context)).toBe(first);
    hub.serve.platform = (served) => ({ ...served, head: { seq: 0, hash: 'x' } });
    expect(await rejection(() => refreshPlatform(setup.context))).toBe('log_rollback');
    const reload = await signedIn(hub, alice, factory);
    expect(await rejection(() => refreshPlatform(reload.context))).toBe('log_chain');
    hub.serve.platform = () => ({ entries: [], head: null });
    expect(await rejection(() => refreshPlatform(reload.context))).toBe('log_rollback');
  });

  it('accepts a hub without a platform log before anything was stored', async () => {
    hub.serve.platform = () => ({ entries: [], head: null });
    const setup = await signedIn(hub, alice);
    expect(await refreshPlatform(setup.context)).toBeNull();
    await verifyOrgLog(setup.context, hub.world.org);
    expect(orgEntry(setup.store.getState().orgs, hub.world.org).log).not.toBeNull();
  });

  it('appends new platform entries to the verified platform log', async () => {
    const { buildPlatformEntries } = await import('../../../src/core/log/entries');
    const { ed25519KeyPair } = await import('../../../src/core/crypto/keys');
    const { seed, EXPIRES, TS, inviteFor } = await import('../log/world');
    const setup = await signedIn(hub, alice);
    const first = (await refreshPlatform(setup.context))!;
    const root = await ed25519KeyPair(await seed('platform root'));
    const invite = await inviteFor('platform user');
    const head = { seq: first.state.seq, hash: first.state.head };
    const more = await buildPlatformEntries(
      head,
      root.privateKey,
      [{ type: 'user_invite', invite_id: invite.inviteId, pk_inv: invite.pkInv, expires: EXPIRES }],
      TS,
    );
    hub.platform.entries.push(...more);
    const next = (await refreshPlatform(setup.context))!;
    expect(next.state.seq).toBe(1);
    expect(await setup.persist.loadPlatformHead()).toEqual({ seq: 1, hash: next.state.head });
  });
});

describe('L10', () => {
  it('blocks the org when an event head stays beyond the refreshed log', async () => {
    const setup = await signedIn(hub, alice);
    expect(checkLh(setup.store, hub.world.org, 99)).toBe(true);
    await verifyOrgLog(setup.context, hub.world.org);
    expect(checkLh(setup.store, hub.world.org, 0)).toBe(true);
    expect(checkLh(setup.store, hub.world.org, 1)).toBe(false);
    expect(orgEntry(setup.store.getState().orgs, hub.world.org).blocked).toEqual({
      code: 'log_fork',
      stored: hub.world.log!.head,
      served: null,
      lh: 1,
    });
  });
});

describe('retry after a hub failure (KOR-05, A-22)', () => {
  it('a network failure at sign-in is retried with backoff until the org is verified, with the other login jobs', async () => {
    const { JobRunner } = await import('../../../src/core/jobs');
    const { job: wraps } = await import('../../../src/core/jobs/wrap-verify');
    const timers: { callback: () => void; ms: number }[] = [];
    const fake = {
      setTimeout: (callback: () => void, ms: number) => timers.push({ callback, ms }),
      clearTimeout: () => undefined,
    };
    hub.fail.set(`/v1/orgs/${hub.world.org}/log`, new HubError('unavailable', 0));
    const setup = await signedIn(hub, alice);
    const runner = new JobRunner(setup.context, [job, wraps], { timers: fake, random: () => 0.5 });
    expect(await runner.trigger('login', hub.world.org)).toBe(false);
    // the org stays "being checked" (no log, not blocked) – until the retry
    expect(orgEntry(setup.store.getState().orgs, hub.world.org)).toMatchObject({ log: null, blocked: null });
    expect(timers.map((timer) => timer.ms)).toEqual([1000]);
    timers.shift()!.callback();
    await vi.waitFor(() => expect(timers.map((timer) => timer.ms)).toEqual([2000]));
    hub.fail.delete(`/v1/orgs/${hub.world.org}/log`);
    timers.shift()!.callback();
    await vi.waitFor(() => expect(orgEntry(setup.store.getState().orgs, hub.world.org).log).not.toBeNull());
    await vi.waitFor(() => expect(orgEntry(setup.store.getState().orgs, hub.world.org).keys.size).toBeGreaterThan(0));
    expect(timers).toEqual([]);
  });
});
