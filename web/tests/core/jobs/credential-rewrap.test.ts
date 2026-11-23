// U-WEB-sync (WEB-10): job 5 of 06-web.md section 11 – the rotation planning from the verified logs (losses of
// eligibility by unassignment, removal, ungrant, platform decertification and the user's own removal from an org; the
// start of the current CK epoch with the clock-skew margin; unknown or future starts fail closed; losses handled once
// per sign-in) and the CK rotation itself (wraps for every reader now, records re-encrypted by the user, records of a
// device that lost eligibility deleted, unreadable records block the rotation, version conflicts retried once, one
// acting queue and one rotation at a time, nothing after a sign-out).
import { describe, expect, it } from 'vitest';
import { parseJson } from '../../../src/core/encoding/json';
import { formatTime } from '../../../src/core/encoding/time';
import { openCredential } from '../../../src/core/crypto/credential';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';
import { unwrapKey, verifyWrap } from '../../../src/core/crypto/wrap';
import { backgroundJobs, type JobContext } from '../../../src/core/jobs';
import {
  credentialLosses,
  exclusive,
  handledFor,
  holderLosses,
  job,
  type Loss,
  removalLosses,
  rewrap,
  rotateCredentialKey,
  rotationOwed,
  SKEW_MS,
  verifiedLogs,
} from '../../../src/core/jobs/credential-rewrap';
import { buildPlatformEntries, entryHash } from '../../../src/core/log/entries';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { blockOrg, ensureOrg, setLog, setPlatform } from '../../../src/core/store/slices/orgs';
import { actor, idOf, seed as seedOf, T0, World } from '../log/world';
import { API_KEY, credWorld, type CredWorld, LOGIN_TOKEN, MAILBOX, seed, SUBSCRIPTION } from '../credentials/world';

/** The epoch start of the world's CK_1 self-wrap (`credWorld`). */
const START = Date.parse('2026-11-21T11:00:00.000Z');
const AFTER = '2026-11-21T12:30:00.000Z';
const BEFORE = '2026-11-21T10:00:00.000Z';

async function unassign(t: CredWorld, ts = AFTER, device = t.device.id) {
  await t.world.batch(t.me, [{ type: 'device_unassign', device, wg: t.wg }], { ts });
  setLog(t.store, t.org, t.world.log!);
}

function jobContext(t: CredWorld): JobContext {
  return {
    store: t.store,
    client: t.client,
    rootKey: t.platform.root,
    now: () => t.clock.now,
    persist: {
      loadPinned: async () => null,
      savePinned: async () => undefined,
      loadPlatformHead: async () => null,
      savePlatformHead: async () => undefined,
    },
  };
}

const run = (t: CredWorld, generation = t.store.generation, org = t.org) => ({
  org,
  trigger: 'log' as const,
  generation,
});

async function decertify(t: CredWorld, device: string, ts: string) {
  const root = await ed25519KeyPair(await seedOf('platform root'));
  const last = t.platform.entries.at(-1)!;
  const head = { seq: t.platform.entries.length - 1, hash: await entryHash(last) };
  const more = await buildPlatformEntries(head, root.privateKey, [{ type: 'device_decertify', device }], ts);
  const log = await verifyPlatformLog([...t.platform.entries, ...more], t.platform.root);
  setPlatform(t.store, log);
  return log;
}

async function assignPlatform(t: CredWorld) {
  const [dev1] = t.devices;
  await t.world.batch(
    t.me,
    [{ type: 'device_assign', device: dev1!.id, wg: t.wg, platform_seq: t.platform.seqs.get(dev1!.id)! }],
    { ts: BEFORE },
  );
  setLog(t.store, t.org, t.world.log!);
  return dev1!;
}

/** A second org of `founder` (`World.create` always takes the same IDs). */
async function secondOrg(founder: CredWorld['me']): Promise<World> {
  const world = new World(await idOf('second org'), await idOf('second default wg'));
  world.add(founder);
  await world.batch(founder, [
    {
      type: 'genesis',
      subject: founder.id,
      pub: founder.identity.public(),
      default_wg: world.defaultWg,
      private: false,
    },
  ]);
  return world;
}

const loss = (at: number, key = `k${at}`): Loss => ({ at, key });

describe('credentialLosses', () => {
  it('finds devices that lost eligibility and when', async () => {
    const t = await credWorld();
    expect(credentialLosses(verifiedLogs(t.store), t.store.getState().orgs.platform, t.me.id).size).toBe(0);
    await unassign(t);
    const losses = credentialLosses(verifiedLogs(t.store), t.store.getState().orgs.platform, t.me.id);
    expect([...losses]).toEqual([[t.device.id, { at: Date.parse(AFTER), key: `${t.device.id} ${Date.parse(AFTER)}` }]]);
  });

  it('forgets a device that is a reader again and keeps the newest loss', async () => {
    const t = await credWorld();
    await unassign(t, BEFORE);
    await t.world.batch(t.me, [{ type: 'device_assign', device: t.device.id, wg: t.wg }], { ts: BEFORE });
    setLog(t.store, t.org, t.world.log!);
    expect(credentialLosses(verifiedLogs(t.store), null, t.me.id).size).toBe(0);
    await unassign(t, AFTER);
    expect(credentialLosses(verifiedLogs(t.store), null, t.me.id).get(t.device.id)!.at).toBe(Date.parse(AFTER));
  });

  it('counts a decertified platform device from its decertification', async () => {
    const t = await credWorld();
    const dev1 = await assignPlatform(t);
    expect(credentialLosses(verifiedLogs(t.store), t.store.getState().orgs.platform, t.me.id).size).toBe(0);
    const log = await decertify(t, dev1.id, AFTER);
    expect(credentialLosses(verifiedLogs(t.store), log, t.me.id).get(dev1.id)!.at).toBe(Date.parse(AFTER));
    // an excluded device without a decertification entry (impossible with a verified platform log) is no loss
    const fake = { ...log, entries: [], state: log.state } as unknown as typeof log;
    expect(credentialLosses(verifiedLogs(t.store), fake, t.me.id).size).toBe(0);
  });

  it('leaves blocked orgs out and orders the logs by org', async () => {
    const t = await credWorld();
    const other = await secondOrg(t.me);
    ensureOrg(t.store, other.org);
    setLog(t.store, other.org, other.log!);
    expect(verifiedLogs(t.store).map((log) => log.state.org)).toEqual([t.org, other.org].sort());
    blockOrg(t.store, t.org, { code: 'log_fork', evidence: {} } as never);
    expect(verifiedLogs(t.store).map((log) => log.state.org)).toEqual([other.org]);
  });
});

describe('removalLosses', () => {
  it('counts the devices of an org the user no longer belongs to, unless another org keeps them', async () => {
    const t = await credWorld();
    const logs = verifiedLogs(t.store);
    expect(removalLosses(logs, new Set([t.org]), t.me.id, 5).size).toBe(0);
    expect([...removalLosses(logs, new Set(), t.me.id, 5)]).toEqual([
      [t.device.id, { at: 5, key: `${t.device.id} left ${t.org}` }],
    ]);
    const other = await secondOrg(t.me);
    await other.admitDevice(t.me, t.device, [other.defaultWg]);
    expect(other.org).not.toBe(t.org);
    expect(removalLosses([...logs, other.log!], new Set([other.org]), t.me.id, 5).size).toBe(0);
  });
});

describe('holderLosses', () => {
  const holders = (epoch: number, ...devices: string[]) => ({ epoch, devices });
  const member = (org: string, seq?: number) => ({ org_id: org, ...(seq === undefined ? {} : { log_head: { seq } }) });

  it('counts the holders of the current CK that no verified log of a member org entitles', async () => {
    const t = await credWorld();
    const gone = await actor('device of a left org');
    const losses = holderLosses(
      t.store.getState().orgs,
      [member(t.org)],
      holders(1, t.device.id, gone.id, t.me.id),
      t.me.id,
      9,
    );
    expect([...losses]).toEqual([[gone.id, { at: 9, key: `${gone.id} holds` }]]);
    expect(holderLosses(t.store.getState().orgs, [member(t.org)], null, t.me.id, 9).size).toBe(0);
    const u = await credWorld({ noPlatform: true });
    expect([
      ...holderLosses(u.store.getState().orgs, [member(u.org)], holders(1, u.device.id, gone.id), u.me.id, 9).keys(),
    ]).toEqual([gone.id]);
  });

  it('waits while an org the hub lists is not verified yet or its log is behind the hub', async () => {
    const t = await credWorld();
    const gone = await actor('device of a left org');
    const head = verifiedLogs(t.store)[0]!.head.seq;
    const current = holders(1, gone.id);
    expect(holderLosses(t.store.getState().orgs, [member(t.org, head)], current, t.me.id, 9).size).toBe(1);
    expect(holderLosses(t.store.getState().orgs, [member(t.org, head + 1)], current, t.me.id, 9).size).toBe(0);
    expect(holderLosses(t.store.getState().orgs, [member(t.org), member('not-loaded')], current, t.me.id, 9).size).toBe(
      0,
    );
    ensureOrg(t.store, 'loading');
    expect(holderLosses(t.store.getState().orgs, [member(t.org), member('loading')], current, t.me.id, 9).size).toBe(0);
    // a blocked org entitles nobody (fail closed): its devices count as lost
    blockOrg(t.store, 'loading', { code: 'log_fork', evidence: {} } as never);
    expect(holderLosses(t.store.getState().orgs, [member(t.org), member('loading')], current, t.me.id, 9).size).toBe(1);
    blockOrg(t.store, t.org, { code: 'log_fork', evidence: {} } as never);
    expect([
      ...holderLosses(t.store.getState().orgs, [member(t.org)], holders(1, t.device.id), t.me.id, 9).keys(),
    ]).toEqual([t.device.id]);
  });

  it('leaves out a decertified platform device as a reader', async () => {
    const t = await credWorld();
    const dev1 = await assignPlatform(t);
    expect(holderLosses(t.store.getState().orgs, [member(t.org)], holders(1, dev1.id), t.me.id, 9).size).toBe(0);
    await decertify(t, dev1.id, AFTER);
    expect([...holderLosses(t.store.getState().orgs, [member(t.org)], holders(1, dev1.id), t.me.id, 9).keys()]).toEqual(
      [dev1.id],
    );
  });
});

describe('rotationOwed', () => {
  it('owes a rotation for a pending loss not clearly older than the epoch start; unknown starts fail closed', () => {
    const now = START + 3600_000;
    expect(rotationOwed(new Map(), START, now)).toBe(false);
    expect(rotationOwed(new Map([['d', loss(START + 1)]]), START, now)).toBe(true);
    // a signer's clock behind the hub's by up to SKEW_MS still owes the rotation
    expect(rotationOwed(new Map([['d', loss(START - SKEW_MS)]]), START, now)).toBe(true);
    expect(rotationOwed(new Map([['d', loss(START - SKEW_MS - 1)]]), START, now)).toBe(false);
    expect(rotationOwed(new Map([['d', loss(START - SKEW_MS - 1)]]), null, now)).toBe(true);
    expect(rotationOwed(new Map([['d', loss(0)]]), now + SKEW_MS + 1, now)).toBe(true);
    expect(rotationOwed(new Map([['d', loss(START + 1, 'x')]]), START, now, new Set(['x']))).toBe(false);
  });
});

describe('rotateCredentialKey', () => {
  it('does nothing without a user, logs, losses or own key, or for a loss clearly older than the epoch', async () => {
    const bare = await credWorld();
    expect(await rotateCredentialKey({ ...bare.context, store: createStore() })).toBeNull();
    const t = await credWorld();
    expect(await rotateCredentialKey(t.context)).toBeNull();
    expect(t.client.of('GET /v1/wraps')).toHaveLength(0);
    await unassign(t, BEFORE);
    expect(await rotateCredentialKey(t.context)).toBeNull();
    t.client.on('GET /v1/wraps', () => ({ wraps: [] }));
    expect(await rotateCredentialKey(t.context)).toBeNull();
    blockOrg(t.store, t.org, { code: 'log_fork', evidence: {} } as never);
    expect(await rotateCredentialKey(t.context)).toBeNull();
    expect(t.client.of('POST /v1/me/credentials/rotate')).toHaveLength(0);
  });

  it('rotates for a loss signed by a clock behind the hub (within the margin)', async () => {
    const t = await credWorld();
    await unassign(t, formatTime(START - 120_000));
    expect(await rotateCredentialKey(t.context)).toMatchObject({ status: 'rotated', epoch: 2 });
  });

  it('fails closed without or with a future epoch start, once per sign-in', async () => {
    const t = await credWorld();
    await unassign(t, BEFORE);
    t.hub.wraps[0] = { ...t.hub.wraps[0], created_at: undefined };
    const handled = new Set<string>();
    expect(await rotateCredentialKey(t.context, handled)).toMatchObject({ status: 'rotated', epoch: 2 });
    // the hub keeps hiding the start: the handled loss is not rotated again in this sign-in
    t.hub.wraps.forEach((wrap, index) => (t.hub.wraps[index] = { ...wrap, created_at: undefined }));
    expect(await rotateCredentialKey(t.context, handled)).toBeNull();
    const u = await credWorld();
    await unassign(u, BEFORE);
    u.hub.wraps[0] = { ...u.hub.wraps[0], created_at: '2027-01-01T00:00:00.000Z' };
    expect(await rotateCredentialKey(u.context)).toMatchObject({ status: 'rotated' });
  });

  it('wraps for every reader now, re-encrypts the records and deletes those of the device that left', async () => {
    const t = await credWorld();
    const stay = await actor('staying-device');
    await t.world.admitDevice(t.me, stay, [t.wg]);
    await seed(t, 'anthropic_api_key', API_KEY);
    await seed(t, 'claude_subscription', SUBSCRIPTION, { author: stay, version: 3 });
    await seed(t, 'mailbox', MAILBOX, { author: t.device, ts: BEFORE });
    await unassign(t);
    t.clock.now = T0 + 3600_000;
    const handled = new Set<string>();
    expect(await rotateCredentialKey(t.context, handled)).toEqual({
      status: 'rotated',
      epoch: 2,
      records: 2,
      dropped: 1,
    });
    expect(handled.has(`${t.device.id} ${Date.parse(AFTER)}`)).toBe(true);
    expect(t.client.of('DELETE /v1/me/credentials/mailbox')).toHaveLength(1);
    const [rotation] = t.hub.rotations;
    expect(rotation!.wraps.map((wrap) => wrap.recipient_id).sort()).toEqual([t.me.id, stay.id].sort());
    const mine = rotation!.wraps.find((wrap) => wrap.recipient_id === t.me.id)!;
    const slot = { scope: 'cred', scope_id: t.me.id, epoch: 2, recipient: t.me.id };
    await verifyWrap(t.me.identity.sigPublic(), slot, mine.wrap as string, mine.sig);
    const key = await unwrapKey(t.me.identity.kem, slot, mine.wrap as string);
    const byKind = Object.fromEntries(rotation!.records.map((record) => [record.header.kind, record]));
    expect(byKind.claude_subscription!.header).toMatchObject({
      version: 4,
      epoch: 2,
      author: t.me.id,
      ts: formatTime(T0 + 3600_000),
    });
    expect(parseJson(await openCredential(key, byKind.anthropic_api_key, t.me.identity.sigPublic()))).toEqual(API_KEY);
    // the next run finds the epoch newer than the loss
    expect(await rotateCredentialKey(t.context)).toBeNull();
  });

  it('re-encrypts a login token of the agent tool unchanged, with its expiry (ADR-056 section 7)', async () => {
    const t = await credWorld();
    await seed(t, 'claude_subscription', LOGIN_TOKEN);
    await unassign(t);
    t.clock.now = T0 + 3600_000;
    expect(await rotateCredentialKey(t.context)).toMatchObject({ status: 'rotated', epoch: 2, records: 1 });
    const [rotation] = t.hub.rotations;
    const mine = rotation!.wraps.find((wrap) => wrap.recipient_id === t.me.id)!;
    const slot = { scope: 'cred', scope_id: t.me.id, epoch: 2, recipient: t.me.id };
    const key = await unwrapKey(t.me.identity.kem, slot, mine.wrap as string);
    const [record] = rotation!.records;
    expect(parseJson(await openCredential(key, record!, t.me.identity.sigPublic()))).toEqual(LOGIN_TOKEN);
  });

  it('is blocked by a record it cannot open: nothing is deleted or rotated, the notice comes once', async () => {
    const t = await credWorld();
    await seed(t, 'anthropic_api_key', { key: 'a', extra: 1 });
    await seed(t, 'mailbox', MAILBOX, { author: t.device, ts: BEFORE });
    const stranger = await actor('stranger');
    await seed(t, 'github', { token: 'x' }, { author: stranger });
    t.hub.records.set('claude_subscription', { ...t.hub.records.get('github')!, record: 'junk' as never });
    await unassign(t);
    const handled = new Set<string>();
    expect(await rotateCredentialKey(t.context, handled)).toEqual({
      status: 'blocked',
      kinds: ['github', 'anthropic_api_key', 'claude_subscription'],
    });
    expect(await rotateCredentialKey(t.context, handled)).toBeNull();
    expect(t.client.of('DELETE /v1/me/credentials/mailbox')).toHaveLength(0);
    expect(t.client.of('POST /v1/me/credentials/rotate')).toHaveLength(0);
  });

  it('rotates after the user was removed from an org (the hub no longer lists it), for the user alone', async () => {
    const t = await credWorld();
    await seed(t, 'anthropic_api_key', API_KEY);
    t.client.on('GET /v1/me', () => ({ orgs: [] }));
    expect(await rotateCredentialKey(t.context)).toMatchObject({ status: 'rotated', records: 1 });
    expect(t.hub.rotations[0]!.wraps.map((wrap) => wrap.recipient_id)).toEqual([t.me.id]);
    const u = await credWorld();
    u.client.on('GET /v1/me', () => null);
    expect(await rotateCredentialKey(u.context)).toMatchObject({ status: 'rotated' });
    const v = await credWorld();
    v.client.on('GET /v1/me', () => ({ orgs: ['x', { org_id: 3 }] }));
    expect(await rotateCredentialKey(v.context)).toMatchObject({ status: 'rotated' });
  });

  it('rotates when a device of an org the user left holds the CK although no browser held that log', async () => {
    // the user is in two orgs, this browser holds only the first; the user is removed from the second
    const t = await credWorld();
    await seed(t, 'anthropic_api_key', API_KEY);
    const second = await secondOrg(t.me);
    const other = await actor('device of the second org');
    await second.admitDevice(t.me, other, [second.defaultWg]);
    let holders = [t.device.id, other.id];
    t.client.on('GET /v1/me', () => ({
      user_id: t.me.id,
      orgs: [{ org_id: t.org }],
      cred_holders: { epoch: t.hub.epoch, devices: holders },
    }));
    const handled = new Set<string>();
    expect(await rotateCredentialKey(t.context, handled)).toEqual({
      status: 'rotated',
      epoch: 2,
      records: 1,
      dropped: 0,
    });
    const recipients = t.hub.rotations[0]!.wraps.map((wrap) => wrap.recipient_id).sort();
    expect(recipients).toEqual([t.me.id, t.device.id].sort());
    expect(recipients).not.toContain(other.id);
    // the hub now names the holders of CK_2: nothing is owed any more
    holders = [t.device.id];
    expect(await rotateCredentialKey(t.context, handled)).toBeNull();
    expect(t.hub.rotations).toHaveLength(1);
  });

  it('deletes a record written by a holder of the left org instead of blocking, and waits for unverified orgs', async () => {
    const t = await credWorld();
    const second = await secondOrg(t.me);
    const other = await actor('device of the second org');
    await second.admitDevice(t.me, other, [second.defaultWg]);
    await seed(t, 'github', { token: 'x' }, { author: other });
    const me = (orgs: unknown[]) => () => ({
      user_id: t.me.id,
      orgs,
      cred_holders: { epoch: t.hub.epoch, devices: [t.device.id, other.id] },
    });
    // a fresh sign-in: the second org is still listed and not verified yet – nothing happens
    t.client.on('GET /v1/me', me([{ org_id: t.org }, { org_id: second.org }]));
    expect(await rotateCredentialKey(t.context)).toBeNull();
    // the log of the first org is behind the hub's head – nothing happens either
    const head = verifiedLogs(t.store)[0]!.head.seq;
    t.client.on('GET /v1/me', me([{ org_id: t.org, log_head: { seq: head + 1 } }]));
    expect(await rotateCredentialKey(t.context)).toBeNull();
    expect(t.client.of('POST /v1/me/credentials/rotate')).toHaveLength(0);
    t.client.on('GET /v1/me', me([{ org_id: t.org, log_head: { seq: head } }]));
    expect(await rotateCredentialKey(t.context)).toEqual({ status: 'rotated', epoch: 2, records: 0, dropped: 1 });
    expect(t.client.of('DELETE /v1/me/credentials/github')).toHaveLength(1);
  });

  it('rotates once per holder and sign-in, also when another view wraps the next CK for it again', async () => {
    const t = await credWorld();
    const gone = await actor('device of a blocked org');
    t.client.on('GET /v1/me', () => ({
      user_id: t.me.id,
      orgs: [{ org_id: t.org }],
      cred_holders: { epoch: t.hub.epoch, devices: [t.device.id, gone.id] },
    }));
    const handled = new Set<string>();
    expect(await rotateCredentialKey(t.context, handled)).toMatchObject({ status: 'rotated', epoch: 2 });
    expect(await rotateCredentialKey(t.context, handled)).toBeNull();
    expect(t.hub.rotations).toHaveLength(1);
  });

  it('counts only the log losses when the hub names holders of another epoch, even lagging after a rotation', async () => {
    const t = await credWorld();
    const gone = await actor('gone');
    await unassign(t);
    t.clock.now = T0 + 3600_000;
    let epoch = 5;
    t.client.on('GET /v1/me', () => ({
      user_id: t.me.id,
      orgs: [{ org_id: t.org }],
      cred_holders: { epoch, devices: [gone.id] },
    }));
    const handled = new Set<string>();
    expect(await rotateCredentialKey(t.context, handled)).toMatchObject({ status: 'rotated', epoch: 2 });
    expect(handled.has(`${gone.id} holds`)).toBe(false);
    // the hub still lists the holders of CK_1 after the rotation: a new sign-in does not rotate for them
    epoch = 1;
    expect(await rotateCredentialKey(t.context, new Set())).toBeNull();
    expect(t.hub.rotations).toHaveLength(1);
  });

  it('ignores holders of another epoch than the newest own CK and malformed holder lists', async () => {
    for (const cred_holders of [
      { epoch: 2, devices: ['gone'] },
      { epoch: '1', devices: ['gone'] },
      { epoch: 1, devices: 'gone' },
      { epoch: 1, devices: [3] },
      'gone',
    ]) {
      const t = await credWorld();
      t.client.on('GET /v1/me', () => ({ user_id: t.me.id, orgs: [{ org_id: t.org }], cred_holders }));
      expect(await rotateCredentialKey(t.context)).toBeNull();
    }
  });

  it('excludes a decertified platform device from the new epoch', async () => {
    const t = await credWorld();
    const dev1 = await assignPlatform(t);
    await decertify(t, dev1.id, AFTER);
    expect(await rotateCredentialKey(t.context)).toEqual({ status: 'rotated', epoch: 2, records: 0, dropped: 0 });
    const recipients = t.hub.rotations[0]!.wraps.map((wrap) => wrap.recipient_id).sort();
    expect(recipients).toEqual([t.me.id, t.device.id].sort());
  });

  it('works without a platform log and skips a record deleted in between', async () => {
    const t = await credWorld({ noPlatform: true });
    await seed(t, 'mailbox', MAILBOX);
    t.client.on('GET /v1/me/credentials/mailbox', () => {
      t.hub.records.delete('mailbox');
      throw new HubError('not_found', 404);
    });
    await unassign(t);
    expect(await rotateCredentialKey(t.context)).toEqual({ status: 'rotated', epoch: 2, records: 0, dropped: 0 });
    expect(t.hub.rotations[0]!.wraps.map((wrap) => wrap.recipient_id)).toEqual([t.me.id]);
  });

  it('stops before writing after a sign-out', async () => {
    const t = await credWorld();
    await seed(t, 'mailbox', MAILBOX, { author: t.device, ts: BEFORE });
    await unassign(t);
    expect(await rotateCredentialKey(t.context, new Set(), () => false)).toBeNull();
    let calls = 0;
    expect(await rotateCredentialKey(t.context, new Set(), () => calls++ === 0)).toBeNull();
    expect(t.client.of('POST /v1/me/credentials/rotate')).toHaveLength(0);
  });
});

describe('the job', () => {
  it('rotates with a toast, retries a version conflict once and passes other failures on', async () => {
    const t = await credWorld();
    await seed(t, 'anthropic_api_key', API_KEY);
    await unassign(t);
    let conflicts = 1;
    t.client.on('POST /v1/me/credentials/rotate', (body) => {
      if (conflicts-- > 0) throw new HubError('version_conflict', 409);
      return t.hub.rotate(body as never);
    });
    expect(await rewrap(jobContext(t), run(t))).toEqual({ status: 'rotated', epoch: 2, records: 1, dropped: 0 });
    expect(t.store.getState().credentials.notices).toMatchObject([{ result: { status: 'rotated', epoch: 2 } }]);
    expect(t.client.of('POST /v1/me/credentials/rotate')).toHaveLength(2);
    const u = await credWorld();
    await unassign(u);
    u.client.on('POST /v1/me/credentials/rotate', () => {
      throw new HubError('version_conflict', 409);
    });
    await expect(rewrap(jobContext(u), run(u))).rejects.toMatchObject({ code: 'version_conflict' });
    u.client.on('POST /v1/me/credentials/rotate', () => {
      throw new HubError('forbidden', 403);
    });
    await expect(rewrap(jobContext(u), run(u))).rejects.toMatchObject({ code: 'forbidden' });
    u.client.on('POST /v1/me/credentials/rotate', () => {
      throw new TypeError('network');
    });
    await expect(rewrap(jobContext(u), run(u))).rejects.toBeInstanceOf(TypeError);
    expect(u.store.getState().credentials.notices).toEqual([]);
  });

  it('acts only in the queue of the first verified org and leaves no toast after a sign-out', async () => {
    const t = await credWorld();
    expect(await rewrap(jobContext(t), run(t))).toBeNull();
    await unassign(t);
    expect(await rewrap(jobContext(t), run(t, t.store.generation, 'another-org'))).toBeNull();
    expect(await rewrap(jobContext(t), run(t, t.store.generation + 1))).toBeNull();
    expect(t.store.getState().credentials.notices).toEqual([]);
    const bare = createStore();
    expect(await rewrap({ ...jobContext(t), store: bare }, run(t, bare.generation))).toBeNull();
  });

  it('keeps handled losses per sign-in', () => {
    const store = createStore();
    const first = handledFor(store);
    first.add('x');
    expect(handledFor(store)).toBe(first);
    store.reset();
    expect(handledFor(store).has('x')).toBe(false);
  });

  it('runs one rotation at a time per store', async () => {
    const t = await credWorld();
    await unassign(t);
    const [first, second] = await Promise.all([rewrap(jobContext(t), run(t)), rewrap(jobContext(t), run(t))]);
    expect(first).toMatchObject({ epoch: 2 });
    expect(second).toBeNull();
    expect(t.hub.rotations).toHaveLength(1);
    const order: string[] = [];
    await Promise.all([
      exclusive(t.store, async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        order.push('a');
        throw new Error('fails');
      }).catch(() => order.push('a failed')),
      exclusive(t.store, async () => order.push('b')),
    ]);
    expect(order).toEqual(['a', 'a failed', 'b']);
  });

  it('is registered after catch-up on sign-in, log changes and the interval', async () => {
    const t = await credWorld();
    expect(job).toMatchObject({ name: 'credential-rewrap', order: 35, triggers: ['login', 'log', 'interval'] });
    const names = backgroundJobs().map((item) => item.name);
    expect(names.indexOf('credential-rewrap')).toBeGreaterThan(names.indexOf('catch-up'));
    await job.run(jobContext(t), run(t));
  });
});
