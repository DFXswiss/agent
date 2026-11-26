// Unit checks of src/core/log that neither the vectors nor the scenarios reach: the compiled root key, the platform
// state accessors, platform devices assigned to a second workgroup, wrap and key helpers, orderings and the error
// plumbing (only `CryptoError` is a refusal; any other error is a defect and propagates).
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { CryptoError } from '../../../src/core/crypto/errors';
import { buildWraps, recipientKem } from '../../../src/core/log/catchup';
import {
  buildEntries,
  buildPlatformEntries,
  checkLogEntry,
  logBody,
  refusalCode,
  signLogEntry,
  succeeds,
} from '../../../src/core/log/entries';
import { appendPlatform, UNKNOWN, verifyPlatformLog } from '../../../src/core/log/platform';
import { buildBatch, checkBatch } from '../../../src/core/log/rotation';
import { compareScopes, compareText } from '../../../src/core/log/state';
import { appendEntries, apply, verifyLog } from '../../../src/core/log/verify';
import { checkWrap, compareSlots, credReaders, slotJson, slotOf } from '../../../src/core/log/wraps';
import { identityValid, parseAccountCode } from '../../../src/core/crypto/identity';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { VECTOR_FILES } from '../crypto/vector-suite';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';
import { actors, type Actor, idOf, inviteFor, keySource, platformWith, seed, T0, TS, World } from './world';

const code = async (promise: Promise<unknown>) => {
  try {
    await promise;
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return null;
};

let alice: Actor, bob: Actor, pdev: Actor;

beforeAll(async () => {
  [alice, bob, pdev] = await actors('alice', 'bob', 'pdev');
});

describe('platform log state', () => {
  it('exposes roles, heads and statuses and appends with hub semantics', async () => {
    const plat = await platformWith([pdev]);
    const state = plat.state;
    expect(state.status(bob.id)).toBe(UNKNOWN);
    expect(state.hasRole(bob.id, 'inviter')).toBe(false);
    expect(state.headValue()).toEqual({ seq: state.seq, hash: state.head });
    expect(state.platformAccess(alice.id, false)).toBe(false);
    const access = state
      .with({
        orgAccess: new Map([
          ['b', true],
          ['a', false],
        ]),
      })
      .projection().org_access;
    expect(Object.keys(access as object)).toEqual(['a', 'b']);
    expect(await code(appendPlatform(null, [], plat.root))).toBe('log_empty');
    const fresh = await appendPlatform(null, plat.entries, plat.root);
    expect(fresh.state.head).toBe(state.head);
    const root = await ed25519KeyPair(await seed('platform root'));
    const grant = await buildPlatformEntries(state.headValue(), root.privateKey, [
      { type: 'role_grant', subject: bob.id, role: 'inviter' },
    ]);
    expect((await appendPlatform(fresh, grant, plat.root)).state.hasRole(bob.id, 'inviter')).toBe(true);
    expect(await code(appendPlatform(fresh, [...grant, ...grant], plat.root))).toBe('log_chain');
  });
});

describe('platform switches and operator subscriptions (ADR-079)', () => {
  it('are off without an entry, follow the latest entry and fall back to the access switch', async () => {
    const plat = await platformWith([]);
    const [a, b, f, o] = await Promise.all(['org a', 'org b', 'org f', 'org o'].map((label) => idOf(label)));
    expect(plat.state.policy('open_platform_access')).toBe(false);
    expect(plat.state.operatorSubscribed(a!)).toBe(false);
    const root = await ed25519KeyPair(await seed('platform root'));
    const fresh = await appendPlatform(null, plat.entries, plat.root);
    const entries = await buildPlatformEntries(plat.state.headValue(), root.privateKey, [
      { type: 'platform_policy', name: 'open_platform_access', on: true },
      { type: 'org_platform_access', org: f, granted: false },
      { type: 'org_operator_subscriptions', org: b, granted: true },
      { type: 'org_operator_subscriptions', org: a, granted: false },
      { type: 'role_grant', subject: bob.id, role: 'operator' },
    ]);
    const log = await appendPlatform(fresh, entries, plat.root);
    const after = log.state;
    expect(after.policy('open_platform_access')).toBe(true);
    expect(after.policy('open_platform_invites')).toBe(false);
    // without an entry of its own an org follows the switch; an explicit entry wins
    expect(after.platformAccess(o!, false)).toBe(true);
    expect(after.platformAccess(f!, false)).toBe(false);
    expect(after.platformAccess(f!, true)).toBe(true);
    expect(after.operatorSubscribed(b!)).toBe(true);
    expect(after.operatorSubscribed(a!)).toBe(false);
    expect(after.hasRole(bob.id, 'operator')).toBe(true);
    const projection = after.projection();
    expect(projection.policies).toEqual({ open_platform_access: true });
    expect(projection.operator_subscriptions).toEqual({ [a!]: false, [b!]: true });
    const off = await buildPlatformEntries(after.headValue(), root.privateKey, [
      { type: 'platform_policy', name: 'open_platform_access', on: false },
    ]);
    expect((await appendPlatform(log, off, plat.root)).state.platformAccess(o!, false)).toBe(false);
  });
});

describe('support roster (ADR-096)', () => {
  it('binds the role to the certification valid at the grant', async () => {
    const plat = await platformWith([]);
    const root = await ed25519KeyPair(await seed('platform root'));
    let log = await appendPlatform(null, plat.entries, plat.root);
    const add = async (...list: { type: string; [key: string]: unknown }[]) => {
      log = await appendPlatform(
        log,
        await buildPlatformEntries(log.state.headValue(), root.privateKey, list),
        plat.root,
      );
      return log.state;
    };
    expect(Object.hasOwn(log.state.projection(), 'support')).toBe(false);
    const grantAlice = await buildPlatformEntries(log.state.headValue(), root.privateKey, [
      { type: 'role_grant', subject: alice.id, role: 'support' },
    ]);
    // the role needs root-certified identity keys
    expect(await code(appendPlatform(log, grantAlice, plat.root))).toBe('log_rights');
    const granted = await add(
      { type: 'user_certify', user: bob.id, pub: bob.identity.public() },
      { type: 'role_grant', subject: bob.id, role: 'support' },
    );
    expect(granted.supportRoster().get(bob.id)).toEqual({ id: bob.id, ...bob.identity.public() });
    expect(granted.projection().support).toEqual([bob.id]);
    // a decertified person leaves the roster, its history stays verifiable
    const left = await add({ type: 'user_decertify', user: bob.id });
    expect(left.supportRoster().has(bob.id)).toBe(false);
    expect(left.supportAuthors().has(bob.id)).toBe(true);
    expect(left.projection().support).toEqual([]);
    // a renewed certification needs a new grant
    const renewed = await add({ type: 'user_certify', user: bob.id, pub: bob.identity.public() });
    expect(renewed.supportRoster().has(bob.id)).toBe(false);
    const again = await add(
      { type: 'role_revoke', subject: bob.id, role: 'support' },
      { type: 'role_grant', subject: bob.id, role: 'support' },
    );
    expect(again.supportRoster().get(bob.id)?.sig).toBe(bob.identity.public().sig);
    expect(again.supportBound.get(bob.id)).toBe(again.seq - 2);
  });
});

describe('platform devices in an org', () => {
  it('keeps the certification of an assigned platform device and refuses another one', async () => {
    const plat = await platformWith([pdev]);
    const world = await World.create(alice, { private: true });
    world.platform = plat.state;
    world.add(pdev);
    const seq = plat.seqs.get(pdev.id)!;
    const wg = await idOf('second');
    await world.batch(alice, [
      { type: 'wg_create', wg, epoch: 1 },
      { type: 'device_assign', device: pdev.id, wg: world.defaultWg, platform_seq: seq },
      { type: 'device_assign', device: pdev.id, wg, platform_seq: seq },
    ]);
    expect(world.state.deviceWgs(pdev.id)).toHaveLength(2);
    const signer = { id: alice.id, key: alice.identity.sig.privateKey };
    const wg3 = await idOf('third');
    const wrong = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      { type: 'wg_create', wg: wg3, epoch: 1 },
      { type: 'device_assign', device: pdev.id, wg: wg3, platform_seq: seq - 1 },
    ]);
    expect(await code(verifyLog([...world.log!.entries, ...wrong], alice.identity.public().sig, plat.state))).toBe(
      'log_rights',
    );
    // hub mode: platform access and a valid certification
    const ok = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      { type: 'wg_create', wg: wg3, epoch: 1 },
      { type: 'device_assign', device: pdev.id, wg: wg3, platform_seq: seq },
    ]);
    await appendEntries(world.log, ok, '', { platform: plat.state, now: T0, platformAccess: true });
    expect(await code(appendEntries(world.log, ok, '', { platform: plat.state, now: T0 }))).toBe('log_rights');
  });
});

describe('wrap helpers', () => {
  it('refuses a non-integer epoch before looking at the wrapper', async () => {
    const world = await World.create(alice);
    const wrap = { scope: 'wg', scope_id: world.defaultWg, epoch: 1.5, recipient_id: alice.id, wrap: 'AA', sig: 'AA' };
    expect(await code(checkWrap(wrap, bob.id, world.log!))).toBe('header_invalid');
  });

  it('skips slots without a held key or a known recipient and finds recipient keys in the verified states', async () => {
    const world = await World.create(alice);
    const key = keySource('u')();
    const slots = [
      { scope: 'org', scope_id: world.org, epoch: 1, recipient: alice.id },
      { scope: 'org', scope_id: world.org, epoch: 1, recipient: bob.id },
      { scope: 'org', scope_id: world.org, epoch: 2, recipient: alice.id },
    ];
    const ring = (_s: string, _i: string, epoch: number) => (epoch === 1 ? key : null);
    const wraps = await buildWraps(alice.identity, slots, ring, [world.state]);
    expect(wraps.map((w) => [w.recipient_id, w.epoch])).toEqual([[alice.id, 1]]);
    expect(recipientKem([world.state], bob.id)).toBeNull();
    expect(recipientKem([world.state], alice.id)).toBe(alice.identity.public().kem);
  });

  it('orders slots and converts the API form', () => {
    const a = { scope: 'wg', scope_id: 'x', epoch: 2, recipient: 'r' };
    expect(compareSlots(a, { ...a })).toBe(0);
    expect(compareSlots(a, { ...a, recipient: 's' })).toBeLessThan(0);
    expect(compareSlots(a, { ...a, epoch: 10 })).toBeLessThan(0);
    expect(compareSlots(a, { ...a, scope_id: 'w' })).toBeGreaterThan(0);
    expect(compareSlots(a, { ...a, scope: 'org' })).toBeGreaterThan(0);
    expect(slotOf(slotJson(a))).toEqual(a);
    expect(compareText('a', 'a')).toBe(0);
    expect(compareScopes(['org', 'b'], ['org', 'a'])).toBe(1);
  });
});

describe('builders', () => {
  it('fills the time from the clock and chains platform entries after a head', async () => {
    vi.useFakeTimers({ now: T0 });
    try {
      const body = logBody({ org: await idOf('o'), head: null }, alice.id, { type: 'remove', subject: bob.id });
      expect(body.ts).toBe(TS);
      const root = await ed25519KeyPair(await seed('platform root'));
      const head = { seq: 4, hash: alice.identity.public().sig };
      const ui = await inviteFor('ui');
      const invite = { type: 'user_invite', invite_id: ui.inviteId, pk_inv: ui.pkInv, expires: TS };
      const [entry] = await buildPlatformEntries(head, root.privateKey, [invite]);
      expect(entry!.body).toMatchObject({ seq: 5, prev: head.hash, ts: TS });
    } finally {
      vi.useRealTimers();
    }
  });

  it('refuses to build an entry a verifier would refuse for its form', async () => {
    const signer = { id: alice.id, key: alice.identity.sig.privateKey };
    const bad = buildEntries({ org: await idOf('o'), head: null }, signer, [{ type: 'remove', subject: 'nobody' }]);
    expect(await code(bad)).toBe('header_invalid');
    expect(() => checkLogEntry({ body: { type: 'remove' }, sig: 'x', inv_sig: 'y' })).toThrow(CryptoError);
  });

  it('builds a batch without held keys (wraps the author cannot create are left to catch-up)', async () => {
    const world = await World.create(alice);
    await world.admitUser(alice, bob);
    const wg = await idOf('nokeys');
    await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
    const built = await buildBatch({
      log: world.log,
      author: alice,
      entries: [{ type: 'grant', subject: bob.id, wg, access: 'read' }],
      ts: TS,
    });
    expect(built.body.wraps).toEqual([]);
    expect(world.state.readableScopes(bob.id).map(([scope]) => scope)).toEqual(['org', 'wg']);
  });
});

describe('error plumbing', () => {
  it('passes errors that are not refusals on', async () => {
    expect(refusalCode(new CryptoError('log_chain'))).toBe('log_chain');
    expect(() => refusalCode(new TypeError('defect'))).toThrow(TypeError);
    await expect(succeeds(() => Promise.reject(new RangeError('defect')))).rejects.toThrow(RangeError);
    expect(await succeeds(() => Promise.reject(new CryptoError('log_chain')))).toBe(false);
    expect(await succeeds(() => Promise.resolve())).toBe(true);
  });
});

describe('direct admission (ADR-060)', () => {
  const data = VECTOR_FILES.log;
  const admitLog = (count: number) => data.logs.admit.slice(0, count) as unknown[];

  it('refuses an admit without the verified platform state (log_admit)', async () => {
    const platform = await verifyPlatformLog(data.logs.platform, data.platform_root);
    const before = (await verifyLog(admitLog(27), data.founder, platform.state)).state;
    expect(await apply(before, data.logs.admit[27], data.founder, { platform: platform.state })).toBeDefined();
    expect(await code(apply(before, data.logs.admit[27], data.founder))).toBe('log_admit');
  });

  it('gives the credential key of an admitted user to no device of that org until it consents', async () => {
    const platform = await verifyPlatformLog(data.logs.platform, data.platform_root);
    const state = (await verifyLog(admitLog(31), data.founder, platform.state)).state;
    const bob = data.logs.admit[29].body.subject as string;
    expect(state.member(bob)!.consented).toBe(false);
    expect([...credReaders([state], bob)]).toEqual([bob]);
    const consented = state.with({
      principals: new Map(state.principals).set(bob, { ...state.member(bob)!, consented: true }),
    });
    expect(credReaders([consented], bob).size).toBe(3);
  });

  it('owes no wraps for an admitted user that leaves in the same batch', async () => {
    const platform = (await verifyPlatformLog(data.logs.platform, data.platform_root)).state;
    const log = await verifyLog(admitLog(27), data.founder, platform);
    const dave = data.identities.dave;
    const admitted = await appendEntries(log, [data.logs.admit[27]], data.founder, { platform });
    const leave = await signLogEntry(
      logBody({ org: log.state.org, head: admitted.head }, dave.id, { type: 'remove', subject: dave.id }, TS),
      (await ed25519KeyPair(b64uDecode(dave.sig_sk))).privateKey,
    );
    const batch = await checkBatch(log, [data.logs.admit[27], leave], [], data.identities.alice.id, { platform });
    expect(batch.log.state.member(dave.id)).toBeNull();
    expect(await code(checkBatch(log, [data.logs.admit[27]], [], data.identities.alice.id, { platform }))).toBe(
      'log_admit',
    );
  });

  it('builds an admit batch with the wraps the admitted user needs (org and default workgroup)', async () => {
    const root = await ed25519KeyPair(await seed('platform root'));
    const entries = await buildPlatformEntries(null, root.privateKey, [
      { type: 'root', pub: b64uEncode(root.publicKey) },
      { type: 'user_certify', user: alice.id, pub: alice.identity.public() },
      { type: 'user_certify', user: bob.id, pub: bob.identity.public() },
    ]);
    const world = await World.create(alice);
    world.platform = (await verifyPlatformLog(entries, b64uEncode(root.publicKey))).state;
    world.add(bob);
    const built = await world.batch(alice, [
      {
        type: 'admit',
        subject: bob.id,
        pub: bob.identity.public(),
        platform_seq: 2,
        id_sig: '',
        by_platform_seq: 1,
        by_id_sig: '',
        role: 'member',
        wgs: {},
      },
    ]);
    expect(built.body.wraps.map((wrap) => [wrap.scope, wrap.recipient_id])).toEqual([
      ['org', bob.id],
      ['wg', bob.id],
    ]);
    expect(world.state.member(bob.id)).toMatchObject({ consented: false, role: 'member' });
  });

  it('refuses an account code that is not text', () => {
    expect(() => parseAccountCode(42)).toThrow(new CryptoError('account_code_invalid'));
  });

  it('passes errors of an identity proof check that are not refusals on', async () => {
    const pub = {
      get kem(): string {
        throw new TypeError('defect');
      },
      sig: '',
    };
    await expect(identityValid(data.platform_root, data.ids.org, pub, '')).rejects.toThrow(TypeError);
  });
});
