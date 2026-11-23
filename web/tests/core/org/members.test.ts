// U-WEB-ui-members (core part): rows of the member list from the verified log and the profiles, the rights that decide
// which controls exist (06-web.md section 1.2), and every planner's checks (null when done, refusals as the log rules).
import { describe, expect, it } from 'vitest';
import type { Directory } from '../../../src/core/tasks/directory';
import {
  canManage,
  grantable,
  grantPlan,
  isLastAdmin,
  leavePlan,
  memberRows,
  removePlan,
  rolePlan,
  selectOwnName,
  ungrantPlan,
  workgroupOrder,
} from '../../../src/core/org/members';
import type { Actor } from '../log/world';
import { idOf } from '../log/world';
import { orgWorld } from './world';

const names = (members: Record<string, string>, workgroups: Record<string, string> = {}): Directory => ({
  orgName: 'Acme',
  workgroups: new Map(Object.entries(workgroups)),
  members: new Map(Object.entries(members)),
});

async function setup() {
  const one = await idOf('wg one');
  const old = await idOf('wg old');
  const w = await orgWorld({
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.batch(a.ada!, [{ type: 'wg_create', wg: old, epoch: 1 }]);
      await world.admitUser(a.ada!, a.bob!, 'member', { [one]: 'read', [old]: 'write' });
      await world.admitUser(a.ada!, a.cy!, 'member', {});
      await world.admitDevice(a.ada!, a.dev1!, [one]);
      await world.batch(a.ada!, [{ type: 'wg_archive', wg: old }]);
    },
  });
  const a = w.others as Record<string, Actor>;
  return { w, a, one, old, log: w.world.log!, state: w.world.state };
}

describe('member rows', () => {
  it('lists active users, admins first, then by name, with their workgroups and flags', async () => {
    const { w, a, one, old, state } = await setup();
    const rows = memberRows(state, a.ada!.id, names({ [a.bob!.id]: 'Bob', [a.cy!.id]: 'Ann' }));
    expect(rows.map((r) => r.id)).toEqual([a.ada!.id, a.cy!.id, a.bob!.id]);
    expect(rows[0]).toMatchObject({ name: null, role: 'admin', isMe: true, lastAdmin: true });
    expect(rows[2]).toMatchObject({ name: 'Bob', role: 'member', isMe: false, lastAdmin: false });
    const order = workgroupOrder(state);
    expect(order[0]).toBe(w.world.defaultWg);
    expect(rows[2]!.access).toEqual(
      order.flatMap((wg) =>
        wg === w.world.defaultWg
          ? [{ wg, access: 'write', isDefault: true }]
          : wg === one
            ? [{ wg, access: 'read', isDefault: false }]
            : wg === old
              ? [{ wg, access: 'write', isDefault: false }]
              : [],
      ),
    );
    // unnamed members sort after named ones, equal names by ID
    const twins = memberRows(state, a.ada!.id, names({ [a.bob!.id]: 'Same', [a.cy!.id]: 'Same' }));
    expect(twins.slice(1).map((r) => r.id)).toEqual([a.bob!.id, a.cy!.id].sort());
    const unnamed = memberRows(state, a.ada!.id, names({ [a.cy!.id]: 'Zed' }));
    expect(unnamed.map((r) => r.id)).toEqual([a.ada!.id, a.cy!.id, a.bob!.id]);
  });

  it('decides the admin controls and what can be granted', async () => {
    const { a, one, old, state } = await setup();
    expect(canManage(state, a.ada!.id)).toBe(true);
    expect(canManage(state, a.bob!.id)).toBe(false);
    expect(isLastAdmin(state, a.ada!.id)).toBe(true);
    expect(isLastAdmin(state, a.bob!.id)).toBe(false);
    expect(grantable(state, a.bob!.id)).toEqual([{ wg: one, access: 'read' }]);
    expect(grantable(state, a.cy!.id)).toEqual([{ wg: one, access: null }]);
    expect(grantable(state, a.cy!.id).some((g) => g.wg === old)).toBe(false);
  });
});

describe('own display name', () => {
  it('is the verified member.profile of the user in the org, else empty', async () => {
    const { w, a } = await setup();
    const { putEvents } = await import('../../../src/core/store/slices/events');
    expect(selectOwnName(null, a.ada!.id)(w.store.getState())).toBe('');
    expect(selectOwnName(w.org, null)(w.store.getState())).toBe('');
    expect(selectOwnName(w.org, a.ada!.id)(w.store.getState())).toBe('');
    putEvents(w.store, w.org, [
      {
        org: w.org,
        scopeId: w.org,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'member.profile', ref: a.ada!.id, author: a.ada!.id, wg: '', id: 'e1' },
        body: { display_name: 'Ada' },
      },
    ]);
    expect(selectOwnName(w.org, a.ada!.id)(w.store.getState())).toBe('Ada');
  });
});

describe('planners', () => {
  it('role: null when unchanged, last admin, rights', async () => {
    const { a, log } = await setup();
    expect(rolePlan(a.bob!.id, 'admin')(log, a.ada!.id)).toEqual({
      entries: [{ type: 'role', subject: a.bob!.id, role: 'admin' }],
    });
    expect(rolePlan(a.bob!.id, 'member')(log, a.ada!.id)).toBeNull();
    expect(() => rolePlan(a.ada!.id, 'member')(log, a.ada!.id)).toThrow(
      expect.objectContaining({ code: 'last_admin' }),
    );
    expect(() => rolePlan(a.cy!.id, 'admin')(log, a.bob!.id)).toThrow(expect.objectContaining({ code: 'forbidden' }));
    expect(() => rolePlan(a.dev1!.id, 'admin')(log, a.ada!.id)).toThrow(expect.objectContaining({ code: 'not_found' }));
    expect(() => rolePlan(a.pdev!.id, 'admin')(log, a.ada!.id)).toThrow(expect.objectContaining({ code: 'not_found' }));
  });

  it('grant: access changes, history, default and archived workgroups', async () => {
    const { w, a, one, old, log } = await setup();
    expect(grantPlan(a.cy!.id, one, 'write', true)(log, a.ada!.id)).toEqual({
      entries: [{ type: 'grant', subject: a.cy!.id, wg: one, access: 'write' }],
      history: true,
    });
    expect(grantPlan(a.bob!.id, one, 'write')(log, a.ada!.id)).toMatchObject({ history: false });
    expect(grantPlan(a.bob!.id, one, 'read')(log, a.ada!.id)).toBeNull();
    const refused = [
      () => grantPlan(a.bob!.id, w.world.defaultWg, 'write')(log, a.ada!.id),
      () => grantPlan(a.bob!.id, old, 'read')(log, a.ada!.id),
      () => grantPlan(a.bob!.id, 'unknown', 'read')(log, a.ada!.id),
      () => grantPlan(a.cy!.id, one, 'read')(log, a.bob!.id),
    ];
    for (const run of refused) expect(run).toThrow(expect.objectContaining({ code: 'forbidden' }));
  });

  it('ungrant: null without access, never the default workgroup', async () => {
    const { w, a, one, log } = await setup();
    expect(ungrantPlan(a.bob!.id, one)(log, a.ada!.id)).toEqual({
      entries: [{ type: 'ungrant', subject: a.bob!.id, wg: one }],
    });
    expect(ungrantPlan(a.cy!.id, one)(log, a.ada!.id)).toBeNull();
    expect(ungrantPlan(a.pdev!.id, one)(log, a.ada!.id)).toBeNull();
    expect(() => ungrantPlan(a.bob!.id, w.world.defaultWg)(log, a.ada!.id)).toThrow(
      expect.objectContaining({ code: 'forbidden' }),
    );
    expect(() => ungrantPlan(a.bob!.id, one)(log, a.cy!.id)).toThrow(expect.objectContaining({ code: 'forbidden' }));
  });

  it('remove and leave', async () => {
    const { a, log } = await setup();
    expect(removePlan(a.bob!.id)(log, a.ada!.id)).toEqual({ entries: [{ type: 'remove', subject: a.bob!.id }] });
    expect(removePlan(a.pdev!.id)(log, a.ada!.id)).toBeNull();
    expect(() => removePlan(a.ada!.id)(log, a.ada!.id)).toThrow(expect.objectContaining({ code: 'forbidden' }));
    expect(() => removePlan(a.cy!.id)(log, a.bob!.id)).toThrow(expect.objectContaining({ code: 'forbidden' }));
    expect(leavePlan()(log, a.bob!.id)).toEqual({ entries: [{ type: 'remove', subject: a.bob!.id }] });
    expect(leavePlan()(log, a.pdev!.id)).toBeNull();
    expect(() => leavePlan()(log, a.ada!.id)).toThrow(expect.objectContaining({ code: 'last_admin' }));
  });
});
