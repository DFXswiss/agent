// The background jobs for DFX support (ADR-096 sections 2 and 3): catch-up offers and confirms the slots of the support
// roster (every epoch of a released workgroup, keys from the verified platform log), nothing else; the owed-rotation
// job rotates released workgroups the hub lists in `rotation_pending` after a person left support – only while the
// verified logs show a former person and an active release.
import { describe, expect, it } from 'vitest';
import type { JobContext, JobRun } from '../../../src/core/jobs';
import { catchUp } from '../../../src/core/jobs/catch-up';
import { owedPlanner, pendingWorkgroups, rotatePending, supportRotatable } from '../../../src/core/jobs/rotation';
import { confirmedSlots, recipientKem } from '../../../src/core/log/catchup';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { ORG, WG } from '../../../src/core/log/state';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import { setPlatform } from '../../../src/core/store/slices/orgs';
import { grantRelease } from '../../../src/core/support/release';
import { EMPTY_DIRECTORY } from '../../../src/core/tasks/directory';
import { idOf } from '../log/world';
import { type OrgWorld, orgWorld } from '../org/world';

function jobContext(w: OrgWorld): JobContext {
  return {
    store: w.store,
    client: w.client,
    rootKey: w.platform.root,
    now: () => w.hub.now,
    persist: {
      loadPinned: async () => null,
      savePinned: async () => undefined,
      loadPlatformHead: async () => null,
      savePlatformHead: async () => undefined,
    },
  };
}

const run = (w: OrgWorld): JobRun => ({ org: w.org, trigger: 'interval', generation: w.store.generation });

async function released(options: { noPlatform?: boolean; former?: boolean; access?: 'read' | 'write' } = {}) {
  const one = await idOf('wg one');
  const w = await orgWorld({
    noPlatform: options.noPlatform ?? false,
    support: options.former ? ['tom'] : ['sam', 'tom'],
    ...(options.former && { formerSupport: ['sam'] }),
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.batch(a.ada, [{ type: 'rotate', scope: WG, scope_id: one, epoch: 2 }]);
    },
  });
  await grantRelease(w.context, w.org, one, options.access ?? 'read', EMPTY_DIRECTORY);
  return { w, a: w.others, one };
}

describe('catch-up for support', () => {
  it('wraps every epoch of a released workgroup to a roster person, nothing the logs do not allow', async () => {
    // released while this browser had no platform log: nothing went to support then
    const { w, a, one } = await released({ noPlatform: true });
    const platform = await verifyPlatformLog(w.platform.entries, w.platform.root);
    setPlatform(w.store, platform);
    const outsider = await idOf('outsider');
    w.client.on(`GET /v1/orgs/${w.org}/catch-up`, () => ({
      missing: [1, 2, 3]
        .map((epoch) => ({ scope: WG, scope_id: one, epoch, recipient_id: a.sam.id }))
        .concat([
          { scope: WG, scope_id: w.world.defaultWg, epoch: 1, recipient_id: a.sam.id },
          { scope: WG, scope_id: one, epoch: 1, recipient_id: outsider },
          { scope: ORG, scope_id: w.org, epoch: 1, recipient_id: a.sam.id },
        ]),
    }));
    expect(await catchUp(jobContext(w), run(w))).toBe(2);
    const [call] = w.client.of('POST /v1/wraps');
    const wraps = (call!.body as { wraps: { epoch: number; recipient_id: string; wrap: string }[] }).wraps;
    expect(wraps.map((wrap) => `${wrap.recipient_id} ${wrap.epoch}`)).toEqual([`${a.sam.id} 1`, `${a.sam.id} 2`]);
    const slot = { scope: WG, scope_id: one, epoch: 2, recipient: a.sam.id };
    await expect(unwrapKey(a.sam.identity.kem, slot, wraps[1]!.wrap)).resolves.toBeDefined();
  });

  it('takes a member key from the org log and a support key from the roster', async () => {
    const { w, a, one } = await released();
    const roster = w.store.getState().orgs.platform!.state.supportRoster();
    const state = w.world.state;
    expect(recipientKem([state], a.bob.id, roster)).toBe(a.bob.identity.public().kem);
    expect(recipientKem([state], a.sam.id, roster)).toBe(a.sam.identity.public().kem);
    expect(recipientKem([state], a.sam.id)).toBeNull();
    const offered = [{ scope: WG, scope_id: one, epoch: 1, recipient: a.sam.id }];
    expect(confirmedSlots(w.world.log!, a.ada.id, offered)).toEqual([]);
    expect(confirmedSlots(w.world.log!, a.ada.id, offered, [], roster)).toEqual(offered);
  });
});

describe('rotation after a person left support', () => {
  it('rotates a listed released workgroup while the platform log shows a former person', async () => {
    const { w, a, one } = await released({ former: true });
    expect(supportRotatable(w.world.state, w.store.getState().orgs.platform!.state, a.ada.id)).toEqual([one]);
    expect(supportRotatable(w.world.state, w.store.getState().orgs.platform!.state, a.bob.id)).toEqual([]);
    w.client.on(`GET /v1/orgs/${w.org}`, () => ({
      rotation_pending: [{ scope: WG, scope_id: one }, { scope: ORG, scope_id: w.org }, 'junk', { scope: WG }],
    }));
    expect(await rotatePending(jobContext(w), run(w))).toBe(1);
    expect(w.world.state.epoch(WG, one)).toBe(3);
    // the new epoch went to the org's readers; tom gets it by catch-up
    const last = w.hub.stored.filter((wrap) => wrap.epoch === 3).map((wrap) => wrap.recipient_id);
    expect(last).toContain(a.bob.id);
    expect(last).not.toContain(a.tom.id);
  });

  it('asks the hub nothing without a former person, and ignores an answer without the list', async () => {
    const { w, one } = await released();
    expect(await rotatePending(jobContext(w), run(w))).toBe(0);
    expect(w.client.of(`GET /v1/orgs/${w.org}`)).toHaveLength(0);
    w.client.on(`GET /v1/orgs/${w.org}`, () => ({}));
    expect(await pendingWorkgroups(w.context, w.org)).toEqual(new Set());
    expect(one).toBeTruthy();
  });

  it('rotates a workgroup owed twice once', async () => {
    const { w, a, one } = await released({ former: true });
    // bob leaves by himself: the workgroup he read is owed anyway
    await w.world.batch(a.bob, [{ type: 'remove', subject: a.bob.id }]);
    await w.sync();
    const plan = owedPlanner(
      w.store.getState().orgs.platform!.state,
      () => null,
      new Set([one]),
    )(w.world.log!, a.ada.id)!;
    const rotations = plan.entries.filter((entry) => entry.type === 'rotate' && entry.scope_id === one);
    expect(rotations).toHaveLength(1);
  });
});
