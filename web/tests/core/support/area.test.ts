// The support area of a DFX support person (ADR-096 section 4, KOR-56): the releases, the verification of a foreign
// organization's log against the platform log, the person's keys from its own wraps (W2 with the roster), the reader
// of the workgroup's events (support authors, locked and refused events), the newest `support.info`, the access and a
// prompt signed by the person.
import { describe, expect, it } from 'vitest';
import { formatTime } from '../../../src/core/encoding/time';
import { HubError } from '../../../src/core/net/signedFetch';
import { WG } from '../../../src/core/log/state';
import {
  accessOf,
  loadReleases,
  openOrg,
  POLL_MS,
  pullSupport,
  readEntries,
  selectInfo,
  sendSupportPrompt,
} from '../../../src/core/support/area';
import { INFO_KIND } from '../../../src/core/support/release';
import { revokeRelease } from '../../../src/core/support/release';
import { setOpened } from '../../../src/core/store/slices/support';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { idOf, T0 } from '../log/world';
import { supportWorld, TASK } from './world';

const RELEASE = { org_id: 'o', wg_id: 'w', access: 'read', granted_seq: 3, granted_at: formatTime(T0) };

describe('releases', () => {
  it('loads the releases, knows a user the hub does not know as support, and records failures', async () => {
    const s = await supportWorld();
    s.client.on('GET /v1/support/releases', () => ({ releases: [RELEASE] }));
    const loading = loadReleases(s.context);
    expect(s.store.getState().support.area.status).toBe('loading');
    await loading;
    expect(s.store.getState().support.area).toEqual({ status: 'ready', code: null, releases: [RELEASE] });
    s.client.on('GET /v1/support/releases', () => ({ releases: [{ ...RELEASE, access: 'admin' }] }));
    await loadReleases(s.context);
    expect(s.store.getState().support.area).toMatchObject({
      status: 'error',
      code: 'unavailable',
      releases: [RELEASE],
    });
    s.client.on('GET /v1/support/releases', () => {
      throw new HubError('not_found', 404);
    });
    await loadReleases(s.context);
    expect(s.store.getState().support.area.status).toBe('none');
    s.client.on('GET /v1/support/releases', () => ({ nothing: true }));
    await loadReleases(s.context);
    expect(s.store.getState().support.area.status).toBe('error');
  });

  it('writes nothing after a sign-out', async () => {
    const s = await supportWorld();
    s.client.on('GET /v1/support/releases', () => {
      s.store.reset();
      return { releases: [RELEASE] };
    });
    await loadReleases(s.context);
    expect(s.store.getState().support.area.status).toBe('idle');
    s.client.on('GET /v1/support/releases', () => {
      s.store.reset();
      throw new HubError('unavailable', 503);
    });
    await loadReleases(s.context);
    expect(s.store.getState().support.area.status).toBe('idle');
    expect(POLL_MS).toBeGreaterThan(1000);
  });
});

describe('opening an organization', () => {
  it('verifies the log, opens the released epochs and extends the log later', async () => {
    const s = await supportWorld();
    const opened = await openOrg(s.context, s.w.org);
    expect(opened.log.head).toEqual(s.w.world.log!.head);
    expect([...opened.keys.keys()]).toEqual([`${WG} ${s.one} 1`]);
    expect(accessOf(s.store.getState(), s.w.org, s.one)).toBe('read');
    expect(accessOf(s.store.getState(), s.w.org, s.w.world.defaultWg)).toBeNull();
    // nothing new: the same log; a new entry: the log is extended and the keys kept
    expect((await openOrg(s.context, s.w.org)).log).toBe(opened.log);
    await s.w.hub.foreign(s.a.ada, { type: 'wg_create', wg: await idOf('another wg'), epoch: 1 });
    const extended = await openOrg(s.context, s.w.org);
    expect(extended.log.head.seq).toBe(opened.log.head.seq + 1);
    expect(extended.keys.size).toBe(1);
  });

  it('refuses without platform log, signed out, a head the hub misstates and another org', async () => {
    const s = await supportWorld({ platform: false });
    await expect(openOrg(s.context, s.w.org)).rejects.toMatchObject({ code: 'unavailable' });
    const t = await supportWorld();
    t.client.on(`GET /v1/orgs/${t.w.org}/log`, () => ({
      entries: t.w.world.log!.entries.map((entry, seq) => ({ seq, entry })),
      head: { seq: 0, hash: 'x' },
      next_after: null,
    }));
    await expect(openOrg(t.context, t.w.org)).rejects.toMatchObject({ code: 'log_chain' });
    t.client.on(`GET /v1/orgs/other/log`, () => ({
      entries: t.w.world.log!.entries.map((entry, seq) => ({ seq, entry })),
      head: t.w.world.log!.head,
      next_after: null,
    }));
    await expect(openOrg(t.context, 'other')).rejects.toMatchObject({ code: 'log_genesis' });
    for (const entries of [[], [{ seq: 0, entry: { body: { pub: 'x' } } }], [{ seq: 0, entry: { x: 1 } }]]) {
      t.client.on(`GET /v1/orgs/${t.w.org}/log`, () => ({ entries, head: null, next_after: null }));
      await expect(openOrg(t.context, t.w.org)).rejects.toBeDefined();
    }
    t.store.reset();
    await expect(openOrg(t.context, t.w.org)).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('uses no wrap the logs do not allow and keeps nothing after a sign-out', async () => {
    const s = await supportWorld();
    const [real] = s.w.hub.stored.filter((wrap) => wrap.recipient_id === s.a.sam.id);
    s.client.on('GET /v1/wraps', () => ({
      wraps: [
        { ...real!, wrapped_by: s.a.tom.id },
        { ...real!, recipient_id: s.a.tom.id },
        { ...real!, scope_id: s.w.world.defaultWg },
        { ...real!, scope_id: 'unknown' },
        { scope: 'org', scope_id: s.w.org },
        real,
        real,
      ],
    }));
    const opened = await openOrg(s.context, s.w.org);
    expect(opened.keys.size).toBe(1);
    s.client.on('GET /v1/wraps', () => ({ nothing: true }));
    const generation = s.store.generation;
    s.client.on(`GET /v1/orgs/${s.w.org}/log`, () => {
      s.store.reset();
      return { entries: [], head: s.w.world.log!.head, next_after: null };
    });
    await openOrg(s.context, s.w.org);
    expect(s.store.generation).toBe(generation + 1);
    expect(s.store.getState().support.opened).toEqual({});
  });
});

describe('reading a released workgroup', () => {
  it('reads members, support persons, locked and refused events and the newest support.info', async () => {
    const s = await supportWorld({ access: 'write' });
    await s.write(s.a.bob, 'prompt', { text: 'Hello', attachments: [] });
    await s.write(s.a.bob, 'prompt', { text: 'Elsewhere' }, { scope_id: s.w.world.defaultWg, wg: s.w.world.defaultWg });
    expect(await pullSupport(s.context, s.w.org, s.one, { kinds: ['prompt', INFO_KIND] })).toBe(3);
    const entries = [...s.store.getState().events[s.one]!.entries.values()];
    expect(entries.map((entry) => entry.status)).toEqual(['verified', 'verified', 'unverifiable']);
    expect(entries[2]!.code).toBe('header_invalid');
    const info = selectInfo(s.one)(s.store.getState())!;
    expect(info).toMatchObject({ orgName: 'DFX AG', wgName: 'Team', author: s.a.ada.id });
    expect(info.members.get(s.a.bob.id)).toBe('Bob');
    expect(selectInfo(s.one)(s.store.getState())).toBe(selectInfo(s.one)(s.store.getState()));
    expect(selectInfo('none')(s.store.getState())).toBeNull();
    // the person's own prompt, signed by its identity
    const seq = await sendSupportPrompt(s.context, s.w.org, s.one, TASK, 'Support here');
    const own = s.store.getState().events[s.one]!.entries.get(seq)!;
    expect(own).toMatchObject({ status: 'verified', body: { text: 'Support here', attachments: [] } });
    expect(own.header!.author).toBe(s.a.sam.id);
    await expect(sendSupportPrompt(s.context, s.w.org, s.one, TASK, '  ')).rejects.toMatchObject({
      code: 'request_invalid',
    });
    await expect(sendSupportPrompt(s.context, s.w.org, s.one, TASK, 'x'.repeat(70000))).rejects.toMatchObject({
      code: 'size_exceeded',
    });
  });

  it('re-reads the log for an event beyond its head and locks events of epochs it does not hold', async () => {
    const s = await supportWorld();
    await openOrg(s.context, s.w.org);
    // the workgroup rotates (the new epoch reaches support by catch-up only), then bob writes at the new head
    await s.w.world.batch(s.a.ada, [{ type: 'rotate', scope: WG, scope_id: s.one, epoch: 2 }]);
    await s.write(s.a.bob, 'prompt', { text: 'New epoch', attachments: [] });
    await pullSupport(s.context, s.w.org, s.one, { kinds: ['prompt'], task: TASK });
    const [entry] = [...s.store.getState().events[s.one]!.entries.values()];
    expect(entry!.status).toBe('locked');
    expect(s.store.getState().support.opened[s.w.org]!.log.head).toEqual(s.w.world.log!.head);
  });

  it('refuses a body that is no JSON, a broken event and one without header, and forgets a pull after sign-out', async () => {
    const s = await supportWorld();
    await s.write(s.a.bob, 'prompt', new TextEncoder().encode('not json'));
    s.put({} as never);
    await pullSupport(s.context, s.w.org, s.one, { kinds: ['prompt'] });
    const entries = [...s.store.getState().events[s.one]!.entries.values()];
    expect(entries.map((entry) => entry.code)).toEqual(['header_invalid', 'header_invalid']);
    const opened = s.store.getState().support.opened[s.w.org]!;
    const platform = s.store.getState().orgs.platform!.state;
    const [first] = s.events;
    const broken = { ...first!, event: { ...first!.event, sig: 'AAAA' } };
    const read = await readEntries(opened, platform, s.w.org, s.one, [broken, { ...first!, event: null as never }]);
    expect(read.map((entry) => entry.status)).toEqual(['unverifiable', 'unverifiable']);
    // an event beyond the verified head: the log is read again, and a sign-out meanwhile keeps everything out
    await s.w.hub.foreign(s.a.ada, { type: 'wg_create', wg: await idOf('later wg'), epoch: 1 });
    await s.write(s.a.bob, 'prompt', { text: 'Later', attachments: [] });
    const log = s.client.routes.get(`GET /v1/orgs/${s.w.org}/log`)!;
    s.client.on(`GET /v1/orgs/${s.w.org}/log`, (body, target) => {
      s.store.reset();
      return log(body, target);
    });
    await pullSupport(s.context, s.w.org, s.one, { kinds: ['prompt'] });
    expect(s.store.getState().events[s.one]).toBeUndefined();
  });

  it('takes the newest valid support.info', async () => {
    const s = await supportWorld();
    const info = (seq: number, body: unknown, status: EventEntry['status'] = 'verified'): EventEntry => ({
      org: s.w.org,
      scopeId: s.one,
      scopeSeq: seq,
      receivedAt: null,
      event: {},
      status,
      code: null,
      header: { kind: INFO_KIND, author: s.a.ada.id },
      body,
    });
    const valid = (name: string) => ({ org_name: name, wg_name: 'W', members: { [s.a.bob.id]: 'Bob', x: '' } });
    putEvents(s.store, s.one, [
      info(50, valid('Newest')),
      info(40, valid('Older')),
      info(60, valid('Not verified'), 'locked'),
      { ...info(61, valid('Other kind')), header: { kind: 'prompt', author: s.a.ada.id } },
      info(62, 'text'),
      info(63, { ...valid('x'), org_name: '' }),
      info(64, { ...valid('x'), org_name: 'o'.repeat(201) }),
      info(65, { ...valid('x'), wg_name: 7 }),
      info(66, { ...valid('x'), members: [] }),
    ]);
    const found = selectInfo(s.one)(s.store.getState())!;
    expect(found.orgName).toBe('Newest');
    expect([...found.members]).toEqual([[s.a.bob.id, 'Bob']]);
  });

  it('sends nothing without a write release, a key or a person', async () => {
    const s = await supportWorld();
    await expect(sendSupportPrompt(s.context, s.w.org, s.one, TASK, 'Hi')).rejects.toMatchObject({ code: 'forbidden' });
    const w = await supportWorld({ access: 'write' });
    const opened = await openOrg(w.context, w.w.org);
    w.client.on('GET /v1/wraps', () => ({ wraps: [] }));
    setOpened(w.store, w.w.org, { log: opened.log, keys: new Map() });
    await expect(sendSupportPrompt(w.context, w.w.org, w.one, TASK, 'Hi')).rejects.toMatchObject({
      code: 'epoch_stale',
    });
    w.store.reset();
    await expect(sendSupportPrompt(w.context, w.w.org, w.one, TASK, 'Hi')).rejects.toMatchObject({
      code: 'unauthorized',
    });
  });

  it('refuses an answer without a position', async () => {
    const s = await supportWorld({ access: 'write' });
    s.client.on(`POST /v1/tasks/${TASK}/prompts`, () => ({}));
    await expect(sendSupportPrompt(s.context, s.w.org, s.one, TASK, 'Hi')).rejects.toMatchObject({
      code: 'unavailable',
    });
  });

  it('loses the access with the revocation', async () => {
    const s = await supportWorld({ access: 'write' });
    await openOrg(s.context, s.w.org);
    expect(accessOf(s.store.getState(), s.w.org, s.one)).toBe('write');
    await revokeRelease(s.w.context, s.w.org, s.one);
    await openOrg(s.context, s.w.org);
    expect(accessOf(s.store.getState(), s.w.org, s.one)).toBeNull();
  });

  it('knows no access for a former person or while signed out', async () => {
    const s = await supportWorld({ former: true });
    await openOrg(s.context, s.w.org);
    expect(accessOf(s.store.getState(), s.w.org, s.one)).toBeNull();
    const t = await supportWorld();
    await openOrg(t.context, t.w.org);
    const state = t.store.getState();
    expect(accessOf({ ...state, me: { ...state.me, userId: null } }, t.w.org, t.one)).toBeNull();
    expect(accessOf({ ...state, orgs: { ...state.orgs, platform: null } }, t.w.org, t.one)).toBeNull();
  });
});
