// U-WEB-ui-members (core part): org invitations – the `invite` entry and the sealed package the invitee opens with the
// code (keys, names, founder, head after the entry, hub key), the link, retries with the same code, the open list
// checked against the verified log, and revocation.
import { describe, expect, it } from 'vitest';
import { inviteSecrets, openPackage, parseCode } from '../../../src/core/crypto/invite';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { ORG, WG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import {
  createInvite,
  DEVICE_EXPIRY_HOURS,
  EXPIRY_HOURS,
  HOUR_MS,
  invitable,
  type InviteInput,
  inviteStatuses,
  openInvites,
  revokeInvite,
  USER_EXPIRY_HOURS,
} from '../../../src/core/org/invites';
import { keyRing } from '../../../src/core/org/batch';
import { orgEntry } from '../../../src/core/store/slices/orgs';
import type { Actor } from '../log/world';
import { idOf, T0 } from '../log/world';
import { orgWorld } from './world';

const HUB_KEY = { id: 'hub-test', public_key: b64uEncode(new Uint8Array(32).fill(9)) };

async function setup(as: 'ada' | 'bob' = 'ada') {
  const one = await idOf('wg one');
  const hidden = await idOf('wg hidden');
  const w = await orgWorld({
    as,
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada!, a.bob!, 'member', {});
      await world.admitUser(a.ada!, a.cy!, 'admin', {});
      // a workgroup of cy that ada cannot read
      await world.batch(a.cy!, [{ type: 'wg_create', wg: hidden, epoch: 1 }]);
    },
  });
  const a = w.others as Record<string, Actor>;
  const directory = {
    orgName: 'Grüße AG',
    workgroups: new Map([
      [w.world.defaultWg, 'Default'],
      [one, 'One'],
    ]),
    members: new Map(),
  };
  const input = (request: InviteInput['request']): InviteInput => ({
    request,
    directory,
    hubKey: HUB_KEY,
    origin: 'https://hub.example',
    code: () => new Uint8Array(16).fill(3),
  });
  return { w, a, one, hidden, input };
}

describe('createInvite', () => {
  it('appends the invite and uploads a package the code opens', async () => {
    const { w, a, one, input } = await setup();
    const created = await createInvite(
      w.context,
      w.org,
      input({ kind: 'user', role: 'member', wgs: { [one]: 'read' }, hours: 72 }),
    );
    expect(created.code).toMatch(/^[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{6}$/);
    expect(created.link).toBe(`https://hub.example/invite#${created.code}`);
    expect(created.expires).toBe('2026-11-24T12:00:00.000Z');
    const state = w.world.state;
    const invite = state.invites.get(created.inviteId)!;
    expect(invite).toMatchObject({ kind: 'user', role: 'member', wgs: { [one]: 'read' }, expires: created.expires });
    expect(orgEntry(w.store.getState().orgs, w.org).log!.head).toEqual(w.world.log!.head);
    const secrets = await inviteSecrets(parseCode(created.code));
    const pkg = await openPackage(secrets, w.hub.invites.get(created.inviteId)!.package_ct);
    expect(pkg.org).toBe(w.org);
    expect(pkg.org_name).toBe('Grüße AG');
    expect(pkg.wg_names).toEqual({ [w.world.defaultWg]: 'Default', [one]: 'One' });
    expect(pkg.founder).toBe(a.ada!.identity.public().sig);
    expect(pkg.head).toEqual(w.world.log!.head);
    expect(pkg.hub_key).toEqual(HUB_KEY);
    const held = keyRing(w.store, w.org);
    const raw = (scope: string, id: string) => held(scope, id, 1)!.reveal((k) => b64uEncode(k));
    expect(pkg.keys).toEqual([
      { scope: ORG, scope_id: w.org, epoch: 1, key: raw(ORG, w.org) },
      ...[w.world.defaultWg, one].sort().map((wg) => ({ scope: WG, scope_id: wg, epoch: 1, key: raw(WG, wg) })),
    ]);
    expect(created.seq).toBe(w.world.log!.head.seq);
  });

  it('drops the default workgroup from the entry, names nothing it does not know, invites devices', async () => {
    const { w, one, input } = await setup();
    const blank = { ...input({ kind: 'user', role: 'admin', wgs: { [w.world.defaultWg]: 'read' }, hours: 1 }) };
    const created = await createInvite(w.context, w.org, {
      ...blank,
      directory: { orgName: null, workgroups: new Map(), members: new Map() },
      code: undefined as never,
    });
    expect(w.world.state.invites.get(created.inviteId)!.wgs).toEqual({});
    const pkg = await openPackage(
      await inviteSecrets(parseCode(created.code)),
      w.hub.invites.get(created.inviteId)!.package_ct,
    );
    expect(pkg.org_name).toBe('');
    expect(pkg.wg_names).toEqual({ [w.world.defaultWg]: '' });
    const device = await createInvite(w.context, w.org, {
      ...input({ kind: 'device', wgs: [one, one], hours: DEVICE_EXPIRY_HOURS }),
      code: () => new Uint8Array(16).fill(4),
    });
    expect(w.world.state.invites.get(device.inviteId)).toMatchObject({ kind: 'device', wgs: { [one]: 'device' } });
    const devicePkg = await openPackage(
      await inviteSecrets(parseCode(device.code)),
      w.hub.invites.get(device.inviteId)!.package_ct,
    );
    expect((devicePkg.keys as { scope_id: string }[]).map((k) => k.scope_id)).toEqual([w.org, one]);
  });

  it('refuses what it cannot do', async () => {
    const { w, hidden, input } = await setup();
    const user = (wgs: Record<string, 'read' | 'write'>, hours = 72) =>
      createInvite(w.context, w.org, input({ kind: 'user', role: 'member', wgs, hours }));
    await expect(user({}, 0)).rejects.toMatchObject({ code: 'params_invalid' });
    await expect(user({}, 721)).rejects.toMatchObject({ code: 'params_invalid' });
    await expect(user({}, 1.5)).rejects.toMatchObject({ code: 'params_invalid' });
    await expect(user({ [hidden]: 'read' })).rejects.toMatchObject({ code: 'epoch_stale' });
    await expect(createInvite(w.context, w.org, input({ kind: 'device', wgs: [], hours: 24 }))).rejects.toMatchObject({
      code: 'params_invalid',
    });
    // without the org key
    (orgEntry(w.store.getState().orgs, w.org).keys as Map<string, unknown>).delete(`org ${w.org} 1`);
    await expect(user({})).rejects.toMatchObject({ code: 'epoch_stale' });
    const member = await setup('bob');
    await expect(
      createInvite(member.w.context, member.w.org, member.input({ kind: 'user', role: 'member', wgs: {}, hours: 72 })),
    ).rejects.toMatchObject({ code: 'forbidden' });
  });

  it('rebuilds on the re-read log with the same code after seq_conflict and checks the answer', async () => {
    const { w, a, input } = await setup();
    w.hub.before.push(() => w.hub.foreign(a.cy!, { type: 'role', subject: a.bob!.id, role: 'admin' }));
    const created = await createInvite(w.context, w.org, input({ kind: 'user', role: 'member', wgs: {}, hours: 72 }));
    expect(w.context.refreshLog).toHaveBeenCalledTimes(1);
    expect(created.code).toBe((await createInviteCode()).code);
    expect(w.world.state.invites.get(created.inviteId)!.seq).toBe(w.world.log!.head.seq);
    w.client.on(`POST /v1/orgs/${w.org}/invites`, () => ({ invite_id: 'other', seq: 99 }));
    const fresh = { ...input({ kind: 'user', role: 'member', wgs: {}, hours: 72 }), code: () => new Uint8Array(16) };
    await expect(createInvite(w.context, w.org, fresh)).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on(`POST /v1/orgs/${w.org}/invites`, () => null);
    await expect(createInvite(w.context, w.org, fresh)).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('offers the expiries of ADR-005 and invites only into workgroups whose key it holds', async () => {
    const { w, one, hidden } = await setup();
    expect(EXPIRY_HOURS[0]).toBe(1);
    expect(EXPIRY_HOURS.at(-1)).toBe(720);
    expect(EXPIRY_HOURS).toContain(USER_EXPIRY_HOURS);
    expect(EXPIRY_HOURS).toContain(DEVICE_EXPIRY_HOURS);
    expect(HOUR_MS).toBe(3_600_000);
    const held = keyRing(w.store, w.org);
    expect(invitable(w.world.state, held, one)).toBe(true);
    expect(invitable(w.world.state, held, hidden)).toBe(false);
    expect(invitable(w.world.state, held, 'unknown')).toBe(false);
  });
});

/** The code text of the fixed test code. */
async function createInviteCode() {
  const { formatCode } = await import('../../../src/core/crypto/invite');
  return { code: formatCode(new Uint8Array(16).fill(3)) };
}

describe('open invitations and revocation', () => {
  it('lists only invitations the verified log knows as open, newest first', async () => {
    const { w, input } = await setup();
    const first = await createInvite(w.context, w.org, input({ kind: 'user', role: 'member', wgs: {}, hours: 72 }));
    const second = await createInvite(w.context, w.org, {
      ...input({ kind: 'user', role: 'member', wgs: {}, hours: 1 }),
      code: () => new Uint8Array(16).fill(5),
    });
    const device = await createInvite(w.context, w.org, {
      ...input({ kind: 'device', wgs: [w.world.defaultWg], hours: 24 }),
      code: () => new Uint8Array(16).fill(6),
    });
    // the hub claims an unknown invitation is open and marks one as used: neither is listed
    w.hub.invites.set('forged', { ...w.hub.invites.get(first.inviteId)!, invite_id: 'forged' });
    const listed = await openInvites(w.context, w.org, 'user');
    expect(listed.map((row) => row.invite_id)).toEqual([second.inviteId, first.inviteId]);
    expect(listed[0]!.expires_at).toBe(second.expires);
    expect((await openInvites(w.context, w.org, 'device')).map((r) => r.invite_id)).toEqual([device.inviteId]);
    w.hub.invites.get(first.inviteId)!.state = 'used';
    expect((await openInvites(w.context, w.org, 'user')).map((r) => r.invite_id)).toEqual([second.inviteId]);
    // expired by the clock
    w.hub.now = T0 + 2 * HOUR_MS;
    expect(await openInvites(w.context, w.org, 'user')).toEqual([]);
  });

  it('gives the inviter the state of each user invitation (KOR-36)', async () => {
    const { w, a, input } = await setup();
    const made = [];
    for (const fill of [3, 4, 5, 7])
      made.push(
        await createInvite(w.context, w.org, {
          ...input({ kind: 'user', role: 'member', wgs: {}, hours: 72 }),
          code: () => new Uint8Array(16).fill(fill),
        }),
      );
    const [open, opened, registered, gone] = made.map((item) => item.inviteId) as [string, string, string, string];
    await createInvite(w.context, w.org, {
      ...input({ kind: 'device', wgs: [w.world.defaultWg], hours: 24 }),
      code: () => new Uint8Array(16).fill(6),
    });
    w.hub.invites.set(opened, { ...w.hub.invites.get(opened)!, opened_at: '2026-11-23T20:00:00.000Z' });
    w.hub.invites.set(registered, { ...w.hub.invites.get(registered)!, registered: true, opened_at: null });
    // the hub no longer lists one as open (used elsewhere, or a lie): not shown while the log has no accept
    w.hub.invites.set(gone, { ...w.hub.invites.get(gone)!, state: 'used' });
    const rows = await inviteStatuses(w.context, w.org);
    expect(rows.map((row) => [row.inviteId, row.status, row.acceptedBy, row.openedAt])).toEqual([
      [registered, 'registered', undefined, null],
      [opened, 'opened', undefined, '2026-11-23T20:00:00.000Z'],
      [open, 'open', undefined, null],
      // the admissions of the setup (bob, cy): accepted in the verified log, with the member who joined
      [rows[3]!.inviteId, 'accepted', a.cy!.id, null],
      [rows[4]!.inviteId, 'accepted', a.bob!.id, null],
    ]);
    expect(rows).toHaveLength(5);
    // expired by the clock: nothing is listed any more
    w.hub.now = T0 + 100 * HOUR_MS;
    expect(await inviteStatuses(w.context, w.org)).toEqual([]);
  });

  it('refuses a malformed list and an unverified org', async () => {
    const { w } = await setup();
    w.client.on(`GET /v1/orgs/${w.org}/invites`, () => ({ invites: [{ invite_id: 1 }] }));
    await expect(openInvites(w.context, w.org, 'user')).rejects.toBeInstanceOf(HubError);
    w.client.on(`GET /v1/orgs/${w.org}/invites`, () => null);
    await expect(openInvites(w.context, w.org, 'user')).rejects.toBeInstanceOf(HubError);
    w.client.on(`GET /v1/orgs/other/invites`, () => ({ invites: [] }));
    await expect(openInvites(w.context, 'other', 'user')).rejects.toMatchObject({ code: 'log_empty' });
    await expect(inviteStatuses(w.context, 'other')).rejects.toMatchObject({ code: 'log_empty' });
    w.client.on(`GET /v1/orgs/${w.org}/invites`, () => null);
    await expect(inviteStatuses(w.context, w.org)).rejects.toBeInstanceOf(HubError);
  });

  it('revokes once, refuses unknown invitations and checks the answer', async () => {
    const { w, a, input } = await setup();
    const created = await createInvite(w.context, w.org, input({ kind: 'user', role: 'member', wgs: {}, hours: 72 }));
    const route = `POST /v1/orgs/${w.org}/invites/${created.inviteId}/revoke`;
    w.client.on(route, async (body) => {
      const result = await w.hub.append(a.ada!.id, {
        expected_seq: (body as { entry: { body: { seq: number } } }).entry.body.seq,
        entries: [(body as { entry: never }).entry],
        wraps: [],
        events: [],
      });
      return { seq: result.head.seq };
    });
    await revokeInvite(w.context, w.org, created.inviteId);
    expect(w.world.state.invites.get(created.inviteId)!.revokedSeq).toBe(w.world.log!.head.seq);
    expect(orgEntry(w.store.getState().orgs, w.org).log!.head).toEqual(w.world.log!.head);
    await revokeInvite(w.context, w.org, created.inviteId);
    expect(w.client.of(route)).toHaveLength(1);
    await expect(revokeInvite(w.context, w.org, 'unknown')).rejects.toMatchObject({ code: 'not_found' });
    const again = await createInvite(w.context, w.org, {
      ...input({ kind: 'user', role: 'member', wgs: {}, hours: 72 }),
      code: () => new Uint8Array(16).fill(7),
    });
    w.client.on(`POST /v1/orgs/${w.org}/invites/${again.inviteId}/revoke`, () => ({ seq: 1 }));
    await expect(revokeInvite(w.context, w.org, again.inviteId)).rejects.toMatchObject({ code: 'unavailable' });
    w.client.on(`POST /v1/orgs/${w.org}/invites/${again.inviteId}/revoke`, () => null);
    await expect(revokeInvite(w.context, w.org, again.inviteId)).rejects.toMatchObject({ code: 'unavailable' });
    const member = await setup('bob');
    await expect(revokeInvite(member.w.context, member.w.org, 'x')).rejects.toMatchObject({ code: 'forbidden' });
  });
});
