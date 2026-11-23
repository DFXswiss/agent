// U-WEB-ui-workgroups (core part): rows of the workgroup list, assignable devices, the creation batch (`wg_create`,
// grants, device assignments, WK_1 for every reader, `wg.profile` at the batch head), archive and rename.
import { describe, expect, it } from 'vitest';
import { openCheckedEvent } from '../../../src/core/log/events';
import type { PlatformState } from '../../../src/core/log/platform';
import { ORG, WG } from '../../../src/core/log/state';
import { submitBatch } from '../../../src/core/org/batch';
import {
  archivePlan,
  assignableDevices,
  createWorkgroupPlan,
  NAME_MAX,
  renameWorkgroup,
  workgroupName,
  workgroupRows,
} from '../../../src/core/org/workgroups';
import { orgEntry } from '../../../src/core/store/slices/orgs';
import type { Actor } from '../log/world';
import { idOf } from '../log/world';
import { holds, orgWorld } from './world';

async function setup(options: { as?: 'ada' | 'bob'; private?: boolean } = {}) {
  const one = await idOf('wg one');
  const old = await idOf('wg old');
  const w = await orgWorld({
    ...options,
    platformDevices: ['pdev'],
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.batch(a.ada!, [{ type: 'wg_create', wg: old, epoch: 1 }]);
      await world.batch(a.ada!, [{ type: 'wg_archive', wg: old }]);
      await world.admitUser(a.ada!, a.bob!, 'member', { [one]: 'read' });
      await world.admitUser(a.ada!, a.cy!, 'member', {});
      await world.admitDevice(a.ada!, a.dev1!, [one]);
      await world.admitDevice(a.ada!, a.dev2!, [one]);
      await world.batch(a.ada!, [{ type: 'device_unassign', device: a.dev2!.id, wg: one }]);
      if (options.private) {
        await world.batch(a.ada!, [
          { type: 'device_assign', device: a.pdev!.id, wg: world.defaultWg, platform_seq: 2 },
        ]);
      }
    },
  });
  return { w, a: w.others as Record<string, Actor>, one, old };
}

describe('workgroup rows and devices', () => {
  it('lists the default workgroup first and archived ones last, with access and devices', async () => {
    const { w, a, one, old } = await setup({ as: 'bob' });
    const directory = { orgName: null, workgroups: new Map([[one, 'One']]), members: new Map() };
    const rows = workgroupRows(w.world.state, a.bob!.id, directory);
    expect(rows.map((r) => r.id)).toEqual([w.world.defaultWg, one, old]);
    expect(rows[0]).toMatchObject({ name: null, isDefault: true, archived: false, myAccess: 'write', devices: [] });
    expect(rows[1]).toMatchObject({
      name: 'One',
      isDefault: false,
      archived: false,
      myAccess: 'read',
      devices: [a.dev1!.id],
    });
    expect(rows[2]).toMatchObject({ archived: true, myAccess: null });
  });

  it('offers active org devices, platform devices only with platform access, never decertified ones', async () => {
    const { w, a } = await setup({ private: true });
    const platform = w.store.getState().orgs.platform!.state;
    const ids = (list: { id: string }[]) => list.map((d) => d.id);
    const all = assignableDevices(w.world.state, platform);
    expect(ids(all)).toEqual([a.dev1!.id, a.dev2!.id, a.pdev!.id].sort());
    expect(all.find((d) => d.id === a.pdev!.id)!.platformSeq).toBe(2);
    expect(all.find((d) => d.id === a.dev1!.id)!.platformSeq).toBeNull();
    const noAccess = { excluded: () => [], platformAccess: () => false } as unknown as PlatformState;
    expect(ids(assignableDevices(w.world.state, noAccess))).toEqual([a.dev1!.id, a.dev2!.id].sort());
    const decertified = { excluded: () => [a.pdev!.id, a.dev2!.id], platformAccess: () => true } as never;
    expect(ids(assignableDevices(w.world.state, decertified))).toEqual([a.dev1!.id]);
    const plain = await setup();
    expect(assignableDevices(plain.w.world.state, null).length).toBe(2);
  });

  it('checks names', () => {
    expect(workgroupName('  Ops  ')).toBe('Ops');
    expect(workgroupName('   ')).toBeNull();
    expect(workgroupName('x'.repeat(NAME_MAX))).toHaveLength(NAME_MAX);
    expect(workgroupName('x'.repeat(NAME_MAX + 1))).toBeNull();
  });
});

describe('creating a workgroup', () => {
  it('sends wg_create, grants, assignments, WK_1 wraps and the wg.profile event in one batch', async () => {
    const { w, a } = await setup({ private: true });
    w.hub.platformAccess = true;
    const wg = await idOf('wg new');
    const plan = createWorkgroupPlan(
      {
        wg,
        name: ' Research ',
        grants: { [a.cy!.id]: 'write', [a.bob!.id]: 'read', [a.ada!.id]: 'read' },
        devices: [a.pdev!.id, a.dev2!.id],
      },
      w.context.now,
    );
    const { built } = await submitBatch(w.context, w.org, plan);
    expect(built!.body.entries.map((e) => e.body.type)).toEqual([
      'wg_create',
      'grant',
      'grant',
      'device_assign',
      'device_assign',
    ]);
    const state = w.world.state;
    expect(state.wgAccess(a.ada!.id, wg)).toBe('write');
    expect(state.wgAccess(a.bob!.id, wg)).toBe('read');
    expect(state.wgAccess(a.cy!.id, wg)).toBe('write');
    expect(state.assigned(a.pdev!.id, wg)).toBe(true);
    expect(state.assigned(a.dev2!.id, wg)).toBe(true);
    for (const reader of [a.ada!, a.bob!, a.cy!, a.dev2!]) expect(holds(w, reader, WG, wg)).toBe(true);
    expect(w.hub.stored.some((x) => x.scope_id === wg && x.recipient_id === a.pdev!.id)).toBe(true);
    // dev2 had no workgroup left: it gets the current OK with the assignment
    expect(holds(w, a.dev2!, ORG, w.org)).toBe(true);
    // the profile event: org scope, the new workgroup, at the batch head, readable with OK
    expect(w.hub.events).toHaveLength(1);
    const key = orgEntry(w.store.getState().orgs, w.org).keys.get(`org ${w.org} ${w.world.state.epoch(ORG, w.org)}`)!;
    const opened = await openCheckedEvent(w.hub.events[0], w.world.log!, key);
    expect(opened.checked.header).toMatchObject({ kind: 'wg.profile', wg, lh: w.world.log!.head.seq });
    expect(JSON.parse(new TextDecoder().decode(opened.plaintext))).toEqual({ name: 'Research' });
    // a retry after the hub took it has nothing left to do
    expect(plan(w.world.log!, a.ada!.id)).toBeNull();
  });

  it('refuses members, empty names and a missing org key', async () => {
    const { w, a } = await setup();
    const wg = await idOf('wg new');
    const plan = (name: string) => createWorkgroupPlan({ wg, name, grants: {}, devices: [] }, w.context.now);
    expect(() => plan('X')(w.world.log!, a.bob!.id)).toThrow(expect.objectContaining({ code: 'forbidden' }));
    expect(() => plan(' ')(w.world.log!, a.ada!.id)).toThrow(expect.objectContaining({ code: 'params_invalid' }));
    (orgEntry(w.store.getState().orgs, w.org).keys as Map<string, unknown>).delete(
      `org ${w.org} ${w.world.state.epoch(ORG, w.org)}`,
    );
    await expect(submitBatch(w.context, w.org, plan('X'))).rejects.toMatchObject({ code: 'epoch_stale' });
  });
});

describe('archive and rename', () => {
  it('archives once, never the default workgroup', async () => {
    const { w, a, one, old } = await setup();
    const log = w.world.log!;
    expect(archivePlan(one)(log, a.ada!.id)).toEqual({ entries: [{ type: 'wg_archive', wg: one }] });
    expect(archivePlan(old)(log, a.ada!.id)).toBeNull();
    for (const run of [
      () => archivePlan(w.world.defaultWg)(log, a.ada!.id),
      () => archivePlan('unknown')(log, a.ada!.id),
      () => archivePlan(one)(log, a.bob!.id),
    ]) {
      expect(run).toThrow(expect.objectContaining({ code: 'forbidden' }));
    }
  });

  it('renames with a new wg.profile event read into the store at once', async () => {
    const { w, one } = await setup();
    await renameWorkgroup(w.context, w.org, one, ' Renamed ');
    expect(w.hub.events).toHaveLength(1);
    expect(w.ingested).toEqual([
      { org: w.org, scope: w.org, items: [{ scope_seq: 100, received_at: null, event: w.hub.events[0] }] },
    ]);
    const key = orgEntry(w.store.getState().orgs, w.org).keys.get(`org ${w.org} ${w.world.state.epoch(ORG, w.org)}`)!;
    const opened = await openCheckedEvent(w.hub.events[0], w.world.log!, key);
    expect(opened.checked.header).toMatchObject({ wg: one, lh: w.world.log!.head.seq });
    await expect(renameWorkgroup(w.context, w.org, one, '')).rejects.toMatchObject({ code: 'params_invalid' });
    await expect(renameWorkgroup(w.context, w.org, 'unknown', 'X')).rejects.toMatchObject({ code: 'not_found' });
    w.client.on('POST /v1/events', () => ({ results: [{ scope_id: 'other', scope_seq: 1 }] }));
    await expect(renameWorkgroup(w.context, w.org, one, 'X')).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on('POST /v1/events', () => ({ results: [{ scope_id: w.org, scope_seq: 'x' }] }));
    await expect(renameWorkgroup(w.context, w.org, one, 'X')).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on('POST /v1/events', () => null);
    await expect(renameWorkgroup(w.context, w.org, one, 'X')).rejects.toMatchObject({ code: 'unavailable' });
    const member = await setup({ as: 'bob' });
    await expect(renameWorkgroup(member.w.context, member.w.org, one, 'X')).rejects.toMatchObject({
      code: 'forbidden',
    });
  });
});
