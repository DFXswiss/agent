// The release of a workgroup to DFX support from the organization's side (ADR-096, KOR-56) against the simulated hub of
// tests/core/org/world.ts, whose batch check now knows the support roster of its platform log: one batch with
// `support_grant`, the wraps of every held epoch to every roster person (keys from the platform log) and the sealed
// `support.info`; a change of the access without wraps; the revocation with the rotation; refusals and retries.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openEvent } from '../../../src/core/crypto/event';
import { Secret } from '../../../src/core/crypto/secret';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import { epochKey, WG } from '../../../src/core/log/state';
import {
  grantRelease,
  infoBody,
  infoEvent,
  INFO_KIND,
  MEMBERS_MAX,
  releaseOf,
  revokeRelease,
  supportWraps,
} from '../../../src/core/support/release';
import { EMPTY_DIRECTORY, type Directory } from '../../../src/core/tasks/directory';
import type { Actor } from '../log/world';
import { idOf } from '../log/world';
import { type OrgWorld, orgWorld } from '../org/world';

const directory = (w: OrgWorld, names: Record<string, string> = {}): Directory => ({
  ...EMPTY_DIRECTORY,
  orgName: 'DFX AG',
  workgroups: new Map([[w.world.defaultWg, 'Team']]),
  members: new Map(Object.entries(names)),
});

async function setup(options: { as?: 'ada' | 'bob' | 'cy'; noPlatform?: boolean } = {}) {
  const one = await idOf('wg one');
  const w = await orgWorld({
    ...options,
    support: ['sam', 'tom'],
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.admitUser(a.ada, a.cy, 'member', { [one]: 'write' });
      await world.admitDevice(a.ada, a.dev1, [one]);
      // a second epoch of workgroup one: the release hands over the history
      await world.batch(a.ada, [{ type: 'rotate', scope: WG, scope_id: one, epoch: 2 }]);
    },
  });
  return { w, a: w.others, one };
}

const posted = (w: OrgWorld) =>
  w.client.of(`POST /v1/orgs/${w.org}/log`).map(
    (call) =>
      call.body as {
        entries: { body: Record<string, unknown> }[];
        wraps: { scope_id: string; epoch: number; recipient_id: string; wrap: string }[];
        events: { header: Record<string, unknown> }[];
      },
  );

/** Opens an event of workgroup `wg` with the key `actor` holds. */
async function open(w: OrgWorld, actor: Actor, event: { header: Record<string, unknown> }) {
  const key = w.world.held
    .get(actor.id)!
    .get(epochKey(WG, event.header.scope_id as string, event.header.epoch as number))!;
  return JSON.parse(fromUtf8(await openEvent(key, event, b64uDecode(actor.identity.public().sig)))) as unknown;
}

describe('release', () => {
  it('releases a workgroup in one batch: the entry, every held epoch to every support person, support.info', async () => {
    const { w, a, one } = await setup();
    const names = { [a.ada.id]: 'Ada', [a.bob.id]: 'Bob', [a.cy.id]: 'C'.repeat(150) };
    const attempts = await grantRelease(w.context, w.org, one, 'read', directory(w, names));
    expect(attempts).toBe(1);
    const [body] = posted(w);
    expect(body!.entries.map((e) => e.body.type)).toEqual(['support_grant']);
    expect(body!.entries[0]!.body).toMatchObject({ wg: one, access: 'read' });
    const slots = body!.wraps.map((wrap) => `${wrap.recipient_id} ${wrap.epoch}`).sort();
    expect(slots).toEqual([`${a.sam.id} 1`, `${a.sam.id} 2`, `${a.tom.id} 1`, `${a.tom.id} 2`].sort());
    // the person opens the keys with its own identity: the key of epoch 2 is the workgroup's
    const wrap = body!.wraps.find((item) => item.recipient_id === a.sam.id && item.epoch === 2)!;
    const slot = { scope: WG, scope_id: one, epoch: 2, recipient: a.sam.id };
    const key = await unwrapKey(a.sam.identity.kem, slot, wrap.wrap);
    expect(key.reveal((raw) => [...raw])).toEqual(
      w.world.held
        .get(a.ada.id)!
        .get(epochKey(WG, one, 2))!
        .reveal((raw) => [...raw]),
    );
    const [info] = body!.events;
    expect(info!.header).toMatchObject({
      kind: INFO_KIND,
      scope: WG,
      scope_id: one,
      wg: one,
      task: '',
      session: '',
      ref: '',
      epoch: 2,
      author: a.ada.id,
      lh: w.world.log!.head.seq,
    });
    expect(await open(w, a.ada, info!)).toEqual({
      org_name: 'DFX AG',
      wg_name: one,
      members: { [a.ada.id]: 'Ada', [a.bob.id]: 'Bob', [a.cy.id]: 'C'.repeat(100) },
    });
    expect(releaseOf(w.world.state, one)).toBe('read');
    expect(releaseOf(w.store.getState().orgs.byId[w.org]!.log!.state, one)).toBe('read');
  });

  it('changes the access without wraps, refuses the same access and an archived workgroup', async () => {
    const { w, one } = await setup();
    await grantRelease(w.context, w.org, one, 'read', directory(w));
    await grantRelease(w.context, w.org, one, 'write', directory(w));
    const [, change] = posted(w);
    expect(change!.wraps).toEqual([]);
    expect(change!.events).toHaveLength(1);
    expect(releaseOf(w.world.state, one)).toBe('write');
    await expect(grantRelease(w.context, w.org, one, 'write', directory(w))).rejects.toMatchObject({
      code: 'params_invalid',
    });
    const unknown = await idOf('unknown wg');
    await expect(grantRelease(w.context, w.org, unknown, 'read', directory(w))).rejects.toMatchObject({
      code: 'forbidden',
    });
  });

  it('lets a writer release, never a reader', async () => {
    const writer = await setup({ as: 'cy' });
    await grantRelease(writer.w.context, writer.w.org, writer.one, 'write', directory(writer.w));
    expect(releaseOf(writer.w.world.state, writer.one)).toBe('write');
    const reader = await setup({ as: 'bob' });
    await expect(
      grantRelease(reader.w.context, reader.w.org, reader.one, 'read', directory(reader.w)),
    ).rejects.toMatchObject({ code: 'forbidden' });
    await expect(revokeRelease(reader.w.context, reader.w.org, reader.one)).rejects.toMatchObject({
      code: 'forbidden',
    });
  });

  it('builds again after seq_conflict', async () => {
    const { w, a, one } = await setup();
    const other = await idOf('another wg');
    w.hub.before.push(() => w.hub.foreign(a.ada, { type: 'wg_create', wg: other, epoch: 1 }));
    expect(await grantRelease(w.context, w.org, one, 'read', directory(w))).toBe(2);
    expect(w.context.refreshLog).toHaveBeenCalledTimes(1);
  });

  it('releases without wraps while the platform log is not verified', async () => {
    const { w, one } = await setup({ noPlatform: true });
    await grantRelease(w.context, w.org, one, 'read', directory(w));
    expect(posted(w)[0]!.wraps).toEqual([]);
  });

  it('revokes with the rotation of the workgroup, the new epoch for the org readers only', async () => {
    const { w, a, one } = await setup();
    await grantRelease(w.context, w.org, one, 'write', directory(w));
    const { built } = await revokeRelease(w.context, w.org, one);
    const body = built!.body;
    expect(body.entries.map((e) => e.body.type)).toEqual(['support_revoke', 'rotate']);
    expect(body.wraps.every((wrap) => wrap.epoch === 3)).toBe(true);
    const recipients = new Set(body.wraps.map((wrap) => wrap.recipient_id));
    expect(recipients.has(a.sam.id) || recipients.has(a.tom.id)).toBe(false);
    expect(recipients.has(a.bob.id)).toBe(true);
    expect(releaseOf(w.world.state, one)).toBeNull();
    // nothing left to revoke
    expect((await revokeRelease(w.context, w.org, one)).built).toBeNull();
  });
});

describe('parts', () => {
  it('names the workgroup readers that are users, by IDs where the directory has no name', async () => {
    const { w, a, one } = await setup();
    const state = w.world.state;
    const body = infoBody(state, { ...EMPTY_DIRECTORY, members: new Map([[a.bob.id, '  ']]) }, one);
    expect(body).toEqual({ org_name: w.org, wg_name: one, members: {} });
    expect(infoBody(state, directory(w, { [a.dev1.id]: 'Device' }), w.world.defaultWg)).toEqual({
      org_name: 'DFX AG',
      wg_name: 'Team',
      members: {},
    });
    expect(MEMBERS_MAX).toBe(1000);
  });

  it('wraps only the epochs it holds and seals only with the current key', async () => {
    const { w, a, one } = await setup();
    const state = w.world.state;
    const roster = w.store.getState().orgs.platform!.state.supportRoster();
    const key = Secret.of(new Uint8Array(32).fill(3));
    const wraps = await supportWraps(a.ada.identity, state, roster, one, (_s, _id, epoch) =>
      epoch === 2 ? key : null,
    );
    expect(wraps.map((wrap) => wrap.epoch)).toEqual([2, 2]);
    await expect(
      infoEvent(state, () => null, { id: a.ada.id, identity: a.ada.identity }, one, {}, 0, w.hub.now),
    ).rejects.toMatchObject({ code: 'epoch_stale' });
  });
});
