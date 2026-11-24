// The event reader: pending → locked → verified once log and keys are there, unverifiable entries with their code
// (never dropped, never content), L10 refresh and fork, blocked orgs, retry, and entries evicted while being read.
import { beforeEach, describe, expect, it } from 'vitest';
import { utf8 } from '../../../src/core/encoding/bytes';
import { newHeader, sealEvent } from '../../../src/core/crypto/event';
import { JobRunner } from '../../../src/core/jobs';
import { HubError } from '../../../src/core/net/signedFetch';
import { EventReader } from '../../../src/core/store/reader';
import type { Store } from '../../../src/core/store';
import { addKeys, orgEntry, setLog } from '../../../src/core/store/slices/orgs';
import { actors, type Actor, idOf, TS } from '../log/world';
import { JobHub, signedIn } from '../jobs/hub';
import { settle } from '../net/stream';

let alice: Actor, bob: Actor;
let hub: JobHub;
let store: Store;
let runner: JobRunner;
let reader: EventReader;
let task: string;

beforeEach(async () => {
  [alice, bob] = await actors('alice', 'bob');
  hub = await JobHub.create(alice);
  task = await idOf('task');
  const setup = await signedIn(hub, alice);
  store = setup.store;
  let current: EventReader | null = null;
  runner = new JobRunner({ ...setup.context, changed: (org) => current!.retry(org) }, undefined, NO_TIMERS);
  reader = new EventReader(store, runner);
  current = reader;
});

/** Retries of failed log runs are not armed in these tests (the runner tests cover them). */
const NO_TIMERS = { timers: { setTimeout: () => 0, clearTimeout: () => undefined } };

async function prompt(label: string, options: { lh?: number; body?: Uint8Array; author?: Actor } = {}) {
  const author = options.author ?? alice;
  const fields = newHeader({
    id: await idOf(`event ${label}`),
    org: hub.world.org,
    scope: 'wg',
    scope_id: hub.world.defaultWg,
    wg: hub.world.defaultWg,
    epoch: 1,
    author: author.id,
    kind: 'prompt',
    task,
    ts: TS,
    lh: options.lh ?? hub.world.log!.head.seq,
  });
  const key = hub.world.ring(alice.id)('wg', hub.world.defaultWg, 1)!;
  return sealEvent(key, fields, options.body ?? utf8(`{"text":"${label}"}`), author.identity.sig.privateKey);
}

const stored = (seq: number, event: unknown) => ({ scope_seq: seq, received_at: '2026-11-21T12:00:00.000Z', event });
const entries = () =>
  [...(store.getState().events[hub.world.defaultWg]?.entries.values() ?? [])].sort((a, b) => a.scopeSeq - b.scopeSeq);
const summary = () => entries().map((e) => [e.scopeSeq, e.status, e.code]);

describe('event reader', () => {
  it('refuses an event delivered under another scope and an ID repeated at another seq', async () => {
    await runner.trigger('login', hub.world.org);
    const good = await prompt('moved');
    await reader.ingest(hub.world.org, hub.world.org, [stored(1, good)]);
    await reader.ingest(await idOf('other org'), hub.world.defaultWg, [stored(1, good)]);
    expect(summary()).toEqual([[1, 'unverifiable', 'header_invalid']]);
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(2, good), stored(3, good), stored(2, good)]);
    expect(summary()).toEqual([
      [1, 'unverifiable', 'header_invalid'],
      [2, 'verified', null],
      [3, 'unverifiable', 'event_conflict'],
    ]);
    const org = [...(store.getState().events[hub.world.org]?.entries.values() ?? [])];
    expect(org.map((e) => [e.status, e.code])).toEqual([['unverifiable', 'header_invalid']]);
  });

  it('does not block the org when the log refresh fails; the event stays pending', async () => {
    await runner.trigger('login', hub.world.org);
    await hub.world.admitUser(alice, bob);
    hub.fail.set(`/v1/orgs/${hub.world.org}/log`, new HubError('rate_limited', 429));
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('later'))]);
    expect(summary()).toEqual([[1, 'pending', null]]);
    expect(orgEntry(store.getState().orgs, hub.world.org).blocked).toBeNull();
    hub.fail.clear();
    await reader.retry(hub.world.org);
    expect(summary()).toEqual([[1, 'verified', null]]);
    // the decrypted body's bytes count towards the store's byte bound
    expect(entries()[0]!.bodyBytes).toBeGreaterThan(0);
  });

  it('does not wait for itself when the log advances but stays below lh (fork after a real refresh)', async () => {
    await runner.trigger('login', hub.world.org);
    const lh = hub.world.log!.head.seq + 3;
    await hub.world.admitUser(alice, bob);
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('ahead', { lh }))]);
    expect(summary()).toEqual([[1, 'unverifiable', 'log_fork']]);
    await settle();
    expect(store.getState().jobs[hub.world.org]).toMatchObject({ running: null, waiting: 0 });
  });

  it('writes nothing after a sign-out that happened while it read', async () => {
    const signingOut = new EventReader(store, {
      trigger: async (trigger, org) => {
        const ok = await runner.trigger(trigger, org);
        store.reset();
        return ok;
      },
    });
    await signingOut.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('bye'))]);
    expect(store.getState().events).toEqual({});
    expect(store.getState().orgs.byId).toEqual({});
  });

  it('verifies the log on demand, locks without key and opens the event once the wrap job brought the key', async () => {
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('hello'))]);
    expect(summary()).toEqual([[1, 'locked', null]]);
    expect(entries()[0]!.header!.kind).toBe('prompt');
    expect(entries()[0]!.body).toBeNull();
    await runner.trigger('wraps', hub.world.org);
    await settle();
    expect(summary()).toEqual([[1, 'verified', null]]);
    expect(entries()[0]!.body).toEqual({ text: 'hello' });
    expect(entries()[0]!.receivedAt).toBe('2026-11-21T12:00:00.000Z');
  });

  it('marks forged and malformed events unverifiable with their code', async () => {
    await runner.trigger('login', hub.world.org);
    const good = await prompt('good');
    const forged = { ...good, sig: (await prompt('other')).sig };
    const noBody = await prompt('nobody', { body: utf8('not json') });
    const stranger = await prompt('stranger', { author: bob });
    await reader.ingest(hub.world.org, hub.world.defaultWg, [
      stored(1, good),
      stored(2, forged),
      stored(3, { header: {} }),
      stored(4, 'x'),
      stored(5, noBody),
      stored(6, stranger),
      stored(7, { ...good, header: { ...good.header, org: 'x' } }),
    ]);
    expect(summary()).toEqual([
      [1, 'verified', null],
      [2, 'unverifiable', 'signature_invalid'],
      [3, 'unverifiable', 'header_invalid'],
      [4, 'unverifiable', 'header_invalid'],
      [5, 'unverifiable', 'header_invalid'],
      [6, 'unverifiable', 'author_not_writer'],
      [7, 'unverifiable', 'header_invalid'],
    ]);
  });

  it('refreshes the log for an lh beyond the head (L10) and blocks the org on a fork', async () => {
    await runner.trigger('login', hub.world.org);
    await hub.world.admitUser(alice, bob);
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('after admit'))]);
    expect(summary()).toEqual([[1, 'verified', null]]);
    expect(orgEntry(store.getState().orgs, hub.world.org).log!.head).toEqual(hub.world.log!.head);
    const beyond = await prompt('beyond', { lh: hub.world.log!.head.seq + 5 });
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(2, beyond)]);
    expect(summary()[1]).toEqual([2, 'unverifiable', 'log_fork']);
    expect(orgEntry(store.getState().orgs, hub.world.org).blocked).toMatchObject({
      code: 'log_fork',
      lh: hub.world.log!.head.seq + 5,
    });
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(3, await prompt('blocked'))]);
    expect(summary()[2]).toEqual([3, 'unverifiable', 'log_fork']);
  });

  it('keeps events pending while the log cannot be read and reads them on retry', async () => {
    hub.fail.set(`/v1/orgs/${hub.world.org}/log`, new HubError('unavailable', 503));
    await reader.ingest(hub.world.org, hub.world.defaultWg, [
      stored(2, await prompt('wait')),
      stored(1, await prompt('first')),
    ]);
    expect(summary()).toEqual([
      [1, 'pending', null],
      [2, 'pending', null],
    ]);
    hub.fail.clear();
    await reader.retry(await idOf('other org'));
    expect(summary().map(([, status]) => status)).toEqual(['pending', 'pending']);
    await reader.retry(hub.world.org);
    expect(summary()).toEqual([
      [1, 'locked', null],
      [2, 'locked', null],
    ]);
  });

  it('leaves an entry evicted while it was read evicted', async () => {
    const evicting = new EventReader(store, {
      trigger: async (trigger, org) => {
        const ok = await runner.trigger(trigger, org);
        store.update('events', () => ({}));
        return ok;
      },
    });
    await evicting.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('gone'))]);
    expect(store.getState().events).toEqual({});
  });

  it('keeps the other entries of a scope when one was evicted while it was read', async () => {
    const scope = hub.world.defaultWg;
    const partly = new EventReader(store, {
      trigger: async (trigger, org) => {
        const ok = await runner.trigger(trigger, org);
        store.update('events', (events) => ({
          [scope]: { entries: new Map([...events[scope]!.entries].filter(([seq]) => seq !== 1)) },
        }));
        return ok;
      },
    });
    await partly.ingest(hub.world.org, scope, [stored(1, await prompt('one')), stored(2, await prompt('two'))]);
    expect(summary().map(([seq]) => seq)).toEqual([2]);
  });

  it('reads an event that arrives before the platform log was verified once it is (new member; KOR-05, A-10)', async () => {
    // a member who just accepted holds the org's log and keys (installOrg) before its first log job verified the
    // platform log; the org has a platform device, so the reader cannot yet tell whether an author is excluded
    const [pdev] = await actors('pdev');
    hub = await JobHub.create(alice, [pdev!]);
    await hub.world.batch(alice, [
      { type: 'device_assign', device: pdev!.id, wg: hub.world.defaultWg, platform_seq: hub.seqs.get(pdev!.id)! },
    ]);
    const setup = await signedIn(hub, alice);
    store = setup.store;
    runner = new JobRunner(setup.context, undefined, NO_TIMERS);
    reader = new EventReader(store, runner);
    setLog(store, hub.world.org, hub.world.log!);
    addKeys(store, hub.world.org, hub.world.held.get(alice.id)!);
    expect(store.getState().orgs.platform).toBeNull();
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('early'))]);
    expect(summary()).toEqual([[1, 'verified', null]]);
    expect(store.getState().orgs.platform).not.toBeNull();
  });

  it('keeps such an event pending while the platform log cannot be read, and reads it once it is', async () => {
    // as after installOrg: the org's log and every key are there, only the platform log is missing; when the log job
    // later verifies the platform log (the org log unchanged), the pending entry is read again
    const [pdev] = await actors('pdev');
    hub = await JobHub.create(alice, [pdev!]);
    await hub.world.batch(alice, [
      { type: 'device_assign', device: pdev!.id, wg: hub.world.defaultWg, platform_seq: hub.seqs.get(pdev!.id)! },
    ]);
    const setup = await signedIn(hub, alice);
    store = setup.store;
    let current: EventReader | null = null;
    runner = new JobRunner({ ...setup.context, changed: (org) => current!.retry(org) }, undefined, NO_TIMERS);
    reader = new EventReader(store, runner);
    current = reader;
    setLog(store, hub.world.org, hub.world.log!);
    addKeys(store, hub.world.org, hub.world.held.get(alice.id)!);
    hub.fail.set('/v1/platform/log', new HubError('unavailable', 0));
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('early'))]);
    expect(summary()).toEqual([[1, 'pending', null]]);
    expect(orgEntry(store.getState().orgs, hub.world.org).blocked).toBeNull();
    hub.fail.clear();
    // the retry of the log job (trigger `lh`, the org log unchanged) verifies the platform log
    await runner.trigger('lh', hub.world.org);
    await settle();
    expect(summary()).toEqual([[1, 'verified', null]]);
  });

  it('runs the log job once, not per event, when the hub has no platform log for an org with platform devices', async () => {
    const [pdev] = await actors('pdev');
    hub = await JobHub.create(alice, [pdev!]);
    await hub.world.batch(alice, [
      { type: 'device_assign', device: pdev!.id, wg: hub.world.defaultWg, platform_seq: hub.seqs.get(pdev!.id)! },
    ]);
    const setup = await signedIn(hub, alice);
    store = setup.store;
    runner = new JobRunner(setup.context, undefined, NO_TIMERS);
    reader = new EventReader(store, runner);
    setLog(store, hub.world.org, hub.world.log!);
    addKeys(store, hub.world.org, hub.world.held.get(alice.id)!);
    hub.serve.platform = () => ({ entries: [], head: null });
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('one'))]);
    const runs = hub.requests.filter((r) => r.startsWith('GET /v1/platform/log')).length;
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(2, await prompt('two'))]);
    expect(hub.requests.filter((r) => r.startsWith('GET /v1/platform/log')).length).toBe(runs);
    expect(summary()).toEqual([
      [1, 'unverifiable', 'log_rights'],
      [2, 'unverifiable', 'log_rights'],
    ]);
  });

  it('reads against an org without platform devices when the hub has no platform log', async () => {
    hub.serve.platform = () => ({ entries: [], head: null });
    await reader.ingest(hub.world.org, hub.world.defaultWg, [stored(1, await prompt('no platform'))]);
    expect(store.getState().orgs.platform).toBeNull();
    expect(summary()).toEqual([[1, 'locked', null]]);
  });
});
