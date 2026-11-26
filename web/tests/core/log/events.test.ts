// Event rules on the reader side and the hub prediction (04-crypto.md sections 4.3, 5.12, 6.4): E3 matrix lookups,
// the header field rules, E5 at `lh` and at receipt, E6 with the 600 s grace rule (inclusive, never for removed or
// excluded authors), E7 session device and E9 replay, plus opening a checked event with its scope key.
import { beforeAll, describe, expect, it } from 'vitest';
import { utf8, fromUtf8 } from '../../../src/core/encoding/bytes';
import { CryptoError } from '../../../src/core/crypto/errors';
import { newHeader, sealEvent, type HeaderInput } from '../../../src/core/crypto/event';
import {
  checkEpoch,
  checkEvent,
  checkEventHub,
  checkReplay,
  GRACE_MS,
  kindRule,
  openCheckedEvent,
} from '../../../src/core/log/events';
import { actors, type Actor, idOf, T0, TS, World } from './world';

const code = async (promise: Promise<unknown>) => {
  try {
    await promise;
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return null;
};
const codeOf = (run: () => unknown) => code(Promise.resolve().then(run));

let alice: Actor, bob: Actor, device: Actor;
let world: World;
let wg: string;

beforeAll(async () => {
  [alice, bob, device] = await actors('alice', 'bob', 'device');
  world = await World.create(alice);
  wg = await idOf('events wg');
  await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
  await world.admitUser(alice, bob, 'member', { [wg]: 'write' });
  await world.admitDevice(alice, device, [wg]);
});

async function sealed(author: Actor, input: Partial<HeaderInput> & { kind: string }, epoch = 1) {
  const scope = input.scope ?? 'wg';
  const scopeId = input.scope_id ?? (scope === 'wg' ? wg : world.org);
  const fields = newHeader({
    id: await idOf(`event ${author.name} ${input.kind} ${input.ref ?? ''} ${input.session ?? ''}`),
    org: world.org,
    scope,
    scope_id: scopeId,
    epoch,
    author: author.id,
    ts: TS,
    lh: world.log!.head.seq,
    ...(scope === 'wg' ? { wg: scopeId } : {}),
    ...input,
  });
  const key = world.ring(alice.id)(scope, scopeId, epoch) ?? world.ring(bob.id)(scope, scopeId, epoch)!;
  return sealEvent(key, fields, utf8('{"x":1}'), author.identity.sig.privateKey);
}

describe('reader checks', () => {
  it('opens a checked event with the scope key of its epoch', async () => {
    const task = await idOf('task');
    const event = await sealed(bob, { kind: 'prompt', task });
    const opened = await openCheckedEvent(event, world.log!, world.ring(bob.id)('wg', wg, 1)!);
    expect(fromUtf8(opened.plaintext)).toBe('{"x":1}');
    expect(opened.checked.rule.right).toBe('wg_write');
  });

  it('applies E7 when the reader knows the session device', async () => {
    const session = await idOf('session');
    const event = await sealed(device, { kind: 'session.output', task: await idOf('task'), session });
    await checkEvent(event, world.log!, { sessionDevice: device.id });
    await checkEvent(event, world.log!);
    expect(await code(checkEvent(event, world.log!, { sessionDevice: bob.id }))).toBe('author_not_writer');
    expect(await code(checkEvent(event, world.log!, { excluded: [device.id] }))).toBe('author_not_writer');
  });

  it('checks the header field rules of the matrix', async () => {
    const profile = await sealed(alice, { kind: 'wg.profile', scope: 'org', scope_id: world.org, wg });
    await checkEvent(profile, world.log!);
    const member = await sealed(bob, { kind: 'member.profile', scope: 'org', scope_id: world.org, ref: bob.id });
    await checkEvent(member, world.log!);
    const other = await sealed(bob, { kind: 'member.profile', scope: 'org', scope_id: world.org, ref: alice.id });
    expect(await code(checkEvent(other, world.log!))).toBe('header_invalid');
    const read = await sealed(device, { kind: 'platform.read', ref: 'tasks' });
    await checkEvent(read, world.log!);
    const op = await sealed(device, { kind: 'operation.result', ref: 'not-a-uuid' });
    expect(await code(checkEvent(op, world.log!))).toBe('header_invalid');
    const board = await sealed(bob, { kind: 'whiteboard.entry', ref: await idOf('entry') });
    await checkEvent(board, world.log!);
    expect(
      await code(checkEvent(await sealed(bob, { kind: 'org.profile', scope: 'org', scope_id: world.org }), world.log!)),
    ).toBe('author_not_writer');
  });

  it('lets any member write a topic with a UUID ref and empty wg and task (ADR-100 section 3)', async () => {
    const ref = await idOf('topic');
    const topic = await sealed(bob, { kind: 'topic.profile', scope: 'org', scope_id: world.org, ref });
    expect((await checkEvent(topic, world.log!)).rule.right).toBe('org_member');
    await checkEventHub(topic, world.log!, T0, null, null);
    const noRef = await sealed(bob, { kind: 'topic.profile', scope: 'org', scope_id: world.org, ref: 'x' });
    expect(await code(checkEvent(noRef, world.log!))).toBe('header_invalid');
    const withTask = await sealed(bob, {
      kind: 'topic.profile',
      scope: 'org',
      scope_id: world.org,
      ref,
      task: await idOf('task'),
    });
    expect(await code(checkEvent(withTask, world.log!))).toBe('header_invalid');
    expect(await codeOf(() => kindRule('topic.profile', 'org', 'device'))).toBe('kind_not_allowed');
  });

  it('refuses an unknown combination (kind_not_allowed) and a foreign org (header_invalid)', async () => {
    expect(await codeOf(() => kindRule('prompt', 'wg', 'device'))).toBe('kind_not_allowed');
    const event = await sealed(bob, { kind: 'prompt', task: await idOf('task') });
    const foreign = { ...event, header: { ...event.header, org: await idOf('other org') } };
    expect(await code(checkEvent(foreign, world.log!))).toBe('header_invalid');
  });
});

describe('hub prediction (E5 at receipt, E6, E7)', () => {
  it('needs the session to run on the author device', async () => {
    const event = await sealed(device, { kind: 'session.status', task: await idOf('task'), session: await idOf('s') });
    await checkEventHub(event, world.log!, T0, null, device.id);
    expect(await code(checkEventHub(event, world.log!, T0, null, null))).toBe('author_not_writer');
  });

  it('accepts the previous epoch within the grace period after the rotation, inclusive', async () => {
    const before = world.log!;
    await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 2 }]);
    const task = await idOf('task');
    const old = await sealed(bob, { kind: 'prompt', task, lh: before.head.seq });
    const rotatedAt = T0;
    await checkEventHub(old, world.log!, rotatedAt + GRACE_MS, rotatedAt, null);
    expect(await code(checkEventHub(old, world.log!, rotatedAt + GRACE_MS + 1, rotatedAt, null))).toBe('epoch_stale');
    expect(await code(checkEventHub(old, world.log!, T0, null, null))).toBe('epoch_stale');
    const header = old.header as Record<string, unknown>;
    expect(await codeOf(() => checkEpoch(world.state, header, T0, T0, [bob.id]))).toBe('epoch_stale');
    expect(await codeOf(() => checkEpoch(world.state, { ...header, epoch: 3 }, T0, T0))).toBe('epoch_stale');
    checkEpoch(world.state, { ...header, epoch: 2 }, T0, null);
  });

  it('never covers a removed author by the grace period and refuses its events at receipt', async () => {
    const task = await idOf('task');
    const lh = world.log!.head.seq;
    const event = await sealed(bob, { kind: 'prompt', task, lh }, 2);
    await world.batch(alice, [{ type: 'remove', subject: bob.id }]);
    await checkEvent(event, world.log!);
    expect(await code(checkEventHub(event, world.log!, T0, T0, null))).toBe('author_not_writer');
    const header = { ...(event.header as Record<string, unknown>), epoch: world.state.epoch('wg', wg) - 1 };
    expect(await codeOf(() => checkEpoch(world.state, header, T0, T0))).toBe('epoch_stale');
  });
});

describe('E9 replay', () => {
  it('is idempotent for identical bytes and a conflict otherwise', async () => {
    const event = { header: { id: 'x' }, ct: 'AA', sig: 'BB' };
    expect(checkReplay(null, event)).toBe(false);
    expect(checkReplay(undefined, event)).toBe(false);
    expect(checkReplay({ ...event }, event)).toBe(true);
    expect(await codeOf(() => checkReplay({ ...event, ct: 'AB' }, event))).toBe('event_conflict');
  });
});
