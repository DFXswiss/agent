// What a pull-request row shows (KOR-48, ADR-084 sections 2, 3 and 5): state and conflict state from the device's read
// (conflict = `mergeable: false` alone, unknown never "no conflict", forks without the button), "Fix conflicts" only
// for writers, the fix feedback from the task's verified operation events, the instruction sent as the task's prompt,
// and the `pulls` slice.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openEvent } from '../../../src/core/crypto/event';
import { epochKey, WG } from '../../../src/core/log/state';
import { fixConflicts } from '../../../src/core/pulls/fix';
import { conflictsFixed, selectFixed } from '../../../src/core/pulls/fixed';
import { fixConflictsInstruction } from '../../../src/core/pulls/instruction';
import { canFix, displayOf, levelCurrent } from '../../../src/core/pulls/view';
import type { PullState } from '../../../src/core/pulls/checks';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import type { OperationRow } from '../../../src/core/store/slices/operations';
import {
  addLink,
  linkList,
  listKey,
  lastState,
  listPage,
  type PullLink,
  pullStatus,
  removeLink,
  setStatuses,
} from '../../../src/core/pulls/state';
import { putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import { createStore } from '../../../src/core/store';
import { taskRow, taskWorld } from '../tasks/world';

const REF = { repo: 'Org/Repo', number: 4 };
const TASK = '0190f000-0000-7000-8000-000000000001';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const WGID = '0190f000-0000-7000-8000-0000000000c1';
const MERGE = 'c'.repeat(40);

const state = (extra: Partial<Record<string, unknown>> = {}): PullState =>
  ({
    repo: 'Org/Repo',
    number: 4,
    found: true,
    title: 'Fix it',
    state: 'open',
    draft: false,
    merged: false,
    mergeable: false,
    mergeable_state: 'dirty',
    head_ref: 'feature',
    head_sha: 'a'.repeat(40),
    head_repo: 'org/repo',
    base_ref: 'main',
    url: 'https://github.com/Org/Repo/pull/4',
    updated_at: '2026-11-24T12:00:00Z',
    ...extra,
  }) as PullState;

const ready = (extra: Partial<Record<string, unknown>> = {}) => ({
  status: 'ready' as const,
  state: state(extra),
  at: 1,
});

describe('displayOf', () => {
  it('shows the states of the status', () => {
    expect(displayOf(REF, null)).toEqual({ kind: 'unchecked' });
    expect(displayOf(REF, { status: 'checking' })).toEqual({ kind: 'checking' });
    expect(displayOf(REF, { status: 'failed', code: 'timeout' })).toEqual({ kind: 'failed', code: 'timeout' });
    expect(
      displayOf(REF, {
        status: 'ready',
        at: 1,
        state: { repo: 'Org/Repo', number: 4, found: false, code: 'not_found' },
      }),
    ).toEqual({ kind: 'missing', code: 'not_found' });
    expect(displayOf(REF, ready())).toEqual({
      kind: 'found',
      state: 'open',
      conflict: 'conflict',
      fork: false,
      title: 'Fix it',
      headRef: 'feature',
      url: 'https://github.com/Org/Repo/pull/4',
    });
  });

  it('takes the conflict from mergeable alone and shows closed and merged pull requests by their state', () => {
    expect(displayOf(REF, ready({ draft: true, mergeable_state: 'draft' }))).toMatchObject({
      state: 'draft',
      conflict: 'conflict',
    });
    expect(displayOf(REF, ready({ mergeable: null }))).toMatchObject({ conflict: 'checking' });
    expect(displayOf(REF, ready({ mergeable: true, mergeable_state: 'dirty' }))).toMatchObject({ conflict: 'clean' });
    expect(displayOf(REF, ready({ state: 'closed', merged: true }))).toMatchObject({ state: 'merged', conflict: null });
    expect(displayOf(REF, ready({ state: 'closed' }))).toMatchObject({ state: 'closed', conflict: null });
  });

  it('marks forks and links GitHub with https only', () => {
    expect(displayOf(REF, ready({ head_repo: 'someone/repo' }))).toMatchObject({ fork: true });
    expect(displayOf(REF, ready({ head_repo: null }))).toMatchObject({ fork: true });
    expect(displayOf(REF, ready({ url: 'http://127.0.0.1:9/pull/4' }))).toMatchObject({
      url: 'https://github.com/Org/Repo/pull/4',
    });
  });

  it('offers "Fix conflicts" only for a conflict in the same repository to writers', () => {
    expect(canFix(displayOf(REF, ready()), true)).toBe(true);
    expect(canFix(displayOf(REF, ready()), false)).toBe(false);
    expect(canFix(displayOf(REF, ready({ head_repo: 'x/y' })), true)).toBe(false);
    expect(canFix(displayOf(REF, ready({ mergeable: null })), true)).toBe(false);
    expect(canFix(displayOf(REF, { status: 'checking' }), true)).toBe(false);
  });
});

let seq = 0;

/** The verified `operation.params` and `operation.result` events of one operation. */
function operation(
  id: string,
  type: string,
  params: Record<string, unknown>,
  result: Record<string, unknown> | null,
  options: { ts?: string; task?: string; author?: string } = {},
): EventEntry[] {
  const header = (kind: string) => ({
    id: `e${++seq}`,
    kind,
    ref: id,
    task: options.task ?? TASK,
    author: options.author ?? DEVICE,
    ts: options.ts ?? '2026-11-24T12:00:00.000Z',
  });
  const entry = (kind: string, body: unknown): EventEntry => ({
    org: 'org',
    scopeId: WGID,
    scopeSeq: ++seq,
    receivedAt: null,
    event: {},
    status: 'verified',
    code: null,
    header: header(kind),
    body,
  });
  return [
    entry('operation.params', { operation_id: id, type, params }),
    entry(
      'operation.result',
      result === null
        ? { operation_id: id, status: 'failed', error: { code: 'non_fast_forward' } }
        : { operation_id: id, status: 'succeeded', result },
    ),
  ];
}

const commit = (id: string, sha: string, extra: Record<string, unknown> = {}, ts?: string) =>
  operation(
    id,
    'github.commit',
    { repo: 'org/repo', branch: 'feature', message: 'm', merge: true, ...extra },
    {
      commit_sha: sha,
    },
    { ...(ts !== undefined && { ts }) },
  );

const push = (id: string, sha: unknown, extra: Record<string, unknown> = {}, ts?: string, result = true) =>
  operation(
    id,
    'github.push',
    { repo: 'Org/Repo', branch: 'feature', commit_sha: 'x', ...extra },
    result ? { branch: (extra.branch as string | undefined) ?? 'feature', sha, url: 'https://github.com/x' } : null,
    { ...(ts !== undefined && { ts }) },
  );

describe('conflictsFixed', () => {
  it('finds the newest push of a merge commit to the head branch', () => {
    const entries = [
      ...commit('c1', MERGE),
      ...push('p1', MERGE, {}, '2026-11-24T12:01:00.000Z'),
      ...commit('c2', 'd'.repeat(40)),
      ...push('p2', 'd'.repeat(40), {}, '2026-11-24T12:05:00.000Z'),
      ...push('p0', MERGE, {}, '2026-11-24T11:00:00.000Z'),
    ];
    expect(conflictsFixed(entries, {}, TASK, REF, 'feature')).toEqual({
      commit: 'd'.repeat(40),
      at: '2026-11-24T12:05:00.000Z',
    });
  });

  it('counts no ordinary commit, other repositories or branches, failures and foreign events', () => {
    const unverifiable: EventEntry = { ...commit('c9', MERGE)[0]!, status: 'unverifiable', code: 'signature_invalid' };
    const entries = [
      ...commit('c1', MERGE, { merge: undefined }),
      ...push('p1', MERGE),
      ...commit('c2', 'e'.repeat(40), { repo: 'other/repo' }),
      ...push('p2', 'e'.repeat(40)),
      ...commit('c3', 'f'.repeat(40)),
      ...push('p3', 'f'.repeat(40), { branch: 'other' }),
      ...push('p4', 'f'.repeat(40), {}, undefined, false),
      ...push('p5', 7),
      ...operation('c4', 'github.commit', { repo: 'org/repo', merge: true }, { commit_sha: 5 }),
      ...commit('c5', '1'.repeat(40), {}, undefined).map((entry) => ({
        ...entry,
        header: { ...entry.header!, task: 'other-task' },
      })),
      ...push('p6', '1'.repeat(40)),
      ...commit('c6', '2'.repeat(40)).map((entry) => ({ ...entry, header: { ...entry.header!, ref: 5 } })),
      ...push('p7', '2'.repeat(40)),
      unverifiable,
      { ...push('p8', MERGE)[0]!, header: { ...push('p8', MERGE)[0]!.header!, kind: 'prompt' } },
      ...operation('x1', 'github.push', { repo: 5 }, { branch: 'feature', sha: MERGE }),
      push('p9', MERGE)[1]!,
      commit('c7', MERGE)[0]!,
    ];
    expect(conflictsFixed(entries, {}, TASK, REF, 'feature')).toBeNull();
  });

  it("needs the row's type to match the parameters when the row is known", () => {
    const entries = [...commit('c1', MERGE), ...push('p1', MERGE)];
    const row = { operation_id: 'p1', type: 'github.comment', device_id: DEVICE } as OperationRow;
    expect(conflictsFixed(entries, { p1: row }, TASK, REF, 'feature')).toBeNull();
    expect(conflictsFixed(entries, { p1: { ...row, type: 'github.push' } }, TASK, REF, 'feature')).not.toBeNull();
  });

  it('is read by a memoized selector', () => {
    const store = createStore();
    const none = selectFixed(WGID, TASK, REF, null);
    expect(none(store.getState())).toBeNull();
    const select = selectFixed(WGID, TASK, REF, 'feature');
    expect(select(store.getState())).toBeNull();
    putEvents(store, WGID, [...commit('c1', MERGE), ...push('p1', MERGE)]);
    const first = select(store.getState());
    expect(first).toMatchObject({ commit: MERGE });
    expect(select(store.getState())).toBe(first);
  });
});

describe('fixConflicts', () => {
  it("sends the instruction as the task's next prompt, loading the task first when it is not known", async () => {
    const t = await taskWorld();
    const row = taskRow(t, { task_id: TASK }) as TaskRow;
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task: row, sessions: [] }));
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => ({ session_id: 's1', scope_seq: 3 }));
    expect(await fixConflicts(t.context, TASK, REF)).toBe('s1');
    expect(t.client.of(`GET /v1/tasks/${TASK}`)).toHaveLength(1);
    putTasks(t.store, [row]);
    await fixConflicts(t.context, TASK, REF);
    expect(t.client.of(`GET /v1/tasks/${TASK}`)).toHaveLength(1);
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const sig = b64uDecode(t.me.identity.public().sig);
    for (const call of t.client.of(`POST /v1/tasks/${TASK}/prompts`)) {
      const opened = JSON.parse(fromUtf8(await openEvent(key, (call.body as { event: never }).event, sig)));
      expect(opened.text).toBe(fixConflictsInstruction(REF));
    }
  });
});

describe('slice pulls', () => {
  const link = (tag: string, task = TASK): PullLink => ({
    wg: WGID,
    epoch: 1,
    tag,
    taskId: task,
    sessionId: null,
    linkedBy: 'u',
    createdAt: 'x',
    pull: REF,
    code: null,
  });

  it('keeps lists, links and statuses', () => {
    const store = createStore();
    const key = listKey(WGID, null);
    expect(linkList(store.getState().pulls, key)).toMatchObject({ ids: [], status: 'loading' });
    listPage(store, key, [link('a')], 'c', true);
    listPage(store, key, [link('a'), link('b')], null, false);
    expect(linkList(store.getState().pulls, key).ids).toEqual([`${WGID} 1 a`, `${WGID} 1 b`]);
    addLink(store, link('b'));
    expect(linkList(store.getState().pulls, key).ids).toEqual([`${WGID} 1 b`, `${WGID} 1 a`]);
    removeLink(store, link('a'));
    expect(linkList(store.getState().pulls, key).ids).toEqual([`${WGID} 1 b`]);
    const before = store.getState().pulls;
    setStatuses(store, WGID, []);
    expect(store.getState().pulls).toBe(before);
    setStatuses(store, WGID, [['org/repo#4', { status: 'checking' }]]);
    expect(pullStatus(store.getState().pulls, WGID, 'org/repo#4')).toEqual({ status: 'checking' });
    expect(pullStatus(store.getState().pulls, WGID, 'org/repo#5')).toBeNull();
    // a new read keeps the last checked state
    setStatuses(store, WGID, [['org/repo#4', ready()]]);
    setStatuses(store, WGID, [['org/repo#4', { status: 'checking' }]]);
    setStatuses(store, WGID, [['org/repo#4', { status: 'failed', code: 'timeout' }]]);
    const kept = pullStatus(store.getState().pulls, WGID, 'org/repo#4');
    expect(kept).toEqual({ status: 'failed', code: 'timeout', last: state() });
    expect(lastState(kept)).toEqual(state());
    expect(lastState(ready())).toEqual(state());
    expect(lastState(null)).toBeNull();
    expect(lastState({ status: 'checking' })).toBeNull();
  });
});

describe('levelCurrent', () => {
  it('marks only the shown workgroup', () => {
    expect(levelCurrent('a', 'a')).toBe('page');
    expect(levelCurrent('b', 'a')).toBeUndefined();
  });
});
