// A malicious hub (04-crypto.md section 9, 06-web.md section 1 principle 1): what the browser refuses although the
// hub serves it – an injected member, swapped keys, a wrap from a principal without the right, an event of a
// non-writer, a rolled-back log and a forked head – and what the client never does on the hub's word (wrapping for
// an outsider offered by the catch-up answer).
import { beforeAll, describe, expect, it } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { CryptoError } from '../../../src/core/crypto/errors';
import { newHeader, sealEvent } from '../../../src/core/crypto/event';
import { signWrap, wrapKey } from '../../../src/core/crypto/wrap';
import { utf8 } from '../../../src/core/encoding/bytes';
import { buildEntries, logBody, signLogEntry } from '../../../src/core/log/entries';
import { catchUpWraps, confirmedSlots } from '../../../src/core/log/catchup';
import { checkEvent } from '../../../src/core/log/events';
import { refreshPlatformLog, verifyPlatformLog } from '../../../src/core/log/platform';
import { buildBatch } from '../../../src/core/log/rotation';
import { refreshLog, resolveLh, verifyLog } from '../../../src/core/log/verify';
import { checkWrap, verifyWraps } from '../../../src/core/log/wraps';
import { actor, actors, type Actor, idOf, inviteFor, keySource, platformWith, TS, World, EXPIRES } from './world';

const code = async (promise: Promise<unknown>) => {
  try {
    await promise;
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return null;
};

let alice: Actor, bob: Actor, mallory: Actor, hub: Actor, device: Actor;
let world: World;
let wg: string;

beforeAll(async () => {
  [alice, bob, mallory, hub, device] = await actors('alice', 'bob', 'mallory', 'hub', 'device');
  world = await World.create(alice);
  wg = await idOf('secret wg');
  await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
  await world.admitUser(alice, bob, 'member', { [wg]: 'read' });
  await world.admitDevice(alice, device, [wg]);
});

const founder = () => alice.identity.public().sig;

describe('injected member', () => {
  it('refuses an accept the hub forged without the invitation code (log_accept)', async () => {
    const secrets = await inviteFor('forged');
    await world.batch(alice, [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'user',
        role: 'admin',
        wgs: {},
        expires: EXPIRES,
      },
    ]);
    const signer = { id: mallory.id, key: mallory.identity.sig.privateKey, invite: hub.identity.sig.privateKey };
    const forged = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      {
        type: 'accept',
        invite_id: secrets.inviteId,
        subject: mallory.id,
        kind: 'user',
        pub: mallory.identity.public(),
      },
    ]);
    expect(await code(verifyLog([...world.log!.entries, ...forged], founder()))).toBe('log_accept');
  });

  it('refuses a grant the hub signed itself in the name of an admin (log_signature)', async () => {
    const body = logBody(
      { org: world.org, head: world.log!.head },
      alice.id,
      { type: 'grant', subject: bob.id, wg, access: 'write' },
      TS,
    );
    const forged = await signLogEntry(body, hub.identity.sig.privateKey);
    expect(await code(verifyLog([...world.log!.entries, forged], founder()))).toBe('log_signature');
  });
});

describe('swapped keys', () => {
  it('refuses a log whose genesis key is not the pinned founder key (log_genesis)', async () => {
    expect(await code(verifyLog(world.log!.entries, hub.identity.public().sig))).toBe('log_genesis');
  });

  it('refuses a platform log under another root (platform_root_mismatch)', async () => {
    const plat = await platformWith([device]);
    expect(await code(verifyPlatformLog(plat.entries, hub.identity.public().sig))).toBe('platform_root_mismatch');
  });

  it('refuses a platform device assignment that names the certification of another device (log_rights)', async () => {
    const pdev = await actor('pdev');
    const qdev = await actor('qdev');
    const plat = await platformWith([pdev, qdev]);
    const signer = { id: alice.id, key: alice.identity.sig.privateKey };
    const assign = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      { type: 'device_assign', device: pdev.id, wg, platform_seq: plat.seqs.get(qdev.id)! },
    ]);
    expect(await code(verifyLog([...world.log!.entries, ...assign], founder(), plat.state))).toBe('log_rights');
  });
});

describe('wraps from principals without the right', () => {
  it('refuses a wrap that names a member as wrapper but is signed by the hub (signature_invalid)', async () => {
    const target = { scope: 'wg', scope_id: wg, epoch: 1, recipient: mallory.id };
    const wrap = await wrapKey(mallory.identity.kemPublic(), target, keySource('x')());
    const record = {
      scope: 'wg',
      scope_id: wg,
      epoch: 1,
      recipient_id: bob.id,
      wrap,
      sig: await signWrap(hub.identity.sig.privateKey, { ...target, recipient: bob.id }, wrap),
    };
    expect(await code(checkWrap(record, alice.id, world.log!))).toBe('signature_invalid');
  });

  it('refuses a wrap to an outsider or by a non-holder (wrap_unauthorized) and the job ignores and counts them', async () => {
    const sign = async (by: Actor, recipient: string, scope = 'wg', scopeId = wg) => {
      const target = { scope, scope_id: scopeId, epoch: 1, recipient };
      const wrap = await wrapKey(device.identity.kemPublic(), target, keySource('y')());
      return {
        scope,
        scope_id: scopeId,
        epoch: 1,
        recipient_id: recipient,
        wrap,
        sig: await signWrap(by.identity.sig.privateKey, target, wrap),
      };
    };
    const outsider = await sign(alice, mallory.id);
    expect(await code(checkWrap(outsider, alice.id, world.log!))).toBe('wrap_unauthorized');
    const unknown = await sign(mallory, bob.id);
    expect(await code(checkWrap(unknown, mallory.id, world.log!))).toBe('wrap_unauthorized');
    const good = world.wraps.find((w) => w.scope === 'wg' && w.scope_id === wg && w.recipient_id === bob.id)!;
    const result = await verifyWraps(
      [
        { wrap: good, wrappedBy: bob.id },
        { wrap: outsider, wrappedBy: alice.id },
        { wrap: unknown, wrappedBy: mallory.id },
        { wrap: { ...good, sig: 'AA' }, wrappedBy: bob.id },
      ],
      world.log!,
    );
    expect(result.accepted.map((a) => a.slot.recipient)).toEqual([bob.id]);
    expect(result.ignored.map((i) => i.code)).toEqual(['wrap_unauthorized', 'wrap_unauthorized', 'signature_invalid']);
  });

  it('never wraps for a recipient the verified log does not entitle, whatever the catch-up answer says', async () => {
    const offered = [
      { scope: 'wg', scope_id: wg, epoch: 1, recipient: mallory.id },
      { scope: 'wg', scope_id: wg, epoch: 1, recipient: device.id },
      { scope: 'wg', scope_id: wg, epoch: 1, recipient: device.id },
      { scope: 'cred', scope_id: alice.id, epoch: 1, recipient: device.id },
      { scope: 'wg', scope_id: wg, epoch: 9, recipient: device.id },
    ];
    const malformed = [
      null,
      7,
      { ...offered[1], epoch: '1' },
      { ...offered[1], epoch: 1.5 },
      { ...offered[1], scope: 1 },
    ];
    expect(confirmedSlots(world.log!, alice.id, [...offered, ...malformed])).toEqual([offered[1]]);
    const wraps = await catchUpWraps(world.log!, alice.id, alice.identity, offered, world.ring(alice.id), null);
    expect(wraps.map((w) => w.recipient_id)).toEqual([device.id]);
    expect(wraps[0]!.wrap).not.toBe(b64uEncode(new Uint8Array(0)));
  });

  it("never wraps older epochs on the hub's request (history only through an explicit grant)", async () => {
    const other = await World.create(alice);
    const wg2 = await idOf('history wg');
    await other.batch(alice, [{ type: 'wg_create', wg: wg2, epoch: 1 }]);
    await other.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: wg2, epoch: 2 }]);
    await other.admitUser(alice, bob);
    await other.batch(alice, [{ type: 'grant', subject: bob.id, wg: wg2, access: 'read' }]);
    const old = { scope: 'wg', scope_id: wg2, epoch: 1, recipient: bob.id };
    const current = { ...old, epoch: 2 };
    expect(confirmedSlots(other.log!, alice.id, [old, current])).toEqual([current]);
    expect(other.currentKey(bob.id, 'wg', wg2)).not.toBeNull();
    expect(other.ring(bob.id)('wg', wg2, 1)).toBeNull();
  });

  it('refuses to create wraps without the platform state once the org has platform devices (log_rights)', async () => {
    const pdev = await actor('pdev');
    const plat = await platformWith([pdev]);
    const org = await World.create(alice, { private: true });
    org.platform = plat.state;
    org.add(pdev);
    await org.batch(alice, [
      { type: 'device_assign', device: pdev.id, wg: org.defaultWg, platform_seq: plat.seqs.get(pdev.id)! },
    ]);
    const slot = { scope: 'wg', scope_id: org.defaultWg, epoch: 1, recipient: pdev.id };
    expect(await code(catchUpWraps(org.log!, alice.id, alice.identity, [slot], org.ring(alice.id), null))).toBe(
      'log_rights',
    );
    const rotate = { type: 'rotate' as const, scope: 'wg' as const, scope_id: org.defaultWg, epoch: 2 };
    expect(await code(buildBatch({ log: org.log, author: alice, entries: [rotate], ts: TS }))).toBe('log_rights');
    expect(await catchUpWraps(org.log!, alice.id, alice.identity, [slot], org.ring(alice.id), plat.state)).toHaveLength(
      1,
    );
  });

  it('detects a platform log that drops a later decertification (log_rollback)', async () => {
    const pdev = await actor('pdev');
    const plat = await platformWith([pdev]);
    const stored = plat.state.headValue();
    expect(await code(refreshPlatformLog(stored, plat.entries.slice(0, -1), plat.root))).toBe('log_rollback');
    expect((await refreshPlatformLog(stored, plat.entries, plat.root)).state.head).toBe(stored.hash);
  });
});

describe('events of non-writers', () => {
  const prompt = async (author: Actor, scopeId: string) => {
    const fields = newHeader({
      id: await idOf(`event ${author.name}`),
      org: world.org,
      scope: 'wg',
      scope_id: scopeId,
      epoch: 1,
      wg: scopeId,
      task: await idOf('task'),
      author: author.id,
      kind: 'prompt',
      ts: TS,
      lh: world.log!.head.seq,
    });
    return sealEvent(
      world.ring(alice.id)('wg', scopeId, 1)!,
      fields,
      utf8('{"text":"hi"}'),
      author.identity.sig.privateKey,
    );
  };

  it('refuses a prompt of a reader without write access (author_not_writer)', async () => {
    expect(await code(checkEvent(await prompt(bob, wg), world.log!))).toBe('author_not_writer');
    expect(await code(checkEvent(await prompt(mallory, wg), world.log!))).toBe('author_not_writer');
    await checkEvent(await prompt(alice, wg), world.log!);
  });
});

describe('rollback and fork', () => {
  it('refuses a log that does not extend the stored head (log_rollback)', async () => {
    const entries = world.log!.entries;
    const stored = world.log!.head;
    expect(await code(refreshLog({ founder: founder(), head: stored }, entries.slice(0, -1)))).toBe('log_rollback');
    const other = await World.create(alice);
    expect(
      await code(refreshLog({ founder: founder(), head: { seq: 0, hash: stored.hash } }, other.log!.entries)),
    ).toBe('log_rollback');
    const ok = await refreshLog({ founder: founder(), head: { seq: 1, hash: world.log!.states[1]!.head } }, entries);
    expect(ok.head).toEqual(stored);
    expect((await refreshLog({ founder: founder(), head: null }, entries)).head).toEqual(stored);
  });

  it('refreshes for an event head beyond the verified one and refuses it if the refreshed log lacks it (log_fork)', async () => {
    const short = await verifyLog(world.log!.entries.slice(0, 2), founder());
    const lh = world.log!.head.seq;
    expect(await resolveLh(world.log!, lh, () => Promise.reject(new Error('no refresh')))).toBe(world.log);
    expect(await resolveLh(short, lh, async () => world.log!)).toBe(world.log);
    expect(await code(resolveLh(short, lh, async () => short))).toBe('log_fork');
  });
});
