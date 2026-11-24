// ADR-067 (core part): the names of people – the directory's name resolution (own `member.profile` first, else the
// name a current admin gave the member from its current membership on, else the name of the invitation it joined
// with), the admin's `member.name` event (`giveName`), the settled invitations (`closeSettledInvites`, the `settled`
// status) and the own name written into an org opened without one (`ensureOwnName`, `ensureOwnNameOnce`).
import { describe, expect, it } from 'vitest';
import { ensureOwnName, ensureOwnNameOnce } from '../../../src/core/account/own-name';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { bodyJson } from '../../../src/core/encoding/json';
import { formatTime } from '../../../src/core/encoding/time';
import { newHeader, sealEvent } from '../../../src/core/crypto/event';
import { checkEvent, openCheckedEvent } from '../../../src/core/log/events';
import { epochKey, ORG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import { createInvite, inviteStatuses } from '../../../src/core/org/invites';
import { memberRows, selectOwnName } from '../../../src/core/org/members';
import { closeSettledInvites, giveName } from '../../../src/core/org/names';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { EMPTY_DIRECTORY, MEMBER_NAME, selectDirectory } from '../../../src/core/tasks/directory';
import { type Actor, EXPIRES, idOf, inviteFor, T0 } from '../log/world';
import { type OrgWorld, orgWorld } from './world';

const HUB_KEY = { id: 'hub-test', public_key: b64uEncode(new Uint8Array(32).fill(9)) };

/** ada (admin), bob (member), cy (admin), dev1 (device); the hub answers profile pulls with empty pages. */
async function setup(as: 'ada' | 'bob' = 'ada') {
  const w = await orgWorld({
    as,
    setup: async (world, a) => {
      await world.admitUser(a.ada, a.bob, 'member', {});
      await world.admitUser(a.ada, a.cy, 'admin', {});
      await world.admitDevice(a.ada, a.dev1, [world.defaultWg]);
    },
  });
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null }));
  return { w, a: w.others as Record<'ada' | 'bob' | 'cy' | 'dev1' | 'dev2', Actor> };
}

let seq = 0;

/** A verified org-scope event as the reader puts it into the store. */
function named(
  org: string,
  kind: string,
  ref: string,
  author: string,
  name: unknown,
  lh = 0,
  status: EventEntry['status'] = 'verified',
): EventEntry {
  seq += 1;
  return {
    org,
    scopeId: org,
    scopeSeq: seq,
    receivedAt: null,
    event: {},
    status,
    code: null,
    header: { kind, ref, author, lh, wg: '', id: `e${seq}` },
    body: { display_name: name },
  };
}

const directory = (w: OrgWorld, org = w.org) => selectDirectory(org)(w.store.getState());

/** The user invitations of the world's log in log order (the accepts of bob and cy). */
const userInvites = (w: OrgWorld) => [...w.world.state.invites.values()].filter((invite) => invite.kind === 'user');

/** Appends an open user (or device) invitation to the log; returns its ID. */
async function invite(w: OrgWorld, kind: 'user' | 'device' = 'user'): Promise<string> {
  const secrets = await inviteFor(`names ${kind} ${w.world.log!.head.seq}`);
  const ada = w.others.ada;
  await w.world.batch(ada, [
    kind === 'user'
      ? {
          type: 'invite',
          invite_id: secrets.inviteId,
          pk_inv: secrets.pkInv,
          kind,
          role: 'member',
          wgs: {},
          expires: EXPIRES,
        }
      : {
          type: 'invite',
          invite_id: secrets.inviteId,
          pk_inv: secrets.pkInv,
          kind,
          wgs: { [w.world.defaultWg]: 'device' },
          expires: EXPIRES,
        },
  ]);
  await w.sync();
  return secrets.inviteId;
}

describe('directory: names of people', () => {
  it("ranks the own profile first: an admin's name never replaces it", async () => {
    const { w, a } = await setup();
    const head = w.world.log!.head.seq;
    putEvents(w.store, w.org, [
      named(w.org, 'member.profile', a.bob.id, a.bob.id, 'Bob'),
      named(w.org, MEMBER_NAME, a.bob.id, a.ada.id, 'Robert', head),
    ]);
    const found = directory(w);
    expect(found.members.get(a.bob.id)).toBe('Bob');
    expect(found.sources.get(a.bob.id)).toEqual({ source: 'own', by: null });
    expect(found.ownNames.get(a.bob.id)).toBe('Bob');
    // the own name in the member list and for the user itself
    const rows = memberRows(w.world.state, a.ada.id, found);
    expect(rows.find((row) => row.id === a.bob.id)).toMatchObject({ name: 'Bob', nameSource: { source: 'own' } });
    expect(rows.find((row) => row.id === a.ada.id)).toMatchObject({ name: null, nameSource: null });
  });

  it('takes the newest name of a current admin from the current membership on, nothing else', async () => {
    const { w, a } = await setup();
    const cy = w.world.state.member(a.cy.id)!;
    const head = w.world.log!.head.seq;
    putEvents(w.store, w.org, [
      // written before cy's current membership: ignored
      named(w.org, MEMBER_NAME, a.cy.id, a.ada.id, 'Before', cy.joinedSeq - 1),
      named(w.org, MEMBER_NAME, a.cy.id, a.ada.id, 'Cyrus', cy.joinedSeq),
      // a member is no admin: ignored, even as the newest
      named(w.org, MEMBER_NAME, a.cy.id, a.bob.id, 'By bob', head),
      // not a name, and not verified: ignored
      named(w.org, MEMBER_NAME, a.cy.id, a.ada.id, 7, head),
      named(w.org, MEMBER_NAME, a.cy.id, a.ada.id, 'Unverified', head, 'unverifiable'),
      // a name of a device: no person, never shown
      named(w.org, MEMBER_NAME, a.dev1.id, a.ada.id, 'Box', head),
      named(w.org, MEMBER_NAME, a.bob.id, a.bob.id, 'Self-named', head),
    ]);
    const found = directory(w);
    expect(found.members.get(a.cy.id)).toBe('Cyrus');
    expect(found.sources.get(a.cy.id)).toEqual({ source: 'admin', by: a.ada.id });
    expect(found.members.has(a.dev1.id)).toBe(false);
    expect(found.members.has(a.bob.id)).toBe(false);
    // an admin-given name is never the member's own
    expect(found.ownNames.has(a.cy.id)).toBe(false);
    expect(selectOwnName(w.org, a.cy.id)(w.store.getState())).toBe('');
    expect(memberRows(w.world.state, a.ada.id, found).find((row) => row.id === a.cy.id)).toMatchObject({
      name: 'Cyrus',
      nameSource: { source: 'admin', by: a.ada.id },
    });
    // the same store and log: the same directory
    expect(directory(w)).toBe(found);
  });

  it('ignores the name of an admin who was demoted or removed since', async () => {
    const { w, a } = await setup();
    const head = w.world.log!.head.seq;
    putEvents(w.store, w.org, [named(w.org, MEMBER_NAME, a.bob.id, a.cy.id, 'Bobby', head)]);
    expect(directory(w).members.get(a.bob.id)).toBe('Bobby');
    await w.world.batch(a.ada, [{ type: 'role', subject: a.cy.id, role: 'member' }]);
    await w.sync();
    expect(directory(w).members.has(a.bob.id)).toBe(false);
    await w.world.batch(a.ada, [{ type: 'role', subject: a.cy.id, role: 'admin' }]);
    await w.sync();
    expect(directory(w).members.get(a.bob.id)).toBe('Bobby');
    await w.world.batch(a.ada, [{ type: 'remove', subject: a.cy.id }]);
    await w.sync();
    expect(directory(w).members.has(a.bob.id)).toBe(false);
  });

  it('names invitations and the member who joined with one; the admin-given name ranks before it', async () => {
    const { w, a } = await setup();
    const [bobs, cys] = userInvites(w).map((item) => ({ inviteId: item.inviteId })) as [
      { inviteId: string },
      { inviteId: string },
    ];
    const open = await invite(w);
    const device = await invite(w, 'device');
    const head = w.world.log!.head.seq;
    putEvents(w.store, w.org, [
      named(w.org, MEMBER_NAME, bobs.inviteId, a.ada.id, 'Bob from the invitation', 0),
      named(w.org, MEMBER_NAME, cys.inviteId, a.ada.id, 'Cy from the invitation', 0),
      named(w.org, MEMBER_NAME, a.cy.id, a.ada.id, 'Cy given', head),
      named(w.org, MEMBER_NAME, open, a.ada.id, 'Dora', head),
      named(w.org, MEMBER_NAME, open, a.bob.id, 'Not by an admin', head),
      named(w.org, MEMBER_NAME, device, a.ada.id, 'A device', head),
    ]);
    const found = directory(w);
    expect(found.invitations.get(open)).toEqual({ name: 'Dora', by: a.ada.id });
    expect(found.invitations.get(bobs.inviteId)).toEqual({ name: 'Bob from the invitation', by: a.ada.id });
    expect(found.invitations.has(device)).toBe(false);
    expect(found.members.get(a.bob.id)).toBe('Bob from the invitation');
    expect(found.sources.get(a.bob.id)).toEqual({ source: 'invitation', by: a.ada.id });
    expect(found.members.get(a.cy.id)).toBe('Cy given');
    expect(found.sources.get(a.cy.id)).toEqual({ source: 'admin', by: a.ada.id });
  });

  it('without a verified log shows only own and directly given names', async () => {
    const { w, a } = await setup();
    const other = 'org-without-log';
    expect(directory(w, other)).toBe(EMPTY_DIRECTORY);
    putEvents(w.store, other, [
      named(other, 'member.profile', a.ada.id, a.ada.id, 'Ada'),
      named(other, MEMBER_NAME, a.ada.id, a.bob.id, 'Given to ada'),
      named(other, MEMBER_NAME, a.cy.id, a.bob.id, 'Cy?'),
      named(other, MEMBER_NAME, a.cy.id, a.bob.id, 'Cy!'),
    ]);
    const found = directory(w, other);
    expect([...found.members]).toEqual([
      [a.ada.id, 'Ada'],
      [a.cy.id, 'Cy!'],
    ]);
    expect(found.sources.get(a.cy.id)).toEqual({ source: 'admin', by: a.bob.id });
    expect(found.invitations.size).toBe(0);
    expect(directory(w, other)).toBe(found);
  });
});

describe('giveName', () => {
  it('seals a member.name about a user or a user invitation that every member can check and read', async () => {
    const { w, a } = await setup();
    const open = await invite(w);
    await giveName(w.context, w.org, a.bob.id, '  Bob Example ');
    await giveName(w.context, w.org, open, 'Dora');
    const sent = w.hub.events.slice(-2);
    const key = w.world.ring(a.cy.id)(ORG, w.org, w.world.state.epoch(ORG, w.org))!;
    const opened = await Promise.all(sent.map((event) => openCheckedEvent(event, w.world.log!, key)));
    expect(opened.map(({ checked }) => [checked.header.kind, checked.header.ref, checked.header.author])).toEqual([
      [MEMBER_NAME, a.bob.id, a.ada.id],
      [MEMBER_NAME, open, a.ada.id],
    ]);
    expect(opened.map(({ plaintext }) => JSON.parse(new TextDecoder().decode(plaintext)))).toEqual([
      { display_name: 'Bob Example' },
      { display_name: 'Dora' },
    ]);
    // the hub sees no name; the reader gets the stored events
    expect(JSON.stringify(sent)).not.toContain('Bob Example');
    expect(w.ingested.map((item) => [item.org, item.scope, item.items[0]!.scope_seq])).toEqual([
      [w.org, w.org, 100],
      [w.org, w.org, 100],
    ]);
  });

  it('refuses an invalid name, a non-admin, an unknown subject or a device and a missing key', async () => {
    const { w, a } = await setup();
    const device = await invite(w, 'device');
    await expect(giveName(w.context, w.org, a.bob.id, '   ')).rejects.toMatchObject({ code: 'params_invalid' });
    await expect(giveName(w.context, w.org, a.bob.id, 'x'.repeat(81))).rejects.toMatchObject({
      code: 'params_invalid',
    });
    await expect(giveName(w.context, w.org, 'nobody', 'Name')).rejects.toMatchObject({ code: 'not_found' });
    await expect(giveName(w.context, w.org, a.dev1.id, 'Name')).rejects.toMatchObject({ code: 'not_found' });
    await expect(giveName(w.context, w.org, device, 'Name')).rejects.toMatchObject({ code: 'not_found' });
    const keys = w.store.getState().orgs.byId[w.org]!.keys as Map<string, unknown>;
    const saved = new Map(keys);
    keys.delete(epochKey(ORG, w.org, w.world.state.epoch(ORG, w.org)));
    await expect(giveName(w.context, w.org, a.bob.id, 'Name')).rejects.toMatchObject({ code: 'epoch_stale' });
    for (const [id, key] of saved) keys.set(id, key);
    expect(w.client.of('POST /v1/events')).toHaveLength(0);
    const member = await setup('bob');
    await expect(giveName(member.w.context, member.w.org, member.a.cy.id, 'Cy')).rejects.toMatchObject({
      code: 'forbidden',
    });
  });

  it("checks the hub's answer", async () => {
    const { w, a } = await setup();
    for (const answer of [
      null,
      { results: [] },
      { results: [{ scope_id: 'other', scope_seq: 1 }] },
      { results: [{ scope_id: w.org, scope_seq: 1.5 }] },
    ]) {
      w.client.on('POST /v1/events', () => answer);
      await expect(giveName(w.context, w.org, a.bob.id, 'Bob')).rejects.toBeInstanceOf(HubError);
    }
    expect(w.ingested).toHaveLength(0);
  });

  it('is refused by every reader when it names a device or comes from a non-admin', async () => {
    const { w, a } = await setup();
    const epoch = w.world.state.epoch(ORG, w.org);
    const key = w.world.ring(a.ada.id)(ORG, w.org, epoch)!;
    const event = async (author: Actor, ref: string) =>
      sealEvent(
        key,
        newHeader({
          id: await idOf(`event ${author.id} ${ref}`),
          org: w.org,
          scope: ORG,
          scope_id: w.org,
          epoch,
          author: author.id,
          kind: MEMBER_NAME,
          ts: formatTime(T0),
          lh: w.world.log!.head.seq,
          ref,
        }),
        bodyJson({ display_name: 'X' }),
        author.identity.sig.privateKey,
      );
    await expect(checkEvent(await event(a.ada, a.cy.id), w.world.log!)).resolves.toMatchObject({});
    await expect(checkEvent(await event(a.ada, a.dev1.id), w.world.log!)).rejects.toMatchObject({
      code: 'header_invalid',
    });
    await expect(checkEvent(await event(a.ada, 'nobody'), w.world.log!)).rejects.toMatchObject({
      code: 'header_invalid',
    });
    await expect(checkEvent(await event(a.bob, a.cy.id), w.world.log!)).rejects.toMatchObject({
      code: 'author_not_writer',
    });
  });
});

describe('settled invitations', () => {
  async function invitations() {
    const made = await setup();
    const { w } = made;
    const ids: string[] = [];
    for (const fill of [3, 4, 5, 6, 7])
      ids.push(
        (
          await createInvite(w.context, w.org, {
            request: { kind: 'user', role: 'member', wgs: {}, hours: 72 },
            directory: EMPTY_DIRECTORY,
            hubKey: HUB_KEY,
            origin: 'https://hub.example',
            code: () => new Uint8Array(16).fill(fill),
          })
        ).inviteId,
      );
    const revoked: string[] = [];
    for (const id of ids)
      w.client.on(`POST /v1/orgs/${w.org}/invites/${id}/revoke`, async (body) => {
        const entry = (body as { entry: { body: { seq: number } } }).entry;
        const result = await w.hub.append(w.me.id, {
          expected_seq: entry.body.seq,
          entries: [entry as never],
          wraps: [],
          events: [],
        });
        revoked.push(id);
        w.hub.invites.get(id)!.state = 'revoked';
        return { seq: result.head.seq };
      });
    const registeredBy = (id: string, user: string | null, registered = true) =>
      w.hub.invites.set(id, { ...w.hub.invites.get(id)!, registered, registered_user: user } as never);
    return { ...made, ids, revoked, registeredBy };
  }

  it('is the status of an open invitation whose registered account is a member now', async () => {
    const { w, a, ids, registeredBy } = await invitations();
    const [forBob, forCy, forStranger, forDevice, plain] = ids as [string, string, string, string, string];
    registeredBy(forBob, a.bob.id);
    registeredBy(forCy, a.cy.id, false);
    registeredBy(forStranger, 'not-a-member');
    registeredBy(forDevice, a.dev1.id);
    registeredBy(plain, null, false);
    const accepted = userInvites(w)[0]!.inviteId;
    w.hub.invites.set(accepted, {
      ...w.hub.invites.get(plain)!,
      invite_id: accepted,
      state: 'used',
      registered_user: a.cy.id,
    } as never);
    const rows = new Map((await inviteStatuses(w.context, w.org)).map((row) => [row.inviteId, row]));
    expect(rows.get(forBob)).toMatchObject({ status: 'settled', acceptedBy: a.bob.id });
    expect(rows.get(forCy)).toMatchObject({ status: 'settled', acceptedBy: a.cy.id });
    expect(rows.get(forStranger)).toMatchObject({ status: 'registered', acceptedBy: undefined });
    expect(rows.get(forDevice)).toMatchObject({ status: 'registered', acceptedBy: undefined });
    expect(rows.get(plain)).toMatchObject({ status: 'open', acceptedBy: undefined });
    // the verified accept decides an accepted invitation, whatever the hub says
    expect(rows.get(accepted)).toMatchObject({ status: 'accepted', acceptedBy: a.bob.id });
  });

  it('closes only the invitations the hub names for exactly that member', async () => {
    const { w, a, ids, revoked, registeredBy } = await invitations();
    const [forBob, againBob, forCy, forStranger] = ids as [string, string, string, string];
    registeredBy(forBob, a.bob.id);
    registeredBy(againBob, a.bob.id);
    registeredBy(forCy, a.cy.id);
    registeredBy(forStranger, 'not-a-member');
    expect((await closeSettledInvites(w.context, w.org, a.bob.id)).sort()).toEqual([forBob, againBob].sort());
    expect(revoked.sort()).toEqual([forBob, againBob].sort());
    expect(w.world.state.invites.get(forCy)!.revokedSeq).toBeNull();
    expect(w.world.state.invites.get(forStranger)!.revokedSeq).toBeNull();
    expect(await closeSettledInvites(w.context, w.org, 'not-a-member')).toEqual([]);
    expect(revoked).toHaveLength(2);
  });
});

describe('ensureOwnName', () => {
  /** The user's own name in another org (`other`, no verified log needed to read it). */
  const elsewhere = (w: OrgWorld, name = 'Ada') =>
    putEvents(w.store, 'other', [named('other', 'member.profile', w.me.id, w.me.id, name)]);

  it('writes the own name of another org into this one, checked and readable by every member', async () => {
    const { w, a } = await setup();
    elsewhere(w);
    expect(await ensureOwnName(w.context, w.org, [w.org, 'other'])).toBe('written');
    const [event] = w.client
      .of('POST /v1/events')
      .map((call) => (call.body as { events: { event: unknown }[] }).events[0]!.event);
    const key = w.world.ring(a.bob.id)(ORG, w.org, w.world.state.epoch(ORG, w.org))!;
    const { checked, plaintext } = await openCheckedEvent(event, w.world.log!, key);
    expect(checked.header).toMatchObject({ kind: 'member.profile', ref: a.ada.id, author: a.ada.id });
    expect(JSON.parse(new TextDecoder().decode(plaintext))).toEqual({ display_name: 'Ada' });
    expect(w.ingested.at(-1)).toMatchObject({ org: w.org, scope: w.org, items: [{ scope_seq: 100, event }] });
  });

  it('writes nothing with an own name here, without one elsewhere or without the current org key', async () => {
    const { w, a } = await setup();
    // an admin-given name is not the own one, here or elsewhere
    putEvents(w.store, w.org, [named(w.org, MEMBER_NAME, a.ada.id, a.cy.id, 'Given', w.world.log!.head.seq)]);
    putEvents(w.store, 'other', [named('other', MEMBER_NAME, a.ada.id, a.cy.id, 'Given elsewhere')]);
    expect(await ensureOwnName(w.context, w.org, [w.org, 'other', 'unreadable'])).toBe('unknown');
    elsewhere(w);
    const keys = w.store.getState().orgs.byId[w.org]!.keys as Map<string, unknown>;
    const saved = new Map(keys);
    keys.clear();
    expect(await ensureOwnName(w.context, w.org, ['other'])).toBe('no-key');
    for (const [id, key] of saved) keys.set(id, key);
    putEvents(w.store, w.org, [named(w.org, 'member.profile', a.ada.id, a.ada.id, 'Ada here')]);
    expect(await ensureOwnName(w.context, w.org, ['other'])).toBe('present');
    expect(w.client.of('POST /v1/events')).toHaveLength(0);
  });

  it("writes nothing when the pull fails or the org is not verified, and checks the hub's answer", async () => {
    const { w } = await setup();
    elsewhere(w);
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    await expect(ensureOwnName(w.context, w.org, ['other'])).rejects.toMatchObject({ code: 'unavailable' });
    await expect(ensureOwnName(w.context, 'unverified', ['other'])).rejects.toMatchObject({ code: 'log_empty' });
    expect(w.client.of('POST /v1/events')).toHaveLength(0);
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null }));
    w.client.on('POST /v1/events', () => ({ results: [{ scope_id: 'other', scope_seq: 1 }] }));
    await expect(ensureOwnName(w.context, w.org, ['other'])).rejects.toBeInstanceOf(HubError);
    w.client.on('POST /v1/events', () => null);
    await expect(ensureOwnName(w.context, w.org, ['other'])).rejects.toBeInstanceOf(HubError);
    expect(w.ingested.flatMap((item) => item.items)).toHaveLength(0);
  });

  it('runs once per user, org and store after a success, and again after a failure', async () => {
    const { w } = await setup();
    elsewhere(w);
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    expect(await ensureOwnNameOnce(w.context, w.org, ['other'])).toBeNull();
    expect(w.client.of('POST /v1/events')).toHaveLength(0);
    // a failure is tried again at the next open; a success only once
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null }));
    expect(await ensureOwnNameOnce(w.context, w.org, ['other'])).toBe('written');
    expect(await ensureOwnNameOnce(w.context, w.org, ['other'])).toBeNull();
    expect(w.client.of('POST /v1/events')).toHaveLength(1);
    // another store (a new sign-in) tries again, once
    const next = await setup();
    elsewhere(next.w);
    expect(await ensureOwnNameOnce(next.w.context, next.w.org, ['other'])).toBe('written');
    expect(await ensureOwnNameOnce(next.w.context, next.w.org, ['other'])).toBeNull();
    expect(next.w.client.of('POST /v1/events')).toHaveLength(1);
    // another org of the same store is its own attempt
    expect(await ensureOwnNameOnce(next.w.context, 'unverified', ['other'])).toBeNull();
  });
});
