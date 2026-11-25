// The device read of pull-request states (KOR-48, ADR-084 section 2): the device a request goes to, one sealed
// `github.pull_status` request at a time with every waiting pull request of a workgroup batched into it (at most 50,
// the task named only when the batch has one), the answer's states, the request's failure for every pull request of
// the batch, the end without an answer, the automatic second read of an unknown `mergeable` (once per asked read) and
// the fresh read after a fix – against the core task world with a device that opens the request and seals answers.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyJson } from '../../../src/core/encoding/json';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openRequest, sealResponse } from '../../../src/core/crypto/request';
import { type PullState, PULLS_MAX } from '../../../src/core/pulls/checks';
import {
  beingChecked,
  checkPulls,
  pickDevice,
  readStatuses,
  RECHECK_MS,
  refreshAfterFix,
  REQUEST_MS,
  type Timer,
} from '../../../src/core/pulls/status';
import { setLog } from '../../../src/core/store/slices/orgs';
import { pullStatus } from '../../../src/core/pulls/state';
import { noteResponse } from '../../../src/core/store/slices/requests';
import { putSession, putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import { actor, type Actor } from '../log/world';
import { sessionRow, taskRow, taskWorld, type TaskWorld } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const OTHER = '0190f000-0000-7000-8000-000000000002';
const SHA = 'a'.repeat(40);

type World = TaskWorld & { device: Actor; second: Actor };

interface Answer {
  /** Response bodies (the last one final), or none. */
  readonly bodies?: readonly unknown[];
  readonly hubState?: string;
}

/** A world with two devices of the workgroup; `answer` decides the device's answer to each request it opens. */
async function world(answer: (body: Record<string, unknown>) => Answer = (body) => ({ bodies: [done(body)] })) {
  const t = await taskWorld();
  const device = await actor('pulls-device');
  const second = await actor('pulls-device-2');
  await t.world.admitDevice(t.me, device, [t.wg]);
  await t.world.admitDevice(t.me, second, [t.wg]);
  setLog(t.store, t.org, t.world.log!);
  const w: World = { ...t, device, second };
  const listed = (id: string, online: boolean, org = t.org, wgs = [t.wg]) => ({
    device_id: id,
    kind: 'org',
    org_id: org,
    wg_ids: wgs,
    online,
    last_seen_at: null,
  });
  t.client.on('GET /v1/me/devices', () => ({
    devices: [
      listed(second.id, false),
      listed(device.id, true),
      listed('0190f000-0000-7000-8000-0000000000ff', true),
      listed(device.id, true, 'other-org'),
      listed(device.id, true, t.org, ['other-wg']),
    ],
  }));
  const bodies: Record<string, unknown>[] = [];
  const devices: string[] = [];
  t.client.on('POST /v1/device-requests', async (body) => {
    const envelope = (body as { envelope: { header: { id: string; device: string } } }).envelope;
    const id = envelope.header.id;
    const target = envelope.header.device === device.id ? device : second;
    devices.push(target.id);
    const opened = JSON.parse(
      fromUtf8(await openRequest(target.identity.kem, envelope, t.me.identity.sigPublic())),
    ) as Record<string, unknown>;
    bodies.push(opened);
    const reply = answer(opened);
    const responses: unknown[] = [];
    for (const [index, item] of (reply.bodies ?? []).entries()) {
      const header = {
        v: 1,
        id: `0190f000-0000-7000-8000-00000000f0${index}0`,
        user: t.me.id,
        device: target.id,
        kind: 'github.pull_status',
        in_reply_to: id,
        seq: index + 1,
        final: index === reply.bodies!.length - 1,
        ts: new Date(t.clock.now).toISOString(),
        enc_alg: 'HPKE-X25519-A256GCM',
      };
      const sealed = await sealResponse(
        header,
        t.me.identity.kemPublic(),
        bodyJson(item),
        target.identity.sig.privateKey,
      );
      responses.push({ seq: index + 1, envelope: sealed, created_at: header.ts });
    }
    t.client.on(`GET /v1/device-requests/${id}`, () => ({
      request_id: id,
      kind: 'github.pull_status',
      device_id: target.id,
      state: reply.hubState ?? (responses.length > 0 ? 'answered' : 'delivered'),
      created_at: '2026-11-24T12:00:00.000Z',
      expires_at: '2026-11-24T12:15:00.000Z',
      responses,
    }));
    return { request_id: id, state: 'pending' };
  });
  return { w, bodies, devices };
}

const found = (repo: string, number: number, extra: Partial<Record<string, unknown>> = {}) => ({
  repo,
  number,
  found: true,
  title: `PR ${number}`,
  state: 'open',
  draft: false,
  merged: false,
  mergeable: false,
  mergeable_state: 'dirty',
  head_ref: 'feature',
  head_sha: SHA,
  head_repo: repo,
  base_ref: 'main',
  url: `https://github.com/${repo}/pull/${number}`,
  updated_at: '2026-11-24T12:00:00Z',
  ...extra,
});

/** `done` with every requested pull request found and in conflict. */
function done(body: Record<string, unknown>, extra: Partial<Record<string, unknown>> = {}) {
  const pulls = body.pulls as { repo: string; number: number }[];
  return { step: 'done', pulls: pulls.map((pull) => found(pull.repo, pull.number, extra)) };
}

/** A timer whose ticks the test runs. */
function manualTimer() {
  const ticks: { tick: () => void; ms: number; stopped: boolean }[] = [];
  const timer: Timer = {
    start: (tick, ms) => {
      ticks.push({ tick, ms, stopped: false });
      return ticks.length - 1;
    },
    stop: (handle) => {
      ticks[handle as number]!.stopped = true;
    },
  };
  const run = (ms: number) => {
    for (const item of ticks.filter((entry) => entry.ms === ms && !entry.stopped)) {
      item.stopped = true;
      item.tick();
    }
  };
  return { ticks, timer, run };
}

const target = (w: TaskWorld, task: string | null = TASK) => ({ org: w.org, wg: w.wg, task });
const status = (w: TaskWorld, key: string) => pullStatus(w.store.getState().pulls, w.wg, key);

/** Waits until no pull request of `keys` is being checked. */
async function settled(w: TaskWorld, keys: readonly string[]) {
  await vi.waitFor(() => {
    for (const key of keys) expect(status(w, key)?.status).not.toBe('checking');
  });
}

afterEach(() => vi.useRealTimers());

describe('pickDevice', () => {
  it("takes the task's device, else an online device of the workgroup, else refuses", async () => {
    const { w } = await world();
    putTasks(w.store, [taskRow(w, { task_id: TASK }) as TaskRow]);
    putSession(w.store, sessionRow(w, { device_id: w.second.id }) as never, [], 0);
    expect(await pickDevice(w.context, target(w))).toBe(w.second.id);
    putSession(w.store, sessionRow(w, { device_id: '0190f000-0000-7000-8000-0000000000ee' }) as never, [], 0);
    expect(await pickDevice(w.context, target(w))).toBe(w.device.id);
    putTasks(w.store, [taskRow(w, { task_id: OTHER, main_session_id: null }) as TaskRow]);
    expect(await pickDevice(w.context, target(w, OTHER))).toBe(w.device.id);
    expect(await pickDevice(w.context, target(w, '0190f000-0000-7000-8000-000000000009'))).toBe(w.device.id);
    const lost = '0190f000-0000-7000-8000-000000000003';
    putTasks(w.store, [
      taskRow(w, { task_id: lost, main_session_id: '0190f000-0000-7000-8000-0000000000a9' }) as TaskRow,
    ]);
    expect(await pickDevice(w.context, target(w, lost))).toBe(w.device.id);
    expect(await pickDevice(w.context, target(w, null))).toBe(w.device.id);
    putSession(w.store, sessionRow(w, { device_id: null }) as never, [], 0);
    expect(await pickDevice(w.context, target(w))).toBe(w.device.id);
    await expect(pickDevice(w.context, { ...target(w), wg: 'nowhere' })).rejects.toMatchObject({
      code: 'device_not_assigned',
    });
  });
});

describe('readStatuses', () => {
  it('seals the request to the device and returns the states of the asked pull requests', async () => {
    const { w, bodies } = await world((body) => ({
      bodies: [{ step: 'done', pulls: [...done(body).pulls, found('x/y', 9)] }],
    }));
    const states = await readStatuses(w.context, target(w), [{ repo: 'A/B', number: 1 }]);
    expect([...states.keys()]).toEqual(['a/b#1']);
    expect(bodies).toEqual([{ org_id: w.org, wg_id: w.wg, task_id: TASK, pulls: [{ repo: 'A/B', number: 1 }] }]);
    await readStatuses(w.context, target(w, null), [{ repo: 'A/B', number: 1 }]);
    expect(bodies[1]).toEqual({ org_id: w.org, wg_id: w.wg, pulls: [{ repo: 'A/B', number: 1 }] });
  });

  it('refuses with the code of a failed answer or of a request the hub ended', async () => {
    const answers: Answer[] = [
      { bodies: [{ step: 'failed', code: 'github_not_connected' }] },
      { hubState: 'expired' },
      { hubState: 'failed' },
      { bodies: [{ step: 'done', pulls: [{ junk: true }] }] },
    ];
    const { w } = await world(() => answers.shift()!);
    const refs = [{ repo: 'a/b', number: 1 }];
    await expect(readStatuses(w.context, target(w), refs)).rejects.toMatchObject({ code: 'github_not_connected' });
    await expect(readStatuses(w.context, target(w), refs)).rejects.toMatchObject({ code: 'timeout' });
    await expect(readStatuses(w.context, target(w), refs)).rejects.toMatchObject({ code: 'request_failed' });
    // an answer the contracts do not allow is no answer: the hub's state ends the request
    await expect(readStatuses(w.context, target(w), refs)).rejects.toMatchObject({ code: 'request_failed' });
  });

  it('gives up after REQUEST_MS without an end', async () => {
    const { w } = await world(() => ({}));
    const { timer, run, ticks } = manualTimer();
    const read = readStatuses(w.context, target(w), [{ repo: 'a/b', number: 1 }], timer);
    await vi.waitFor(() => expect(ticks.some((item) => item.ms === REQUEST_MS)).toBe(true));
    run(REQUEST_MS);
    await expect(read).rejects.toMatchObject({ code: 'timeout' });
  });

  it('ends at the first final answer and stops its deadline', async () => {
    const { w } = await world(() => ({}));
    const { timer, ticks } = manualTimer();
    const read = readStatuses(w.context, target(w), [{ repo: 'a/b', number: 1 }], timer);
    await vi.waitFor(() => expect(w.client.of('POST /v1/device-requests')).toHaveLength(1));
    const id = (w.client.of('POST /v1/device-requests')[0]!.body as { envelope: { header: { id: string } } }).envelope
      .header.id;
    const view = w.client.routes.get(`GET /v1/device-requests/${id}`)!;
    w.client.on(`GET /v1/device-requests/${id}`, async (body, path) => ({
      ...((await view(body, path)) as object),
      state: 'expired',
    }));
    noteResponse(w.store, id, 1);
    await expect(read).rejects.toMatchObject({ code: 'timeout' });
    expect(ticks.find((item) => item.ms === REQUEST_MS)!.stopped).toBe(true);
  });
});

describe('checkPulls', () => {
  it('batches the pull requests of one render into one request and stores their states', async () => {
    const { w, bodies } = await world();
    checkPulls(w.context, target(w), [{ repo: 'a/b', number: 1 }]);
    checkPulls(w.context, target(w), [{ repo: 'a/b', number: 2 }]);
    expect(status(w, 'a/b#1')).toEqual({ status: 'checking' });
    await settled(w, ['a/b#1', 'a/b#2']);
    expect(bodies).toHaveLength(1);
    expect(bodies[0]!.task_id).toBe(TASK);
    expect(status(w, 'a/b#2')).toMatchObject({ status: 'ready', state: { mergeable: false } });
    // known states are not read again unless asked
    checkPulls(w.context, target(w), [{ repo: 'a/b', number: 1 }]);
    expect(bodies).toHaveLength(1);
    checkPulls(w.context, target(w), [{ repo: 'a/b', number: 1 }], { force: true });
    checkPulls(w.context, target(w), [{ repo: 'a/b', number: 1 }], { force: true });
    await settled(w, ['a/b#1']);
    expect(bodies).toHaveLength(2);
  });

  it('names no task for a batch of several tasks and splits more than PULLS_MAX', async () => {
    const { w, bodies } = await world();
    const refs = Array.from({ length: PULLS_MAX + 1 }, (_, index) => ({ repo: 'a/b', number: index + 1 }));
    checkPulls(w.context, target(w, TASK), refs.slice(0, 10));
    checkPulls(w.context, target(w, OTHER), refs.slice(10));
    await settled(
      w,
      refs.map((ref) => `a/b#${ref.number}`),
    );
    expect(bodies.map((body) => (body.pulls as unknown[]).length)).toEqual([PULLS_MAX, 1]);
    expect(bodies.every((body) => body.task_id === undefined)).toBe(true);
  });

  it('marks every pull request of a failed request and one the answer leaves out', async () => {
    const answers: Answer[] = [
      { bodies: [{ step: 'failed', code: 'github_not_connected' }] },
      { bodies: [{ step: 'done', pulls: [found('a/b', 1)] }] },
    ];
    const { w } = await world(() => answers.shift()!);
    checkPulls(w.context, target(w), [
      { repo: 'a/b', number: 1 },
      { repo: 'a/b', number: 2 },
    ]);
    await settled(w, ['a/b#1', 'a/b#2']);
    expect(status(w, 'a/b#2')).toEqual({ status: 'failed', code: 'github_not_connected' });
    checkPulls(
      w.context,
      target(w),
      [
        { repo: 'a/b', number: 1 },
        { repo: 'a/b', number: 2 },
      ],
      { force: true },
    );
    await settled(w, ['a/b#1', 'a/b#2']);
    expect(status(w, 'a/b#1')).toMatchObject({ status: 'ready' });
    expect(status(w, 'a/b#2')).toEqual({ status: 'failed', code: 'unavailable' });
  });

  it('reads an unknown mergeable once more after RECHECK_MS, and again only after "Check again"', async () => {
    const { w, bodies } = await world((body) => ({ bodies: [done(body, { mergeable: null })] }));
    const { timer, run } = manualTimer();
    const ref = { repo: 'a/b', number: 1 };
    checkPulls(w.context, target(w), [ref], { timer });
    await settled(w, ['a/b#1']);
    expect(status(w, 'a/b#1')).toMatchObject({ status: 'ready', state: { mergeable: null } });
    run(RECHECK_MS);
    await vi.waitFor(() => expect(bodies).toHaveLength(2));
    await settled(w, ['a/b#1']);
    run(RECHECK_MS);
    await Promise.resolve();
    expect(bodies).toHaveLength(2);
    checkPulls(w.context, target(w), [ref], { timer, force: true });
    await settled(w, ['a/b#1']);
    run(RECHECK_MS);
    await vi.waitFor(() => expect(bodies).toHaveLength(4));
  });

  it('writes nothing after a sign-out', async () => {
    const { w: early, bodies } = await world();
    checkPulls(early.context, target(early), [{ repo: 'a/b', number: 1 }]);
    early.store.reset();
    await Promise.resolve();
    await Promise.resolve();
    expect(bodies).toHaveLength(0);
    const { w } = await world();
    w.client.on('GET /v1/me/devices', () => {
      w.store.reset();
      return { devices: [] };
    });
    checkPulls(w.context, target(w), [{ repo: 'a/b', number: 1 }]);
    await vi.waitFor(() => expect(w.store.getState().pulls.status).toEqual({}));
    // a queue of the old generation stops before its next batch
    const { w: u } = await world();
    checkPulls(u.context, target(u), [{ repo: 'a/b', number: 1 }]);
    checkPulls(u.context, { ...target(u), wg: 'nowhere' }, [{ repo: 'a/b', number: 1 }]);
    u.client.on('GET /v1/me/devices', () => {
      u.store.reset();
      return { devices: [] };
    });
    await vi.waitFor(() => expect(u.store.getState().pulls.status).toEqual({}));
    await Promise.resolve();
    expect(u.store.getState().pulls.status).toEqual({});
  });
});

describe('refreshAfterFix', () => {
  it('reads once more when the read head is not the merge commit yet', async () => {
    const { w, bodies } = await world();
    const ref = { repo: 'a/b', number: 1 };
    refreshAfterFix(w.context, target(w), ref, 'b'.repeat(40));
    expect(bodies).toHaveLength(0);
    checkPulls(w.context, target(w), [ref]);
    await settled(w, ['a/b#1']);
    refreshAfterFix(w.context, target(w), ref, SHA);
    await Promise.resolve();
    expect(bodies).toHaveLength(1);
    refreshAfterFix(w.context, target(w), ref, 'b'.repeat(40));
    await vi.waitFor(() => expect(bodies).toHaveLength(2));
    await settled(w, ['a/b#1']);
    refreshAfterFix(w.context, target(w), ref, 'b'.repeat(40));
    await Promise.resolve();
    expect(bodies).toHaveLength(2);
  });

  it('does not read a pull request the device did not find', async () => {
    const { w, bodies } = await world((body) => ({
      bodies: [{ step: 'done', pulls: [{ ...(body.pulls as object[])[0], found: false, code: 'not_found' }] }],
    }));
    const ref = { repo: 'a/b', number: 1 };
    checkPulls(w.context, target(w), [ref]);
    await settled(w, ['a/b#1']);
    refreshAfterFix(w.context, target(w), ref, 'b'.repeat(40));
    await Promise.resolve();
    expect(bodies).toHaveLength(1);
  });
});

describe('beingChecked', () => {
  it('is an open, unmerged pull request with an unknown mergeable', () => {
    const open = found('a/b', 1, { mergeable: null }) as PullState;
    expect(beingChecked(open)).toBe(true);
    expect(beingChecked({ ...open, state: 'closed' } as PullState)).toBe(false);
    expect(beingChecked({ ...open, merged: true } as PullState)).toBe(false);
    expect(beingChecked({ ...open, mergeable: true } as PullState)).toBe(false);
    expect(beingChecked({ repo: 'a/b', number: 1, found: false, code: 'not_found' })).toBe(false);
  });
});
