// U-WEB-ui-tasks (send sequence, 06-web.md section 4.3, 03-api.md section 4.6.1): rights and platform-access cases of
// the automatic device assignment, the batch (`device_assign` + WK/OK wraps, then the CK wrap), conflicts and
// refreshes, the task request and its retry with the same IDs, follow-ups and "Retry".
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { CryptoError } from '../../../src/core/crypto/errors';
import { openEvent } from '../../../src/core/crypto/event';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { epochKey, WG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import { putEvents } from '../../../src/core/store/slices/events';
import { blockOrg, setPlatform } from '../../../src/core/store/slices/orgs';
import { putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import {
  assignFirstCandidate,
  certified,
  ensureDevice,
  latestPrompt,
  loadTask,
  prepareTask,
  PROMPT_MAX,
  retryPrompt,
  sendable,
  SendError,
  sendErrorCode,
  prepareFollowUp,
  sendFollowUp,
  sendTask,
  wrapCredentialKey,
  writableWorkgroups,
} from '../../../src/core/tasks/send';
import { candidate, sessionRow, taskRow, type TaskWorld, taskWorld } from './world';

const WG_PATH = (t: TaskWorld) => `GET /v1/workgroups/${t.wg}`;
const CANDIDATES = (t: TaskWorld) => `GET /v1/workgroups/${t.wg}/device-candidates`;
const LOG_POST = (t: TaskWorld) => `POST /v1/orgs/${t.org}/log`;

async function rejects(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return sendErrorCode(error);
  }
  throw new Error('expected a rejection');
}

/** Routes of a successful assignment: no device yet, the given candidates, the hub accepting the batch. */
function assignable(t: TaskWorld, candidates: unknown[] = [candidate(t, 0)]) {
  let assigned = 0;
  t.client
    .on(WG_PATH(t), () => ({ devices: Array.from({ length: assigned }, () => ({})) }))
    .on(CANDIDATES(t), () => ({ candidates }))
    .on(LOG_POST(t), async (body) => {
      const head = await headOf(t, body);
      assigned = 1;
      return { head };
    })
    .on('GET /v1/wraps', () => ({ wraps: [t.credWrap] }))
    .on('POST /v1/wraps', () => ({ stored: 1, ignored: 0 }));
}

describe('rights and inputs', () => {
  it('lists writable workgroups, the default first', async () => {
    const t = await taskWorld();
    expect(writableWorkgroups(null, t.me.id)).toEqual([]);
    expect(writableWorkgroups(t.world.state, null)).toEqual([]);
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: '0190f000-0000-7000-8000-00000000000a', epoch: 1 },
      { type: 'wg_create', wg: '0190f000-0000-7000-8000-000000000009', epoch: 1 },
      { type: 'wg_create', wg: '0190f000-0000-7000-8000-00000000000b', epoch: 1 },
    ]);
    await t.world.batch(t.ada, [
      { type: 'grant', subject: t.ada.id, wg: '0190f000-0000-7000-8000-00000000000b', access: 'read' },
    ]);
    expect(writableWorkgroups(t.world.state, t.ada.id)).toEqual([
      t.wg,
      '0190f000-0000-7000-8000-000000000009',
      '0190f000-0000-7000-8000-00000000000a',
    ]);
  });

  it('accepts prompts with text up to the limit', () => {
    expect(sendable('  \n ')).toBe(false);
    expect(sendable('Hi')).toBe(true);
    expect(sendable('x'.repeat(PROMPT_MAX))).toBe(true);
    expect(sendable('x'.repeat(PROMPT_MAX + 1))).toBe(false);
  });

  it('maps every failure to a catalog code', () => {
    expect(sendErrorCode(new SendError('forbidden'))).toBe('forbidden');
    expect(sendErrorCode(new HubError('no_device', 409))).toBe('no_device');
    expect(sendErrorCode(new CryptoError('log_rights'))).toBe('log_rights');
    expect(sendErrorCode(new Error('x'))).toBe('unavailable');
  });
});

describe('prepareTask', () => {
  it('seals task.meta and prompt with the current workgroup key at the verified head', async () => {
    const t = await taskWorld();
    const prepared = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: '  First line  \nsecond' });
    expect(prepared.title).toBe('First line');
    expect(prepared.body.task_id).toBe(prepared.taskId);
    expect(prepared.body.wg_id).toBe(t.wg);
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const [meta, prompt] = prepared.body.events as { header: Record<string, unknown> }[];
    expect(meta!.header).toMatchObject({
      kind: 'task.meta',
      task: prepared.taskId,
      lh: 0,
      scope_id: t.wg,
      session: '',
    });
    expect(prompt!.header).toMatchObject({ kind: 'prompt', task: prepared.taskId, ref: '' });
    const sig = b64uDecode(t.me.identity.public().sig);
    expect(JSON.parse(fromUtf8(await openEvent(key, meta, sig)))).toEqual({ title: 'First line' });
    expect(JSON.parse(fromUtf8(await openEvent(key, prompt, sig)))).toEqual({
      text: '  First line  \nsecond',
      attachments: [],
    });
  });

  it('sends the chosen GitHub identity with the task, none for automatic (ADR-076)', async () => {
    const t = await taskWorld();
    const id = '0190f000-0000-7000-8000-0000000000e1';
    const chosen = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x', githubIdentity: id });
    expect(chosen.body.github_identity).toBe(id);
    const automatic = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x' });
    expect(automatic.body).not.toHaveProperty('github_identity');
  });

  it('refuses without write access, without text, above the size limit and in a blocked org', async () => {
    const t = await taskWorld();
    const input = { org: t.org, wg: t.wg, prompt: 'x' };
    expect(await rejects(prepareTask(t.context, { ...input, wg: '0190f000-0000-7000-8000-00000000000f' }))).toBe(
      'forbidden',
    );
    expect(await rejects(prepareTask(t.context, { ...input, prompt: ' ' }))).toBe('request_invalid');
    expect(await rejects(prepareTask(t.context, { ...input, prompt: 'x'.repeat(PROMPT_MAX + 1) }))).toBe(
      'size_exceeded',
    );
    blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: 3 });
    expect(await rejects(prepareTask(t.context, input))).toBe('log_fork');
  });

  it('needs the signed-in identity and a verified log', async () => {
    const anonymous = await taskWorld();
    anonymous.store.update('me', (me) => ({ ...me, identity: null }));
    expect(await rejects(prepareTask(anonymous.context, { org: anonymous.org, wg: anonymous.wg, prompt: 'x' }))).toBe(
      'unauthorized',
    );
    const unknown = await taskWorld();
    expect(
      await rejects(
        prepareTask(unknown.context, { org: '0190f000-0000-7000-8000-0000000000ff', wg: unknown.wg, prompt: 'x' }),
      ),
    ).toBe('log_empty');
  });

  it('asks the jobs for a missing key once, then gives up with epoch_stale', async () => {
    const t = await taskWorld();
    const keys = t.store.getState().orgs.byId[t.org]!.keys;
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, keys: new Map() } },
    }));
    expect(await rejects(prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x' }))).toBe('epoch_stale');
    expect(t.context.refreshLog).toHaveBeenCalledWith(t.org);
    t.context.refreshLog.mockImplementation(async () => {
      t.store.update('orgs', (orgs) => ({
        ...orgs,
        byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, keys } },
      }));
      return true;
    });
    const prepared = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x' });
    expect(prepared.body.events).toHaveLength(2);
  });
});

describe('certified', () => {
  it('needs a valid certification with the listed keys', async () => {
    const t = await taskWorld();
    const platform = t.platform.state;
    expect(certified(platform, candidate(t, 0) as never)).toBe(true);
    expect(certified(platform, candidate(t, 0, { platform_seq: 0 }) as never)).toBe(false);
    expect(certified(platform, candidate(t, 0, { device_id: t.devices[1]!.id }) as never)).toBe(false);
    expect(certified(platform, candidate(t, 0, { pub: t.devices[1]!.identity.public() }) as never)).toBe(false);
    const kemOnly = { kem: t.devices[0]!.identity.public().kem, sig: t.devices[1]!.identity.public().sig };
    expect(certified(platform, candidate(t, 0, { pub: kemOnly }) as never)).toBe(false);
  });
});

describe('ensureDevice', () => {
  it('does nothing when the workgroup has a device', async () => {
    const t = await taskWorld();
    t.client.on(WG_PATH(t), () => ({ devices: [{ device_id: 'd' }] }));
    expect(await ensureDevice(t.context, t.org, t.wg)).toBeNull();
    expect(t.client.calls).toHaveLength(1);
  });

  it('refuses a malformed workgroup answer', async () => {
    const t = await taskWorld();
    t.client.on(WG_PATH(t), () => ({ devices: 'none' }));
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('unavailable');
    t.client.on(WG_PATH(t), () => null);
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('unavailable');
  });

  it('assigns the first certified candidate: device_assign, WK and OK wraps, then the own CK', async () => {
    const t = await taskWorld();
    let posted: { entries: { body: Record<string, unknown> }[]; wraps: Record<string, unknown>[] } | null = null;
    assignable(t, [candidate(t, 0, { pub: t.devices[1]!.identity.public() }), candidate(t, 1)]);
    const accept = t.client.routes.get(LOG_POST(t))!;
    t.client.on(LOG_POST(t), (body, target) => {
      posted = body as never;
      return accept(body, target);
    });
    let assigning = 0;
    const ms = await ensureDevice(t.context, t.org, t.wg, () => (assigning += 1));
    expect(ms).toBe(0);
    expect(assigning).toBe(1);
    const device = t.devices[1]!;
    expect(posted!.entries.map((entry) => entry.body.type)).toEqual(['device_assign']);
    expect(posted!.entries[0]!.body).toMatchObject({
      device: device.id,
      wg: t.wg,
      platform_seq: t.platform.seqs.get(device.id),
    });
    expect(posted!.wraps.map((wrap) => [wrap.scope, wrap.recipient_id])).toEqual([
      ['org', device.id],
      ['wg', device.id],
    ]);
    expect(t.store.getState().orgs.byId[t.org]!.log!.state.assigned(device.id, t.wg)).toBe(true);
    const [cred] = t.client.of('POST /v1/wraps');
    const record = (cred!.body as { wraps: Record<string, unknown>[] }).wraps[0]!;
    expect(record).toMatchObject({ scope: 'cred', scope_id: t.me.id, epoch: 1, recipient_id: device.id });
    const slot = { scope: 'cred', scope_id: t.me.id, epoch: 1, recipient: device.id };
    const opened = await unwrapKey(device.identity.kem, slot, record.wrap as string);
    expect(opened.reveal((raw) => [...raw])).toEqual(t.credKey.reveal((raw) => [...raw]));
  });

  it('assigns nothing for non-admins, orgs without platform access and workgroups without a certified candidate', async () => {
    const member = await taskWorld({ member: true });
    member.client.on(WG_PATH(member), () => ({ devices: [] }));
    expect(await ensureDevice(member.context, member.org, member.wg)).toBeNull();
    expect(await rejects(assignFirstCandidate(member.context, member.org, member.wg))).toBe('no_device_available');
    const shared = await taskWorld({ private: false });
    shared.client.on(WG_PATH(shared), () => ({ devices: [] }));
    expect(await ensureDevice(shared.context, shared.org, shared.wg)).toBeNull();
    const none = await taskWorld();
    assignable(none, [candidate(none, 0, { platform_seq: 0 }), { device_id: 1 }]);
    expect(await ensureDevice(none.context, none.org, none.wg)).toBeNull();
    expect(none.context.refreshLog).toHaveBeenCalledTimes(1);
    const malformed = await taskWorld();
    assignable(malformed);
    malformed.client.on(CANDIDATES(malformed), () => ({ candidates: 'x' }));
    expect(await rejects(assignFirstCandidate(malformed.context, malformed.org, malformed.wg))).toBe(
      'no_device_available',
    );
    const failing = await taskWorld();
    assignable(failing);
    failing.client.on(CANDIDATES(failing), () => {
      throw new HubError('unavailable', 503);
    });
    expect(await rejects(ensureDevice(failing.context, failing.org, failing.wg))).toBe('unavailable');
  });

  it('verifies the platform log first when the store has none, and assigns nothing if it stays missing', async () => {
    const t = await taskWorld({ noPlatform: true });
    assignable(t);
    expect(await ensureDevice(t.context, t.org, t.wg)).toBeNull();
    expect(t.context.refreshLog).toHaveBeenCalledTimes(1);
    const later = await taskWorld({ noPlatform: true });
    assignable(later);
    passHead(later);
    later.context.refreshLog.mockImplementation(async () => {
      setPlatform(later.store, await verifyPlatformLog(later.platform.entries, later.platform.root));
      return true;
    });
    expect(await ensureDevice(later.context, later.org, later.wg)).toBe(0);
  });

  it('refreshes the platform log once when the hub lists a device certified after the last verification', async () => {
    const t = await taskWorld();
    const verified = await verifyPlatformLog(t.platform.entries, t.platform.root);
    const short = await verifyPlatformLog(t.platform.entries.slice(0, 3), t.platform.root);
    setPlatform(t.store, short);
    assignable(t, [candidate(t, 1)]);
    passHead(t);
    t.context.refreshLog.mockImplementation(async () => {
      setPlatform(t.store, verified);
      return true;
    });
    expect(await ensureDevice(t.context, t.org, t.wg)).toBe(0);
    expect(t.context.refreshLog).toHaveBeenCalledTimes(1);
  });

  it('refuses to assign without the current workgroup and org keys', async () => {
    for (const scope of ['org', 'wg']) {
      const t = await taskWorld();
      assignable(t);
      const id = scope === 'org' ? t.org : t.wg;
      t.store.update('orgs', (orgs) => {
        const entry = orgs.byId[t.org]!;
        const keys = new Map(entry.keys);
        keys.delete(epochKey(scope, id, 1));
        return { ...orgs, byId: { ...orgs.byId, [t.org]: { ...entry, keys } } };
      });
      expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('epoch_stale');
    }
  });

  it('refuses an answer with another head', async () => {
    const t = await taskWorld();
    assignable(t);
    t.client.on(LOG_POST(t), () => ({ head: { seq: 1, hash: 'other' } }));
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('unavailable');
    t.client.on(LOG_POST(t), () => ({}));
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('unavailable');
  });

  it('after seq_conflict re-reads the log: done if the workgroup got a device meanwhile, else builds again', async () => {
    const t = await taskWorld();
    assignable(t);
    let devices = 0;
    t.client.on(WG_PATH(t), () => ({ devices: Array.from({ length: devices }, () => ({})) }));
    t.client.on(LOG_POST(t), () => {
      throw new HubError('seq_conflict', 409);
    });
    t.context.refreshLog.mockImplementation(async () => {
      devices = 1;
      return true;
    });
    expect(await ensureDevice(t.context, t.org, t.wg)).toBe(0);
    const again = await taskWorld();
    assignable(again);
    passHead(again, 1);
    expect(await ensureDevice(again.context, again.org, again.wg)).toBe(0);
    expect(again.client.of(LOG_POST(again))).toHaveLength(2);
  });

  it('gives up after repeated conflicts and passes other refusals on', async () => {
    const t = await taskWorld();
    assignable(t);
    t.client.on(LOG_POST(t), () => {
      throw new HubError('seq_conflict', 409);
    });
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('seq_conflict');
    expect(t.client.of(LOG_POST(t))).toHaveLength(3);
    t.client.on(LOG_POST(t), () => {
      throw new HubError('platform_access_denied', 403);
    });
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('platform_access_denied');
    t.client.on(LOG_POST(t), () => {
      throw new Error('network');
    });
    expect(await rejects(ensureDevice(t.context, t.org, t.wg))).toBe('unavailable');
  });

  it('treats already_assigned as done when the workgroup now has a device', async () => {
    const t = await taskWorld();
    assignable(t);
    let devices = 0;
    t.client.on(WG_PATH(t), () => ({ devices: Array.from({ length: devices }, () => ({})) }));
    t.client.on(LOG_POST(t), () => {
      devices = 1;
      throw new HubError('already_assigned', 409);
    });
    expect(await ensureDevice(t.context, t.org, t.wg)).toBe(0);
    const still = await taskWorld();
    assignable(still);
    still.client.on(LOG_POST(still), () => {
      throw new HubError('already_assigned', 409);
    });
    expect(await rejects(ensureDevice(still.context, still.org, still.wg))).toBe('already_assigned');
  });
});

/** Makes the fake hub answer the head of the batch the client built (after `conflicts` refused attempts). */
function passHead(t: TaskWorld, conflicts = 0): void {
  let refused = 0;
  t.client.on(LOG_POST(t), async (body) => {
    if (refused < conflicts) {
      refused += 1;
      throw new HubError('seq_conflict', 409);
    }
    return { head: await headOf(t, body) };
  });
}

async function headOf(t: TaskWorld, body: unknown): Promise<{ seq: number; hash: string }> {
  const { checkBatch } = await import('../../../src/core/log/rotation');
  const data = body as { entries: unknown[]; wraps: unknown[] };
  const log = t.store.getState().orgs.byId[t.org]!.log!;
  const result = await checkBatch(log, data.entries, data.wraps, t.me.id, {
    platform: t.store.getState().orgs.platform!.state,
  });
  return result.log.head;
}

describe('wrapCredentialKey', () => {
  const device = (t: TaskWorld) => ({ id: t.devices[0]!.id, kem: t.devices[0]!.identity.public().kem });

  it('wraps the newest own CK that opens, skipping foreign and broken wraps', async () => {
    const t = await taskWorld();
    const foreign = { ...t.credWrap, epoch: 3, wrapped_by: t.devices[1]!.id };
    const broken = { ...t.credWrap, epoch: 2 };
    const malformed = { ...t.credWrap, epoch: 'x' };
    t.client
      .on('GET /v1/wraps', () => ({
        wraps: [t.credWrap, foreign, broken, malformed, 'x', { ...t.credWrap, scope: 'wg' }],
      }))
      .on('POST /v1/wraps', () => ({}));
    expect(await wrapCredentialKey(t.context, { userId: t.me.id, identity: t.me.identity }, device(t))).toBe(true);
    const posted = t.client.of('POST /v1/wraps')[0]!.body as { wraps: { epoch: number }[] };
    expect(posted.wraps[0]!.epoch).toBe(1);
    expect(t.client.of('GET /v1/wraps')[0]!.target).toBe(`/v1/wraps?scope=cred&scope_id=${t.me.id}`);
  });

  it('is best effort: no wraps, a refused upload or a failed read give false', async () => {
    const t = await taskWorld();
    const who = { userId: t.me.id, identity: t.me.identity };
    t.client.on('GET /v1/wraps', () => null);
    expect(await wrapCredentialKey(t.context, who, device(t))).toBe(false);
    t.client
      .on('GET /v1/wraps', () => ({ wraps: [t.credWrap] }))
      .on('POST /v1/wraps', () => {
        throw new HubError('wrap_unauthorized', 400);
      });
    expect(await wrapCredentialKey(t.context, who, device(t))).toBe(false);
    t.client.on('GET /v1/wraps', () => {
      throw new HubError('unavailable', 503);
    });
    expect(await wrapCredentialKey(t.context, who, device(t))).toBe(false);
  });
});

describe('sendTask', () => {
  function created(t: TaskWorld, prepared: { taskId: string }, extra: Record<string, unknown> = {}) {
    return {
      task: taskRow(t, { task_id: prepared.taskId }),
      session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 2500 },
      events: [
        { id: 'a', scope_id: t.wg, scope_seq: 4, replayed: false },
        { id: 'b', scope_id: t.wg, scope_seq: 5, replayed: false },
      ],
      ...extra,
    };
  }

  it('creates the task, records the launch and reads its own events; a retry sends the identical body', async () => {
    const t = await taskWorld();
    const prepared = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'What is 2+2?' });
    t.client
      .on(WG_PATH(t), () => ({ devices: [{}] }))
      .on('POST /v1/tasks', () => {
        throw new HubError('unavailable', 503);
      });
    expect(await rejects(sendTask(t.context, prepared))).toBe('unavailable');
    t.client.on('POST /v1/tasks', () => created(t, prepared, { replayed: true }));
    t.clock.now += 1234;
    const sent = await sendTask(t.context, prepared);
    expect(sent).toMatchObject({ sessionId: 's1', estimateMs: 2500, replayed: true });
    const bodies = t.client.of('POST /v1/tasks').map((call) => JSON.stringify(call.body));
    expect(bodies[0]).toBe(bodies[1]);
    const state = t.store.getState().tasks;
    expect(state.tasks[prepared.taskId]!.wg_id).toBe(t.wg);
    expect(state.launches[prepared.taskId]).toEqual({
      sessionId: 's1',
      localAt: t.clock.now,
      estimateMs: 2500,
      assignedMs: null,
    });
    expect(t.ingested).toEqual([
      {
        org: t.org,
        scope: t.wg,
        items: [
          { scope_seq: 4, received_at: null, event: prepared.body.events[0] },
          { scope_seq: 5, received_at: null, event: prepared.body.events[1] },
        ],
      },
    ]);
  });

  it('creates the task without a device when none can be assigned, and sends the chosen placement', async () => {
    const t = await taskWorld();
    const prepared = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x' });
    expect(prepared.placement).toBeNull();
    expect(prepared.body).not.toHaveProperty('placement');
    t.client.on(WG_PATH(t), () => ({ devices: [] })).on(CANDIDATES(t), () => ({ candidates: [] }));
    t.client.on('POST /v1/tasks', () =>
      created(t, prepared, {
        session: { session_id: 's1', state: 'requested', device_id: null, estimate_ms: null },
        events: ['x'],
      }),
    );
    const sent = await sendTask(t.context, prepared);
    expect(sent.estimateMs).toBeNull();
    expect(t.store.getState().tasks.launches[prepared.taskId]!.assignedMs).toBeNull();
    expect(t.ingested).toEqual([]);
    const device = { device_id: 'd1', vm_id: null };
    const chosen = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x', placement: device });
    expect(chosen.placement).toEqual(device);
    expect(chosen.body.placement).toEqual({ device_id: 'd1' });
    const vm = await prepareTask(t.context, {
      org: t.org,
      wg: t.wg,
      prompt: 'x',
      placement: { device_id: 'd1', vm_id: 'vm-1a2b3c4d' },
    });
    expect(vm.body.placement).toEqual({ device_id: 'd1', vm_id: 'vm-1a2b3c4d' });
  });

  it('refuses malformed answers and answers about another task', async () => {
    const t = await taskWorld();
    const prepared = await prepareTask(t.context, { org: t.org, wg: t.wg, prompt: 'x' });
    t.client.on(WG_PATH(t), () => ({ devices: [{}] }));
    for (const answer of [null, { task: {} }, { task: taskRow(t), session: null }]) {
      t.client.on('POST /v1/tasks', () => answer);
      expect(await rejects(sendTask(t.context, prepared))).toBe('unavailable');
    }
    t.client.on('POST /v1/tasks', () => created(t, prepared, { task: taskRow(t) }));
    expect(await rejects(sendTask(t.context, prepared))).toBe('unavailable');
    t.client.on('POST /v1/tasks', () => created(t, prepared, { session: { session_id: 1 } }));
    expect(await rejects(sendTask(t.context, prepared))).toBe('unavailable');
    t.client.on('POST /v1/tasks', () => created(t, prepared, { events: 'x' }));
    expect((await sendTask(t.context, prepared)).estimateMs).toBe(2500);
  });
});

describe('loadTask', () => {
  it('stores the task, its sessions and their transitions', async () => {
    const t = await taskWorld();
    const transition = { seq: 1, state: 'requested', reason: null, at: 'a', received_at: 'b' };
    t.client.on('GET /v1/tasks/x', () => ({
      task: taskRow(t),
      sessions: [
        { session: sessionRow(t), usage: null, transitions: [transition, { bad: 1 }] },
        { session: sessionRow(t, { session_id: 's2' }), usage: null, transitions: null },
        { session: { bad: true } },
        'x',
      ],
      transitions: [],
    }));
    const row = await loadTask(t.context, 'x');
    expect(row.task_id).toBe(taskRow(t).task_id);
    const sessions = t.store.getState().tasks.sessions;
    expect(sessions[sessionRow(t).session_id]!.transitions).toEqual([transition]);
    expect(sessions.s2!.transitions).toEqual([]);
    t.client.on('GET /v1/tasks/x', () => ({ task: taskRow(t), sessions: null }));
    expect(await rejects(loadTask(t.context, 'x'))).toBe('unavailable');
    // an answer after a sign-out writes nothing (KOR-11: the reload after a reconnect may still run)
    t.client.on('GET /v1/tasks/x', () => {
      t.store.reset();
      return { task: taskRow(t), sessions: [{ session: sessionRow(t), usage: null, transitions: [] }] };
    });
    expect((await loadTask(t.context, 'x')).task_id).toBe(taskRow(t).task_id);
    expect(t.store.getState().tasks.tasks).toEqual({});
    expect(t.store.getState().tasks.sessions).toEqual({});
  });
});

describe('follow-ups and Retry', () => {
  function stored(t: TaskWorld, task: TaskRow) {
    putTasks(t.store, [task]);
  }

  it('sends a follow-up prompt and follows the session the hub names', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    stored(t, task);
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => ({ scope_seq: 9, session_id: 's9' }));
    t.clock.now += 50;
    const sealed = await prepareFollowUp(t.context, task, 'Again');
    expect(await sendFollowUp(t.context, task, sealed)).toBe('s9');
    const event = (t.client.calls[0]!.body as { event: { header: Record<string, unknown> } }).event;
    expect(event.header).toMatchObject({ kind: 'prompt', task: task.task_id, scope_id: t.wg });
    expect(t.store.getState().tasks.launches[task.task_id]).toEqual({
      sessionId: 's9',
      localAt: t.clock.now,
      estimateMs: null,
      assignedMs: null,
    });
    expect(t.ingested[0]!.items[0]!.scope_seq).toBe(9);
    expect(await rejects(prepareFollowUp(t.context, task, ' '))).toBe('request_invalid');
    expect(await rejects(prepareFollowUp(t.context, task, 'x'.repeat(PROMPT_MAX + 1)))).toBe('size_exceeded');
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => ({ scope_seq: 'x', session_id: 's' }));
    expect(await rejects(sendFollowUp(t.context, task, sealed))).toBe('unavailable');
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => null);
    expect(await rejects(sendFollowUp(t.context, task, sealed))).toBe('unavailable');
  });

  it('re-sends the same sealed prompt after a failed Retry and seals anew after a success or a sign-out', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    putEvents(t.store, t.wg, [
      {
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'prompt', task: task.task_id, id: 'e1' },
        body: { text: 'Known', attachments: [] },
      },
    ]);
    let answers = 0;
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => {
      answers += 1;
      if (answers === 1) throw new HubError('unavailable', 503);
      return { scope_seq: 2, session_id: 's2' };
    });
    expect(await rejects(retryPrompt(t.context, task))).toBe('unavailable');
    expect(await retryPrompt(t.context, task)).toBe('s2');
    expect(await retryPrompt(t.context, task)).toBe('s2');
    const ids = t.client
      .of(`POST /v1/tasks/${task.task_id}/prompts`)
      .map((call) => (call.body as { event: { header: { id: string } } }).event.header.id);
    expect(ids[0]).toBe(ids[1]);
    expect(ids[2]).not.toBe(ids[1]);
    answers = 0;
    expect(await rejects(retryPrompt(t.context, task))).toBe('unavailable');
    const generation = t.store.generation;
    t.store.reset();
    expect(t.store.generation).toBe(generation + 1);
    // after the sign-out nothing kept is re-used: the prompt is looked up (pulled) again
    expect(await rejects(retryPrompt(t.context, task))).toBe('not_found');
    expect(t.client.calls.at(-1)!.target).toContain(`/v1/scopes/${t.wg}/events`);
  });

  it('retries without a pull when the prompt is in the store', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    putEvents(t.store, t.wg, [
      {
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'prompt', task: task.task_id, id: 'e1' },
        body: { text: 'Known', attachments: [] },
      },
    ]);
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => ({ scope_seq: 2, session_id: 's2' }));
    expect(await retryPrompt(t.context, task)).toBe('s2');
    expect(t.client.calls.map((call) => call.method)).toEqual(['POST']);
  });

  it('retries with the newest verified prompt of the task, pulling the prompts when none is read', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    const entry = (seq: number, text: unknown, kind = 'prompt', status: 'verified' | 'locked' = 'verified') => ({
      org: t.org,
      scopeId: t.wg,
      scopeSeq: seq,
      receivedAt: null,
      event: {},
      status,
      code: null,
      header: { kind, task: task.task_id, id: `e${seq}` },
      body: { text },
    });
    expect(latestPrompt(t.store, task)).toBeNull();
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    expect(await rejects(retryPrompt(t.context, task))).toBe('decrypt_failed');
    t.context.ingest.mockImplementation(async () => {
      putEvents(t.store, t.wg, [
        entry(1, 'Old'),
        entry(2, 'New'),
        entry(3, 'x', 'task.meta'),
        entry(4, 'L', 'prompt', 'locked'),
      ]);
    });
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => ({ scope_seq: 9, session_id: 's9' }));
    expect(await retryPrompt(t.context, task)).toBe('s9');
    const sent = t.client.of(`POST /v1/tasks/${task.task_id}/prompts`)[0]!;
    expect(sent).toBeDefined();
    expect(latestPrompt(t.store, task)).toBe('New');
    putEvents(t.store, t.wg, [entry(5, 7), { ...entry(8, 'x'), body: null }]);
    expect(latestPrompt(t.store, task)).toBe('New');
    putEvents(t.store, t.wg, [{ ...entry(6, 'Other'), header: { kind: 'prompt', task: 'other', id: 'e6' } }]);
    putEvents(t.store, 'another-scope', [{ ...entry(7, 'Elsewhere'), scopeId: 'another-scope' }]);
    expect(latestPrompt(t.store, task)).toBe('New');
  });
});
