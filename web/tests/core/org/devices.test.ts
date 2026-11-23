// U-WEB-ui-devices (core part): rows of the device list (verified log + the hub's presence and capacity), labels from
// `device.profile`, rights and platform access (ADR-026), platform candidates checked against the platform log, the
// batches of assign (WK/OK by the batch, CK afterwards), unassign and unpair with their rotation, and rename.
import { describe, expect, it } from 'vitest';
import { openEvent } from '../../../src/core/crypto/event';
import { CERTIFIED, type PlatformState, VerifiedPlatformLog } from '../../../src/core/log/platform';
import { ORG, WG } from '../../../src/core/log/state';
import { actionErrorCode, keyRing, submitBatch } from '../../../src/core/org/batch';
import {
  assignableWorkgroups,
  assignDevice,
  assignPlan,
  canManageDevices,
  capacityUse,
  type Capacity,
  deviceLabel,
  deviceRows,
  enrollCommand,
  hasPlatformAccess,
  type HubDevice,
  LABEL_MAX,
  loadCandidates,
  loadOrgDevices,
  renameDevice,
  selectDeviceLabels,
  unassignPlan,
  unpairPlan,
} from '../../../src/core/org/devices';
import { createStore } from '../../../src/core/store';
import { putEvents } from '../../../src/core/store/slices/events';
import { orgEntry, setPlatform } from '../../../src/core/store/slices/orgs';
import type { Store } from '../../../src/core/store/store';
import type { Actor } from '../log/world';
import { idOf } from '../log/world';
import { holds, orgWorld, type OrgWorld } from './world';

const CAPACITY: Capacity = {
  vm_memory_budget_mib: 8192,
  vm_memory_used_mib: 0,
  vm_slots_total: 8,
  vm_slots_free: 6,
  pool_ready: 1,
};

const hubRow = (id: string, extra: Partial<HubDevice> = {}): HubDevice => ({
  device_id: id,
  kind: 'org',
  state: 'active',
  online: true,
  last_seen_at: '2026-11-21T12:00:00.000Z',
  agent_version: '0.1.0',
  capacity: CAPACITY,
  capacity_low: false,
  assigned_wgs: [],
  ...extra,
});

/** ada (admin) with workgroup `one` (+ archived `old`), bob (member), org devices dev1 (one, default) and dev2
 *  (default), the platform device pdev certified (seq 2) and – in private orgs – assigned to the default workgroup. */
async function setup(options: { as?: 'ada' | 'bob'; private?: boolean; noPlatform?: boolean } = {}) {
  const one = await idOf('wg one');
  const old = await idOf('wg old');
  const w = await orgWorld({
    ...options,
    platformDevices: ['pdev'],
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_create', wg: old, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_archive', wg: old }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.admitDevice(a.ada, a.dev1, [world.defaultWg, one]);
      await world.admitDevice(a.ada, a.dev2, [world.defaultWg]);
      if (options.private) {
        await world.batch(a.ada, [{ type: 'device_assign', device: a.pdev.id, wg: world.defaultWg, platform_seq: 2 }]);
      }
    },
  });
  w.hub.platformAccess = options.private ?? false;
  return { w, a: w.others as Record<string, Actor>, one, old };
}

const candidates = (w: OrgWorld, wg: string, rows: unknown[]) =>
  w.client.on(`GET /v1/workgroups/${wg}/device-candidates`, () => ({ candidates: rows }));

const pdevCandidate = (a: Record<string, Actor>, extra: object = {}) => ({
  device_id: a.pdev!.id,
  platform_seq: 2,
  pub: a.pdev!.identity.public(),
  online: true,
  capacity: CAPACITY,
  capacity_low: false,
  ...extra,
});

/** Replaces the browser's verified platform state (as if the platform log said so). */
function believe(store: Store, state: PlatformState): void {
  const current = store.getState().orgs.platform!;
  setPlatform(store, new VerifiedPlatformLog(current.entries, [...current.states, state]));
}

const decertified = (state: PlatformState) =>
  state.with({
    certifications: new Map([...state.certifications].map(([seq, c]) => [seq, { ...c, decertifiedSeq: 9 }])),
  });

async function refusal(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return actionErrorCode(error);
  }
  throw new Error('not refused');
}

function refusalOf(run: () => unknown): string {
  try {
    run();
  } catch (error) {
    return actionErrorCode(error);
  }
  throw new Error('not refused');
}

describe('labels, the command and capacity', () => {
  it('accepts single-line labels of 1 to 100 characters after trimming', () => {
    expect(deviceLabel('  Mac Studio  ')).toBe('Mac Studio');
    expect(deviceLabel('   ')).toBeNull();
    expect(deviceLabel('x'.repeat(LABEL_MAX))).toBe('x'.repeat(LABEL_MAX));
    expect(deviceLabel('😀'.repeat(LABEL_MAX))).not.toBeNull();
    expect(deviceLabel('x'.repeat(LABEL_MAX + 1))).toBeNull();
    expect(deviceLabel('a\nb')).toBeNull();
    expect(deviceLabel('a\rb')).toBeNull();
  });

  it('builds the enrollment command', () => {
    expect(enrollCommand('https://hub.example', 'AAAAA-BBBBB')).toBe(
      'agent-runner enroll --hub https://hub.example --code AAAAA-BBBBB',
    );
  });

  it('computes the used share of the VM slots', () => {
    expect(capacityUse(CAPACITY)).toEqual({ used: 2, total: 8, share: 0.25 });
    expect(capacityUse({ ...CAPACITY, vm_slots_total: 0, vm_slots_free: 0 })).toEqual({ used: 0, total: 0, share: 0 });
    expect(capacityUse({ ...CAPACITY, vm_slots_free: 12 })).toEqual({ used: 0, total: 8, share: 0 });
  });
});

describe('the hub list', () => {
  it('reads valid lists and refuses malformed ones', async () => {
    const { w } = await setup();
    const rows = [hubRow('d1'), hubRow('d2', { kind: 'platform', last_seen_at: null })];
    w.client.on(`GET /v1/orgs/${w.org}/devices`, () => ({ devices: rows }));
    expect(await loadOrgDevices(w.context, w.org)).toEqual(rows);
    const bad: unknown[] = [
      null,
      { devices: 'x' },
      { devices: [{ ...hubRow('d'), kind: 'other' }] },
      { devices: [{ ...hubRow('d'), capacity: { ...CAPACITY, pool_ready: -1 } }] },
      { devices: [{ ...hubRow('d'), capacity: null }] },
      { devices: [{ ...hubRow('d'), assigned_wgs: [1] }] },
      { devices: [{ ...hubRow('d'), assigned_wgs: 'x' }] },
      { devices: [{ ...hubRow('d'), last_seen_at: 1 }] },
      { devices: [{ ...hubRow('d'), device_id: 1 }] },
      { devices: [{ ...hubRow('d'), state: 1 }] },
      { devices: [{ ...hubRow('d'), online: 'yes' }] },
      { devices: [{ ...hubRow('d'), agent_version: null }] },
      { devices: [{ ...hubRow('d'), capacity_low: null }] },
    ];
    for (const body of bad) {
      w.client.on(`GET /v1/orgs/${w.org}/devices`, () => body);
      expect(await refusal(loadOrgDevices(w.context, w.org))).toBe('unavailable');
    }
  });
});

describe('device rows', () => {
  it('lists the active devices of the log newest first with labels, kinds, workgroups and the hub view', async () => {
    const { w, a, one } = await setup({ private: true });
    const platform = w.hub.platform!.state;
    const rows = deviceRows(
      w.world.state,
      platform,
      [hubRow(a.dev1!.id, { capacity_low: true }), hubRow('not-in-the-log')],
      new Map([[a.dev2!.id, 'Studio']]),
    );
    expect(rows.map((row) => row.id)).toEqual([a.pdev!.id, a.dev2!.id, a.dev1!.id]);
    expect(rows[0]).toMatchObject({ kind: 'platform', platformSeq: 2, decertified: false, hub: null, label: null });
    expect(rows[0]!.workgroups).toEqual([w.world.defaultWg]);
    expect(rows[1]).toMatchObject({ kind: 'org', platformSeq: null, label: 'Studio', hub: null });
    expect(rows[2]!.workgroups).toEqual([w.world.defaultWg, one]);
    expect(rows[2]!.hub?.capacity_low).toBe(true);
    expect(platform.status(a.pdev!.id)).toBe(CERTIFIED);
  });

  it('marks decertified platform devices, orders equal joins by ID and skips removed devices', async () => {
    const { w, a } = await setup({ private: true });
    const platform = w.hub.platform!.state;
    expect(deviceRows(w.world.state, decertified(platform), [], new Map())[0]).toMatchObject({
      id: a.pdev!.id,
      decertified: true,
    });
    // without a platform state nothing is excluded
    expect(deviceRows(w.world.state, null, [], new Map())[0]!.decertified).toBe(false);
    await w.world.batch(w.me, [{ type: 'remove', subject: a.dev2!.id }]);
    const ids = deviceRows(w.world.state, platform, [], new Map()).map((row) => row.id);
    expect(ids).not.toContain(a.dev2!.id);
    // the same join seq (not possible in a real log): by ID
    const state = w.world.state;
    const principals = new Map(state.principals);
    for (const id of [a.dev1!.id, a.pdev!.id]) principals.set(id, { ...principals.get(id)!, joinedSeq: 1 });
    const same = deviceRows(state.with({ principals }), platform, [], new Map()).map((row) => row.id);
    expect(same).toEqual([a.dev1!.id, a.pdev!.id].sort());
  });

  it('reads labels from verified device.profile events, the newest winning', () => {
    const store = createStore();
    expect(selectDeviceLabels('org')(store.getState()).size).toBe(0);
    const entry = (seq: number, status: 'verified' | 'unverifiable', kind: string, ref: string, body: unknown) => ({
      org: 'org',
      scopeId: 'org',
      scopeSeq: seq,
      receivedAt: null,
      event: {},
      status,
      code: status === 'verified' ? null : 'event_signature',
      header: status === 'verified' ? { kind, ref, id: `e${seq}` } : null,
      body: status === 'verified' ? body : null,
    });
    putEvents(store, 'org', [
      entry(3, 'verified', 'device.profile', 'd1', { label: 'Newer' }),
      entry(1, 'verified', 'device.profile', 'd1', { label: 'Older' }),
      entry(2, 'verified', 'device.profile', 'd2', { other: 1 }),
      entry(4, 'unverifiable', 'device.profile', 'd2', null),
      entry(5, 'verified', 'wg.profile', 'd3', { label: 'not a device' }),
      entry(6, 'verified', 'device.profile', 'd4', null),
    ]);
    const labels = selectDeviceLabels('org')(store.getState());
    expect([...labels]).toEqual([['d1', 'Newer']]);
    expect(selectDeviceLabels('org')(store.getState())).toBe(labels);
  });
});

describe('rights and platform access', () => {
  it('lets only admins manage devices', async () => {
    const { w, a } = await setup();
    expect(canManageDevices(w.world.state, a.ada!.id)).toBe(true);
    expect(canManageDevices(w.world.state, a.bob!.id)).toBe(false);
  });

  it('grants platform access to private orgs and to orgs the platform log grants (ADR-026)', async () => {
    const priv = await setup({ private: true });
    expect(hasPlatformAccess(priv.w.world.state, null)).toBe(true);
    const { w } = await setup();
    const platform = w.hub.platform!.state;
    expect(hasPlatformAccess(w.world.state, null)).toBe(false);
    expect(hasPlatformAccess(w.world.state, platform)).toBe(false);
    expect(hasPlatformAccess(w.world.state, platform.with({ orgAccess: new Map([[w.org, true]]) }))).toBe(true);
    expect(hasPlatformAccess(w.world.state, platform.with({ orgAccess: new Map([[w.org, false]]) }))).toBe(false);
  });

  it('offers every writable workgroup, whatever keys the user holds (ADR-059)', async () => {
    const { w, one } = await setup({ as: 'bob' });
    expect(assignableWorkgroups(w.world.state)).toEqual([w.world.defaultWg, one]);
  });
});

describe('platform candidates', () => {
  it('checks every candidate against the verified platform log', async () => {
    const { w, a } = await setup({ private: true });
    const two = await idOf('wg two');
    candidates(w, two, [
      pdevCandidate(a),
      pdevCandidate(a, { pub: a.dev1!.identity.public(), capacity: 'x', online: 'yes', capacity_low: 'no' }),
      { device_id: 'malformed' },
      'not a candidate',
    ]);
    const rows = await loadCandidates(w.context, w.org, two);
    expect(rows).toHaveLength(4);
    // malformed candidates stay visible as unverifiable rows
    expect(rows[2]).toMatchObject({ device_id: 'malformed', verified: false, platform_seq: -1 });
    expect(rows[3]).toMatchObject({ device_id: '#4', verified: false });
    expect(rows[0]).toMatchObject({ device_id: a.pdev!.id, verified: true, online: true, capacity: CAPACITY });
    expect(rows[1]).toMatchObject({ verified: false, online: false, capacity: null, capacity_low: false });
    // an unverified candidate refreshes the platform log once
    expect(w.context.refreshLog).toHaveBeenCalledTimes(1);
    candidates(w, two, [pdevCandidate(a)]);
    await loadCandidates(w.context, w.org, two);
    expect(w.context.refreshLog).toHaveBeenCalledTimes(1);
  });

  it('verifies nothing without a platform log and refuses malformed answers', async () => {
    const { w, a } = await setup({ noPlatform: true });
    candidates(w, 'wg', [pdevCandidate(a)]);
    expect((await loadCandidates(w.context, w.org, 'wg'))[0]!.verified).toBe(false);
    for (const body of [null, { candidates: 'x' }]) {
      w.client.on('GET /v1/workgroups/wg/device-candidates', () => body);
      expect(await refusal(loadCandidates(w.context, w.org, 'wg'))).toBe('unavailable');
    }
  });
});

describe('planners', () => {
  it('assign: admins only, not archived, null once assigned, org devices of the log, platform devices by seq', async () => {
    const { w, a, one, old } = await setup();
    const log = orgEntry(w.store.getState().orgs, w.org).log!;
    const me = a.ada!.id;
    expect(refusalOf(() => assignPlan(a.dev2!.id, one, null)(log, a.bob!.id))).toBe('forbidden');
    expect(refusalOf(() => assignPlan(a.dev2!.id, old, null)(log, me))).toBe('forbidden');
    expect(assignPlan(a.dev1!.id, one, null)(log, me)).toBeNull();
    expect(refusalOf(() => assignPlan(a.bob!.id, one, null)(log, me))).toBe('not_found');
    expect(refusalOf(() => assignPlan('unknown', one, null)(log, me))).toBe('not_found');
    expect(assignPlan(a.dev2!.id, one, null)(log, me)).toEqual({
      entries: [{ type: 'device_assign', device: a.dev2!.id, wg: one }],
    });
    expect(assignPlan(a.pdev!.id, one, 2)(log, me)).toEqual({
      entries: [{ type: 'device_assign', device: a.pdev!.id, wg: one, platform_seq: 2 }],
    });
    // an org device cannot be assigned as a platform device
    expect(refusalOf(() => assignPlan(a.dev2!.id, one, 2)(log, me))).toBe('log_rights');
  });

  it('assign: a platform device of the org keeps its certification; org-device assignment refuses it', async () => {
    const { w, a, one } = await setup({ private: true });
    const log = orgEntry(w.store.getState().orgs, w.org).log!;
    expect(assignPlan(a.pdev!.id, one, 2)(log, a.ada!.id)).not.toBeNull();
    expect(refusalOf(() => assignPlan(a.pdev!.id, one, 5)(log, a.ada!.id))).toBe('log_rights');
    expect(refusalOf(() => assignPlan(a.pdev!.id, one, null)(log, a.ada!.id))).toBe('not_found');
  });

  it('unassign and unpair: admins only, null once done, never a user', async () => {
    const { w, a, one } = await setup();
    const log = orgEntry(w.store.getState().orgs, w.org).log!;
    const me = a.ada!.id;
    expect(refusalOf(() => unassignPlan(a.dev1!.id, one)(log, a.bob!.id))).toBe('forbidden');
    expect(unassignPlan(a.dev2!.id, one)(log, me)).toBeNull();
    expect(unassignPlan(a.dev1!.id, one)(log, me)).toEqual({
      entries: [{ type: 'device_unassign', device: a.dev1!.id, wg: one }],
    });
    expect(refusalOf(() => unpairPlan(a.dev1!.id)(log, a.bob!.id))).toBe('forbidden');
    expect(unpairPlan('unknown')(log, me)).toBeNull();
    expect(refusalOf(() => unpairPlan(a.bob!.id)(log, me))).toBe('forbidden');
    expect(unpairPlan(a.dev1!.id)(log, me)).toEqual({ entries: [{ type: 'remove', subject: a.dev1!.id }] });
  });
});

describe('assign, unassign, unpair', () => {
  it('assigns a platform device with WK and OK in the batch and wraps the own CK afterwards', async () => {
    const { w, a, one } = await setup();
    w.hub.platformAccess = true;
    believe(w.store, w.hub.platform!.state.with({ orgAccess: new Map([[w.org, true]]) }));
    const pdev = a.pdev!;
    w.world.add(pdev);
    const steps: string[] = [];
    const done = await assignDevice(w.context, w.org, one, { kind: 'platform', device: pdev.id, platformSeq: 2 }, (s) =>
      steps.push(s.step),
    );
    expect(done.built).not.toBeNull();
    // only a new attempt would be reported (key handling stays out of sight)
    expect(steps).toEqual([]);
    expect(w.world.state.assigned(pdev.id, one)).toBe(true);
    expect(holds(w, pdev, WG, one)).toBe(true);
    expect(holds(w, pdev, ORG, w.org)).toBe(true);
    // the CK wrap is attempted with the user's own cred wraps
    expect(w.client.of('GET /v1/wraps').length).toBe(1);
    // again: nothing to do, no CK wrap
    const again = await assignDevice(w.context, w.org, one, { kind: 'platform', device: pdev.id, platformSeq: 2 });
    expect(again.built).toBeNull();
    expect(w.client.of('GET /v1/wraps').length).toBe(1);
  });

  it('assigns an org device of the log to a further workgroup', async () => {
    const { w, a, one } = await setup();
    w.world.add(a.dev2!);
    await assignDevice(w.context, w.org, one, { kind: 'org', device: a.dev2!.id });
    expect(w.world.state.assigned(a.dev2!.id, one)).toBe(true);
    expect(holds(w, a.dev2!, WG, one)).toBe(true);
    expect(await refusal(assignDevice(w.context, w.org, one, { kind: 'org', device: 'unknown' }))).toBe('not_found');
  });

  it('refuses platform devices without platform access or a valid certification', async () => {
    const { w, a, one } = await setup();
    const target = { kind: 'platform' as const, device: a.pdev!.id, platformSeq: 2 };
    expect(await refusal(assignDevice(w.context, w.org, one, target))).toBe('platform_access_denied');
    // the hub refuses as well (its platform access is off) when the browser believes in a grant
    believe(w.store, w.hub.platform!.state.with({ orgAccess: new Map([[w.org, true]]) }));
    expect(await refusal(assignDevice(w.context, w.org, one, target))).toBe('log_rights');
    const priv = await setup({ private: true });
    const p = priv.a.pdev!.id;
    expect(
      await refusal(assignDevice(priv.w.context, priv.w.org, priv.one, { ...target, device: p, platformSeq: 1 })),
    ).toBe('log_rights');
    expect(
      await refusal(assignDevice(priv.w.context, priv.w.org, priv.one, { ...target, device: 'x', platformSeq: 2 })),
    ).toBe('log_rights');
    believe(priv.w.store, decertified(priv.w.hub.platform!.state));
    expect(await refusal(assignDevice(priv.w.context, priv.w.org, priv.one, { ...target, device: p }))).toBe(
      'log_rights',
    );
  });

  it('unassigns with the rotation of the workgroup and unpairs with the rotation of everything the device read', async () => {
    const { w, a, one } = await setup();
    const epochOne = w.world.state.epoch(WG, one);
    const epochOrg = w.world.state.epoch(ORG, w.org);
    await submitBatch(w.context, w.org, unassignPlan(a.dev1!.id, one));
    expect(w.world.state.assigned(a.dev1!.id, one)).toBe(false);
    expect(w.world.state.epoch(WG, one)).toBe(epochOne + 1);
    expect(w.world.state.epoch(ORG, w.org)).toBe(epochOrg);
    const epochDefault = w.world.state.epoch(WG, w.world.defaultWg);
    await submitBatch(w.context, w.org, unpairPlan(a.dev1!.id));
    expect(w.world.state.member(a.dev1!.id)).toBeNull();
    expect(w.world.state.epoch(WG, w.world.defaultWg)).toBe(epochDefault + 1);
    expect(w.world.state.epoch(ORG, w.org)).toBe(epochOrg + 1);
    expect(holds(w, a.dev1!, ORG, w.org)).toBe(false);
  });
});

describe('rename', () => {
  it('seals a device.profile event with the current OK and reads it in', async () => {
    const { w, a } = await setup();
    await renameDevice(w.context, w.org, a.dev1!.id, '  Studio  ');
    const event = w.hub.events.at(-1) as { header: Record<string, unknown> };
    expect(event.header).toMatchObject({ kind: 'device.profile', ref: a.dev1!.id, scope: ORG, scope_id: w.org });
    const key = keyRing(w.store, w.org)(ORG, w.org, w.world.state.epoch(ORG, w.org))!;
    const plain = await openEvent(key, event, a.ada!.identity.sigPublic());
    expect(JSON.parse(new TextDecoder().decode(plain))).toEqual({ label: 'Studio' });
    expect(w.ingested.at(-1)).toMatchObject({ org: w.org, scope: w.org });
    expect(w.ingested.at(-1)!.items[0]!.scope_seq).toBe(100);
  });

  it('refuses bad labels, non-admins, unknown devices, a missing OK and malformed answers', async () => {
    const { w, a } = await setup();
    expect(await refusal(renameDevice(w.context, w.org, a.dev1!.id, ' '))).toBe('params_invalid');
    expect(await refusal(renameDevice(w.context, w.org, a.bob!.id, 'x'))).toBe('not_found');
    w.client.on('POST /v1/events', () => ({ results: [] }));
    expect(await refusal(renameDevice(w.context, w.org, a.dev1!.id, 'x'))).toBe('unavailable');
    w.client.on('POST /v1/events', () => null);
    expect(await refusal(renameDevice(w.context, w.org, a.dev1!.id, 'x'))).toBe('unavailable');
    w.client.on('POST /v1/events', () => ({ results: [{ scope_id: 'other', scope_seq: 1 }] }));
    expect(await refusal(renameDevice(w.context, w.org, a.dev1!.id, 'x'))).toBe('unavailable');
    w.client.on('POST /v1/events', () => ({ results: [{ scope_id: w.org, scope_seq: 'x' }] }));
    expect(await refusal(renameDevice(w.context, w.org, a.dev1!.id, 'x'))).toBe('unavailable');
    const bob = await setup({ as: 'bob' });
    expect(await refusal(renameDevice(bob.w.context, bob.w.org, bob.a.dev1!.id, 'x'))).toBe('forbidden');
    // no OK in memory
    const fresh = await setup();
    fresh.w.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [fresh.w.org]: { ...orgs.byId[fresh.w.org]!, keys: new Map() } },
    }));
    expect(await refusal(renameDevice(fresh.w.context, fresh.w.org, fresh.a.dev1!.id, 'x'))).toBe('epoch_stale');
  });
});
