// The chat timeline: events of the task by (ts, scope_seq), system rows from transitions and the close, countdown items
// per main session, unverifiable rows, hidden kinds, subagent nodes, the session chat and the memoized selector; the
// session tree with depth ≤ 2 and the link rules of rendered Markdown.
import { describe, expect, it } from 'vitest';
import { buildTimeline, selectTimeline } from '../../../src/core/chat/timeline';
import { imageLabel, LINK_REL, safeUrl } from '../../../src/core/chat/links';
import { childrenOf, flatten, selectSessionIndex, sessionMeta, sessionTree } from '../../../src/core/sessions/tree';
import { createStore } from '../../../src/core/store';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { putSession, putTasks, type SessionRow, type TaskRow } from '../../../src/core/store/slices/tasks';

const ORG = 'org';
const WG = 'wg';
const TASK = 'task-1';
const MAIN = 'main-1';

const task = (overrides: Partial<TaskRow> = {}): TaskRow => ({
  task_id: TASK,
  org_id: ORG,
  wg_id: WG,
  owner_id: 'u1',
  state: 'working',
  state_reason: null,
  created_at: '2026-11-21T12:00:00.000Z',
  state_at: '2026-11-21T12:00:00.000Z',
  last_activity_at: '2026-11-21T12:00:00.000Z',
  closed_at: null,
  main_session_id: MAIN,
  title_event_id: null,
  ...overrides,
});

const session = (id: string, overrides: Record<string, unknown> = {}): SessionRow =>
  ({
    session_id: id,
    task_id: TASK,
    org_id: ORG,
    wg_id: WG,
    device_id: 'd1',
    state: 'working',
    state_reason: null,
    estimate_ms: 3000,
    created_at: '2026-11-21T12:00:01.000Z',
    first_working_at: null,
    parent_session_id: null,
    depth: 0,
    model_id: 'claude-x',
    ...overrides,
  }) as SessionRow;

function event(
  seq: number,
  kind: string,
  options: { ts?: string; session?: string; task?: string; body?: unknown; status?: EventEntry['status'] } = {},
): EventEntry {
  const status = options.status ?? 'verified';
  const header = {
    id: `id-${seq}`,
    kind,
    task: options.task ?? TASK,
    session: options.session ?? '',
    author: kind === 'prompt' ? 'u1' : 'd1',
    ts: options.ts ?? `2026-11-21T12:00:0${seq}.000Z`,
  };
  return {
    org: ORG,
    scopeId: WG,
    scopeSeq: seq,
    receivedAt: '2026-11-21T12:00:09.000Z',
    event: { header },
    status,
    code: status === 'unverifiable' ? 'signature_invalid' : null,
    header: status === 'verified' ? header : null,
    body: status === 'verified' ? (options.body ?? { text: `t${seq}` }) : null,
  };
}

const sessions = (...rows: { row: SessionRow; transitions?: { seq: number; state: string; at: string }[] }[]) =>
  Object.fromEntries(
    rows.map(({ row, transitions }) => [
      row.session_id,
      {
        row,
        transitions: (transitions ?? []).map((item) => ({ ...item, reason: null, received_at: item.at })),
        estimatedAt: null,
      },
    ]),
  );

const scope = (...entries: EventEntry[]) => ({ entries: new Map(entries.map((entry) => [entry.scopeSeq, entry])) });

describe('timeline', () => {
  it('orders the task events by time and seq, hides titles, terminal and non-deny hooks, keeps unverifiable rows', () => {
    const items = buildTimeline({
      task: task(),
      sessions: {},
      scope: scope(
        event(1, 'prompt'),
        event(2, 'task.meta'),
        event(3, 'session.output', { session: MAIN }),
        event(4, 'session.hook', { session: MAIN, body: { decision: 'allow' } }),
        event(5, 'session.hook', { session: MAIN, body: { decision: 'deny', tool: 'Bash' } }),
        event(6, 'session.terminal', { session: MAIN }),
        event(7, 'session.tool', { session: MAIN, ts: '2026-11-21T12:00:03.000Z' }),
        event(8, 'unverifiable', { status: 'unverifiable', session: MAIN }),
        event(9, 'prompt', { task: 'other' }),
        event(10, 'prompt', { status: 'pending' }),
        event(11, 'prompt', { status: 'locked' }),
        event(12, 'prompt', { status: 'unverifiable', task: 'other' }),
        { ...event(13, 'x', { status: 'unverifiable' }), event: { header: { task: TASK, kind: 'session.terminal' } } },
      ),
    });
    expect(items.map((item) => item.key)).toEqual(['e1', 'e3', 'e7', 'e5', 'e8']);
    expect(items[1]).toMatchObject({ kind: 'session.output', sessionId: MAIN, author: 'd1', subagent: null });
    expect(items[4]).toMatchObject({
      kind: 'unverifiable',
      source: 'unverifiable',
      code: 'signature_invalid',
      sessionId: MAIN,
      at: Date.parse('2026-11-21T12:00:08.000Z'),
    });
  });

  it('places unverifiable rows without a usable time at their receipt, else at the start', () => {
    const broken = { ...event(1, 'x', { status: 'unverifiable' }), event: { header: { task: TASK, ts: 'no' } } };
    const bare = { ...event(2, 'x', { status: 'unverifiable' }), receivedAt: null, event: { header: { task: TASK } } };
    const raw = { ...event(3, 'x', { status: 'unverifiable' }), event: 'garbage' };
    const items = buildTimeline({ task: task(), sessions: {}, scope: scope(broken, bare, raw) });
    expect(items.map((item) => [item.key, item.at, item.sessionId])).toEqual([
      ['e2', 0, null],
      ['e1', Date.parse('2026-11-21T12:00:09.000Z'), null],
    ]);
    // an event header without a parseable time sorts first
    const odd = buildTimeline({ task: task(), sessions: {}, scope: scope(event(4, 'prompt', { ts: 'x' })) });
    expect(odd[0]!.at).toBe(0);
  });

  it('adds countdown items per main session, system rows for transitions and the close, and subagent nodes', () => {
    const child = session('child-1', {
      parent_session_id: MAIN,
      depth: 1,
      created_at: '2026-11-21T12:00:02.000Z',
    });
    const items = buildTimeline({
      task: task({ closed_at: '2026-11-21T12:00:09.500Z' }),
      sessions: sessions(
        {
          row: session(MAIN),
          transitions: [
            { seq: 1, state: 'working', at: '2026-11-21T12:00:01.500Z' },
            { seq: 2, state: 'suspended', at: '2026-11-21T12:00:04.000Z' },
            { seq: 3, state: 'stopped', at: 'bad' },
          ],
        },
        { row: child },
        {
          row: session('main-2', { created_at: '2026-11-21T12:00:06.000Z' }),
          transitions: [{ seq: 1, state: 'failed', at: 'bad' }],
        },
      ),
      scope: scope(event(5, 'session.output', { session: 'child-1' })),
    });
    expect(items.map((item) => `${item.kind}:${item.key}`)).toEqual([
      `session.status:t${MAIN}:3`,
      'session.status:tmain-2:1',
      `countdown:c${MAIN}`,
      `session.status:t${MAIN}:2`,
      'session.output:e5',
      'countdown:cmain-2',
      'session.status:closed',
    ]);
    expect(items[3]!.body).toEqual({ state: 'suspended', reason: null });
    expect(items[4]!.subagent).toMatchObject({ depth: 1, path: [1] });
    expect(items[6]).toMatchObject({ source: 'task', body: { state: 'closed', reason: null } });
  });

  it('restricts the session chat to the own events of the session', () => {
    const items = buildTimeline({
      task: task(),
      sessions: sessions({ row: session(MAIN) }),
      session: MAIN,
      scope: scope(
        event(1, 'prompt'),
        event(2, 'session.output', { session: MAIN }),
        event(3, 'session.output', { session: 'other' }),
        event(4, 'x', { status: 'unverifiable', session: MAIN }),
      ),
    });
    expect(items.map((item) => item.key)).toEqual(['e2', 'e4']);
    expect(buildTimeline({ task: task(), sessions: {}, scope: undefined })).toEqual([]);
  });

  it('keeps only the latest workvm.run item of a job, in the task and in the session chat', () => {
    const job = (state: string) => ({
      workvm_id: 'wvm-0123abcd',
      job_id: 'j-0123456789ab',
      kind: 'exec',
      command: 'ls',
      state,
      exit_code: state === 'running' ? null : 0,
      started_at: '2026-11-21T12:00:01.000Z',
      ended_at: state === 'running' ? null : '2026-11-21T12:00:02.000Z',
      duration_ms: 1000,
      output: '',
      output_bytes: 0,
    });
    const events = scope(
      event(1, 'workvm.run', { session: MAIN, body: job('running') }),
      event(2, 'workvm.state', { session: MAIN, body: {} }),
      event(3, 'workvm.run', { session: MAIN, body: job('exited') }),
    );
    const input = { task: task(), sessions: sessions({ row: session(MAIN) }), scope: events };
    expect(buildTimeline({ ...input, session: MAIN }).map((item) => item.key)).toEqual(['e2', 'e3']);
    expect(
      buildTimeline(input)
        .filter((item) => item.kind.startsWith('workvm'))
        .map((item) => item.key),
    ).toEqual(['e2', 'e3']);
  });

  it('folds held-operation status events into one card per kind, also in the session chat (KOR-25)', () => {
    const waiting = (seq: number, id: string) =>
      event(seq, 'session.status', {
        session: MAIN,
        body: {
          state: 'waiting',
          waiting_for: 'github_connection',
          operation_id: `0193002d-0000-7000-8000-00000000000${id}`,
          operation_type: 'github.list_issues',
          until: '2026-11-21T12:15:00.000Z',
        },
      });
    const input = {
      task: task(),
      sessions: sessions({ row: session(MAIN) }),
      scope: scope(
        waiting(1, '1'),
        event(2, 'session.status', { session: MAIN, body: { state: 'failed' } }),
        waiting(3, '2'),
      ),
    };
    const items = buildTimeline(input);
    expect(items.filter((item) => item.kind === 'session.status').map((item) => item.key)).toEqual(['e2', 'e3']);
    expect(items.find((item) => item.key === 'e3')!.waiting!.operations).toHaveLength(2);
    expect(buildTimeline({ ...input, session: MAIN }).map((item) => item.key)).toEqual(['e2', 'e3']);
  });

  it('selects and memoizes the timeline of a task in the store', () => {
    const store = createStore();
    const select = selectTimeline(WG, TASK);
    expect(select(store.getState())).toEqual([]);
    expect(select(store.getState())).toBe(select(store.getState()));
    putTasks(store, [task()]);
    putEvents(store, WG, [event(1, 'prompt')]);
    const first = select(store.getState());
    expect(first.map((item) => item.key)).toEqual(['e1']);
    expect(select(store.getState())).toBe(first);
    putSession(store, session(MAIN), [], 0);
    const second = select(store.getState());
    expect(second).not.toBe(first);
    expect(selectTimeline(WG, TASK, MAIN)(store.getState())).toEqual([]);
  });
});

describe('session tree', () => {
  it('builds main sessions with children by parent, depth at most 2, in creation order', () => {
    const rows = sessions(
      { row: session(MAIN) },
      { row: session('c2', { parent_session_id: MAIN, depth: 1, created_at: '2026-11-21T12:00:03.000Z' }) },
      { row: session('c1', { parent_session_id: MAIN, depth: 1, created_at: '2026-11-21T12:00:02.000Z' }) },
      { row: session('c1a', { parent_session_id: 'c1', depth: 2, created_at: '2026-11-21T12:00:04.000Z' }) },
      { row: session('c1a1', { parent_session_id: 'c1a', depth: 3, created_at: '2026-11-21T12:00:05.000Z' }) },
      { row: session('orphan', { parent_session_id: 'unknown', depth: 1 }) },
      { row: session('main-0', { created_at: '2026-11-21T12:00:01.000Z', session_id: 'main-0' }) },
      { row: session('foreign', { task_id: 'other' }) },
      { row: session('broken', { created_at: 'garbage', parent_session_id: 'c1a', depth: 3 }) },
    );
    const tree = sessionTree(rows, TASK);
    expect(tree.map((node) => node.entry.row.session_id)).toEqual(['main-0', MAIN]);
    expect(flatten(tree).map((node) => [node.entry.row.session_id, node.depth, node.path.join('.')])).toEqual([
      ['main-0', 0, ''],
      [MAIN, 0, ''],
      ['c1', 1, '1'],
      ['c1a', 2, '1.1'],
      ['c2', 1, '2'],
    ]);
    expect(childrenOf(rows, MAIN).map((entry) => entry.row.session_id)).toEqual(['c1', 'c2']);
    const store = createStore();
    for (const entry of Object.values(rows)) putSession(store, entry.row, [], 0);
    const select = selectSessionIndex(TASK);
    expect(select(store.getState()).get('c1a')!.path).toEqual([1, 1]);
    expect(select(store.getState())).toBe(select(store.getState()));
  });

  it('reads the extra row fields defensively', () => {
    const entry = { row: { session_id: 's' } as SessionRow, transitions: [], estimatedAt: null };
    expect(sessionMeta(entry)).toEqual({ parent: null, depth: 0, modelId: null });
    expect(sessionMeta({ ...entry, row: null as never })).toEqual({ parent: null, depth: 0, modelId: null });
  });
});

describe('links', () => {
  it.each([
    ['https://example.com/a?b#c', 'https://example.com/a?b#c'],
    ['http://example.com', 'http://example.com'],
    ['  mailto:a@example.com ', 'mailto:a@example.com'],
    ['javascript:alert(1)', null],
    ['JaVaScRiPt:alert(1)', null],
    ['java\tscript:alert(1)', null],
    ['data:text/html,<b>x</b>', null],
    ['vbscript:x', null],
    ['file:///etc/passwd', null],
    ['/relative', null],
    ['//evil.example', null],
    ['#anchor', null],
    ['https://exa mple.com', null],
    ['', null],
  ])('%s → %s', (url, expected) => {
    expect(safeUrl(url)).toBe(expected);
  });

  it('names the link relation and labels images', () => {
    expect(LINK_REL).toBe('noopener noreferrer nofollow');
    expect(imageLabel('logo', 'https://x')).toBe('logo');
    expect(imageLabel('', 'https://x')).toBe('https://x');
    expect(imageLabel(undefined, 'https://x')).toBe('https://x');
  });
});
