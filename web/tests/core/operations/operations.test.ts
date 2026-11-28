// Operations (06-web.md sections 5.2 and 5.4, 03-api.md sections 4.7 and 9): the slice (rows never go back, live
// frames), loading lists, details and the operation's events, and the card states with result facts and duplicates.
import { describe, expect, it } from 'vitest';
import { NEWEST } from '../../../src/core/net/pages';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import {
  ensureOperations,
  loadOperation,
  loadOperations,
  pullOperationEvents,
} from '../../../src/core/operations/load';
import {
  factsOf,
  httpsUrl,
  issueOf,
  listingOf,
  operationView,
  purposeOf,
  selectOperation,
  subjectOf,
  SUBJECT_CHARS,
} from '../../../src/core/operations/view';
import { HubError } from '../../../src/core/net/signedFetch';
import type { EventEntry } from '../../../src/core/store/slices/events';
import { putEvents } from '../../../src/core/store/slices/events';
import {
  FINAL_STATES,
  isOperationRow,
  newestFirst,
  type OperationRow,
  putOperations,
  putTransitions,
} from '../../../src/core/store/slices/operations';
import { taskWorld, type TaskWorld } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const OP = '0190f000-0000-7000-8000-0000000000b1';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';

function row(t: TaskWorld, overrides: Partial<OperationRow> = {}): OperationRow {
  return {
    operation_id: OP,
    org_id: t.org,
    wg_id: t.wg,
    task_id: TASK,
    session_id: '0190f000-0000-7000-8000-0000000000a1',
    device_id: DEVICE,
    actor_id: t.me.id,
    type: 'github.create_pull_request',
    state: 'accepted',
    error_code: null,
    retryable: false,
    duplicate_of: null,
    attempt: 1,
    params_event_id: null,
    result_event_id: null,
    created_at: '2026-11-21T12:00:00.000Z',
    decided_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

let seq = 100;

function event(
  t: TaskWorld,
  kind: string,
  body: unknown,
  options: { ref?: string; author?: string; status?: EventEntry['status'] } = {},
): EventEntry {
  const header = {
    id: `e${seq}`,
    kind,
    ref: options.ref ?? OP,
    author: options.author ?? DEVICE,
    ts: '2026-11-21T12:00:01.000Z',
  };
  const status = options.status ?? 'verified';
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq++,
    receivedAt: null,
    event: { header },
    status,
    code: status === 'unverifiable' ? 'signature_invalid' : null,
    header: status === 'verified' ? header : null,
    body: status === 'verified' ? body : null,
  };
}

const params = (type: string, p: unknown, op = OP) => ({ operation_id: op, type, params: p });
const succeeded = (result: unknown, op = OP) => ({ operation_id: op, status: 'succeeded', result });

describe('operations slice', () => {
  it('stores rows from frames, never backwards, and ignores malformed frames', async () => {
    const t = await taskWorld();
    applyFrame(t.store, 'operation', row(t, { state: 'executing' }), 0, frameHandlers());
    applyFrame(t.store, 'operation', row(t, { state: 'accepted' }), 0, frameHandlers());
    applyFrame(t.store, 'operation', { ...row(t), state: 'unknown' }, 0, frameHandlers());
    expect(t.store.getState().operations.rows[OP]!.state).toBe('executing');
    putOperations(t.store, [row(t, { state: 'succeeded' })]);
    expect(t.store.getState().operations.rows[OP]!.state).toBe('succeeded');
    const before = t.store.getState().operations;
    putOperations(t.store, []);
    putOperations(t.store, [row(t, { state: 'received' })]);
    expect(t.store.getState().operations).toBe(before);
  });

  it('checks rows and orders newest first with the ID as tie-breaker', async () => {
    const t = await taskWorld();
    expect(isOperationRow(row(t))).toBe(true);
    for (const bad of [
      null,
      { ...row(t), type: 1 },
      { ...row(t), error_code: 3 },
      { ...row(t), retryable: 'no' },
      { ...row(t), attempt: 1.5 },
      { ...row(t), created_at: null },
    ]) {
      expect(isOperationRow(bad)).toBe(false);
    }
    const a = row(t, { operation_id: 'a', created_at: '2026-11-21T12:00:01.000Z' });
    const b = row(t, { operation_id: 'b' });
    const c = row(t, { operation_id: 'c' });
    expect([b, a, c].sort(newestFirst).map((r) => r.operation_id)).toEqual(['a', 'c', 'b']);
    expect(newestFirst(b, b)).toBe(0);
    expect(FINAL_STATES.has('duplicate')).toBe(true);
  });

  it('keeps transitions sorted and unique', async () => {
    const t = await taskWorld();
    const step = (n: number, state: string) => ({
      seq: n,
      state,
      error_code: null,
      at: '2026-11-21T12:00:00.000Z',
      by_id: null,
    });
    putTransitions(t.store, OP, [step(2, 'accepted'), step(1, 'received'), step(2, 'accepted')]);
    expect(t.store.getState().operations.transitions[OP]!.map((s) => s.seq)).toEqual([1, 2]);
  });
});

describe('loading', () => {
  it('loads a page of the task, keeps the paging position and drops foreign rows', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/tasks/${TASK}/operations`, (_body, target) => ({
      operations: [row(t), row(t, { operation_id: 'x', task_id: 'other' })],
      next_before: target.includes('before=') ? null : 'cursor',
    }));
    await loadOperations(t.context, TASK);
    expect(Object.keys(t.store.getState().operations.rows)).toEqual([OP]);
    expect(t.store.getState().operations.tasks[TASK]).toEqual({ nextBefore: 'cursor', loaded: true });
    await loadOperations(t.context, TASK, 'cursor');
    expect(t.client.calls.at(-1)!.target).toBe(`/v1/tasks/${TASK}/operations?limit=50&before=cursor`);
    expect(t.store.getState().operations.tasks[TASK]).toEqual({ nextBefore: null, loaded: true });
    for (const bad of [
      null,
      { operations: null, next_before: null },
      { operations: [{}], next_before: null },
      { operations: [], next_before: 3 },
    ]) {
      t.client.on(`GET /v1/tasks/${TASK}/operations`, () => bad);
      await expect(loadOperations(t.context, TASK)).rejects.toMatchObject({ code: 'unavailable' });
    }
  });

  it('shares the first page per generation and forgets failures', async () => {
    const t = await taskWorld();
    let fail = true;
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
      if (fail) throw new HubError('unavailable', 503);
      return { operations: [], next_before: null };
    });
    await expect(ensureOperations(t.context, TASK)).rejects.toMatchObject({ code: 'unavailable' });
    fail = false;
    await Promise.all([ensureOperations(t.context, TASK), ensureOperations(t.context, TASK)]);
    await ensureOperations(t.context, TASK);
    expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(2);
  });

  it('pulls the list again after a resync frame (frames may have been dropped; KOR-05, A-23)', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
    await ensureOperations(t.context, TASK);
    applyFrame(t.store, 'resync', { reason: 'buffer_overflow' }, 0, frameHandlers());
    expect(t.store.getState().operations.round).toBe(1);
    await ensureOperations(t.context, TASK);
    expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(2);
  });

  it('loads one operation with its transitions', async () => {
    const t = await taskWorld();
    const step = { seq: 1, state: 'received', error_code: null, at: '2026-11-21T12:00:00.000Z', by_id: DEVICE };
    t.client.on(`GET /v1/operations/${OP}`, () => ({ operation: row(t), transitions: [step] }));
    expect((await loadOperation(t.context, OP)).operation_id).toBe(OP);
    expect(t.store.getState().operations.transitions[OP]).toEqual([step]);
    const coded = { ...step, seq: 2, state: 'failed', error_code: 'timeout', by_id: null };
    t.client.on(`GET /v1/operations/${OP}`, () => ({ operation: row(t), transitions: [step, coded] }));
    await loadOperation(t.context, OP);
    expect(t.store.getState().operations.transitions[OP]).toEqual([step, coded]);
    for (const bad of [
      null,
      { operation: row(t, { operation_id: 'other' }), transitions: [] },
      { operation: row(t), transitions: [{}] },
      { operation: row(t), transitions: [{ ...step, error_code: 1 }] },
      { operation: row(t), transitions: [{ ...step, by_id: 1 }] },
      { operation: row(t), transitions: [{ ...step, at: null }] },
      { operation: row(t), transitions: [{ ...step, state: null }] },
      { operation: row(t), transitions: null },
    ]) {
      t.client.on(`GET /v1/operations/${OP}`, () => bad);
      await expect(loadOperation(t.context, OP)).rejects.toMatchObject({ code: 'unavailable' });
    }
  });

  it('pulls the newest page of the operation events into the reader', async () => {
    const t = await taskWorld();
    const item = { scope_seq: 4, received_at: null, event: {} };
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [item], next_before: null, head: 9 }));
    expect(await pullOperationEvents(t.context, row(t))).toBe(1);
    expect(t.client.calls[0]!.target).toBe(
      `/v1/scopes/${t.wg}/events?before=${NEWEST}&limit=1000&kinds=operation.params%2Coperation.result&ref=${OP}`,
    );
    expect(t.ingested).toHaveLength(1);
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_before: 'x' }));
    await expect(pullOperationEvents(t.context, row(t))).rejects.toMatchObject({ code: 'unavailable' });
  });
});

describe('rounds and sign-out', () => {
  it('pulls the list again after every hello frame', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
    await ensureOperations(t.context, TASK);
    await ensureOperations(t.context, TASK);
    applyFrame(t.store, 'hello', { heads: {} }, 0, frameHandlers());
    expect(t.store.getState().operations.round).toBe(1);
    await ensureOperations(t.context, TASK);
    expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(2);
  });

  it('writes nothing that arrives after a sign-out', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
      t.store.reset();
      return { operations: [row(t)], next_before: 'x' };
    });
    await loadOperations(t.context, TASK);
    expect(t.store.getState().operations).toEqual({ rows: {}, transitions: {}, tasks: {}, round: 0 });
    t.client.on(`GET /v1/operations/${OP}`, () => {
      t.store.reset();
      return { operation: row(t), transitions: [] };
    });
    expect((await loadOperation(t.context, OP)).operation_id).toBe(OP);
    expect(t.store.getState().operations.rows).toEqual({});
  });
});

describe('operationView', () => {
  it('reads the checked result of a succeeded test run, and nothing of other types or a bad result', async () => {
    const t = await taskWorld();
    const testResult = {
      run_id: 'tr-0123456789ab',
      state: 'failed',
      exit_code: 1,
      duration_ms: 1000,
      ready_ms: 500,
      summary: { passed: 2, failed: 1, skipped: null, errors: null },
      log: null,
      tail: '',
      reason: null,
    };
    const p = event(t, 'operation.params', params('test.run', { kind: 'unit', command: 'npm test' }));
    const r = event(t, 'operation.result', succeeded(testResult));
    expect(operationView([p, r], OP, row(t, { state: 'succeeded', type: 'test.run' })).testRun).toEqual(testResult);
    const bad = event(t, 'operation.result', succeeded({ ...testResult, state: 'green' }));
    expect(operationView([p, bad], OP, row(t, { type: 'test.run' })).testRun).toBeNull();
    expect(operationView([r], OP, row(t, { type: 'github.commit' })).testRun).toBeNull();
    expect(operationView([p], OP, row(t, { type: 'test.run' })).testRun).toBeNull();
  });

  it('joins the row with the verified params and result', async () => {
    const t = await taskWorld();
    const p = event(
      t,
      'operation.params',
      params('github.create_pull_request', { repo: 'dfx/agent', head: 'feat', base: 'main', title: 'T' }),
    );
    const r = event(
      t,
      'operation.result',
      succeeded({ number: 7, url: 'https://github.com/dfx/agent/pull/7', id: 1, draft: true }),
    );
    const view = operationView([p, r], OP, row(t, { state: 'succeeded' }));
    expect(view).toMatchObject({
      type: 'github.create_pull_request',
      state: 'succeeded',
      subject: 'dfx/agent feat',
      facts: [{ label: 'pull_request', text: '#7', href: 'https://github.com/dfx/agent/pull/7' }],
      params: { status: 'verified', at: '2026-11-21T12:00:01.000Z' },
    });
  });

  it('works without a row from the events alone, and with nothing at all', async () => {
    const t = await taskWorld();
    const p = event(t, 'operation.params', params('github.comment', { repo: 'dfx/agent', number: 3, body: 'x' }));
    const r = event(t, 'operation.result', {
      operation_id: OP,
      status: 'failed',
      error: { code: 'repo_not_allowed', message: 'no', retryable: false },
    });
    const view = operationView([p, r], OP, null);
    expect(view).toMatchObject({
      type: 'github.comment',
      state: 'failed',
      errorCode: 'repo_not_allowed',
      subject: 'dfx/agent#3',
      facts: [],
    });
    expect(view.result).toMatchObject({
      status: 'verified',
      value: { error: { code: 'repo_not_allowed', message: 'no', retryable: false } },
    });
    expect(operationView([], OP, null)).toMatchObject({
      type: 'unknown',
      state: 'received',
      subject: null,
      params: { status: 'missing' },
      sessionId: null,
      createdAt: null,
    });
    const plain = event(t, 'operation.result', {
      operation_id: OP,
      status: 'denied',
      error: { code: 'rights_denied' },
    });
    expect(operationView([plain], OP, null).result).toMatchObject({
      value: { error: { message: null, retryable: null } },
    });
  });

  it('reports unverifiable events, foreign bodies, other authors and malformed bodies', async () => {
    const t = await taskWorld();
    expect(operationView([event(t, 'operation.params', null, { status: 'unverifiable' })], OP, null).params).toEqual({
      status: 'unverifiable',
      code: 'signature_invalid',
    });
    const noCode = { ...event(t, 'operation.params', null, { status: 'unverifiable' }), code: null };
    expect(operationView([noCode], OP, null).params).toEqual({ status: 'unverifiable', code: 'header_invalid' });
    expect(operationView([event(t, 'operation.params', params('x.y', {}, 'other'))], OP, null).params).toEqual({
      status: 'unverifiable',
      code: 'header_invalid',
    });
    expect(
      operationView([event(t, 'operation.params', params('x.y', {}), { author: 'someone' })], OP, row(t)).params,
    ).toEqual({ status: 'unverifiable', code: 'header_invalid' });
    expect(
      operationView([event(t, 'operation.params', { operation_id: OP, type: 1, params: {} })], OP, null).params,
    ).toEqual({ status: 'unverifiable', code: 'params_invalid' });
    expect(
      operationView([event(t, 'operation.result', { operation_id: OP, status: 'succeeded' })], OP, null).result,
    ).toEqual({ status: 'unverifiable', code: 'params_invalid' });
    expect(
      operationView([event(t, 'operation.result', { operation_id: OP, status: 'failed', error: {} })], OP, null).result,
    ).toEqual({ status: 'unverifiable', code: 'params_invalid' });
    expect(operationView([event(t, 'operation.result', { operation_id: OP, status: 3 })], OP, null).result).toEqual({
      status: 'unverifiable',
      code: 'params_invalid',
    });
  });

  it('takes the newest event of the operation and skips pending, locked and other events', async () => {
    const t = await taskWorld();
    const older = event(t, 'operation.params', params('github.push', { repo: 'a/b', branch: 'x', commit_sha: 'c' }));
    const newer = event(t, 'operation.params', params('github.push', { repo: 'a/b', branch: 'y', commit_sha: 'c' }));
    const pending = { ...event(t, 'operation.params', null), status: 'pending' as const };
    const locked = { ...event(t, 'operation.params', null), status: 'locked' as const };
    const other = event(t, 'operation.params', params('github.push', {}, 'x'), { ref: 'x' });
    const unverifiedOther = event(t, 'operation.result', null, { status: 'unverifiable', ref: 'x' });
    expect(operationView([newer, older, pending, locked, other, unverifiedOther], OP, null).subject).toBe('a/b y');
    // a newer unverifiable event (e.g. injected by the hub) never hides a verified one
    const injected = event(t, 'operation.params', null, { status: 'unverifiable' });
    const olderInjected = { ...event(t, 'operation.params', null, { status: 'unverifiable' }), scopeSeq: 1 };
    expect(operationView([older, injected, newer], OP, null).subject).toBe('a/b y');
    expect(operationView([injected, olderInjected], OP, null).params).toEqual({
      status: 'unverifiable',
      code: 'signature_invalid',
    });
    const raw = { ...event(t, 'operation.params', null, { status: 'unverifiable' }), event: null };
    expect(operationView([raw], OP, null).params).toEqual({ status: 'missing' });
  });

  it('carries duplicates, error codes and session of the row', async () => {
    const t = await taskWorld();
    const view = operationView([], OP, row(t, { state: 'duplicate', duplicate_of: 'orig', error_code: 'timeout' }));
    expect(view).toMatchObject({
      duplicateOf: 'orig',
      errorCode: 'timeout',
      sessionId: row(t).session_id,
      createdAt: row(t).created_at,
    });
  });

  it('memoizes the view per scope and row', async () => {
    const t = await taskWorld();
    const select = selectOperation(t.wg, OP);
    const first = select(t.store.getState());
    expect(select(t.store.getState())).toBe(first);
    putEvents(t.store, t.wg, [event(t, 'operation.params', params('task.set_title', { title: 'New' }))]);
    expect(select(t.store.getState()).subject).toBe('New');
  });
});

describe('purposeOf', () => {
  it('takes the purpose only when the verified result names the child', async () => {
    const t = await taskWorld();
    const p = event(t, 'operation.params', params('subagent.request', { purpose: 'Review', prompt: 'x' }));
    const r = (session: string) => event(t, 'operation.result', succeeded({ session_id: session }));
    expect(purposeOf(operationView([p], OP, null), 'c1')).toEqual({ status: 'verified', text: 'Review' });
    expect(purposeOf(operationView([p, r('c1')], OP, null), 'c1')).toEqual({ status: 'verified', text: 'Review' });
    expect(purposeOf(operationView([p, r('c2')], OP, null), 'c1')).toEqual({
      status: 'unverifiable',
      code: 'header_invalid',
    });
    const bad = event(t, 'operation.params', null, { status: 'unverifiable' });
    expect(purposeOf(operationView([bad], OP, null), 'c1')).toEqual({
      status: 'unverifiable',
      code: 'signature_invalid',
    });
    expect(purposeOf(operationView([], OP, null), 'c1')).toBeNull();
    const plain = event(t, 'operation.params', params('subagent.request', { purpose: 3 }));
    expect(purposeOf(operationView([plain], OP, null), 'c1')).toBeNull();
    const failed = event(t, 'operation.result', {
      operation_id: OP,
      status: 'denied',
      error: { code: 'limit_exceeded' },
    });
    expect(purposeOf(operationView([p, failed], OP, null), 'c1')).toEqual({ status: 'verified', text: 'Review' });
  });
});

describe('subjectOf and factsOf', () => {
  it('describes the parameters of each type', () => {
    expect(subjectOf('subagent.request', { purpose: 'Review', prompt: 'p' })).toBe('Review');
    expect(subjectOf('subagent.instruct', { session_id: 's1', prompt: 'Check the tests.' })).toBe('Check the tests.');
    expect(subjectOf('subagent.instruct', { session_id: 's1', prompt: `${'ä'.repeat(SUBJECT_CHARS)}tail` })).toBe(
      `${'ä'.repeat(SUBJECT_CHARS)}…`,
    );
    expect(subjectOf('subagent.instruct', { session_id: 's1' })).toBeNull();
    expect(subjectOf('subagent.stop', { session_id: 's1' })).toBe('s1');
    expect(subjectOf('task.set_title', { title: 'T' })).toBe('T');
    expect(subjectOf('human.question', { question: 'Which branch?' })).toBe('Which branch?');
    expect(subjectOf('human.approval', { question: 'Deploy?' })).toBe('Deploy?');
    expect(subjectOf('github.list_repositories', {})).toBeNull();
    expect(subjectOf('github.checkout', { repo: 'a/b' })).toBe('a/b');
    expect(subjectOf('github.create_branch', { repo: 'a/b', branch: 'x', from: 'main' })).toBe('a/b x');
    expect(subjectOf('github.get_pull_request', { repo: 'a/b', number: 2 })).toBe('a/b#2');
    expect(subjectOf('workvm.request', {})).toBe('standard');
    expect(subjectOf('workvm.request', { size: 'large' })).toBe('large');
    expect(subjectOf('workvm.release', {})).toBe('task');
    expect(subjectOf('workvm.release', { scope: 'session' })).toBe('session');
    expect(subjectOf('test.run', { kind: 'unit', command: 'npm test' })).toBe('unit: npm test');
    expect(subjectOf('test.run', { command: 'make check' })).toBe('custom: make check');
    expect(subjectOf('test.run', { kind: 'lint' })).toBeNull();
    expect(subjectOf('test.run', { kind: 'lint', command: `${'ä'.repeat(SUBJECT_CHARS)}tail` })).toBe(
      `lint: ${'ä'.repeat(SUBJECT_CHARS)}…`,
    );
    expect(subjectOf('github.get_issue', { repo: 'a/b', number: 3 })).toBe('a/b#3');
    expect(subjectOf('github.list_issues', { repo: 'a/b', state: 'all' })).toBe('a/b');
    expect(subjectOf('github.create_issue', { repo: 'a/b', title: 'Bug' })).toBe('a/b: Bug');
    expect(subjectOf('github.create_issue', { repo: 'a/b' })).toBe('a/b');
    expect(subjectOf('github.request_connection', {})).toBeNull();
    expect(subjectOf('browser.open', { url: 'https://example.com/' })).toBe('https://example.com/');
    expect(subjectOf('browser.open', {})).toBeNull();
    expect(subjectOf('browser.close', {})).toBeNull();
  });

  it('lists the result links and values of each type', () => {
    const none = { status: 'missing' } as const;
    const sha = 'a'.repeat(40);
    expect(factsOf('github.get_pull_request', { number: 2, url: 'javascript:alert(1)' }, none)).toEqual([
      { label: 'pull_request', text: '#2' },
    ]);
    expect(factsOf('github.create_pull_request', { url: 'https://x' }, none)).toEqual([]);
    expect(factsOf('github.commit', { commit_sha: sha }, none)).toEqual([{ label: 'commit', text: sha }]);
    expect(factsOf('github.push', { branch: 'b', sha, url: 'https://github.com/a/b/tree/b' }, none)).toEqual([
      { label: 'branch', text: 'b', href: 'https://github.com/a/b/tree/b' },
      { label: 'commit', text: sha },
    ]);
    expect(factsOf('github.comment', { comment_id: 1, url: 'https://github.com/c' }, none)).toEqual([
      { label: 'comment', text: 'https://github.com/c', href: 'https://github.com/c' },
    ]);
    expect(factsOf('github.create_branch', { branch: 'b', sha }, none)).toHaveLength(2);
    expect(factsOf('github.checkout', { commit_sha: sha }, none)).toHaveLength(1);
    expect(factsOf('github.list_repositories', { repositories: [{}, {}] }, none)).toEqual([
      { label: 'repositories', text: '2' },
    ]);
    expect(factsOf('github.list_repositories', {}, none)).toEqual([]);
    expect(factsOf('subagent.request', { session_id: 's1' }, none)).toEqual([
      { label: 'session', text: 's1', session: 's1' },
    ]);
    expect(factsOf('subagent.request', {}, none)).toEqual([]);
    expect(factsOf('subagent.instruct', { session_id: 's1', instruction_id: 'i1' }, none)).toEqual([
      { label: 'session', text: 's1', session: 's1' },
    ]);
    expect(factsOf('subagent.stop', { session_id: 's1', state: 'stopped' }, none)).toEqual([
      { label: 'session', text: 's1', session: 's1' },
      { label: 'stop_state', text: 'stopped' },
    ]);
    expect(factsOf('subagent.stop', { session_id: 's1', state: 'idle' }, none)).toEqual([
      { label: 'session', text: 's1', session: 's1' },
    ]);
    expect(factsOf('task.set_title', {}, none)).toEqual([]);
    const titled = { status: 'verified', at: '', value: { type: 'task.set_title', params: { title: 'New' } } } as const;
    expect(factsOf('task.set_title', {}, titled)).toEqual([{ label: 'title', text: 'New' }]);
    expect(factsOf('workvm.request', { workvm_id: 'wvm-0123abcd' }, none)).toEqual([
      { label: 'workvm', text: 'wvm-0123abcd' },
    ]);
    expect(factsOf('workvm.release', { released: false }, none)).toEqual([]);
    expect(factsOf('test.run', { run_id: 'tr-0123456789ab', exit_code: 1 }, none)).toEqual([
      { label: 'run', text: 'tr-0123456789ab' },
      { label: 'exit_code', text: '1' },
    ]);
    expect(factsOf('test.run', { run_id: 'tr-0123456789ab', exit_code: null }, none)).toHaveLength(1);
    expect(factsOf('custom.type', {}, none)).toEqual([]);
    expect(
      factsOf('browser.open', { browser_id: 'br-0123456789ab', url: 'https://example.com/', ready_ms: 400 }, none),
    ).toEqual([
      { label: 'browser', text: 'br-0123456789ab' },
      { label: 'url', text: 'https://example.com/', href: 'https://example.com/' },
      { label: 'ready_ms', text: '400' },
    ]);
    expect(
      factsOf('browser.open', { browser_id: 'br-0123456789ab', url: 'http://intranet/', ready_ms: 0 }, none),
    ).toContainEqual({ label: 'url', text: 'http://intranet/' });
    expect(
      factsOf(
        'browser.close',
        { closed: true, browser_id: 'br-0123456789ab', actions: 7, reason: 'browser_closed' },
        none,
      ),
    ).toEqual([
      { label: 'browser', text: 'br-0123456789ab' },
      { label: 'actions', text: '7' },
      { label: 'reason', text: 'browser_closed' },
    ]);
  });

  it('lists the facts of the issue types and the connection request', () => {
    const none = { status: 'missing' } as const;
    const url = 'https://github.com/a/b/issues/4';
    expect(factsOf('github.list_issues', { issues: [{}, {}, {}] }, none)).toEqual([{ label: 'issues', text: '3' }]);
    expect(factsOf('github.list_issues', {}, none)).toEqual([]);
    expect(factsOf('github.list_pull_requests', { pull_requests: [] }, none)).toEqual([
      { label: 'pull_requests', text: '0' },
    ]);
    expect(factsOf('github.list_pull_requests', {}, none)).toEqual([]);
    expect(factsOf('github.get_issue', { number: 4, url }, none)).toEqual([{ label: 'issue', text: '#4', href: url }]);
    expect(factsOf('github.create_issue', { number: 4, url: 'http://github.com/x', id: 1 }, none)).toEqual([
      { label: 'issue', text: '#4' },
    ]);
    expect(factsOf('github.create_issue', { url }, none)).toEqual([]);
    expect(
      factsOf('github.update_issue', { number: 4, url, state: 'closed', state_reason: 'not_planned' }, none),
    ).toEqual([
      { label: 'issue', text: '#4', href: url },
      { label: 'state', text: 'closed' },
      { label: 'state_reason', text: 'not_planned' },
    ]);
    expect(factsOf('github.update_issue', { number: 4, state: 'weird', state_reason: null }, none)).toEqual([
      { label: 'issue', text: '#4' },
    ]);
    expect(factsOf('github.request_connection', { login: 'octo' }, none)).toEqual([{ label: 'login', text: 'octo' }]);
    const pull = 'https://github.com/a/b/pull/7';
    expect(
      factsOf('github.update_pull_request', { number: 7, url: pull, title: 'T', state: 'open', draft: false }, none),
    ).toEqual([
      { label: 'pull_request', text: '#7', href: pull },
      { label: 'state', text: 'open' },
    ]);
    expect(factsOf('github.update_pull_request', { state: 'merged' }, none)).toEqual([]);
  });
});

describe('httpsUrl, listingOf and issueOf', () => {
  it('keeps https links only', () => {
    expect(httpsUrl('https://github.com/a')).toBe('https://github.com/a');
    expect(httpsUrl('HTTPS://github.com/a')).toBe('HTTPS://github.com/a');
    expect(httpsUrl('http://github.com/a')).toBeNull();
    expect(httpsUrl('javascript:alert(1)')).toBeNull();
    expect(httpsUrl('mailto:a@b.c')).toBeNull();
    expect(httpsUrl('/relative')).toBeNull();
    expect(httpsUrl(7)).toBeNull();
  });

  it('reads the listed issues and pull requests entry by entry', () => {
    const issue = {
      number: 4,
      title: '<b>Bug</b>',
      state: 'open',
      state_reason: null,
      author: 'octo',
      labels: ['bug', 3, 'p1'],
      url: 'https://github.com/a/b/issues/4',
    };
    expect(listingOf('github.list_issues', { issues: [issue], next_page: 2, truncated: true })).toEqual({
      kind: 'issues',
      entries: [
        {
          number: 4,
          title: '<b>Bug</b>',
          state: 'open',
          draft: false,
          author: 'octo',
          labels: ['bug', 'p1'],
          href: 'https://github.com/a/b/issues/4',
        },
      ],
      nextPage: 2,
      truncated: true,
    });
    const bad = [
      null,
      { title: 'no number' },
      { number: 0, title: 'zero' },
      { number: 1.5, title: 'fraction' },
      { number: 2 },
    ];
    const pr = { number: 9, title: 'PR', state: 'closed', draft: true, url: 'javascript:x', labels: 'x' };
    expect(listingOf('github.list_pull_requests', { pull_requests: [...bad, pr], next_page: null })).toEqual({
      kind: 'pull_requests',
      entries: [{ number: 9, title: 'PR', state: 'closed', draft: true, author: null, labels: [] }],
      nextPage: null,
      truncated: false,
    });
    expect(listingOf('github.list_issues', { next_page: -1 })).toEqual({
      kind: 'issues',
      entries: [],
      nextPage: null,
      truncated: false,
    });
    expect(listingOf('github.get_issue', {})).toBeNull();
    expect(listingOf('toString', {})).toBeNull();
  });

  it('reads an issue with its page of comments', () => {
    const result = {
      number: 4,
      title: 'Bug',
      body: 'Steps',
      state: 'closed',
      state_reason: 'completed',
      author: 'octo',
      labels: ['bug'],
      assignees: [],
      url: 'https://github.com/a/b/issues/4',
      pull_request: false,
      comments_total: 3,
      comments: [
        { id: 1, author: 'a', body: 'first', created_at: '2026-11-20T10:00:00Z' },
        { id: 'x2', body: 'no author' },
        { id: true, body: 'bad id' },
        { id: 3, body: 5 },
        'junk',
      ],
      comments_next_page: 2,
      truncated: true,
    };
    expect(issueOf('github.get_issue', result)).toEqual({
      number: 4,
      title: 'Bug',
      state: 'closed',
      draft: false,
      author: 'octo',
      labels: ['bug'],
      href: 'https://github.com/a/b/issues/4',
      stateReason: 'completed',
      body: 'Steps',
      pullRequest: false,
      comments: [
        { id: '1', author: 'a', body: 'first', createdAt: '2026-11-20T10:00:00Z' },
        { id: 'x2', author: null, body: 'no author', createdAt: null },
      ],
      commentsTotal: 3,
      commentsNextPage: 2,
      truncated: true,
    });
    expect(issueOf('github.get_issue', { number: 5, title: 'PR', body: null, pull_request: true })).toMatchObject({
      body: null,
      pullRequest: true,
      comments: [],
      commentsTotal: null,
      commentsNextPage: null,
      stateReason: null,
      truncated: false,
    });
    expect(issueOf('github.get_issue', { title: 'no number' })).toBeNull();
    expect(issueOf('github.list_issues', result)).toBeNull();
  });

  it('carries the listing and the issue in the view of a succeeded operation', async () => {
    const t = await taskWorld();
    const list = operationView(
      [
        event(t, 'operation.params', params('github.list_issues', { repo: 'a/b' })),
        event(t, 'operation.result', succeeded({ issues: [], next_page: null, truncated: false })),
      ],
      OP,
      null,
    );
    expect(list.listing).toEqual({ kind: 'issues', entries: [], nextPage: null, truncated: false });
    expect(list.issue).toBeNull();
    const issue = operationView(
      [
        event(t, 'operation.params', params('github.get_issue', { repo: 'a/b', number: 4 })),
        event(t, 'operation.result', succeeded({ number: 4, title: 'Bug' })),
      ],
      OP,
      null,
    );
    expect(issue.issue).toMatchObject({ number: 4, title: 'Bug' });
    expect(issue.listing).toBeNull();
    const pending = operationView(
      [event(t, 'operation.params', params('github.get_issue', { repo: 'a/b' }))],
      OP,
      null,
    );
    expect(pending.issue).toBeNull();
    expect(pending.listing).toBeNull();
  });
});
